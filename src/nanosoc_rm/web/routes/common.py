"""What the core route modules share: the request's config and identity, the page shell, NDJSON answers, job
cards, and the session-cookie rules that both the guard (web/app.py) and the sign-in routes apply."""
from __future__ import annotations

import hmac
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from nanosoc_rm import auth as authmod
from nanosoc_rm import clock, db, jobs
from nanosoc_rm.config import Config
from nanosoc_rm.web import registry_facts, routers, shell
from nanosoc_rm.web.page import Page, env, render

__all__ = ["NDJSON", "ctx", "state_of", "ndjson", "page", "not_found", "registry_problems", "delegate",
           "job_fragment", "job_html", "redacted_alert", "same", "safe_next", "cookie_secure", "set_session", "session_header"]

NDJSON = "application/x-ndjson"


def ctx(request: Request) -> tuple[Config, authmod.Identity]:
    """The config and identity the guard put on the request."""
    return request.state.config, request.state.identity


def state_of(request: Request) -> Any:
    """The app's `_State` (web/app.py): config path, authenticator, throttles, navigation."""
    return request.app.state.rm


def ndjson(docs: list[dict], status: int = 200) -> Response:
    body = "".join(json.dumps(d, separators=(",", ":"), sort_keys=True) + "\n" for d in docs)
    return Response(body, status_code=status, media_type=NDJSON)


def page(request: Request, template: str, title: str, status: int = 200, problems: list[str] | None = None,
         **kw: Any) -> HTMLResponse:
    """`template` inside the shell: navigation rail, notification bell, crumbs, CSRF token, sign-in state.
    The rail and the bell never take a page down: a failure to read them is a problem line, or no bell."""
    state = state_of(request)
    config, ident = ctx(request)
    path = request.url.path
    problems = list(problems or [])
    no_rail = bool(kw.pop("no_rail", False))         # full-width pages (Home, Notifications, Catalogue)
    rail = None
    if not no_rail:
        try:
            rail = shell.rail(config, ident, path, state.nav)
        except Exception as e:                       # noqa: BLE001
            problems.append("the navigation rail could not be read: %s" % str(e)[:200])
    bell = None
    notes = routers.optional("notifications")
    if notes is not None:
        try:
            bell = int(notes.unread_count(config, getattr(ident, "login", "") or ""))
        except Exception:                            # noqa: BLE001
            bell = None
    if kw.get("crumbs") is None:
        kw["crumbs"] = _default_crumbs(state.nav, path, title)
    html = render(template, title=title, identity=ident, problems=problems, csrf=state.csrf_for(ident, config),
                  nav=state.nav, path=path, auth_mode=config.auth_mode, rail=rail,
                  local_ci=jobs.is_local_ci(config), actions=state.local_routes, bell=bell, **kw)
    return HTMLResponse(html, status_code=status)


def _default_crumbs(nav: tuple, path: str, title: str) -> list[tuple[str, str | None]]:
    """Projects, then the navigation entry the page sits under (a plug-in's or a Tools page's), then the page.
    A workspace handler passes its own trail instead."""
    if path == "/":
        return [("Projects", None)]
    entry = next((n for n in nav if n[1] != "/" and any(
        path == p or path.startswith(p.rstrip("/") + "/") for p in n[2])), None)
    trail: list[tuple[str, str | None]] = [("Projects", "/")]
    if entry is not None and entry[1].rstrip("/") != path.rstrip("/"):
        trail.append((entry[0], entry[1]))
    return shell.crumbs(trail + [(title, None)])


def not_found(request: Request, what: str) -> HTMLResponse:
    return page(request, "message.html", "Not found", status=404, message="No %s is known here." % what)


def registry_problems(config: Config) -> list[str]:
    return registry_facts.refresh(config)


def delegate(name: str, request: Request, *args: Any) -> Response | None:
    """The workspace module that owns this page (routers.DELEGATES) answers it when it is built here. None: it is
    not built, or it declined, and the older page is served."""
    target = routers.DELEGATES.get(name)
    mod = routers.optional(target[0]) if target else None
    fn = getattr(mod, target[1], None) if mod is not None else None
    return fn(request, *args) if fn is not None else None


def job_fragment(request: Request, job: dict, status: int = 200) -> HTMLResponse:
    """One job's status, log tail and (when done) result: the htmx polling target."""
    config, ident = ctx(request)
    p = Page()
    html = env.get_template("_job.html").render(
        job=job, log=jobs.log_text(config, job["id"]), position=jobs.queue_position(config, job["id"]),
        csrf=state_of(request).csrf_for(ident, config), fact=p.fact, page=p, stored=_stored_result(config, job))
    return HTMLResponse(html, status_code=status)


def job_html(request: Request, job: dict | None) -> str:
    """The job card of `job` as text for a page that embeds it ("" for no job)."""
    return job_fragment(request, job).body.decode() if job else ""


def _stored_result(config: Config, job: dict) -> dict | None:
    """The stored compat_result of a finished compat job (None for a what-if: it is never stored)."""
    doc = job.get("result") if job["kind"] == "compat" else None
    if not doc or job["params"].get("pins"):
        return None
    hist = db.history(doc["subject"], config=config)
    return hist[-1] if hist else None


def redacted_alert(doc: dict, config: Config) -> dict:
    """A copy of an alert observation whose message has passed through shell.redact, as /alerts and the inbox
    show it, so no hostname or credential-bearing URL reaches /facts/alert:* or /api/v1/alerts."""
    if doc.get("kind") != "alert" or not isinstance(doc.get("fact"), dict):
        return doc
    return {**doc, "fact": {**doc["fact"], "message": shell.redact(doc["fact"].get("message"), config)}}


# ----- session cookies and tokens ------------------------------------------------------------------------------
def same(given: str, want: str) -> bool:
    """Constant-time equality of two tokens. Compared as bytes, so a non-ASCII token is a mismatch (403), not the
    TypeError `hmac.compare_digest` raises for a non-ASCII str."""
    return bool(given) and bool(want) and hmac.compare_digest(given.encode("utf-8", "surrogateescape"),
                                                              want.encode("utf-8", "surrogateescape"))


def safe_next(value: str | None) -> str:
    """A local path to return to after sign-in; anything else is "/" (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def cookie_secure(request: Request, state: Any) -> bool:
    """Cookies are `Secure` when this process serves TLS (`serve --tls-cert`) or the request is https."""
    return state.tls or request.url.scheme == "https"


def set_session(resp: Response, sessions: authmod.Sessions | None, d: dict, *, secure: bool,
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


def session_header(sessions: authmod.Sessions | None, d: dict, *, secure: bool) -> str:
    probe = Response()
    set_session(probe, sessions, d, secure=secure)
    return probe.headers.get("set-cookie", "")
