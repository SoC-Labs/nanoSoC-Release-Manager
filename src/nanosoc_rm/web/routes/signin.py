"""Sign-in and sign-out (section 14). These routes sit under the guard's open prefix `/auth`, so each one reads the
config itself and does its own token check: the GitHub OAuth state cookie, the local form's double-submit token,
or the session's CSRF token for sign-out."""
from __future__ import annotations

import secrets

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import auth as authmod
from nanosoc_rm import clock
from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.web.page import env
from nanosoc_rm.web.routers import Route
from nanosoc_rm.web.routes.common import cookie_secure, safe_next, same, set_session, state_of

__all__ = ["ROUTES"]


def login(request: Request) -> Response:
    state = state_of(request)
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
    resp.set_cookie(authmod.STATE_COOKIE, sessions.dump_state(st, safe_next(request.query_params.get("next"))),
                    max_age=authmod.STATE_MAX_AGE_S, httponly=True, samesite="lax",
                    secure=cookie_secure(request, state), path=authmod.AUTH_PREFIX)
    return resp


def _login_form(request: Request, message: str = "", status: int = 200, login: str = "") -> Response:
    """The local sign-in form, with a double-submit token (cookie + hidden field)."""
    token = request.cookies.get(authmod.LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(24)
    if request.query_params.get("signed_out"):
        message = message or "Signed out."
    html = env.get_template("login.html").render(
        token=token, next=safe_next(request.query_params.get("next")), message=message, login=login)
    resp = HTMLResponse(html, status_code=status)
    resp.set_cookie(authmod.LOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True, samesite="strict",
                    secure=cookie_secure(request, state_of(request)), path=authmod.AUTH_PREFIX)
    return resp


async def login_post(request: Request) -> Response:
    """Check the local user's password; on success the signed session cookie (SameSite=Strict)."""
    state = state_of(request)
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
    if not same(given, cookie):
        return _login_form(request, "The form expired. Sign in again.", 403, login)
    source = request.client.host if request.client else "?"
    if not state.ip_throttle.attempt(source):       # the source address first: a blocked host reserves nothing
        return _login_form(request, "Too many failed sign-ins from this address: wait 5 minutes.", 429, login)
    if not state.throttle.attempt(login):           # reserved before the slow check: holds under concurrency
        state.ip_throttle.release(source)
        return _login_form(request, "Too many failed sign-ins for this login: wait 5 minutes.", 429, login)
    ok = await run_in_threadpool(authmod.check_local_password, config, login, password)
    if not ok:
        return _login_form(request, "Wrong login or password.", 401, login)
    state.throttle.clear(login)
    state.ip_throttle.release(source)               # only this check's slot: a sign-in never wipes the address
    sessions = state.sessions(config)
    resp = RedirectResponse(safe_next(str(form.get("next") or "")), 303)
    user = authmod.local_users(config)[login]
    set_session(resp, sessions, {"login": login, "status": "member", "checked_at": clock.iso(),
                                 "hash_tag": authmod.hash_tag(user["hash"])},
                secure=cookie_secure(request, state), same_site="strict")
    resp.delete_cookie(authmod.LOGIN_CSRF_COOKIE, path=authmod.AUTH_PREFIX)
    return resp


def callback(request: Request) -> Response:
    state = state_of(request)
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
    if saved is None or not same(given, str(saved["state"])):
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
            token = None                             # section 14: the user's token is kept nowhere
    except (authmod.AuthError, ConfigError) as e:
        return PlainTextResponse("502 Bad Gateway: GitHub sign-in failed: %s" % e, 502)
    member = membership == "active" and authmod.MEMBER_ROLE in authmod.roles_for(config, who)
    if member:
        resp: Response = RedirectResponse(safe_next(saved.get("next")), 303)
    else:
        resp = PlainTextResponse("403 Forbidden: signed in to GitHub as %s, which is not an active SoC-Labs "
                                 "member with a role in this service" % who, 403)
    set_session(resp, sessions, {"login": who, "status": "member" if member else "refused",
                                 "checked_at": clock.iso()}, secure=cookie_secure(request, state))
    resp.delete_cookie(authmod.STATE_COOKIE, path=authmod.AUTH_PREFIX)
    return resp


def _logout_session(request: Request) -> tuple[Config | None, authmod.Sessions | None, dict | None]:
    """The config, the session signer and the request's session payload (None when absent or invalid)."""
    state = state_of(request)
    try:
        config = state.config()
    except ConfigError:
        return None, None, None
    sessions = state.sessions(config)
    d = sessions.load_session(request.cookies.get(authmod.SESSION_COOKIE, "")) if sessions else None
    return config, sessions, d


def logout_form(request: Request) -> Response:
    """GET /auth/logout signs nobody out, because a cross-site link or <img> could request it: it shows a
    sign-out button that POSTs with the session's CSRF token."""
    _config, sessions, d = _logout_session(request)
    if d is None:
        return RedirectResponse(authmod.LOGIN_PATH + "?signed_out=1", 303)
    token = sessions.csrf(d["login"], d.get("sid", ""))
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign out</title></head><body>'
            '<form method="post" action="%s"><input type="hidden" name="%s" value="%s">'
            '<button type="submit">Sign out</button></form></body></html>'
            % (authmod.LOGOUT_PATH, authmod.CSRF_FIELD, token))
    return HTMLResponse(body, 200)


async def logout(request: Request) -> Response:
    """POST /auth/logout, both modes: needs the session's CSRF token, then revokes the session id so a copy of
    the cookie stops working too, and deletes the cookie."""
    config, sessions, d = _logout_session(request)
    local = config is not None and config.auth_mode == "local"
    if d is not None:
        given = ""
        if "form" in request.headers.get("content-type", ""):
            given = str((await request.form()).get(authmod.CSRF_FIELD) or "")
        given = request.headers.get("x-csrf-token", "") or given
        if not same(given, sessions.csrf(d["login"], d.get("sid", ""))):
            return PlainTextResponse("403 Forbidden: the form's CSRF token is missing or wrong; reload the page "
                                     "and try again", 403)
        await run_in_threadpool(authmod.revoke_session, config, d.get("sid", ""),
                                authmod.session_seconds_left(d) or 0)
    resp: Response = RedirectResponse(authmod.LOGIN_PATH + "?signed_out=1", 303) if local \
        else PlainTextResponse("Signed out.", 200)
    resp.delete_cookie(authmod.SESSION_COOKIE, path="/")
    return resp


ROUTES = (
    Route("GET", authmod.LOGIN_PATH, login),
    Route("GET", authmod.CALLBACK_PATH, callback),
    Route("GET", authmod.LOGOUT_PATH, logout_form),
    Route("POST", authmod.LOGIN_PATH, login_post, local_only=True),
    Route("POST", authmod.LOGOUT_PATH, logout),                      # both modes
)
