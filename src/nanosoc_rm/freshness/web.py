"""The retired /freshness page and the on-demand scan (interfaces.md section 24.4), through the web plug-in seam.

David 2026-10-15 (CR-1015-retire): the older /freshness/ page duplicated Home and the Matrix's Freshness tab, so it
is retired. Its GETs redirect to Home; the scan it queued stays, as Home's "Scan <id> now" and the CLI
(`nanosoc-rm freshness show` and `freshness scan` are unchanged).

    GET  /freshness, /freshness/      301 to `/`; with `?configuration=ID` naming a registered configuration, 302 to
                                      that configuration's row on Home (its project and itself open, it selected)
    GET  /freshness/{rest:path}       the same; the first path segment may name the configuration
                                      (/freshness/eth-main -> its Home row); anything else 301 to `/`
    POST /freshness/scan              queue `nanosoc-rm freshness scan [CONFIG] --store --json` as a local job, or
                                      find the one queued or running that covers it, and 303 to the configuration's
                                      Home row (or `/`); local deployments only, elsewhere 403 with the CLI command

A GET reads the stored registry records only (to find a configuration's project): no git, no job, no network.
The module adds no navigation entry (NAV_LABEL is None).
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from nanosoc_rm.config import load_config

__all__ = ["router", "PREFIX", "NAV_LABEL", "home_url"]

PREFIX = "/freshness"
NAV_LABEL = None                    # CR-1015-retire: no navigation entry (web.plugins.load skips it)


def _config(request: Request):
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def _login(request: Request) -> str:
    return getattr(getattr(request.state, "identity", None), "login", "") or ""


def _local(request: Request) -> bool:
    return bool(getattr(getattr(getattr(request.app, "state", None), "rm", None), "local_routes", False))


def home_url(config: Any, configuration: str | None) -> str | None:
    """The Home URL of a registered configuration's row (its project and itself open, it selected), from the
    stored registry records; None when `configuration` is empty or not a registered configuration."""
    if not configuration:
        return None
    from nanosoc_rm.web import home, views
    try:
        recs = {d["fact"]["record"]["id"]: d["fact"]["record"] for d in views.configurations(config)}
    except Exception:                                            # noqa: BLE001 - nothing stored yet
        return None
    rec = recs.get(configuration)
    if rec is None:
        return None
    key = "c:" + configuration
    params = [("v", "1")] + ([("o", "p:" + rec["component"])] if rec.get("component") else []) + \
        [("o", key), ("sel", key)]
    return "/?" + urllib.parse.urlencode(params) + "#" + home._row_id(key)


def _redirect(request: Request, named: str | None) -> Response:
    url = home_url(_config(request), named)
    if url is None:
        return RedirectResponse("/", 301)
    return RedirectResponse(url, 302)               # depends on the stored registry: not a permanent answer


def router(app: Any = None) -> APIRouter:
    """GET /freshness[/...] redirects everywhere; POST /freshness/scan only in a local deployment (section 13a)."""
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> Response:
        return _redirect(request, (request.query_params.get("configuration") or "").strip() or None)

    def sub(request: Request, rest: str) -> Response:
        named = (request.query_params.get("configuration") or "").strip() or rest.strip("/").split("/", 1)[0]
        return _redirect(request, named or None)

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
        back = home_url(config, cid) or "/"
        try:
            for j in jobs.jobs(config, kind="freshness-scan", states=("queued", "running")):
                if (j["params"].get("configuration") or None) in (None, cid):
                    return RedirectResponse(back, 303)
            await run_in_threadpool(jobs.submit, config, "freshness-scan", {"configuration": cid}, _login(request))
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s\n" % e, 400)
        return RedirectResponse(back, 303)

    r.add_api_route("", index, methods=["GET"], include_in_schema=False)
    r.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    r.add_api_route("/{rest:path}", sub, methods=["GET"], include_in_schema=False)
    if local:
        r.add_api_route("/scan", post_scan, methods=["POST"], include_in_schema=False)
    return r
