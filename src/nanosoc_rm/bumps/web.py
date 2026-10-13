"""The /bumps pages (E1, E2; interfaces.md section 18.8), mounted through the web plug-in seam (web/plugins.py).

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
local deployment (the app's local routes, CR-16/CR-17); elsewhere they answer 403 with the CLI command. Pages read
stored records and the mirrors; the scan itself always runs as a job (nanosoc_rm.jobs), never in a request.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm.config import load_config

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "ENV", "STATE_CLASS", "OUTCOME_CLASS", "CLASS_CLASS",
           "STATUS_CLASS"]

PREFIX = "/bumps"
NAV_LABEL = "Bumps"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
_WEB_TEMPLATES = HERE.parent / "web" / "templates"

# README section 6 colours; every pill also carries its text.
STATE_CLASS = {"proposed": "state-info", "blocked": "state-amber"}
OUTCOME_CLASS = {"agreed": "state-green", "disagreed": "state-red", "pending": "state-grey",
                 "superseded": "state-grey", "unknown": "state-amber"}
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
                  'data-source-for="%s" href="%s" title="observed %s; how it was found">src</a>'
                  % (escape(s), escape(obs["observed_at"]), escape(s if label is None else label), escape(s),
                     escape(href), escape(obs["observed_at"])))


def _pill(text: Any, table: dict) -> Markup:
    return Markup('<span class="pill %s">%s</span>' % (escape(table.get(str(text), "")), escape(text)))


def _env() -> Environment:
    loaders = [FileSystemLoader(str(HERE / "templates"))]
    if _WEB_TEMPLATES.is_dir():
        loaders.append(FileSystemLoader(str(_WEB_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    env.globals.update(bfact=_fact, pill=_pill, STATE_CLASS_B=STATE_CLASS, OUTCOME_CLASS=OUTCOME_CLASS,
                       CLASS_CLASS=CLASS_CLASS, STATUS_CLASS=STATUS_CLASS, NO_WRITES=NO_WRITES,
                       qs=lambda **kw: "?" + urllib.parse.urlencode({k: v for k, v in kw.items() if v}),
                       short=lambda s, n=12: (s or "")[:n])
    return env


ENV = _env()


def _config(request: Request):
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def _login(request: Request) -> str:
    ident = getattr(request.state, "identity", None)
    return getattr(ident, "login", "") or ""


def _local(request: Request) -> bool:
    rm = getattr(getattr(request.app, "state", None), "rm", None)
    return bool(getattr(rm, "local_routes", False))


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    from nanosoc_rm.web import plugins
    return plugins.render_page(request, template, title, status=status, environment=ENV, local=_local(request), **ctx)


def _ledger_safe(config) -> tuple[dict | None, str | None]:
    from nanosoc_rm import bumps
    try:
        return bumps.ledger(config=config), None
    except Exception as e:                                # noqa: BLE001 - the page still shows the queue
        return None, "the shadow ledger could not be computed: %s" % str(e)[:300]


def _queue_data(config, q) -> dict:
    from nanosoc_rm import bumps, jobs
    from nanosoc_rm.bumps import store
    scans = bumps.scans(config=config)
    kept = store.proposals(config)
    led, problem = _ledger_safe(config)
    outcome = {e["id"]: e for e in (led or {}).get("fact", {}).get("entries", [])}
    sel = {k: (q.get(k) or "") for k in ("configuration", "state", "cls", "outcome", "component")}
    rows = []
    for k in kept:
        d, f = k["doc"], k["doc"]["fact"]
        o = outcome.get(f["id"], {}).get("outcome", "")
        if sel["configuration"] and sel["configuration"] not in k["configurations"]:
            continue
        if sel["state"] and sel["state"] != f["state"]:
            continue
        if sel["cls"] and sel["cls"] != f["change"]["class"]:
            continue
        if sel["outcome"] and sel["outcome"] != o:
            continue
        if sel["component"] and sel["component"] not in (f["dependency"]["component"], f["consumer"]["component"]):
            continue
        rows.append({"obs": d, "f": f, "outcome": o, "configurations": k["configurations"]})
    rows.sort(key=lambda r: (-r["f"]["depth"], r["f"]["order"], r["f"]["id"]))
    try:
        recent = jobs.jobs(config, limit=5, kind="bumps")
    except Exception:                                     # noqa: BLE001
        recent = []
    return {"scans": scans, "rows": rows, "total": len(kept), "ledger": led, "sel": sel,
            "problems": [problem] if problem else [], "recent": recent, "schedule": bumps.schedule_status(),
            "configs": sorted({s["fact"]["configuration"] for s in scans})}


def router() -> APIRouter:
    r = APIRouter(prefix=PREFIX)

    async def queue(request: Request) -> HTMLResponse:
        config = _config(request)
        data = await run_in_threadpool(_queue_data, config, request.query_params)
        job = request.query_params.get("job") or ""
        problems = data.pop("problems")
        return _page(request, "bumps_queue.html", "Bump proposals (shadow mode)", problems=problems,
                     job=int(job) if job.isdigit() else None, **data)

    async def proposal(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps
        config = _config(request)
        try:
            d = bumps.get(pid, config=config)
        except bumps.BumpUsage as e:
            return _page(request, "bumps_message.html", "Bump proposal", status=404, message=str(e))
        led, problem = await run_in_threadpool(_ledger_safe, config)
        entry = next((e for e in (led or {}).get("fact", {}).get("entries", []) if e["id"] == pid), None)
        f = d["fact"]
        return _page(request, "bumps_proposal.html", "Bump %s to %s" % (f["dependency"]["component"], f["to"]["tag"]),
                     problems=[problem] if problem else [], obs=d, f=f, entry=entry,
                     marks=bumps.marks(pid, config=config), patch=bumps.patch_text(f),
                     commands=bumps.person_commands(f), message=bumps.commit_message(f))

    def patch_file(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps
        try:
            d = bumps.get(pid, config=_config(request))
        except bumps.BumpUsage as e:
            return PlainTextResponse(str(e) + "\n", 404)
        return Response(bumps.patch_text(d["fact"]), media_type="text/x-diff",
                        headers={"content-disposition": 'attachment; filename="%s"' % d["fact"]["patch"]["file"]})

    async def ledger_page(request: Request) -> HTMLResponse:
        led, problem = await run_in_threadpool(_ledger_safe, _config(request))
        return _page(request, "bumps_ledger.html", "Shadow ledger (E2)", problems=[problem] if problem else [],
                     ledger=led)

    def facts(request: Request, subject: str) -> Response:
        from nanosoc_rm import bumps
        config = _config(request)
        kind, _, rest = subject.partition(":")
        obs = None
        if kind == "bump_proposal":
            try:
                obs = bumps.get(rest.split(":")[-1], config=config)
            except bumps.BumpUsage:
                obs = None
        elif kind == "bump_scan":
            obs = next((s for s in bumps.scans(config=config) if s["subject"] == subject), None)
        elif kind == "bump_ledger":
            obs, _ = _ledger_safe(config)
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
        if not _local(request):
            return refused("nanosoc-rm bumps %s--store" % ((cid + " ") if cid else ""))
        config = _config(request)
        try:
            for j in jobs.jobs(config, kind="bumps", states=("queued", "running")):
                if (j["params"].get("configuration") or None) == cid:
                    return RedirectResponse("%s/?job=%d" % (PREFIX, j["id"]), 303)
            job = await run_in_threadpool(jobs.submit, config, "bumps", {"configuration": cid}, _login(request))
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse("%s/?job=%d" % (PREFIX, job["id"]), 303)

    async def post_mark(request: Request, pid: str) -> Response:
        from nanosoc_rm import bumps
        form = await request.form()
        decision = str(form.get("decision") or "")
        if not _local(request):
            return refused("nanosoc-rm bumps mark %s %s --by <login>" % (pid, decision or "done"))
        try:
            await run_in_threadpool(lambda: bumps.mark(pid, _login(request), decision, str(form.get("note") or "") or None,
                                                       config=_config(request)))
        except bumps.BumpUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse("%s/p/%s" % (PREFIX, urllib.parse.quote(pid, safe="")), 303)

    get = ["GET"]
    r.add_api_route("/", queue, methods=get, include_in_schema=False)
    r.add_api_route("/ledger", ledger_page, methods=get, include_in_schema=False)
    r.add_api_route("/facts/{subject:path}", facts, methods=get, include_in_schema=False)
    r.add_api_route("/p/{pid}/patch.diff", patch_file, methods=get, include_in_schema=False)
    r.add_api_route("/p/{pid}", proposal, methods=get, include_in_schema=False)
    r.add_api_route("/scan", post_scan, methods=["POST"], include_in_schema=False)
    r.add_api_route("/p/{pid}/mark", post_mark, methods=["POST"], include_in_schema=False)
    return r
