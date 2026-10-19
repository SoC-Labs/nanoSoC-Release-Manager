"""The POSTs that start or change work. Run T0 (both forms) exists in every deployment; the rest only in a local
one (section 13a: local sign-in, or no CI project), so a GitHub + CI deployment keeps the two Run routes as its
only writes (D5). None of them accepts a result: each queues a job, runs a read, or clears an alert."""
from __future__ import annotations

import urllib.parse

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import auth as authmod
from nanosoc_rm import db, jobs
from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.web import views
from nanosoc_rm.web.page import Page, env
from nanosoc_rm.web.routers import Route
from nanosoc_rm.web.routes.common import ctx, job_fragment, ndjson, page, same, state_of
from nanosoc_rm.web.routes.pages import branches_view, hosts_view

__all__ = ["ROUTES"]


async def post_run(request: Request, id: str) -> Response:
    """POST /configurations/{id}/runs: the Run T0 button (htmx form with CSRF)."""
    return await _run(request, id, api=False)


async def post_run_api(request: Request, id: str) -> Response:
    """POST /api/v1/configurations/{id}/runs: the same action as the button."""
    return await _run(request, id, api=True)


async def _run(request: Request, cid: str, *, api: bool) -> Response:
    config, ident = ctx(request)
    state = state_of(request)
    if state.auth.requires_csrf:
        token = request.headers.get("x-csrf-token", "")
        if not token and "form" in request.headers.get("content-type", ""):
            token = str((await request.form()).get(authmod.CSRF_FIELD) or "")
        if not same(token, state.csrf_for(ident, config)):
            return PlainTextResponse("403 Forbidden: the form's CSRF token is missing or wrong", 403)
    if not views.is_valid_id(cid):
        return PlainTextResponse("400 Bad Request: not a configuration id", 400)
    if jobs.is_local_ci(config):                     # no CI project here, so Run runs compat locally
        return await _queue(request, "compat", {"configuration": cid}, api=api, active_for=cid)
    status = 202
    try:
        run = await run_in_threadpool(views.run_t0, config, cid, ident.login)
    except views.RunPending as e:
        run, status = e.run, 409
    except ConfigError as e:
        return PlainTextResponse("503 Service Unavailable: %s" % e, 503)
    except (LookupError, ValueError) as e:
        return PlainTextResponse("400 Bad Request: %s" % (str(e)[:300] or type(e).__name__), 400)
    except Exception as e:                           # noqa: BLE001 - the trigger failed: say so, never a bare 500
        return PlainTextResponse("500 Internal Server Error: the T0 trigger could not start the run (%s: %s)"
                                 % (type(e).__name__, str(e)[:300]), 500)
    if api:
        return ndjson([run], status)
    if request.headers.get("hx-request"):
        p = Page()
        return HTMLResponse(env.get_template("run_fragment.html").render(run=run, fact=p.fact, page=p, status=status),
                            status_code=status)
    return page(request, "run_started.html", "Run T0", status=status, run=run, pending_status=status)


def _duplicate(config: Config, kind: str, params: dict, run_button: bool) -> dict | None:
    """A queued or running job that already does what this request asks (Run, Walk now, Sync)."""
    for j in views.safe_jobs(config, states=("queued", "running"), kind=kind):
        jp = j["params"]
        if kind == "compat" and run_button and jp.get("configuration") == params["configuration"] \
                and not jp.get("pins") and not jp.get("tier"):
            return j
        if kind == "mirror-sync" and jp.get("component") == params.get("component"):
            return j
        if kind == "walk" and jp.get("configuration") == params.get("configuration"):
            return j
    return None


def _registered_ids(config: Config) -> set[str]:
    """The registry's configuration ids; empty when the registry cannot be read (the job's own error says why)."""
    try:
        return {d["fact"]["id"] for d in views.configurations(config)}
    except Exception:                                # noqa: BLE001
        return set()


