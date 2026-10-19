"""The web app: the section 13 pages and JSON API behind the section 14 sign-in (A3, A5, H1, H2).

`create_app(config_path=None, auth=None)` builds it. The config is read per request (`load_config`), so a
change of roles or registry takes effect without a restart.

Guard. One ASGI middleware sits in front of EVERY route, including a route someone appends to the router
after construction, and a websocket route too: only `/auth/*` and `/static/*` pass without a member (http),
and `/executor/*` goes to the executor API (executor/api.py: POST only, its own bearer token, no session,
never routed by this app, so a session never reaches it and its token never reaches any other route),
and a websocket without a member is closed with 1008. Only the ASGI lifespan scope passes unchecked.
Anonymous: pages redirect to `/auth/login`, `/api` and non-GET requests get 401. A refused session (not an
active member, or no role) gets 403 everywhere. Refusals carry no data and never echo the path.

CSRF. Every request other than GET and HEAD outside `/auth/*` must carry the session's CSRF token (form field
`csrf_token` or header `X-CSRF-Token`; htmx sends the header from the page's `hx-headers`). The guard checks it
before routing, so a POST route added by a plug-in is covered too.

Routes. The handlers live in web/routes/ (pages, actions, api, signin) and are registered by routers.py: the
workspace routers first, then the app's own routes, then the plug-ins (web/plugins.py). With no `[ci] project`
configured, Run T0 queues a local compat job instead of triggering CI. FastAPI's own docs and OpenAPI routes
are switched off.
"""
from __future__ import annotations

import html
import logging
import os
import secrets
import sqlite3
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
from nanosoc_rm import clock, jobs
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.capabilities import bumpcheck
from nanosoc_rm.executor import api as executor_api
from nanosoc_rm.web import plugins, routers
from nanosoc_rm.web.page import NAV
from nanosoc_rm.web.routes import common
from nanosoc_rm.web.routes.common import NDJSON, cookie_secure as _cookie_secure, same as _same
from nanosoc_rm.web.routes.common import session_header as _session_header, set_session as _set_session

__all__ = ["create_app", "STATIC_DIR", "STATIC_PREFIX", "NDJSON"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static"
_LOG = logging.getLogger("nanosoc_rm.web")
_OPEN_PREFIXES = (authmod.AUTH_PREFIX, STATIC_PREFIX)
_SAFE_METHODS = ("GET", "HEAD")
_MAX_FORM_BYTES = 1024 * 1024


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class _State:
    def __init__(self, config_path: str | None, auth: Any):
        self.config_path = config_path
        self.auth = auth if auth is not None else authmod.ConfiguredAuth()
        self.ephemeral_secret = secrets.token_hex(32)     # CSRF key when no session secret is configured
        self.nav: tuple = NAV
        self.page: Callable[..., HTMLResponse] | None = None
        self.throttle = authmod.Throttle()
        # Per source address as well: one host cannot run more than IP_LIMIT failed checks
        # in the window whatever logins it tries. The per-login throttle above still applies.
        self.ip_throttle = authmod.Throttle(limit=authmod.IP_LIMIT)
        self.local_routes = False                         # local-deployment POST routes registered (create_app)
        self.tls = False                                  # serve terminates TLS itself (create_app)

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
        started = False
        headers = _security_headers(scope, self.state)

        async def tracked(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                have = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in headers if k not in have]
            await send(message)
        try:
            await self._http(scope, receive, tracked)
        except sqlite3.DatabaseError as e:
            # A store that cannot be read (corrupt, locked past its timeout, read-only) on any route, the guard's
            # own session check included: a page that names the store, never a bare 500.
            if started:
                raise
            resp = await run_in_threadpool(_store_problem, self.state, scope, e)
            await resp(scope, receive, tracked)
        except Exception as e:                       # noqa: BLE001 - any other fault: a styled page, logged
            # A page that raised (a template meeting a result shape it never saw, a plug-in bug): the person gets
            # the service's own 500 page that names the fault class, never the bare text "Internal Server Error";
            # the traceback goes to the service log. A response already started cannot be replaced.
            if started:
                raise
            _LOG.exception("unhandled error serving %s %s", scope.get("method"), scope.get("path"))
            await _server_error(scope, e)(scope, receive, tracked)

    async def _http(self, scope: dict, receive: Callable, send: Callable) -> None:
        path = scope.get("path", "")
        if _is_under(path, executor_api.PREFIX):       # bearer token only, never a session
            await executor_api.serve(scope, receive, send, self.state.config)
            return
        if path == bumpcheck.PATH and bumpcheck.bearer(scope):   # a CI job's reader token (section 26.7)
            await bumpcheck.serve(scope, receive, send, self.state.config)
            return
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
            if not _same(token, want):
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


_STORES = (("rm.sqlite", "the fact store"), ("jobs.sqlite", "the job store"),
           ("sessions.sqlite", "the session store"), ("gui.sqlite", "the GUI state store"),
           ("bumps.sqlite", "the bump store"))


def _store_fails(path: Path) -> str | None:
    """Why the SQLite file at `path` cannot be read (None: it reads, or it does not exist). Read-only, short
    timeout: this runs only to name the store on an error page."""
    if not path.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(path)), uri=True, timeout=0.5)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return str(e)
    return None


