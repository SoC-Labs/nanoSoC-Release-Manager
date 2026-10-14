"""The /freshness page and the on-demand scan (interfaces.md section 24.4), through the web plug-in seam.

    GET  /freshness/          every scanned tree: verdict word, lagging pins (direct and whole tree), days since the
                              newest bump with Fresh/Aging/Stale, root commit; stored facts only, re-judged today
    POST /freshness/scan      queue `nanosoc-rm freshness scan [CONFIG] --store --json` as a local job (local
                              deployments only; elsewhere 403 with the CLI command)

A GET reads rm.sqlite only: no git, no job, no network. The projects home page and the freshness grid build on
`freshness.view` the same way.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm.config import load_config
from nanosoc_rm.freshness import rules

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "ENV", "STATE_CLASS"]

PREFIX = "/freshness"
NAV_LABEL = "Freshness"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
# README section 6 colours; the word is always printed beside it (state never by colour alone).
STATE_CLASS = {"unpushed": "state-red", "outdated": "state-red", "behind": "state-amber", "current": "state-green",
               "frozen": "state-info", "not_measured": "state-grey"}
AGE_CLASS = {"fresh": "state-green", "aging": "state-amber", "stale": "state-red"}


def _env() -> Environment:
    web = HERE.parent / "web" / "templates"
    loaders = [FileSystemLoader(str(HERE / "templates"))] + ([FileSystemLoader(str(web))] if web.is_dir() else [])
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    env.globals.update(STATE_CLASS=STATE_CLASS, AGE_CLASS=AGE_CLASS, WORDS=rules.WORDS, AGE_WORDS=rules.AGE_WORDS,
                       OUTDATED_BEHIND=rules.OUTDATED_BEHIND, OUTDATED_DAYS=rules.OUTDATED_DAYS,
                       FRESH_DAYS=rules.FRESH_DAYS, STALE_DAYS=rules.STALE_DAYS)
    return env


ENV = _env()


def _config(request: Request):
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def _login(request: Request) -> str:
    return getattr(getattr(request.state, "identity", None), "login", "") or ""


def _local(request: Request) -> bool:
    return bool(getattr(getattr(getattr(request.app, "state", None), "rm", None), "local_routes", False))


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    from nanosoc_rm.web import plugins
    return plugins.render_page(request, template, title, status=status, environment=ENV, local=_local(request), **ctx)


def router(app: Any = None) -> APIRouter:
    """GET /freshness/ always; POST /freshness/scan only in a local deployment (section 13a)."""
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        from nanosoc_rm.freshness import view
        config = _config(request)
        try:
            rows = view.trees(config=config)
        except Exception as e:                                   # noqa: BLE001 - shown, never a bare 500
            return _page(request, "freshness_index.html", "Freshness", status=500, rows=[], job=None,
                         problems=["the stored freshness could not be read: %s" % str(e)[:300]])
        job = request.query_params.get("job")
        return _page(request, "freshness_index.html", "Freshness", rows=rows,
                     job=int(job) if job and job.isdigit() else None)

    async def post_scan(request: Request) -> Response:
        from nanosoc_rm import jobs
        form = await request.form()
        cid = str(form.get("configuration") or "").strip() or None
        if not _local(request):
            return PlainTextResponse("403 Forbidden: this deployment has no local job worker; run `nanosoc-rm "
                                     "freshness scan %s--store` on a login server instead.\n" % ((cid + " ") if cid else ""),
                                     403)
        config = _config(request)
        if cid is not None:
            from nanosoc_rm.mirror.catalogue import load_catalogue
            try:
                known = await run_in_threadpool(lambda: {c["id"] for c in load_catalogue(config).configurations()})
            except Exception as e:                               # noqa: BLE001 - the registry does not load
                return PlainTextResponse("400 Bad Request: %s\n" % str(e)[:300], 400)
            if cid not in known:
                return PlainTextResponse("400 Bad Request: %r is not a registered configuration\n" % cid[:60], 400)
        try:
            for j in jobs.jobs(config, kind="freshness-scan", states=("queued", "running")):
                if (j["params"].get("configuration") or None) in (None, cid):
                    return RedirectResponse("%s/?job=%d" % (PREFIX, j["id"]), 303)
            job = await run_in_threadpool(jobs.submit, config, "freshness-scan", {"configuration": cid},
                                          _login(request))
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse("%s/?job=%d" % (PREFIX, job["id"]), 303)

    r.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    if local:
        r.add_api_route("/scan", post_scan, methods=["POST"], include_in_schema=False)
    return r