async def _queue(request: Request, kind: str, params: dict, *, api: bool = False,
                 active_for: str | None = None) -> Response:
    config, ident = ctx(request)
    status = 202
    cid = params.get("configuration")
    if cid and views.is_valid_id(cid):
        known = await run_in_threadpool(_registered_ids, config)
        if known and cid not in known:               # a job the CLI would refuse: never queued, never logged
            return PlainTextResponse("404 Not Found: no registered configuration %r" % cid, 404)
    try:
        job = _duplicate(config, kind, params, run_button=active_for is not None)
        if job is not None:
            status = 409
        else:
            job = await run_in_threadpool(jobs.submit, config, kind, params, ident.login)
    except jobs.JobUsage as e:
        return PlainTextResponse("400 Bad Request: %s" % e, 400)
    if api:
        return JSONResponse({"job": {k: job[k] for k in ("id", "kind", "params", "state", "by", "created_at",
                                                         "command")}}, status)
    if request.headers.get("hx-request"):
        return job_fragment(request, job, status)
    target = {"compat": "/compat?job=%d", "mirror-sync": "/mirrors?job=%d"}.get(kind, "/compat?job=%d")
    if kind == "walk":
        target = "/configurations/%s?job=%%d" % urllib.parse.quote(params["configuration"], safe="")
    return RedirectResponse(target % job["id"], 303)


async def post_compat(request: Request) -> Response:
    form = await request.form()
    pins = [str(v) for v in form.getlist("pin") if str(v).strip()]
    pins += [ln.strip() for ln in str(form.get("pins_extra") or "").splitlines() if ln.strip()]
    params = {"configuration": str(form.get("configuration") or ""), "pins": pins,
              "tier": str(form.get("tier") or "") or None, "force_tier": bool(form.get("force_tier"))}
    return await _queue(request, "compat", params)


async def post_cancel(request: Request, id: str) -> Response:
    config, ident = ctx(request)
    job = jobs.get(config, n) if (n := jobs.parse_id(id)) is not None else None
    if job is None:
        return PlainTextResponse("404 Not Found: no such job", 404)
    job = await run_in_threadpool(jobs.cancel, config, job["id"], ident.login)
    if request.headers.get("hx-request"):
        return job_fragment(request, job)
    return RedirectResponse(("/mirrors?job=%d" if job["kind"] == "mirror-sync" else "/compat?job=%d") % job["id"], 303)


async def post_walk(request: Request, id: str) -> Response:
    return await _queue(request, "walk", {"configuration": id})


async def post_mirror_sync(request: Request) -> Response:
    form = await request.form()
    return await _queue(request, "mirror-sync", {"component": str(form.get("component") or "") or None})


async def post_clear_alert(request: Request) -> Response:
    config, ident = ctx(request)
    subject = str((await request.form()).get("subject") or "")
    try:
        await run_in_threadpool(db.clear_alert, subject, ident.login, config=config)
    except KeyError:
        return PlainTextResponse("404 Not Found: no such alert", 404)
    return RedirectResponse("/alerts", 303)


async def post_branches_refresh(request: Request) -> Response:
    """Re-read one component's mirror now, store the branch inventory, show it."""
    form = await request.form()
    comp, all_repos = str(form.get("component") or ""), bool(form.get("all"))
    if not views.is_valid_id(comp):
        return PlainTextResponse("400 Bad Request: not a component id", 400)
    return await run_in_threadpool(branches_view, request, comp, all_repos, "", True)


async def post_hosts_refresh(request: Request) -> Response:
    """Compare the host copies now (one component, or all), store and show them."""
    config, _ = ctx(request)
    comp = str((await request.form()).get("component") or "") or None
    if comp and not views.is_valid_id(comp):
        return PlainTextResponse("400 Bad Request: not a component id", 400)
    problems = await run_in_threadpool(views.compare_now, config, comp)
    return hosts_view(request, comp, problems)


ROUTES = (
    Route("POST", "/configurations/{id}/runs", post_run),
    Route("POST", "/api/v1/configurations/{id}/runs", post_run_api),
    *(Route("POST", path, fn, local_only=True) for path, fn in (
        ("/compat/jobs", post_compat), ("/jobs/{id}/cancel", post_cancel), ("/configurations/{id}/walk", post_walk),
        ("/mirrors/sync", post_mirror_sync), ("/alerts/clear", post_clear_alert),
        ("/branches/refresh", post_branches_refresh), ("/hosts/refresh", post_hosts_refresh))),
)
