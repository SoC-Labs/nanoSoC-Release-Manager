"""The Publish pages (CR-ART-publish; G8): a FastAPI router mounted at /publish through the plug-in seam.

    GET  /publish/                                       the ledger (what was published), drafts ready to publish,
                                                         tapeout drafts, why publishing is off here (if it is)
    GET  /publish/release/{configuration}/{version}      one release: draft summary, the stored preview, Publish
    GET  /publish/tapeout/{tapeout_id}                   one tapeout draft: the stored preview, Publish
    POST /publish/release/{configuration}/{version}/preview   queue `publish-release C/V --dry-run`
    POST /publish/release/{configuration}/{version}/confirm   queue `publish-release C/V --confirm SHA256`
    POST /publish/tapeout/{tapeout_id}/preview                queue `register-tapeout ID --dry-run`
    POST /publish/tapeout/{tapeout_id}/confirm                queue `register-tapeout ID --confirm SHA256`

This is the ONE place the web app leads to a write outside the host (David 2026-10-14: a GUI Publish button for
asic-release). The pages only show stored facts (the ledger, the preview file a job wrote) and never call the
store; both POSTs queue a job (a child process holds the token, never the web process). A POST answers 404
outside a local deployment, 403 for a login that is not in `[stores.artifactory] publishers` or is a shared login,
409 when publishing cannot run here (no token, no signing key: the button is shown disabled with the reason).
Confirm is accepted only for the sha256 of the preview on the page, only when that preview found no problem, and
only with the confirmation box ticked; it is logged in `actions` with the login.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import jobs
from nanosoc_rm.config import Config, load_config

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH"]

PREFIX = "/publish"
NAV_LABEL = "Publish"
NAV_PATH = PREFIX + "/"
JOB_KINDS = ("publish-release", "register-tapeout")
HERE = Path(__file__).resolve().parent
_WEB_TEMPLATES = HERE.parent / "web" / "templates"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _env() -> Environment:
    loaders = [FileSystemLoader(str(HERE / "templates"))]
    if _WEB_TEMPLATES.is_dir():
        loaders.append(FileSystemLoader(str(_WEB_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    from nanosoc_rm.web.page import env as web_env
    env.globals.update(web_env.globals)
    env.globals.update(purl=purl)
    return env


def purl(kind: str, key: str, page: str = "") -> str:
    return "%s/%s/%s%s" % (PREFIX, kind, "/".join(urllib.parse.quote(k, safe="") for k in key.split("/")),
                           ("/" + page) if page else "")


def _config(request: Request) -> Config:
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def _local(request: Request) -> bool:
    return bool(getattr(request.app.state.rm, "local_routes", False))


def _login(request: Request) -> str:
    return getattr(getattr(request.state, "identity", None), "login", "") or ""


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    from nanosoc_rm.web import plugins
    return plugins.render_page(request, template, title, status=status, environment=ENV, local=_local(request), **ctx)


def _job_html(request: Request, config: Config, job: dict | None) -> Markup:
    if job is None:
        return Markup("")
    from nanosoc_rm.web.page import Page
    p = Page()
    ident = getattr(request.state, "identity", None)
    csrf = request.app.state.rm.csrf_for(ident, config) if ident is not None else ""
    return Markup(ENV.get_template("_job.html").render(
        job=job, log=jobs.log_text(config, job["id"]), position=jobs.queue_position(config, job["id"]), csrf=csrf,
        fact=p.fact, page=p, stored=None))


def _recent(config: Config, match: dict | None = None, limit: int = 10) -> list[dict]:
    try:
        rows = jobs.jobs(config, limit=200)
    except Exception:                                   # noqa: BLE001 - no job store yet: no jobs
        return []
    out = [j for j in rows if j["kind"] in JOB_KINDS
           and all(j["params"].get(k) == v for k, v in (match or {}).items())]
    return out[:limit]


def _blocked(config: Config, login: str) -> list[str]:
    from nanosoc_rm.publish.core import readiness
    try:
        return readiness(config, login)
    except Exception as e:                              # noqa: BLE001 - say why, never hide the page
        return ["the publish settings do not read: %s" % str(e)[:200]]


def _published(config: Config, kind: str, key: str) -> list[dict]:
    from nanosoc_rm.publish.service import publications
    out = []
    for r in publications(config):
        f = r["fact"]
        if f["kind"] != kind:
            continue
        if kind == "release" and "%s/%s" % (f.get("configuration"), f["version"]) == key:
            out.append(r)
        elif kind == "tapeout" and f.get("tapeout_id") == key:
            out.append(r)
    return out


def _tapeout_drafts(config: Config) -> list[str]:
    try:
        d = Path(config.require("registry", "path")) / "tapeouts"
    except Exception:                                   # noqa: BLE001 - no registry configured: none
        return []
    return sorted(p.name[:-len(".tapeout.yaml")] for p in d.glob("*.tapeout.yaml")
                  if _ID.match(p.name[:-len(".tapeout.yaml")])) if d.is_dir() else []


def _release_drafts(config: Config) -> list[dict]:
    try:
        from nanosoc_rm.releases.builder import drafts
        return [r for r in drafts(config) if r["doc"]]
    except Exception:                                   # noqa: BLE001 - no drafts folder yet
        return []


async def _submit(request: Request, kind: str, params: dict, back: str, *, confirm: bool) -> Response:
    if not _local(request):
        return PlainTextResponse("404 Not Found: this deployment runs no local jobs; use the CLI", 404)
    config = _config(request)
    login = _login(request)
    from nanosoc_rm.publish.core import can_publish, readiness
    refused = can_publish(config, login)
    if refused:
        return PlainTextResponse("403 Forbidden: %s" % "; ".join(refused), 403)
    if confirm:
        blocked = readiness(config)
        if blocked:
            return PlainTextResponse("409 Conflict: publishing cannot run here: %s" % "; ".join(blocked), 409)
    params = dict(params, by=login)
    try:
        norm = jobs._params(kind, dict(params))
    except jobs.JobUsage as e:
        return PlainTextResponse("400 Bad Request: %s" % e, 400)
    for j in _recent(config, limit=200):
        if j["kind"] == kind and j["state"] in ("queued", "running") and j["params"] == norm:
            return RedirectResponse(back + "?job=%d" % j["id"], 303)
    try:
        job = await run_in_threadpool(jobs.submit, config, kind, params, login)
    except jobs.JobUsage as e:
        return PlainTextResponse("400 Bad Request: %s" % e, 400)
    if confirm:
        from nanosoc_rm import db
        target = "%s:%s" % ("release" if kind == "publish-release" else "tapeout",
                            norm.get("tapeout_id") or "%s/%s" % (norm.get("configuration"), norm.get("version")))
        db.log_action(login, "publish_release" if kind == "publish-release" else "register_tapeout", target,
                      "confirmed sha256 %s, job %d queued" % (norm["confirm"], job["id"]), config=config)
    return RedirectResponse(back + "?job=%d" % job["id"], 303)


async def _confirm(request: Request, kind: str, key: str, params: dict, back: str) -> Response:
    from nanosoc_rm.publish.service import read_preview
    form = await request.form()
    sha = str(form.get("sha256") or "")
    if not _SHA256.match(sha):
        return PlainTextResponse("400 Bad Request: no previewed sha256 to confirm", 400)
    if not form.get("ack"):
        return PlainTextResponse("400 Bad Request: tick the box to confirm you read the preview", 400)
    config = _config(request)
    prev = read_preview(config, kind, key)
    if prev is None or prev.get("sha256") != sha:
        return PlainTextResponse("409 Conflict: that is not the stored preview's sha256: preview again", 409)
    if prev.get("problems"):
        return PlainTextResponse("409 Conflict: the preview found problems: %s" % "; ".join(prev["problems"])[:600], 409)
    return await _submit(request, "publish-release" if kind == "release" else "register-tapeout",
                         dict(params, confirm=sha),
                         back, confirm=True)


def router(app: Any = None) -> APIRouter:
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        from nanosoc_rm.publish.core import settings
        from nanosoc_rm.publish.service import publications
        config = _config(request)
        rows = sorted(publications(config), key=lambda d: d["fact"]["published_at"], reverse=True)
        return _page(request, "publish_index.html", "Publish", rows=rows, blocked=_blocked(config, _login(request)),
                     drafts=_release_drafts(config), tapeouts=_tapeout_drafts(config), repo=settings(config).release_repo,
                     recent=_recent(config))

    def item(request: Request, kind: str, key: str, title: str, summary: dict | None) -> HTMLResponse:
        from nanosoc_rm.publish.service import read_preview
        config = _config(request)
        q = request.query_params
        job = jobs.get(config, int(q["job"])) if (q.get("job") or "").isdigit() else None
        match = {"tapeout_id": key} if kind == "tapeout" else dict(zip(("configuration", "version"), key.split("/")))
        recent = _recent(config, match)
        return _page(request, "publish_item.html", title, kind=kind, key=key, summary=summary,
                     preview=read_preview(config, kind, key), published=_published(config, kind, key),
                     blocked=_blocked(config, _login(request)), job_html=_job_html(request, config, job or (
                         recent[0] if recent else None)), recent=recent)

    def release_page(request: Request, configuration: str, version: str) -> HTMLResponse:
        if not _ID.match(configuration) or not _SEMVER.match(version):
            return _page(request, "publish_message.html", "Not found", status=404, message="No such release.")
        config = _config(request)
        draft = next((d for d in _release_drafts(config)
                      if d["configuration"] == configuration and d["version"] == version), None)
        if draft is None:
            return _page(request, "publish_message.html", "Not found", status=404,
                         message="No release draft %s %s here: draft it on the Releases page first." % (
                             configuration, version))
        return item(request, "release", "%s/%s" % (configuration, version),
                    "Publish release %s %s" % (configuration, version), draft["doc"]["fact"])

    def tapeout_page(request: Request, tapeout_id: str) -> HTMLResponse:
        config = _config(request)
        if not _ID.match(tapeout_id) or tapeout_id not in _tapeout_drafts(config):
            return _page(request, "publish_message.html", "Not found", status=404,
                         message="No tapeout draft %s in the registry's tapeouts/ folder." % tapeout_id)
        return item(request, "tapeout", tapeout_id, "Register tapeout %s" % tapeout_id, None)

    def _release_ok(configuration: str, version: str) -> bool:
        return bool(_ID.match(configuration) and _SEMVER.match(version))

    async def release_preview(request: Request, configuration: str, version: str) -> Response:
        if not _release_ok(configuration, version):
            return PlainTextResponse("404 Not Found", 404)
        form = await request.form()
        arts = [ln.strip() for ln in str(form.get("artefacts") or "").splitlines() if ln.strip()]
        return await _submit(request, "publish-release", {"configuration": configuration, "version": version,
                                                          "artefacts": arts},
                             purl("release", "%s/%s" % (configuration, version)), confirm=False)

    async def release_confirm(request: Request, configuration: str, version: str) -> Response:
        if not _release_ok(configuration, version):
            return PlainTextResponse("404 Not Found", 404)
        key = "%s/%s" % (configuration, version)
        from nanosoc_rm.publish.service import read_preview
        prev = read_preview(_config(request), "release", key) or {}
        arts = ["%s=%s" % (a[0], a[1]) for a in prev.get("artefacts") or []]
        return await _confirm(request, "release", key, {"configuration": configuration, "version": version,
                                                        "artefacts": arts}, purl("release", key))

    async def tapeout_preview(request: Request, tapeout_id: str) -> Response:
        if not _ID.match(tapeout_id):
            return PlainTextResponse("404 Not Found", 404)
        return await _submit(request, "register-tapeout", {"tapeout_id": tapeout_id}, purl("tapeout", tapeout_id),
                             confirm=False)

    async def tapeout_confirm(request: Request, tapeout_id: str) -> Response:
        if not _ID.match(tapeout_id):
            return PlainTextResponse("404 Not Found", 404)
        return await _confirm(request, "tapeout", tapeout_id, {"tapeout_id": tapeout_id}, purl("tapeout", tapeout_id))

    get = ["GET"]
    r.add_api_route("/", index, methods=get, include_in_schema=False)
    r.add_api_route("/release/{configuration}/{version}", release_page, methods=get, include_in_schema=False)
    r.add_api_route("/tapeout/{tapeout_id}", tapeout_page, methods=get, include_in_schema=False)
    if local:
        post = ["POST"]
        r.add_api_route("/release/{configuration}/{version}/preview", release_preview, methods=post,
                        include_in_schema=False)
        r.add_api_route("/release/{configuration}/{version}/confirm", release_confirm, methods=post,
                        include_in_schema=False)
        r.add_api_route("/tapeout/{tapeout_id}/preview", tapeout_preview, methods=post, include_in_schema=False)
        r.add_api_route("/tapeout/{tapeout_id}/confirm", tapeout_confirm, methods=post, include_in_schema=False)
    return r


ENV = _env()
