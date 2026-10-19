"""The /bumps pages (E1, E2; interfaces.md section 19.8), mounted through the web plug-in seam (web/plugins.py).

    GET  /bumps/                      the queue: kept proposals with filters, per-configuration scans, the ledger
                                      summary, the schedule (DISABLED, naming the Phase 4 gate)
    GET  /bumps/p/{id}                one proposal: the move, changelog, defects, matrix cell or Run, blockers,
                                      the patch and the commands for a person, marks
    GET  /bumps/p/{id}/patch.diff     the patch file (download)
    GET  /bumps/ledger                E2: what the bot would have done against what people did
    GET  /bumps/facts/{subject}       one bump observation with its sources verbatim (A3)
    POST /bumps/scan                  queue `nanosoc-rm bumps [CONFIG] --store --json` as a local job
    POST /bumps/p/{id}/mark           "mark as done by a person" (or declined), logged with the login

Every page says it: the tool never pushes, writes a branch or opens a pull request. The POST routes act only in a
local deployment (the app's local routes, section 13a); elsewhere they answer 403 with the CLI command. Pages read
only bumps.sqlite (the kept proposals, scans, marks and the kept ledger): no git and no ledger computation inside a
request. The scan and the ledger always run as jobs (nanosoc_rm.jobs): Scan now queues `bumps scan --store`, which
also keeps the ledger, and a mark queues `bumps ledger --store`.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm.web.page import src_name
from nanosoc_rm.web.plugins import config_of, is_local, login_of, renderer, template_env

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "ENV", "STATE_CLASS", "OUTCOME_CLASS", "CLASS_CLASS",
           "STATUS_CLASS"]

PREFIX = "/bumps"
NAV_LABEL = "Bump proposals"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent

# README section 6 colours; every pill also carries its text.
STATE_CLASS = {"proposed": "state-info", "blocked": "state-amber"}
OUTCOME_CLASS = {"agreed": "state-green", "disagreed": "state-red", "pending": "state-grey",
                 "superseded": "state-grey", "unknown": "state-amber", "held": "state-grey"}
CLASS_CLASS = {"breaking": "state-red", "feature": "state-info", "fix": "state-green", "none": "state-grey",
               "unknown": "state-amber"}
STATUS_CLASS = {"proposal": "state-info", "blocked": "state-amber", "up-to-date": "state-green",
                "held-by-range": "state-amber", "no-release-tag": "state-grey", "external": "state-grey",
                "not-catalogued": "state-grey", "duplicate": "state-grey", "release-branch": "state-grey",
                "consumer-not-a-branch": "state-grey", "consumer-not-mirrored": "state-amber",
                "consumer-path-gone": "state-amber", "pin-not-in-mirror": "state-amber", "no-gitlink": "state-grey"}
NO_WRITES = ("Shadow mode: this tool never pushes, writes a branch or opens a pull request. A proposal is a patch "
             "and commands for a person to apply.")


def _fact(page: Any, obs: dict, label: Any = None) -> Markup:
    """A bump fact with the section 13 hooks; its sources page is /bumps/facts/<subject> (these kinds live in
    bumps.sqlite, not in rm.sqlite)."""
    if page is not None:
        page.facts.append(obs)
    s = obs["subject"]
    href = "%s/facts/%s" % (PREFIX, urllib.parse.quote(s, safe=""))
    return Markup('<span class="fact" data-subject="%s" data-observed-at="%s">%s</span> <a class="src" '
                  'data-source-for="%s" href="%s" title="observed %s; how it was found" aria-label="How %s was found">'
                  'src</a>'
                  % (escape(s), escape(obs["observed_at"]), escape(s if label is None else label), escape(s),
                     escape(href), escape(obs["observed_at"]), escape(src_name(label, s))))


def _pill(text: Any, table: dict) -> Markup:
    return Markup('<span class="pill %s">%s</span>' % (escape(table.get(str(text), "")), escape(text)))


ENV = template_env(HERE, STATE_CLASS_B=STATE_CLASS, bfact=_fact, pill=_pill, OUTCOME_CLASS=OUTCOME_CLASS,
                   CLASS_CLASS=CLASS_CLASS, STATUS_CLASS=STATUS_CLASS, NO_WRITES=NO_WRITES,
                   qs=lambda **kw: "?" + urllib.parse.urlencode({k: v for k, v in kw.items() if v}),
                   short=lambda s, n=12: (s or "")[:n])
_page = renderer(ENV, local=True)


def _ledger_kept(config) -> tuple[dict | None, str | None, list[str]]:
    """(the kept ledger, when it was computed, problems). Never computes it: that reads the mirrors (a job does)."""
    from nanosoc_rm import bumps
    try:
        led, at = bumps.stored_ledger(config=config)
    except Exception as e:                                # noqa: BLE001 - the page still shows the queue
        return None, None, ["the kept shadow ledger could not be read: %s" % str(e)[:300]]
    return led, at, []


def _queue_data(config, q) -> dict:
    from nanosoc_rm import bumps, jobs
    from nanosoc_rm.bumps import store
    scans = bumps.scans(config=config)
    kept = store.proposals(config)
    led, led_at, problems = _ledger_kept(config)
    outcome = {e["id"]: e for e in (led or {}).get("fact", {}).get("entries", [])}
    sel = {k: (q.get(k) or "") for k in ("configuration", "state", "cls", "outcome", "component")}
    rows = []
    for k, cid, d in [(k, cid, d) for k in kept for cid, d in sorted(k["docs"].items())]:
        f = d["fact"]
        o = outcome.get(f["id"], {}).get("outcome", "")
        if sel["configuration"] and sel["configuration"] != cid:
            continue
        if sel["state"] and sel["state"] != f["state"]:
            continue
        if sel["cls"] and sel["cls"] != f["change"]["class"]:
            continue
        if sel["outcome"] and sel["outcome"] != o:
            continue
        if sel["component"] and sel["component"] not in (f["dependency"]["component"], f["consumer"]["component"]):
            continue
        rows.append({"obs": d, "f": f, "outcome": o, "configurations": k["configurations"], "configuration": cid})
    rows.sort(key=lambda r: (-r["f"]["depth"], r["f"]["order"], r["f"]["id"]))
    try:
        recent = jobs.jobs(config, limit=5, kind="bumps")
    except Exception:                                     # noqa: BLE001
        recent = []
    return {"scans": scans, "rows": rows, "total": len(kept), "ledger": led, "ledger_at": led_at, "sel": sel,
            "problems": problems, "recent": recent, "schedule": bumps.schedule_status(),
            "configs": sorted({s["fact"]["configuration"] for s in scans})}


def _job_id(text: str) -> int | None:
    from nanosoc_rm import jobs
    return jobs.parse_id(text)


def _later_jobs(config, f: dict, since: str) -> dict[str, dict]:
    """BUMPS-R8: compat what-ifs for exactly this move's pins that the kept cell does not show (queued, running, or
    finished after the scan), newest per tier. Reads jobs.sqlite only (no git). The page then names them, asks for a
    rescan and offers no second Run for that tier."""
    from nanosoc_rm import clock, jobs
    pins = sorted(f["matrix"].get("pins") or [])
    shown = {c["job"] for c in (f["matrix"].get("tiers") or {}).values() if c}
    out: dict[str, dict] = {}
    try:
        records = jobs.jobs(config, limit=500, kind="compat")
    except Exception:                                     # noqa: BLE001 - no job store: nothing to name
        return out
    for j in records:                                     # newest first
        if (j["params"].get("configuration") != f["configuration"] or j["id"] in shown
                or sorted(j["params"].get("pins") or []) != pins or j["state"] in ("failed", "cancelled", "infra")):
            continue
        done_at = j.get("finished_at")
        try:
            if done_at and clock.parse(done_at) <= clock.parse(since):
                continue                                  # the scan already judged it (used, or another tree)
        except ValueError:
            pass
        out.setdefault(j["params"].get("tier") or "t0", {"id": j["id"], "state": j["state"],
                                                          "verdict": j.get("verdict")})
    return out


def router(app: Any = None) -> APIRouter:
    """The plug-in's routes. Its POST routes are registered only in a local deployment (`app.state.rm.local_routes`,
    interfaces.md section 13a), so a GitHub + CI deployment keeps section 13's two Run routes as its only writes
    (D5). Each POST still re-checks at request time."""
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    async def queue(request: Request) -> HTMLResponse:
        config = config_of(request)
        data = await run_in_threadpool(_queue_data, config, request.query_params)
        job = request.query_params.get("job") or ""
        problems = data.pop("problems")
        return _page(request, "bumps_queue.html", "Bump proposals", problems=problems,
                     job=_job_id(job), **data)

    async def proposal(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps
        config = config_of(request)
        cid = request.query_params.get("configuration") or None
        try:
            d = await run_in_threadpool(lambda: bumps.get(pid, configuration=cid, config=config))
        except bumps.BumpUsage as e:
            return _page(request, "bumps_message.html", "Bump proposal", status=404, message=str(e))
        led, led_at, problems = await run_in_threadpool(_ledger_kept, config)
        entry = next((e for e in (led or {}).get("fact", {}).get("entries", []) if e["id"] == pid), None)
        f = d["fact"]
        job = request.query_params.get("job") or ""
        return _page(request, "bumps_proposal.html", "Bump %s to %s" % (f["dependency"]["component"], f["to"]["tag"]),
                     problems=problems, obs=d, f=f, entry=entry, ledger_at=led_at,
                     job=_job_id(job),
                     missing_tiers=[t for t in ("t1", "t2") if not (f["matrix"].get("tiers") or {}).get(t)],
                     later_jobs=await run_in_threadpool(_later_jobs, config, f, d["observed_at"]),
                     marks=bumps.marks(pid, config=config), patch=bumps.patch_text(f),
                     commands=bumps.person_commands(f), message=bumps.commit_message(f))

    def patch_file(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps
        try:
            d = bumps.get(pid, config=config_of(request))
        except bumps.BumpUsage as e:
            return PlainTextResponse(str(e) + "\n", 404)
        return Response(bumps.patch_text(d["fact"]), media_type="text/x-diff",
                        headers={"content-disposition": 'attachment; filename="%s"' % d["fact"]["patch"]["file"]})

    async def ledger_page(request: Request) -> HTMLResponse:
        led, led_at, problems = await run_in_threadpool(_ledger_kept, config_of(request))
        return _page(request, "bumps_ledger.html", "Shadow ledger", problems=problems, ledger=led,
                     ledger_at=led_at)

    def facts(request: Request, subject: str) -> Response:
        from nanosoc_rm import bumps
        config = config_of(request)
        kind, _, rest = subject.partition(":")
        obs = None
        if kind == "bump_proposal":
            cid, _, pid = rest.rpartition(":")
            try:
                obs = bumps.get(pid, configuration=cid or None, config=config)
            except bumps.BumpUsage:
                obs = None
        elif kind == "bump_scan":
            obs = next((s for s in bumps.scans(config=config) if s["subject"] == subject), None)
        elif kind == "bump_ledger":
            obs, _, _ = _ledger_kept(config)
        if obs is None or obs["subject"] != subject:
            return _page(request, "bumps_message.html", "Fact", status=404, message="No bump fact %s is kept." % subject)
        return _page(request, "bumps_fact.html", "Fact", obs=obs, doc=json.dumps(obs, indent=1, sort_keys=True))

    def refused(what: str) -> PlainTextResponse:
        return PlainTextResponse("403 Forbidden: this deployment has no local job worker or local sign-in; run `%s` on "
                                 "a login server instead.\n" % what, 403)

    async def post_scan(request: Request) -> Response:
        from nanosoc_rm import jobs
        form = await request.form()
        cid = str(form.get("configuration") or "").strip() or None
        if not is_local(request):
            return refused("nanosoc-rm bumps %s--store" % ((cid + " ") if cid else ""))
        config = config_of(request)
        if cid is not None:
            # BUMPS-R8: an unknown configuration is refused here, not queued as a job that can only fail
            from nanosoc_rm.bumps.engine import CannotAnswer, _load
            try:
                known = await run_in_threadpool(lambda: {c["id"] for c in _load(config).configurations()})
            except CannotAnswer as e:
                return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
            if cid not in known:
                return PlainTextResponse("400 Bad Request: %r is not a registered configuration\n" % cid[:60], 400)
        try:
            for j in jobs.jobs(config, kind="bumps", states=("queued", "running")):
                if (j["params"].get("configuration") or None) == cid:
                    return RedirectResponse("%s/?job=%d" % (PREFIX, j["id"]), 303)
            job = await run_in_threadpool(jobs.submit, config, "bumps", {"configuration": cid}, login_of(request))
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse("%s/?job=%d" % (PREFIX, job["id"]), 303)

    async def post_mark(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps, jobs
        form = await request.form()
        decision = str(form.get("decision") or "")
        if not is_local(request):
            return refused("nanosoc-rm bumps mark %s %s --by <login>" % (pid, decision or "done"))
        config = config_of(request)
        try:
            await run_in_threadpool(lambda: bumps.mark(pid, login_of(request), decision, str(form.get("note") or "") or None,
                                                       config=config))
        except bumps.BumpUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)

        def refresh() -> int:
            """The ledger counts the mark once it is recomputed: queue that (once), never run it here."""
            for j in jobs.jobs(config, kind="bumps", states=("queued",)):
                if j["params"].get("ledger"):
                    return j["id"]
            return jobs.submit(config, "bumps", {"ledger": True}, login_of(request))["id"]
        job = await run_in_threadpool(refresh)
        cid = str(form.get("configuration") or "")
        return RedirectResponse("%s/p/%s?%s" % (PREFIX, urllib.parse.quote(pid, safe=""), urllib.parse.urlencode(
            {k: v for k, v in (("configuration", cid), ("job", job)) if v})), 303)

    get = ["GET"]
    r.add_api_route("/", queue, methods=get, include_in_schema=False)
    r.add_api_route("/ledger", ledger_page, methods=get, include_in_schema=False)
    r.add_api_route("/facts/{subject:path}", facts, methods=get, include_in_schema=False)
    r.add_api_route("/p/{pid}/patch.diff", patch_file, methods=get, include_in_schema=False)
    r.add_api_route("/p/{pid}", proposal, methods=get, include_in_schema=False)
    if local:
        r.add_api_route("/scan", post_scan, methods=["POST"], include_in_schema=False)
    if local:
        r.add_api_route("/p/{pid}/mark", post_mark, methods=["POST"], include_in_schema=False)
    return r