def _store_problem(state: "_State", scope: dict, error: sqlite3.DatabaseError) -> Response:
    """The answer to a request a store could not serve: 503, naming each store that does not read now (or
    "a data store" when the failure was transient), and the error. No traceback, no data."""
    named = []
    try:
        data_dir = Path(state.config().data_dir)
        named = ["%s (%s) could not be read: %s" % (what, name, why) for name, what in _STORES
                 if (why := _store_fails(data_dir / name)) is not None]
    except Exception:                                # noqa: BLE001 - the page still answers
        pass
    stores = "; ".join(named) or "a data store could not be read: %s" % str(error)[:300]
    text = ("Problem: %s. The service is up, but this page needs that data. If this persists, the store needs "
            "repair or restoring from backup." % stores)
    if _is_under(scope.get("path", ""), "/api"):
        return JSONResponse({"error": text}, 503)
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Problem: data store</title></head>'
            '<body><main><h1>Problem</h1><p class="problem">%s</p><p><a href="/">Inbox</a></p></main></body></html>'
            % html.escape(text))
    return HTMLResponse(body, 503)


# Response headers on every answer, the open prefixes (sign-in, static) included.
# Scripts come from /static only: no inline script and no inline event handler anywhere in the templates
# (tests/unit/web/test_security_headers.py checks that). Inline style attributes stay allowed.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
HSTS = "max-age=31536000"
_BASE_HEADERS = ((b"content-security-policy", CSP.encode()), (b"x-frame-options", b"DENY"),
                 (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"))


def _security_headers(scope: dict, state: "_State") -> list[tuple[bytes, bytes]]:
    """The headers above, plus HSTS when this answer goes over TLS (the process serves TLS itself, or the
    request arrived over https). Never HSTS on plain http: a browser would ignore it there anyway."""
    out = list(_BASE_HEADERS)
    if getattr(state, "tls", False) or scope.get("scheme") == "https":
        out.append((b"strict-transport-security", HSTS.encode()))
    return out


def _server_error(scope: dict, error: BaseException) -> Response:
    """The answer to a request whose handler raised: 500, the fault class and when, styled like every page (the
    stylesheet is served without sign-in). No traceback and no data in the page."""
    when = clock.iso()
    # Only a GET or HEAD is known to have changed nothing: a POST that raised may have written before it failed.
    effect = ("nothing was changed by this request" if scope.get("method", "GET") in _SAFE_METHODS
              else "this request may have been partly applied, so check the page before trying again")
    text = ("This page failed with %s%s. The service is up and %s; the details are in the service log."
            % (type(error).__name__, " at %s" % when if when else "", effect))
    if _is_under(scope.get("path", ""), "/api"):
        return JSONResponse({"error": text}, 500)
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" '
            'content="light dark"><title>Problem · nanoSoC Release Manager</title>'
            '<link rel="stylesheet" href="%s/rm.css"></head><body><main id="main" class="error-page">'
            '<h1>Problem</h1><p class="banner problem" data-error="500">%s</p>'
            '<p><a href="/">Projects</a> · <a href="%s">Try this page again</a></p></main></body></html>'
            % (STATIC_PREFIX, html.escape(text), html.escape(scope.get("path", "/") or "/", quote=True)))
    return HTMLResponse(body, 500)


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


def create_app(config_path: str | os.PathLike | None = None, auth: Any = None, *, tls: bool = False) -> FastAPI:
    """The web app (interfaces.md sections 13, 14). `config_path` None means `$NANOSOC_RM_CONFIG`, read per
    request. `auth`: the authenticator; None is GitHub sign-in, `FakeMembership(login=)` the test seam. `tls`:
    this process serves HTTPS itself, so every cookie it sets is `Secure`."""
    state = _State(None if config_path is None else os.fspath(config_path), auth)
    state.tls = tls
    # The local POST routes exist only in a deployment that needs them (local sign-in, or no CI project), so a
    # GitHub + CI deployment keeps section 13's two Run routes as its only writes (D5). Decided when the app is
    # built: changing [auth] mode or [ci] project needs a restart.
    try:
        built = state.config()
        state.local_routes = built.auth_mode == "local" or jobs.is_local_ci(built)
    except ConfigError:
        state.local_routes = False
    state.page = common.page
    app = FastAPI(title="nanoSoC Release Manager", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rm = state
    app.add_middleware(Guard, state=state)
    app.mount(STATIC_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static")
    routers.mount(app)                   # first: a workspace's deeper routes match before the app's own
    routers.mount_core(app)
    state.nav = NAV + tuple(plugins.load(app))
    return app
