"""The Catalogue page (Wave F, CR-WF-gui; David 2026-10-14: front page approved, Characterise.dc.html), mounted at
/catalogue through the web plug-in seam.

    GET  /catalogue/          the new-repo queue (the current `repo_seen` snapshots of the anonymous org scan), the
                              "needs your call" list (entries with a `question`), every group, the ignored list,
                              and "What Save writes"
    POST /catalogue/patch     (local deployments) the page's choices -> catalogue.preview -> the catalogue.yaml
                              patch: `do=preview` shows it on the page, `do=download` returns it as a file
    POST /catalogue/scan      (local deployments) "Scan now": queue one `org-scan` local job (section 23.2:
                              read-only, anonymous listing), or show the one already queued or running; 303 to
                              /catalogue/?job=N. The scheduler queues the same job nightly (CR-1015-orgscan)

Nothing here writes the registry: the patch is for a person to review and apply by merge request (the registry
is the source of truth). A GET reads the registry checkout's files, the store and the job records only: no scan, no
job, no network. The patch POST writes nothing either (no gui.sqlite, no store); it is a POST because it carries the
page's choices and the CSRF token. The scan POST only queues the job; the worker runs it as a child process. Only
local deployments register either (section 13a).
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm.catalogue import model
from nanosoc_rm.web.plugins import config_of, is_local, local_routes, render_page, template_env

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "QUICK", "catalogue_view", "choices_from_form", "scan_status"]

PREFIX = "/catalogue"
NAV_LABEL = "Catalogue"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
# The one-click groups of a new repo (the design's five, Not nanoSoC last); every other group is under Other.
QUICK = ("project", "component", "toolkit", "arch")
IGNORE = "ignore"
GROUP_TONE = {"project": "#123A8C", "soc": "#0A5566", "subsystem": "#3D414B", "component": "#16603F",
              "toolkit": "#7A3E00", "flow": "#5B2E91", "generator": "#94204F", "arch": "#0A5566", "lib": "#454953",
              "infra": "#6A4E00", "legacy": "#4A4E58"}


ENV = template_env(HERE, GROUPS=model.GROUPS, GROUP_LABELS=model.GROUP_LABELS, TYPE_LABELS=model.TYPE_LABELS,
                   ROLE_LABELS=model.ROLE_LABELS, QUICK=QUICK, GROUP_TONE=GROUP_TONE)


def _registry(config):
    from nanosoc_rm.registry import load_registry
    path = (config.get("registry") or {}).get("path")
    if not path:
        raise LookupError("no registry checkout is configured ([registry] path)")
    return load_registry(Path(path).expanduser())


def _new_repos(config) -> list[dict]:
    from nanosoc_rm import db
    try:
        docs = db.latest("repo_seen", config=config)
    except Exception:                                            # noqa: BLE001 - nothing stored yet
        return []
    return sorted(docs, key=lambda d: (not d["fact"].get("mentions_nanosoc"), d["fact"]["repo"]["host"],
                                       d["fact"]["repo"]["path"].lower()))


def _suggest(doc: dict) -> str:
    """The suggested answer of a new repo, shown as a hint and never pre-selected ("Nothing is checked until you
    decide"): Project when it says nanoSoC, else none."""
    return "project" if doc["fact"].get("mentions_nanosoc") else ""


def _posted_rows(form: Any) -> dict[str, str]:
    """{repo key (host:path): the row index it was posted under} from the page's hidden `new.<i>.repo` fields.
    A choice is matched to its repo by this key, never by the row's position: the queue can change between the
    GET that drew the page and the Save."""
    out: dict[str, str] = {}
    for key in list(form.keys()):
        if key.startswith("new.") and key.endswith(".repo"):
            out[str(form.get(key) or "")] = key.split(".")[1]
    return out


def catalogue_view(config, *, form: dict | None = None) -> dict:
    """What catalogue.html prints. `form`: the submitted choices to show again (a preview)."""
    problems: list[str] = []
    try:
        reg = _registry(config)
        chars = reg.characterisations()
        ignored = reg.ignored()
    except Exception as e:                                       # noqa: BLE001 - a banner, never a 500
        problems.append("the registry could not be read: %s" % str(e)[:300])
        reg, chars, ignored = None, {}, []
    form = form or {}
    posted = _posted_rows(form)
    new = []
    for i, d in enumerate(_new_repos(config)):
        f = d["fact"]
        repo = "%s:%s" % (f["repo"]["host"], f["repo"]["path"])
        hint = []
        if f.get("created_on"):
            hint.append("Created %s" % f["created_on"])
        hint.append(f.get("visibility") or "visibility unknown")
        if f.get("description"):
            hint.append(f["description"])
        hint.append("mentions nanoSoC" if f.get("mentions_nanosoc") else "no nanoSoC mention found")
        j = posted.get(repo)                                     # this repo's row on the submitted page, if any
        sub = (lambda f: form.get("new.%s.%s" % (j, f), "")) if j is not None else (lambda f: "")
        new.append({"i": i, "repo": repo, "doc": d, "hint": " · ".join(hint), "picked": sub("group"),
                    "suggest": _suggest(d), "other": sub("other"), "type": sub("type"), "role": sub("role")})
    ask = []
    for cid, ch in chars.items():
        if not ch.question:
            continue
        ask.append({"id": cid, "question": ch.question, "cls": ch.cls, "group": ch.group,
                    "picked": form.get("ask.%s" % cid, ch.group),
                    "confirm": form.get("confirm.%s" % cid) == "1", "guessed": "group" in ch.guessed})
    groups = []
    for g in model.GROUPS:
        ids = sorted(cid for cid, ch in chars.items() if ch.group == g)
        groups.append({"key": g, "label": model.GROUP_LABELS[g], "ids": ids,
                       "guessed": {cid for cid in ids if chars[cid].unconfirmed}})
    unconfirmed = sum(1 for ch in chars.values() if ch.unconfirmed)
    return {"new": new, "ask": ask, "groups": groups, "ignored": ignored, "problems": problems,
            "n_components": len(chars), "unconfirmed": unconfirmed,
            "registry": reg.path if reg is not None else None}


def choices_from_form(form: Any, view: dict) -> tuple[list[dict], list[str]]:
    """The catalogue.preview choices for a submitted page, and the reasons any part was not usable."""
    out: list[dict] = []
    skipped: list[str] = []
    by_repo = {n["repo"]: n for n in view["new"]}
    for key in list(form.keys()):
        if not (key.startswith("new.") and key.endswith(".group")):
            continue
        i = key.split(".")[1]
        group = str(form.get("new.%s.other" % i) or form.get(key) or "")
        if not group:
            continue
        rk = str(form.get("new.%s.repo" % i) or "")
        n = by_repo.get(rk)
        if n is None:
            skipped.append("new repo %s is no longer in the queue" % (rk[:120] or "(row %s, no repo key)" % i[:10]))
            continue
        host, _, path = n["repo"].partition(":")
        if group == IGNORE:
            out.append({"op": "ignore", "repo": {"host": host, "path": path}, "reason": "not nanoSoC (set in the GUI)"})
            continue
        if group not in model.GROUPS:
            skipped.append("%s: %r is not a group" % (n["repo"], group[:30]))
            continue
        ch = {"op": "add", "repo": {"host": host, "path": path}, "group": group}
        if group == "project":
            t, r = str(form.get("new.%s.type" % i) or ""), str(form.get("new.%s.role" % i) or "")
            if t in ("chip", "chiplet"):
                ch["type"] = t
            if r in model.ROLES:
                ch["role"] = r
        out.append(ch)
    for a in view["ask"]:
        picked = str(form.get("ask.%s" % a["id"]) or a["group"])
        if picked not in model.GROUPS:
            skipped.append("%s: %r is not a group" % (a["id"], picked[:30]))
            continue
        if picked != a["group"]:
            out.append({"op": "set", "id": a["id"], "fields": {"group": picked}})
        elif form.get("confirm.%s" % a["id"]) == "1" and a["guessed"]:
            out.append({"op": "confirm", "id": a["id"], "fields": ["group"]})
    return out, skipped


def scan_status(config, job_id: int | None = None) -> dict:
    """What the page says about the org scan, from the job records only: the job named by `?job=N` (an org-scan
    job), else the latest org-scan job; and `hosts`, the configured hosts the scan lists (none: nothing to scan)."""
    from nanosoc_rm import jobs
    from nanosoc_rm.catalogue.orgscan import scanned_hosts
    job = None
    try:
        if job_id is not None:
            j = jobs.get(config, job_id)
            job = j if j is not None and j.get("kind") == "org-scan" else None
        if job is None:
            recent = jobs.jobs(config, kind="org-scan", limit=1)
            job = recent[0] if recent else None
    except Exception:                                            # noqa: BLE001 - no job store yet
        job = None
    try:
        hosts = scanned_hosts(config)
    except Exception:                                            # noqa: BLE001
        hosts = ()
    return {"job": job, "hosts": hosts, "named": job is not None and job_id is not None and job["id"] == job_id}


def _page(request: Request, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    config = config_of(request)
    jq = request.query_params.get("job") or ""
    ctx.setdefault("scan", scan_status(config, int(jq) if jq.isdigit() and len(jq) < 12 else None))
    return render_page(request, "catalogue.html", title, status=status, environment=ENV, local=is_local(request),
                       no_rail=True, bare=True, crumbs=[], **ctx)


def router(app: Any = None) -> APIRouter:
    """GET /catalogue/ always; POST /catalogue/patch and POST /catalogue/scan only in a local deployment (13a)."""
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        config = config_of(request)
        v = catalogue_view(config)
        return _page(request, "Catalogue", c=v, preview=None, problems=v["problems"])

    async def post_patch(request: Request) -> Response:
        from nanosoc_rm.catalogue.preview import PreviewUsage, preview
        config = config_of(request)
        form = await request.form()
        fields = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        v = await run_in_threadpool(catalogue_view, config, form=fields)
        if v["registry"] is None:
            return _page(request, "Catalogue", status=503, c=v, preview=None, problems=v["problems"])
        choices, skipped = choices_from_form(form, v)
        try:
            p = await run_in_threadpool(preview, v["registry"], choices)
        except PreviewUsage as e:
            return _page(request, "Catalogue", status=400, c=v, preview=None, problems=["%s" % e])
        if str(form.get("do") or "") == "download":
            if not p.patch:
                return PlainTextResponse("400 Bad Request: no change chosen, so there is no patch\n", 400)
            return Response(p.patch, media_type="text/x-diff",
                            headers={"Content-Disposition": 'attachment; filename="catalogue.patch"'})
        return _page(request, "Catalogue", c=v, preview={"patch": p.patch, "changes": p.changes,
                                                         "problems": p.problems, "valid": p.valid,
                                                         "skipped": skipped, "n": len(choices)},
                     problems=v["problems"])

    async def post_scan(request: Request) -> Response:
        from nanosoc_rm import jobs
        from nanosoc_rm.catalogue.orgscan import scanned_hosts
        config = config_of(request)
        if not scanned_hosts(config):
            return PlainTextResponse("400 Bad Request: no [hosts.github] org or [hosts.gitlab] group is configured, "
                                     "so there is nothing to scan\n", 400)
        try:
            running = jobs.jobs(config, kind="org-scan", states=("queued", "running"))
            if running:
                return RedirectResponse("%s/?job=%d#new" % (PREFIX, running[0]["id"]), 303)
            login = getattr(getattr(request.state, "identity", None), "login", "") or ""
            job = await run_in_threadpool(jobs.submit, config, "org-scan", {}, login)
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse("%s/?job=%d#new" % (PREFIX, job["id"]), 303)

    r.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    if local:
        r.add_api_route("/patch", post_patch, methods=["POST"], include_in_schema=False)
        r.add_api_route("/scan", post_scan, methods=["POST"], include_in_schema=False)
    return r


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")
