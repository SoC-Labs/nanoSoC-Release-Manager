"""The /versions pages (C1-C12, G1, G5, G6), mounted through the web plug-in seam (web/plugins.py).

    GET  /versions/                                   every component: current tag, unreleased entries by class,
                                                      next version and class, manifest/API.md, changelog and tags
    GET  /versions/{component}                        the version plan with every signal, the changelog findings,
                                                      the tag table, the manifest proposal and the release drafts
    GET  /versions/{component}/manifest.diff          the proposed nanosoc-component.yaml as a patch (C11)
    GET  /versions/{component}/drafts/{version}/{file}  one file of a release draft folder (G1)
    POST /versions/refresh                            queue `nanosoc-rm version refresh [COMPONENT]` (a job)
    POST /versions/{component}/draft                  queue `nanosoc-rm version draft COMPONENT` (a job)
    POST /versions/{component}/drafts/{version}/approve   record the SIGNED-IN login's approval of one step (G5)

Pages show STORED results (`version refresh` stores them); long work runs as a job through nanosoc_rm.jobs, never
inside a request. The POST routes exist only in a local deployment (interfaces.md section 13a). Every page says
that nothing is pushed: drafts and patches are for a person. Verdict colours with text labels (README section 6).
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "ENV"]

PREFIX = "/versions"
NAV_LABEL = "Versions"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
_WEB_TEMPLATES = HERE.parent / "web" / "templates"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
NOT_PUSHED = "Nothing here is pushed, tagged or opened: drafts and patches are files for a person to review and apply."
CLASS_CLASS = {"breaking": "state-red", "feature": "state-amber", "fix": "state-green", "none": "state-grey"}


def _env() -> Environment:
    loaders = [FileSystemLoader(str(HERE / "templates"))]
    if _WEB_TEMPLATES.is_dir():
        loaders.append(FileSystemLoader(str(_WEB_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    env.globals.update(CLASS_CLASS=CLASS_CLASS, NOT_PUSHED=NOT_PUSHED, vurl=_vurl)
    return env


def _vurl(component: str, page: str = "") -> str:
    return "%s/%s%s" % (PREFIX, urllib.parse.quote(component, safe=""), ("/" + page) if page else "")


ENV = _env()


def _config(request: Request):
    from nanosoc_rm.config import load_config
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def _page(request: Request, template: str, title: str, status: int = 200, **ctx):
    from nanosoc_rm.web.plugins import render_page
    return render_page(request, template, title, status=status, environment=ENV, **ctx)


def _job(config, q) -> dict | None:
    from nanosoc_rm import jobs
    j = q.get("job") or ""
    return jobs.get(config, n) if (n := jobs.parse_id(j)) is not None else None


def _drafts(config, component: str) -> list[dict]:
    root = config.data_dir / "drafts" / component
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            if d.is_dir() and _VER.match(d.name):
                try:
                    approvals = json.loads((d / "release.json").read_text()).get("approvals") or []
                except (OSError, ValueError):
                    approvals = []
                out.append({"version": d.name, "files": [f.name for f in sorted(d.iterdir()) if f.is_file()],
                            "approvals": approvals})
    return out


def _known(config, component: str) -> bool:
    """Is `component` a catalogue component the pages show (review item 8: never queue a job for another)?"""
    from nanosoc_rm import versioning
    try:
        return _ID.match(component) is not None and any(r["component"] == component
                                                         for r in versioning.overview(config))
    except Exception:                                              # noqa: BLE001 - registry unreadable: not known
        return False


def router(app=None) -> APIRouter:
    from nanosoc_rm import versioning
    from nanosoc_rm.versioning.commands import ensure_job_kinds
    ensure_job_kinds()
    local = bool(getattr(getattr(getattr(app, "state", None), "rm", None), "local_routes", False))
    r = APIRouter(prefix=PREFIX)

    @r.get("/")
    def overview(request: Request):
        from nanosoc_rm import jobs
        config = _config(request)
        problems = []
        try:
            rows = versioning.overview(config)
        except Exception as e:                                  # noqa: BLE001 - the registry cannot be read: say so
            rows, problems = [], ["the registry cannot be read: %s" % e]
        recent = [j for j in jobs.jobs(config, limit=10, kind="versions-refresh")]
        return _page(request, "versions_overview.html", "Versions", problems=problems, rows=rows,
                     job=_job(config, request.query_params), recent=recent, local=local)

    @r.get("/{component}")
    def component_page(request: Request, component: str):
        config = _config(request)
        if not _ID.match(component):
            return _page(request, "message.html", "Not found", status=404, message="No component %r." % component)
        try:
            rows = {x["component"]: x for x in versioning.overview(config)}
        except Exception as e:                                  # noqa: BLE001
            return _page(request, "message.html", "Versions", status=200, message="The registry cannot be read: %s" % e)
        row = rows.get(component)
        if row is None:
            return _page(request, "message.html", "Not found", status=404,
                         message="%s is not a catalogue component." % component)
        hist = versioning.stored("version_plan", component, config=config)
        drafts_obs = versioning.stored("tool_release_draft", component, config=config)
        ident = getattr(request.state, "identity", None)
        cur = row.get("plan") or {}
        earlier = [h for h in hist if (h.get("subject"), h.get("observed_at")) != (cur.get("subject"),
                                                                                    cur.get("observed_at"))]
        return _page(request, "versions_component.html", "Versions: %s" % component, row=row, history=earlier[-10:],
                     drafts=_drafts(config, component), draft_obs=drafts_obs[-5:], job=_job(config, request.query_params),
                     local=local, me=getattr(ident, "login", ""))

    @r.get("/{component}/manifest.diff")
    def manifest_diff(request: Request, component: str):
        if not _ID.match(component):
            return PlainTextResponse("404 Not Found", 404)
        config = _config(request)
        try:
            obs, patch = versioning.manifest_init(component, config=config)
        except versioning.VersioningUsage as e:
            return PlainTextResponse("404 Not Found: %s" % e, 404)
        except versioning.VersioningError as e:
            return PlainTextResponse("could not answer: %s" % e, 503)
        if not patch.strip():
            return PlainTextResponse("404 Not Found: nothing to patch: the manifest already matches the proposal", 404)
        head = "# nanosoc-component.yaml for %s: a patch for a person (git apply); nothing was applied.\n" % component
        return PlainTextResponse(head + patch, headers={"content-disposition": 'attachment; filename="%s-manifest.diff"'
                                                          % component})

    @r.get("/{component}/drafts/{version}/{name}")
    def draft_file(request: Request, component: str, version: str, name: str):
        from nanosoc_rm.versioning.release import DRAFT_FILES
        if not _ID.match(component) or not _VER.match(version) or name not in DRAFT_FILES:
            return PlainTextResponse("404 Not Found", 404)
        f = _config(request).data_dir / "drafts" / component / version / name
        if not f.is_file():
            return PlainTextResponse("404 Not Found", 404)
        return PlainTextResponse(f.read_text())

    if local:
        async def refresh(request: Request) -> Response:
            from nanosoc_rm import jobs
            config = _config(request)
            form = await request.form()
            comp = str(form.get("component") or "").strip()
            if comp and not await run_in_threadpool(_known, config, comp):
                return PlainTextResponse("404 Not Found: %r is not a catalogue component" % comp[:80], 404)
            try:
                job = await run_in_threadpool(jobs.submit, config, "versions-refresh",
                                              {"components": [comp] if comp else []}, request.state.identity.login)
            except jobs.JobUsage as e:
                return PlainTextResponse("400 Bad Request: %s" % e, 400)
            return RedirectResponse((_vurl(comp) if comp else PREFIX + "/") + "?job=%d" % job["id"], 303)

        async def make_draft(request: Request, component: str) -> Response:
            from nanosoc_rm import jobs
            config = _config(request)
            if not await run_in_threadpool(_known, config, component):
                return PlainTextResponse("404 Not Found: %r is not a catalogue component" % component[:80], 404)
            try:
                job = await run_in_threadpool(jobs.submit, config, "version-draft", {"component": component},
                                              request.state.identity.login)
            except jobs.JobUsage as e:
                return PlainTextResponse("400 Bad Request: %s" % e, 400)
            return RedirectResponse(_vurl(component) + "?job=%d" % job["id"], 303)

        async def approve_step(request: Request, component: str, version: str) -> Response:
            """G5 from the GUI: the SIGNED-IN login approves (review item 6); a typed login is ignored."""
            config = _config(request)
            if not _ID.match(component) or not _VER.match(version):
                return PlainTextResponse("404 Not Found", 404)
            d = config.data_dir / "drafts" / component / version
            if not (d / "release.json").is_file():
                return PlainTextResponse("404 Not Found: no draft %s %s" % (component, version), 404)
            form = await request.form()
            step = str(form.get("step") or "").strip()
            login = request.state.identity.login
            try:
                await run_in_threadpool(lambda: versioning.approve(d, step, login, config=config,
                                                                         session_login=login))
            except versioning.VersioningUsage as e:
                return PlainTextResponse("400 Bad Request: %s" % e, 400)
            except versioning.VersioningError as e:
                return PlainTextResponse("could not answer: %s" % e, 503)
            return RedirectResponse(_vurl(component) + "?approved=%s" % urllib.parse.quote(step), 303)

        # local deployment only (interfaces.md section 13a); registered as the app registers its own local POSTs
        r.add_api_route("/refresh", refresh, methods=["POST"], include_in_schema=False)
        r.add_api_route("/{component}/draft", make_draft, methods=["POST"], include_in_schema=False)
        r.add_api_route("/{component}/drafts/{version}/approve", approve_step, methods=["POST"], include_in_schema=False)

    return r
