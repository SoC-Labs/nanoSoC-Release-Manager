"""The Store pages (interfaces.md section 22), mounted at /store through the plug-in seam (web/plugins.py).

    GET  /store/                 the stored index: probes, sections, counts, streams, builds, manifests, foundry
                                 returns, documents; `?q=` filters by artefact path, name, run tag, commit or md5
    GET  /store/file/{sha1}      a document's verified cached copy (only one the shown index lists)
    POST /store/index            queue one `store-index` job (local deployment only; CSRF-checked by the guard)

Every GET reads the stored store_index fact and the job records: none calls the store, and none queues work. A
download is served from `<data_dir>/store-cache/`, which the index job filled after verifying each file's sha1
against the store; the Bearer token is never in a page, a link or a response.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import jobs
from nanosoc_rm.web.plugins import config_of, is_local, job_card, renderer, template_env

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "ENV"]

PREFIX = "/store"
NAV_LABEL = "Store"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
ROLE_ORDER = ("stream", "manifest", "signature", "source", "doc")


ENV = template_env(HERE, app_globals=True)
_page = renderer(ENV, local=True)


def _recent(config: Any, limit: int = 5) -> list[dict]:
    try:
        return jobs.jobs(config, limit=limit, kind="store-index")
    except Exception:                                             # noqa: BLE001 - no job store yet
        return []


def router(app: Any = None) -> APIRouter:
    """GET pages everywhere; the one POST only in a local deployment (section 13a)."""
    from nanosoc_rm.store import views
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index_page(request: Request) -> HTMLResponse:
        config = config_of(request)
        doc, new = views.shown(config)
        q = (request.query_params.get("q") or "").strip()[:200]
        jq = request.query_params.get("job") or ""
        job = jobs.get(config, int(jq)) if jq.isdigit() else None
        configured = bool((config.get("stores") or {}).get("artifactory"))
        hits = views.search(config, q, limit=500) if q else []
        fact = doc["fact"] if doc else None
        rows = {role: [{"a": a, "i": views._identity(config, a)} for a in (fact or {}).get("artefacts") or []
                       if a["role"] == role] for role in ROLE_ORDER}
        for role in rows:
            rows[role].sort(key=lambda r: r["a"].get("created") or "", reverse=True)
        return _page(request, "store_index.html", "Store", doc=doc, new=new, q=q, hits=hits, rows=rows,
                     coll=views.collateral(config)["rows"], configured=configured,
                     job_html=job_card(ENV, request, config, job), recent=_recent(config), NO_IDENTITY=views.NO_IDENTITY)

    def file_view(request: Request, sha1: str) -> Response:
        config = config_of(request)
        got = views.cached_file(config, sha1)
        if got is None:
            return PlainTextResponse("404 Not Found: no verified copy of that document is held by the tool", 404)
        path, a = got
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in a["name"])[:120] or "document"
        return FileResponse(str(path), media_type="application/octet-stream", filename=name,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    async def post_index(request: Request) -> Response:
        if not is_local(request):
            return PlainTextResponse("404 Not Found: this deployment runs no local jobs; use the CLI", 404)
        config = config_of(request)
        if not (config.get("stores") or {}).get("artifactory"):
            return PlainTextResponse("400 Bad Request: no [stores.artifactory] in the service config", 400)
        for j in _recent(config, limit=50):
            if j["state"] in ("queued", "running"):
                return RedirectResponse("/store/?job=%d" % j["id"], 303)
        ident = getattr(request.state, "identity", None)
        try:
            job = await run_in_threadpool(jobs.submit, config, "store-index", {}, getattr(ident, "login", "") or "")
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s" % e, 400)
        return RedirectResponse("/store/?job=%d" % job["id"], 303)

    r.add_api_route("/", index_page, methods=["GET"], include_in_schema=False)
    r.add_api_route("/file/{sha1}", file_view, methods=["GET"], include_in_schema=False)
    if local:
        r.add_api_route("/index", post_index, methods=["POST"], include_in_schema=False)
    return r
