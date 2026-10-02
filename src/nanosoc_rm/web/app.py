"""The web app: the section 13 pages and JSON API behind the section 14 sign-in (A3, A5, H1, H2).

`create_app(config_path=None, auth=None)` builds it. The config is read per request (`load_config`), so a
change of roles or registry takes effect without a restart.

Guard. One ASGI middleware sits in front of EVERY route, including a route someone appends to the router
after construction: only `/auth/*` and `/static/*` pass without a member. Anonymous: pages redirect to
`/auth/login`, `/api` and non-GET requests get 401. A refused session (not an active member, or no role)
gets 403 everywhere. Refusals carry no data and never echo the path.

Routes. The two Run T0 POSTs are the app's only non-GET routes; neither accepts a result (D5). FastAPI's own
docs and OpenAPI routes are switched off, so every route is one of the contract's.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from nanosoc_rm import auth as authmod
from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.web import registry_facts, views
from nanosoc_rm.web.page import env, render

__all__ = ["create_app", "STATIC_DIR", "STATIC_PREFIX", "NDJSON"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static"
NDJSON = "application/x-ndjson"
_OPEN_PREFIXES = (authmod.AUTH_PREFIX, STATIC_PREFIX)


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _safe_next(value: str | None) -> str:
    """A local path to return to after sign-in; anything else is "/" (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https"


class _State:
    def __init__(self, config_path: str | None, auth: Any):
        self.config_path = config_path
        self.auth = auth if auth is not None else authmod.GitHubAuth()
        self.ephemeral_secret = secrets.token_hex(32)     # CSRF key when no session secret is configured

    def config(self) -> Config:
        return load_config(self.config_path)

    def sessions(self, config: Config) -> authmod.Sessions | None:
        try:
            return authmod.Sessions.from_config(config)
        except ConfigError:
            return None

    def csrf_for(self, ident: authmod.Identity, config: Config) -> str:
        sessions = self.sessions(config) or authmod.Sessions(self.ephemeral_secret)
        return sessions.csrf(ident.login, ident.sid)


class Guard:
    """Section 14 for every request, before routing (so no route can be left open)."""

    def __init__(self, app: Callable, state: _State):
        self.app = app
        self.state = state

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(_is_under(path, p) for p in _OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            config = self.state.config()
        except ConfigError:
            await PlainTextResponse("503 Service Unavailable: the service config cannot be read", 503)(scope, receive, send)
            return
        ident = await run_in_threadpool(self.state.auth.identify, request.cookies, config, self.state.sessions(config))
        if ident is None:
            if _is_under(path, "/api"):
                resp: Response = JSONResponse({"error": "sign in with GitHub as a SoC-Labs member"}, 401)
            elif scope.get("method") in ("GET", "HEAD"):
                resp = RedirectResponse(authmod.LOGIN_PATH + "?" + urllib.parse.urlencode({"next": path}), 303)
            else:
                resp = PlainTextResponse("401 Unauthorized: sign in with GitHub as a SoC-Labs member", 401)
            await resp(scope, receive, send)
            return
        if ident.status != "member" or authmod.MEMBER_ROLE not in authmod.roles_for(config, ident.login):
            resp = PlainTextResponse("403 Forbidden: this service is open to active SoC-Labs members with a role "
                                     "in its configuration only", 403)
            if ident.refreshed:
                _set_session(resp, self.state.sessions(config), ident.refreshed, secure=request.url.scheme == "https")
            await resp(scope, receive, send)
            return
        scope.setdefault("state", {})
        scope["state"]["identity"] = ident
        scope["state"]["config"] = config
        if ident.refreshed:
            sessions = self.state.sessions(config)
            cookie = _session_header(sessions, ident.refreshed, secure=request.url.scheme == "https")

            async def send_with_cookie(message: dict) -> None:
                if message["type"] == "http.response.start" and cookie:
                    message.setdefault("headers", []).append((b"set-cookie", cookie.encode("latin-1")))
                await send(message)
            await self.app(scope, receive, send_with_cookie)
            return
        await self.app(scope, receive, send)


def _set_session(resp: Response, sessions: authmod.Sessions | None, d: dict, *, secure: bool) -> None:
    if sessions is None:
        return
    resp.set_cookie(authmod.SESSION_COOKIE, sessions.dump_session(d["login"], d["status"], d.get("sid"),
                                                                  d.get("checked_at")),
                    max_age=authmod.SESSION_MAX_AGE_S, httponly=True, samesite="lax", secure=secure, path="/")


def _session_header(sessions: authmod.Sessions | None, d: dict, *, secure: bool) -> str:
    probe = Response()
    _set_session(probe, sessions, d, secure=secure)
    return probe.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------------------------------------
# Helpers for routes
# ---------------------------------------------------------------------------------------------------------
def _ctx(request: Request) -> tuple[Config, authmod.Identity]:
    return request.state.config, request.state.identity


def _ndjson(docs: list[dict], status: int = 200) -> Response:
    body = "".join(json.dumps(d, separators=(",", ":"), sort_keys=True) + "\n" for d in docs)
    return Response(body, status_code=status, media_type=NDJSON)


def create_app(config_path: str | os.PathLike | None = None, auth: Any = None) -> FastAPI:
    """The web app (interfaces.md sections 13, 14). `config_path` None means `$NANOSOC_RM_CONFIG`, read per
    request. `auth`: the authenticator; None is GitHub sign-in, `FakeMembership(login=)` the test seam."""
    state = _State(None if config_path is None else os.fspath(config_path), auth)
    app = FastAPI(title="nanoSoC Release Manager", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rm = state
    app.add_middleware(Guard, state=state)
    app.mount(STATIC_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static")

    def page(request: Request, template: str, title: str, status: int = 200, problems: list[str] | None = None,
             **ctx: Any) -> HTMLResponse:
        config, ident = _ctx(request)
        html = render(template, title=title, identity=ident, problems=problems, csrf=state.csrf_for(ident, config),
                      **ctx)
        return HTMLResponse(html, status_code=status)

    def not_found(request: Request, what: str) -> HTMLResponse:
        return page(request, "message.html", "Not found", status=404, message="No %s is known here." % what)

    def registry_problems(config: Config) -> list[str]:
        return registry_facts.refresh(config)

    # ----- pages ---------------------------------------------------------------------------------------
    def dashboard(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        return page(request, "dashboard.html", "Dashboard", problems=problems, d=views.dashboard(config))

    def configuration(request: Request, id: str) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        c = views.configuration_page(config, id) if views.is_valid_id(id) else None
        if c is None:
            return not_found(request, "configuration")
        return page(request, "configuration.html", "Configuration %s" % id, problems=problems, c=c)

    def component(request: Request, id: str) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        c = views.component_page(config, id) if views.is_valid_id(id) else None
        if c is None:
            return not_found(request, "component")
        return page(request, "component.html", "Component %s" % id, problems=problems, c=c)

    def contains_page(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        sha = request.query_params.get("sha") or ""
        comp = request.query_params.get("component") or None
        rows, problems, computed = ([], [], False) if not sha else views.containment(config, sha, comp)
        return page(request, "contains.html", "Contains commit", problems=problems, sha=sha, component=comp,
                    rows=rows, computed=computed)

    def silicon(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        rows = views.silicon_protection(config, request.query_params.get("configuration") or None)
        return page(request, "silicon.html", "Silicon protection", rows=rows)

    def defects_page(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        return page(request, "defects.html", "Defects", problems=problems, defects=views.defects(config),
                    errata=views.errata(config))

    def defect_page(request: Request, id: str) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        d = next((x for x in views.defects(config) if x["fact"]["id"] == id), None)
        if d is None:
            return not_found(request, "defect")
        er = [e for e in views.errata(config) if e["fact"]["record"].get("defect") == id]
        return page(request, "defect.html", id, problems=problems, d=d, errata=er)

    def errata_page(request: Request, configuration: str) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        rec = views.configuration_record(config, configuration)
        er = views.errata(config, configuration)
        if rec is None and not er:
            return not_found(request, "configuration")
        return page(request, "errata.html", "Errata of %s" % configuration, problems=problems,
                    configuration=configuration, record=rec, errata=er)

    def run_page(request: Request, nonce: str) -> HTMLResponse:
        config, _ = _ctx(request)
        run = views.run_by_nonce(config, nonce)
        if run is None:
            return not_found(request, "run")
        return page(request, "run.html", "T0 run %s" % nonce[:12], run=run)

    def facts_page(request: Request, subject: str) -> HTMLResponse:
        config, _ = _ctx(request)
        obs, current = views.fact_by_subject(config, subject)
        if obs is None:
            return not_found(request, "fact")
        return page(request, "fact.html", "Fact", obs=obs, current=current)

    async def post_run(request: Request, id: str) -> Response:
        """Section 13 POST /configurations/{id}/runs: the Run T0 button (htmx form with CSRF)."""
        return await _run(request, id, api=False)

    async def post_run_api(request: Request, id: str) -> Response:
        """Section 13 POST /api/v1/configurations/{id}/runs: the same action as the button."""
        return await _run(request, id, api=True)

    async def _run(request: Request, cid: str, *, api: bool) -> Response:
        config, ident = _ctx(request)
        if state.auth.requires_csrf:
            token = request.headers.get("x-csrf-token", "")
            if not token and "form" in request.headers.get("content-type", ""):
                token = str((await request.form()).get(authmod.CSRF_FIELD) or "")
            if not token or not hmac.compare_digest(token, state.csrf_for(ident, config)):
                return PlainTextResponse("403 Forbidden: the form's CSRF token is missing or wrong", 403)
        if not views.is_valid_id(cid):
            return PlainTextResponse("400 Bad Request: not a configuration id", 400)
        status = 202
        try:
            run = await run_in_threadpool(views.run_t0, config, cid, ident.login)
        except views.RunPending as e:
            run, status = e.run, 409
        except views.TriggerUnavailable as e:
            return PlainTextResponse("503 Service Unavailable: %s" % e, 503)
        except ConfigError as e:
            return PlainTextResponse("503 Service Unavailable: %s" % e, 503)
        except (LookupError, ValueError) as e:
            return PlainTextResponse("400 Bad Request: %s" % (str(e)[:300] or type(e).__name__), 400)
        except Exception as e:                       # noqa: BLE001 - the trigger failed: say so, never a bare 500
            return PlainTextResponse("500 Internal Server Error: the T0 trigger could not start the run (%s: %s)"
                                     % (type(e).__name__, str(e)[:300]), 500)
        if api:
            return _ndjson([run], status)
        if request.headers.get("hx-request"):
            frag = env.get_template("run_fragment.html")
            from nanosoc_rm.web.page import Page
            p = Page()
            return HTMLResponse(frag.render(run=run, fact=p.fact, page=p, status=status), status_code=status)
        return page(request, "run_started.html", "Run T0", status=status, run=run, pending_status=status)

    # ----- JSON API (section 13): every line is an observation -----------------------------------------
    def api_configurations(request: Request) -> Response:
        config, _ = _ctx(request)
        registry_problems(config)
        return _ndjson(views.configurations(config))

    def api_pins(request: Request, id: str) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.pins(config, id))

    def api_hosts(request: Request, id: str) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.host_compares(config, id))

    def api_contains(request: Request, sha: str) -> Response:
        config, _ = _ctx(request)
        rows, problems, _ = views.containment(config, sha, request.query_params.get("component") or None)
        if problems and not rows:
            return JSONResponse({"error": problems[0]}, 422 if "not a" in problems[0] else 503)
        return _ndjson(rows)

    def api_silicon(request: Request) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.silicon_protection(config, request.query_params.get("configuration") or None))

    def api_runs(request: Request) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.runs(config, request.query_params.get("configuration") or None))

    def api_errata(request: Request, configuration: str) -> Response:
        config, _ = _ctx(request)
        registry_problems(config)
        return _ndjson(views.errata(config, configuration))

    def api_mirrors(request: Request) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.mirrors(config))

    def api_inventory(request: Request) -> Response:
        config, _ = _ctx(request)
        flag = (request.query_params.get("unclassified") or "").lower() in ("1", "true", "yes")
        return _ndjson(views.inventory(config, unclassified=flag))

    def api_runner(request: Request) -> Response:
        config, _ = _ctx(request)
        return _ndjson(views.runner_settings(config))

    def api_alerts(request: Request) -> Response:
        config, _ = _ctx(request)
        return _ndjson(db.alerts(open_only=True, config=config))

    # ----- auth (section 14) -------------------------------------------------------------------------------
    def login(request: Request) -> Response:
        try:
            config = state.config()
            sessions = state.sessions(config)
            if sessions is None:
                return PlainTextResponse("503 Service Unavailable: sign-in is not configured "
                                         "([session] secret_file)", 503)
            st = secrets.token_urlsafe(24)
            url = authmod.authorize_url(config, st)
        except ConfigError:
            return PlainTextResponse("503 Service Unavailable: sign-in is not configured", 503)
        resp = RedirectResponse(url, 302)
        resp.set_cookie(authmod.STATE_COOKIE, sessions.dump_state(st, _safe_next(request.query_params.get("next"))),
                        max_age=authmod.STATE_MAX_AGE_S, httponly=True, samesite="lax",
                        secure=_cookie_secure(request), path=authmod.AUTH_PREFIX)
        return resp

    def callback(request: Request) -> Response:
        try:
            config = state.config()
            sessions = state.sessions(config)
        except ConfigError:
            sessions = None
        if sessions is None:
            return PlainTextResponse("503 Service Unavailable: sign-in is not configured", 503)
        saved = sessions.load_state(request.cookies.get(authmod.STATE_COOKIE, ""))
        given = request.query_params.get("state") or ""
        if saved is None or not given or not hmac.compare_digest(given, saved["state"]):
            return PlainTextResponse("403 Forbidden: the sign-in state does not match; start again at "
                                     + authmod.LOGIN_PATH, 403)
        code = request.query_params.get("code")
        if not code:
            return PlainTextResponse("400 Bad Request: GitHub returned no code", 400)
        try:
            token = authmod.exchange_code(config, code)
            try:
                who = authmod.user_login(config, token)
                membership = authmod.membership_state(config, token)
            finally:
                token = None                       # section 14: the user's token is kept nowhere
        except (authmod.AuthError, ConfigError) as e:
            return PlainTextResponse("502 Bad Gateway: GitHub sign-in failed: %s" % e, 502)
        member = membership == "active" and authmod.MEMBER_ROLE in authmod.roles_for(config, who)
        if member:
            resp: Response = RedirectResponse(_safe_next(saved.get("next")), 303)
        else:
            resp = PlainTextResponse("403 Forbidden: signed in to GitHub as %s, which is not an active SoC-Labs "
                                     "member with a role in this service" % who, 403)
        _set_session(resp, sessions, {"login": who, "status": "member" if member else "refused",
                                      "checked_at": clock.iso()}, secure=_cookie_secure(request))
        resp.delete_cookie(authmod.STATE_COOKIE, path=authmod.AUTH_PREFIX)
        return resp

    def logout(request: Request) -> Response:
        resp = PlainTextResponse("Signed out.", 200)
        resp.delete_cookie(authmod.SESSION_COOKIE, path="/")
        return resp

    # Registration. api_route with explicit methods: no route is registered through a `.post(...)` call.
    get = ["GET"]
    for path, fn in (("/", dashboard), ("/configurations/{id}", configuration), ("/components/{id}", component),
                     ("/contains", contains_page), ("/silicon-protection", silicon), ("/defects", defects_page),
                     ("/defects/{id}", defect_page), ("/errata/{configuration}", errata_page),
                     ("/runs/{nonce}", run_page), ("/facts/{subject:path}", facts_page),
                     ("/api/v1/configurations", api_configurations), ("/api/v1/configurations/{id}/pins", api_pins),
                     ("/api/v1/components/{id}/hosts", api_hosts), ("/api/v1/contains/{sha}", api_contains),
                     ("/api/v1/silicon-protection", api_silicon), ("/api/v1/runs", api_runs),
                     ("/api/v1/errata/{configuration}", api_errata), ("/api/v1/mirrors", api_mirrors),
                     ("/api/v1/inventory", api_inventory), ("/api/v1/runner", api_runner),
                     ("/api/v1/alerts", api_alerts),
                     (authmod.LOGIN_PATH, login), (authmod.CALLBACK_PATH, callback), (authmod.LOGOUT_PATH, logout)):
        app.add_api_route(path, fn, methods=get, include_in_schema=False)
    app.add_api_route("/configurations/{id}/runs", post_run, methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/v1/configurations/{id}/runs", post_run_api, methods=["POST"], include_in_schema=False)
    return app
