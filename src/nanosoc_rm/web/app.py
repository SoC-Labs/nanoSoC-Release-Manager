"""The web app: the section 13 pages and JSON API behind the section 14 sign-in (A3, A5, H1, H2).

`create_app(config_path=None, auth=None)` builds it. The config is read per request (`load_config`), so a
change of roles or registry takes effect without a restart.

Guard. One ASGI middleware sits in front of EVERY route, including a route someone appends to the router
after construction, and a websocket route too: only `/auth/*` and `/static/*` pass without a member (http),
and a websocket without a member is closed with 1008. Only the ASGI lifespan scope passes unchecked. Anonymous: pages redirect to
`/auth/login`, `/api` and non-GET requests get 401. A refused session (not an active member, or no role)
gets 403 everywhere. Refusals carry no data and never echo the path.

CSRF. Every request other than GET and HEAD outside `/auth/*` must carry the session's CSRF token (form field
`csrf_token` or header `X-CSRF-Token`; htmx sends the header from the page's `hx-headers`). The guard checks it
before routing, so a POST route added by a plug-in is covered too.

Routes. Section 13's pages and JSON API; the Run T0 POSTs, which accept no result (D5). The local GUI (CR-16,
CR-17; interfaces.md section 13a) adds the pages Compat, Branches, Hosts, Mirrors, Alerts, Components and Errata,
and POSTs that queue local jobs (compat, mirror sync, walk), cancel one, or clear an alert; none of them accepts
a result either. With no `[ci] project` configured, Run T0 queues a local compat job instead of triggering CI.
FastAPI's own docs and OpenAPI routes are switched off. Plug-ins (web/plugins.py) mount extra routers.
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
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketClose

from nanosoc_rm import auth as authmod
from nanosoc_rm import clock, db, jobs
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.web import plugins, registry_facts, routers, shell, views
from nanosoc_rm.web.page import NAV, Page, env, render

__all__ = ["create_app", "STATIC_DIR", "STATIC_PREFIX", "NDJSON"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static"
NDJSON = "application/x-ndjson"
_OPEN_PREFIXES = (authmod.AUTH_PREFIX, STATIC_PREFIX)
_SAFE_METHODS = ("GET", "HEAD")
_MAX_FORM_BYTES = 1024 * 1024


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _safe_next(value: str | None) -> str:
    """A local path to return to after sign-in; anything else is "/" (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _cookie_secure(request: Request, state: "_State") -> bool:
    """Cookies are `Secure` when this process serves TLS (CR-20, `serve --tls-cert`) or the request is https."""
    return state.tls or request.url.scheme == "https"


class _State:
    def __init__(self, config_path: str | None, auth: Any):
        self.config_path = config_path
        self.auth = auth if auth is not None else authmod.ConfiguredAuth()
        self.ephemeral_secret = secrets.token_hex(32)     # CSRF key when no session secret is configured
        self.nav: tuple = NAV
        self.page: Callable[..., HTMLResponse] | None = None
        self.throttle = authmod.Throttle()
        self.local_routes = False                         # CR-16/CR-17 POST routes registered (create_app)
        self.tls = False                                  # CR-20: serve terminates TLS itself (create_app)

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
        kind = scope["type"]
        if kind == "lifespan":                     # startup/shutdown: no request, nothing served
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await self._websocket(scope, receive, send)
            return
        if kind != "http":                         # an ASGI scope this app does not know: served to nobody
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
            who = "sign in" if config.auth_mode == "local" else "sign in with GitHub as a SoC-Labs member"
            if _is_under(path, "/api"):
                resp: Response = JSONResponse({"error": who}, 401)
            elif scope.get("method") in _SAFE_METHODS:
                resp = RedirectResponse(authmod.LOGIN_PATH + "?" + urllib.parse.urlencode({"next": path}), 303)
            else:
                resp = PlainTextResponse("401 Unauthorized: %s" % who, 401)
            await resp(scope, receive, send)
            return
        if ident.status != "member" or authmod.MEMBER_ROLE not in authmod.roles_for(config, ident.login):
            resp = PlainTextResponse("403 Forbidden: this service is open to active SoC-Labs members with a role "
                                     "in its configuration only", 403)
            if ident.refreshed:
                _set_session(resp, self.state.sessions(config), ident.refreshed,
                             secure=_cookie_secure(request, self.state))
            await resp(scope, receive, send)
            return
        scope.setdefault("state", {})
        scope["state"]["identity"] = ident
        scope["state"]["config"] = config
        if scope.get("method") not in _SAFE_METHODS and getattr(self.state.auth, "requires_csrf", True):
            body = await _read_body(receive)
            if body is None:
                await PlainTextResponse("413 Payload Too Large", 413)(scope, receive, send)
                return
            token = await _csrf_token(scope, body)
            want = self.state.csrf_for(ident, config)
            if not token or not hmac.compare_digest(token, want):
                await PlainTextResponse("403 Forbidden: the form's CSRF token is missing or wrong; reload the page "
                                        "and try again", 403)(scope, receive, send)
                return
            receive = _replay(body, receive)
        if ident.refreshed:
            sessions = self.state.sessions(config)
            cookie = _session_header(sessions, ident.refreshed, secure=_cookie_secure(request, self.state))

            async def send_with_cookie(message: dict) -> None:
                if message["type"] == "http.response.start" and cookie:
                    message.setdefault("headers", []).append((b"set-cookie", cookie.encode("latin-1")))
                await send(message)
            await self.app(scope, receive, send_with_cookie)
            return
        await self.app(scope, receive, send)


    async def _websocket(self, scope: dict, receive: Callable, send: Callable) -> None:
        """The same rule for a websocket (none is routed today; one added later is guarded): a member with a
        role connects; anyone else is closed with 1008 (policy violation) before the handshake completes."""
        try:
            config = self.state.config()
            ident = await run_in_threadpool(self.state.auth.identify, HTTPConnection(scope).cookies, config,
                                            self.state.sessions(config))
        except ConfigError:
            ident, config = None, None
        if ident is None or ident.status != "member" or \
                authmod.MEMBER_ROLE not in authmod.roles_for(config, ident.login):
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        scope.setdefault("state", {})
        scope["state"]["identity"] = ident
        scope["state"]["config"] = config
        await self.app(scope, receive, send)


async def _read_body(receive: Callable) -> bytes | None:
    """The whole request body (None when it is larger than _MAX_FORM_BYTES)."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _MAX_FORM_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Callable) -> Callable:
    """A receive() that hands the app the body the guard already read, then the client's own messages."""
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def replay() -> dict:
        return pending.pop(0) if pending else await receive()
    return replay


async def _csrf_token(scope: dict, body: bytes) -> str:
    """X-CSRF-Token, else the form field `csrf_token` (urlencoded or multipart)."""
    req = Request(scope, receive=_replay(body, _no_more))      # starlette's Request; no outbound call
    token = req.headers.get("x-csrf-token", "")
    if token:
        return token
    ctype = req.headers.get("content-type", "")
    if "form" not in ctype:
        return ""
    try:
        form = await req.form()
    except Exception:                                # noqa: BLE001 - a malformed form has no token
        return ""
    return str(form.get(authmod.CSRF_FIELD) or "")


async def _no_more() -> dict:
    return {"type": "http.disconnect"}


def _set_session(resp: Response, sessions: authmod.Sessions | None, d: dict, *, secure: bool,
                 same_site: str = "lax") -> None:
    """Write session `d`. Its `iat` (sign-in time) is kept, so the cookie lives only as long as the session
    has left: re-signing after a re-check never extends it (section 14: valid 8 h)."""
    if sessions is None:
        return
    iat = d.get("iat") or clock.iso()
    left = authmod.session_seconds_left({"iat": iat})
    resp.set_cookie(authmod.SESSION_COOKIE, sessions.dump_session(d["login"], d["status"], d.get("sid"),
                                                                  d.get("checked_at"), iat, d.get("attempt_at"),
                                                                  d.get("hash_tag")),
                    max_age=max(1, left if left is not None else 0), httponly=True, samesite=same_site, secure=secure,
                    path="/")


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


def create_app(config_path: str | os.PathLike | None = None, auth: Any = None, *, tls: bool = False) -> FastAPI:
    """The web app (interfaces.md sections 13, 14). `config_path` None means `$NANOSOC_RM_CONFIG`, read per
    request. `auth`: the authenticator; None is GitHub sign-in, `FakeMembership(login=)` the test seam. `tls`
    (CR-20): this process serves HTTPS itself, so every cookie it sets is `Secure`."""
    state = _State(None if config_path is None else os.fspath(config_path), auth)
    state.tls = tls
    # CR-16/CR-17 POST routes exist only in a deployment that needs them (local sign-in, or no CI project), so a
    # GitHub + CI deployment keeps section 13's two Run routes as its only writes (D5). Decided when the app is
    # built: changing [auth] mode or [ci] project needs a restart.
    try:
        _built = state.config()
        state.local_routes = _built.auth_mode == "local" or jobs.is_local_ci(_built)
    except ConfigError:
        state.local_routes = False
    app = FastAPI(title="nanoSoC Release Manager", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rm = state
    app.add_middleware(Guard, state=state)
    app.mount(STATIC_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static")

    def page(request: Request, template: str, title: str, status: int = 200, problems: list[str] | None = None,
             **ctx: Any) -> HTMLResponse:
        config, ident = _ctx(request)
        path = request.url.path
        problems = list(problems or [])
        try:
            rail = shell.rail(config, ident, path, state.nav)
        except Exception as e:                       # noqa: BLE001 - the rail never takes a page down
            rail = None
            problems.append("the navigation rail could not be read: %s" % str(e)[:200])
        if ctx.get("crumbs") is None:
            ctx["crumbs"] = _default_crumbs(path, title)
        html = render(template, title=title, identity=ident, problems=problems, csrf=state.csrf_for(ident, config),
                      nav=state.nav, path=path, auth_mode=config.auth_mode, rail=rail,
                      local_ci=jobs.is_local_ci(config), actions=state.local_routes, **ctx)
        return HTMLResponse(html, status_code=status)
    state.page = page

    def _default_crumbs(path: str, title: str) -> list[tuple[str, str | None]]:
        """Spec 1.5: Inbox, then the navigation entry the page sits under (a plug-in's or a Tools page's), then
        the page. A workspace handler passes its own."""
        if path == "/":
            return [("Inbox", None)]
        entry = next((n for n in state.nav if n[1] != "/" and any(
            path == p or path.startswith(p.rstrip("/") + "/") for p in n[2])), None)
        trail: list[tuple[str, str | None]] = [("Inbox", "/")]
        if entry is not None and entry[1].rstrip("/") != path.rstrip("/"):
            trail.append((entry[0], entry[1]))
        return shell.crumbs(trail + [(title, None)])

    def delegate(name: str, request: Request, *args: Any) -> Response | None:
        """Wave C core-router seam (routers.DELEGATES): a workspace module that is built here answers the page;
        None means it did not (not built, or it declines), and the older page is served."""
        target = routers.DELEGATES.get(name)
        mod = routers.optional(target[0]) if target else None
        fn = getattr(mod, target[1], None) if mod is not None else None
        return fn(request, *args) if fn is not None else None

    def not_found(request: Request, what: str) -> HTMLResponse:
        return page(request, "message.html", "Not found", status=404, message="No %s is known here." % what)

    def registry_problems(config: Config) -> list[str]:
        return registry_facts.refresh(config)

    # ----- pages ---------------------------------------------------------------------------------------
    def dashboard(request: Request) -> Response:
        answer = delegate("dashboard", request)
        if answer is not None:
            return answer
        config, _ = _ctx(request)
        problems = registry_problems(config)
        return page(request, "dashboard.html", "Dashboard", problems=problems, d=views.dashboard(config))

    def configuration(request: Request, id: str) -> Response:
        answer = delegate("configuration", request, id)
        if answer is not None:
            return answer
        config, _ = _ctx(request)
        problems = registry_problems(config)
        c = views.configuration_page(config, id) if views.is_valid_id(id) else None
        if c is None:
            return not_found(request, "configuration")
        q = request.query_params
        job = jobs.get(config, n) if (n := jobs.parse_id(q.get("job"))) is not None else None
        return page(request, "configuration.html", "Configuration %s" % id, problems=problems, c=c,
                    job_html=job_fragment(request, job).body.decode() if job else "")

    def component(request: Request, id: str) -> Response:
        answer = delegate("component", request, id)
        if answer is not None:
            return answer
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
        if jobs.is_local_ci(config):                 # CR-17: no CI project here, so Run runs compat locally
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

    # ----- local GUI (CR-16, CR-17; interfaces.md section 13a) -------------------------------------------
    def job_fragment(request: Request, job: dict, status: int = 200) -> HTMLResponse:
        """One job's status, log tail and (when done) result: the htmx polling target."""
        config, ident = _ctx(request)
        p = Page()
        html = env.get_template("_job.html").render(
            job=job, log=jobs.log_text(config, job["id"]), position=jobs.queue_position(config, job["id"]),
            csrf=state.csrf_for(ident, config), fact=p.fact, page=p, stored=_stored_result(config, job))
        return HTMLResponse(html, status_code=status)

    def _stored_result(config: Config, job: dict) -> dict | None:
        """The stored compat_result of a finished compat job (None for a what-if: never stored)."""
        doc = job.get("result") if job["kind"] == "compat" else None
        if not doc or job["params"].get("pins"):
            return None
        hist = db.history(doc["subject"], config=config)
        return hist[-1] if hist else None

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
        """The registry's configuration ids; empty when the registry cannot be read (the CLI then says why)."""
        try:
            return {d["fact"]["id"] for d in views.configurations(config)}
        except Exception:                                         # noqa: BLE001 - the job's own error explains
            return set()

    async def _queue(request: Request, kind: str, params: dict, *, api: bool = False,
                     active_for: str | None = None) -> Response:
        config, ident = _ctx(request)
        status = 202
        cid = params.get("configuration")
        if cid and views.is_valid_id(cid):
            known = await run_in_threadpool(_registered_ids, config)
            if known and cid not in known:           # a job the CLI would refuse: never queued, never logged
                return PlainTextResponse("404 Not Found: no registered configuration %r" % cid, 404)
        try:
            job = _duplicate(config, kind, params, run_button=active_for is not None)
            if job is not None:
                status = 409                          # the same job is already queued or running
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

    def compat_page(request: Request) -> Response:
        answer = delegate("compat", request)
        if answer is not None:
            return answer
        config, _ = _ctx(request)
        problems = registry_problems(config)
        q = request.query_params
        job = None
        if (n := jobs.parse_id(q.get("job"))) is not None:
            job = jobs.get(config, n)
            if job is None:
                return not_found(request, "job")
        cid = q.get("configuration") or (job["params"].get("configuration") if job else None)
        configs = views.configurations(config)
        if cid and not views.is_valid_id(cid):
            cid = None
        pins = views.pins(config, cid) if cid else []
        refs: dict[str, list[dict]] = {}
        for p in pins:
            comp = (p["fact"].get("child") or {}).get("component")
            if comp and comp not in refs:
                refs[comp] = views.component_refs(config, comp)
        tiers = {}
        if cid:
            try:
                from nanosoc_rm.ci import configs as cimod
                tiers = cimod.tiers(cid, config)
            except Exception as e:                   # noqa: BLE001 - the form still works without tiers
                problems = problems + ["tiers.yaml could not be read for %s: %s" % (cid, str(e)[:200])]
        return page(request, "compat.html", "Compatibility check", problems=problems, configs=configs, cid=cid,
                    pins=pins, refs=refs, tiers=tiers, job=job,
                    job_html=job_fragment(request, job).body.decode() if job else "",
                    recent=views.safe_jobs(config, limit=25, kind="compat"),
                    results=views.compat_results(config, cid)[:10] if cid else views.compat_results(config))

    async def post_compat(request: Request) -> Response:
        form = await request.form()
        pins = [str(v) for v in form.getlist("pin") if str(v).strip()]
        pins += [ln.strip() for ln in str(form.get("pins_extra") or "").splitlines() if ln.strip()]
        params = {"configuration": str(form.get("configuration") or ""), "pins": pins,
                  "tier": str(form.get("tier") or "") or None, "force_tier": bool(form.get("force_tier"))}
        return await _queue(request, "compat", params)

    def job_fragment_route(request: Request, id: str) -> Response:
        config, _ = _ctx(request)
        job = jobs.get(config, n) if (n := jobs.parse_id(id)) is not None else None
        if job is None:
            return PlainTextResponse("404 Not Found: no such job", 404)
        return job_fragment(request, job)

    def job_log(request: Request, id: str) -> Response:
        config, _ = _ctx(request)
        job = jobs.get(config, n) if (n := jobs.parse_id(id)) is not None else None
        if job is None:
            return PlainTextResponse("404 Not Found: no such job", 404)
        return PlainTextResponse(jobs.log_text(config, job["id"]) or "(no output yet)")

    async def post_cancel(request: Request, id: str) -> Response:
        config, ident = _ctx(request)
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

    def branches_page(request: Request) -> Response:
        answer = delegate("branches", request)
        if answer is not None:
            return answer
        q = request.query_params
        return _branches(request, q.get("component") or "", q.get("all") in ("1", "on", "true"), q.get("verdict") or "")

    def _branches(request: Request, comp: str, all_repos: bool, verdict: str, refresh: bool = False) -> HTMLResponse:
        # A GET never re-reads on request (section 13a): Re-read mirror is the local POST /branches/refresh.
        config, _ = _ctx(request)
        problems = registry_problems(config)
        rows, more, computed = (views.branch_rows(config, comp, all_repos=all_repos, refresh=refresh)
                                if comp else ([], [], False))
        counts: dict[str, int] = {}
        for d in rows:
            counts[d["fact"]["verdict"]] = counts.get(d["fact"]["verdict"], 0) + 1
        shown = [d for d in rows if not verdict or d["fact"]["verdict"] == verdict
                 or (verdict == "deletable" and d["fact"]["deletable"])]
        mirrored = {d["fact"].get("component") for d in views.mirrors(config)}
        comps = [c for c in views.components(config) if c["fact"]["id"] in mirrored] if not comp else []
        return page(request, "branches.html", "Branches", problems=problems + more, component=comp,
                    all_repos=all_repos, rows=shown, total=len(rows), counts=counts, verdict=verdict,
                    computed=computed, delete_text=views.delete_text(rows) if rows else "", comps=comps,
                    choices=sorted(mirrored - {None}))

    def hosts_page(request: Request) -> HTMLResponse:
        return _hosts(request, request.query_params.get("component") or None)

    def _hosts(request: Request, comp: str | None, problems: list[str] | None = None) -> HTMLResponse:
        config, _ = _ctx(request)
        if comp and not views.is_valid_id(comp):
            comp = None
        rows = views.host_compares(config, comp)
        return page(request, "hosts.html", "Host copies", problems=registry_problems(config) + (problems or []),
                    rows=rows, component=comp)

    async def post_branches_refresh(request: Request) -> Response:
        """Local deployment: re-read one component's mirror now, store the branch inventory, show it."""
        form = await request.form()
        comp, all_repos = str(form.get("component") or ""), bool(form.get("all"))
        if not views.is_valid_id(comp):
            return PlainTextResponse("400 Bad Request: not a component id", 400)
        return await run_in_threadpool(_branches, request, comp, all_repos, "", True)

    async def post_hosts_refresh(request: Request) -> Response:
        """Local deployment: compare the host copies now (one component, or all), store and show them."""
        config, _ = _ctx(request)
        comp = str((await request.form()).get("component") or "") or None
        if comp and not views.is_valid_id(comp):
            return PlainTextResponse("400 Bad Request: not a component id", 400)
        problems = await run_in_threadpool(views.compare_now, config, comp)
        return _hosts(request, comp, problems)

    def mirrors_page(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        q = request.query_params
        job = jobs.get(config, n) if (n := jobs.parse_id(q.get("job"))) is not None else None
        rows = views.mirrors(config)
        return page(request, "mirrors.html", "Mirrors", rows=rows, summary=views.mirror_summary(rows),
                    job=job, job_html=job_fragment(request, job).body.decode() if job else "",
                    recent=views.safe_jobs(config, limit=10, kind="mirror-sync"))

    def alerts_page(request: Request) -> Response:
        answer = delegate("alerts", request)
        if answer is not None:
            return answer
        config, _ = _ctx(request)
        open_, cleared = views.all_alerts(config)
        return page(request, "alerts.html", "Alerts", open_alerts=open_, cleared=cleared[:50])

    async def post_clear_alert(request: Request) -> Response:
        config, ident = _ctx(request)
        subject = str((await request.form()).get("subject") or "")
        try:
            await run_in_threadpool(db.clear_alert, subject, ident.login, config=config)
        except KeyError:
            return PlainTextResponse("404 Not Found: no such alert", 404)
        return RedirectResponse("/alerts", 303)

    def components_page(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        mirrored = {d["fact"].get("component") for d in views.mirrors(config)}
        return page(request, "components.html", "Components", problems=problems,
                    components=views.components(config), mirrored=mirrored)

    def errata_index(request: Request) -> HTMLResponse:
        config, _ = _ctx(request)
        problems = registry_problems(config)
        return page(request, "errata_all.html", "Errata", problems=problems, errata=views.errata(config))

    def search_page(request: Request) -> Response:
        """Spec 1.4: the command bar. An htmx request gets the hits fragment; a plain GET (no JavaScript) the
        full results page. GET only; reads stored facts and registry records."""
        config, _ = _ctx(request)
        q = (request.query_params.get("q") or "").strip()[:80]
        scope = request.query_params.get("scope") or None
        scope = scope if scope == "component" else None
        hits = shell.search(config, q, scope=scope)
        if request.headers.get("hx-request"):
            p = Page()
            return HTMLResponse(env.get_template("_search_hits.html").render(
                hits=hits, q=q, min_query=shell.MIN_QUERY, fact=p.fact, page=p))
        return page(request, "search.html", "Search", hits=hits, q=q, scope=scope, min_query=shell.MIN_QUERY,
                    configs=views.configurations(config))

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
            if config.auth_mode == "local":
                return _login_form(request)
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
                        secure=_cookie_secure(request, state), path=authmod.AUTH_PREFIX)
        return resp

    def _login_form(request: Request, message: str = "", status: int = 200, login: str = "") -> Response:
        """CR-16: the local sign-in form, with a double-submit token (cookie + hidden field)."""
        token = request.cookies.get(authmod.LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(24)
        if request.query_params.get("signed_out"):
            message = message or "Signed out."
        html = env.get_template("login.html").render(
            token=token, next=_safe_next(request.query_params.get("next")), message=message, login=login)
        resp = HTMLResponse(html, status_code=status)
        resp.set_cookie(authmod.LOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True, samesite="strict",
                        secure=_cookie_secure(request, state), path=authmod.AUTH_PREFIX)
        return resp

    async def login_post(request: Request) -> Response:
        """CR-16: check the local user's password; on success the signed session cookie (SameSite=Strict)."""
        try:
            config = state.config()
        except ConfigError:
            return PlainTextResponse("503 Service Unavailable: the service config cannot be read", 503)
        if config.auth_mode != "local":
            return PlainTextResponse("405 Method Not Allowed: sign in with GitHub at %s" % authmod.LOGIN_PATH, 405)
        form = await request.form()
        login, password = str(form.get("login") or "").strip(), str(form.get("password") or "")
        cookie = request.cookies.get(authmod.LOGIN_CSRF_COOKIE, "")
        given = str(form.get(authmod.CSRF_FIELD) or "")
        if not cookie or not given or not hmac.compare_digest(cookie, given):
            return _login_form(request, "The form expired. Sign in again.", 403, login)
        if state.throttle.blocked(login):
            return _login_form(request, "Too many failed sign-ins for this login: wait 5 minutes.", 429, login)
        ok = await run_in_threadpool(authmod.check_local_password, config, login, password)
        if not ok:
            state.throttle.fail(login)
            return _login_form(request, "Wrong login or password.", 401, login)
        state.throttle.clear(login)
        sessions = state.sessions(config)
        resp = RedirectResponse(_safe_next(str(form.get("next") or "")), 303)
        user = authmod.local_users(config)[login]
        _set_session(resp, sessions, {"login": login, "status": "member", "checked_at": clock.iso(),
                                      "hash_tag": authmod.hash_tag(user["hash"])},
                     secure=_cookie_secure(request, state), same_site="strict")
        resp.delete_cookie(authmod.LOGIN_CSRF_COOKIE, path=authmod.AUTH_PREFIX)
        return resp

    def callback(request: Request) -> Response:
        try:
            config = state.config()
            if config.auth_mode == "local":
                return PlainTextResponse("404 Not Found: this service signs in locally at %s" % authmod.LOGIN_PATH,
                                         404)
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
                                      "checked_at": clock.iso()}, secure=_cookie_secure(request, state))
        resp.delete_cookie(authmod.STATE_COOKIE, path=authmod.AUTH_PREFIX)
        return resp

    def logout(request: Request) -> Response:
        try:
            config = state.config()
            local = config.auth_mode == "local"
        except ConfigError:
            local = False
        if local:                                    # revoke, so a copy of the cookie stops working too
            sessions = state.sessions(config)
            d = sessions.load_session(request.cookies.get(authmod.SESSION_COOKIE, "")) if sessions else None
            if d:
                authmod.revoke_session(config, d.get("sid", ""), authmod.session_seconds_left(d) or 0)
        resp: Response = RedirectResponse(authmod.LOGIN_PATH + "?signed_out=1", 303) if local \
            else PlainTextResponse("Signed out.", 200)
        resp.delete_cookie(authmod.SESSION_COOKIE, path="/")
        return resp

    # Registration. api_route with explicit methods: no route is registered through a `.post(...)` call.
    # The Wave C workspace routers first (routers.mount), so their deeper routes match before the app's own.
    routers.mount(app)
    get = ["GET"]
    for path, fn in (("/", dashboard), ("/search", search_page), ("/configurations/{id}", configuration), ("/components/{id}", component),
                     ("/contains", contains_page), ("/silicon-protection", silicon), ("/defects", defects_page),
                     ("/defects/{id}", defect_page), ("/errata/{configuration}", errata_page),
                     ("/runs/{nonce}", run_page), ("/facts/{subject:path}", facts_page),
                     ("/compat", compat_page), ("/branches", branches_page), ("/hosts", hosts_page),
                     ("/mirrors", mirrors_page), ("/alerts", alerts_page), ("/components", components_page),
                     ("/errata", errata_index), ("/jobs/{id}/fragment", job_fragment_route), ("/jobs/{id}/log", job_log),
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
    local_posts = (("/compat/jobs", post_compat), ("/jobs/{id}/cancel", post_cancel),
                   ("/configurations/{id}/walk", post_walk), ("/mirrors/sync", post_mirror_sync),
                   ("/alerts/clear", post_clear_alert), ("/branches/refresh", post_branches_refresh),
                   ("/hosts/refresh", post_hosts_refresh), (authmod.LOGIN_PATH, login_post), (authmod.LOGOUT_PATH, logout))
    for path, fn in local_posts if state.local_routes else ():
        app.add_api_route(path, fn, methods=["POST"], include_in_schema=False)
    state.nav = NAV + tuple(plugins.load(app))
    return app
