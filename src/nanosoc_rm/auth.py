"""Sign-in and roles (interfaces.md section 14; A5, H2).

Who may see anything: a GitHub user who is an ACTIVE member of the SoC-Labs organisation and holds a role in
the service config. Everything else is refused.

- **Login:** `/auth/login` redirects to GitHub's authorize URL with `scope=read:org`, the configured client id
  and a random `state`, kept in a short-lived signed cookie. `/auth/callback` refuses a `state` that does not
  match, exchanges the code for a token (`POST login/oauth/access_token`, the one read-only POST of
  `WRITE_CALLS`), reads the login (`GET user`) and the membership (`GET user/memberships/orgs/<org>`), and then
  drops the token: it is kept in neither the session nor the database.
- **Membership:** only `state: active` counts. A pending invitee and a non-member complete the callback, get a
  signed REFUSED session, and then a 403 on every route (`/api` included), so a refused user is told so instead
  of being sent round the login loop.
- **Roles:** `roles.default` for every active member, plus `roles."<login>"` (section 1). v1's one role,
  `member`, allows viewing and Run T0. No role means refused, so an empty table refuses everyone.
- **Session:** a cookie signed with `session.secret_file` (itsdangerous), valid 8 hours from sign-in. The
  sign-in time (`iat`) is in the signed payload and is never moved: re-signing the cookie after a re-check
  does not extend the session. Membership is re-checked at most hourly with the service's read token,
  `is_member(config, login)`. When GitHub cannot answer, the session is kept and the failed attempt is
  recorded (`attempt_at`), so GitHub is asked again an hour later, not on every request.
- **CSRF:** every form carries `csrf_token`, an HMAC of the session id and login. It is checked on every POST
  authenticated by the session cookie (the only ambient credential a browser sends cross-site).
- **Test seam:** `create_app(config_path, auth=FakeMembership(login=...))` signs every request in as that
  login, as an active member. Roles still come from the config. There is no cookie, so there is no
  cross-site request to forge, and the CSRF check does not apply to it.

The OAuth exchange is a POST that writes nothing, declared `read-post` in `nanosoc_rm.contracts.WRITE_CALLS`.
Tokens never appear in a URL, a log, an error or a page (interfaces.md section 0.3).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from nanosoc_rm import clock
from nanosoc_rm.config import Config, ConfigError

__all__ = ["SESSION_COOKIE", "STATE_COOKIE", "SESSION_MAX_AGE_S", "STATE_MAX_AGE_S", "RECHECK_S", "MEMBER_ROLE",
           "AUTH_PREFIX", "LOGIN_PATH", "CALLBACK_PATH", "LOGOUT_PATH", "CSRF_FIELD", "Identity", "AuthError",
           "FakeMembership", "GitHubAuth", "roles_for", "is_member", "authorize_url", "exchange_code",
           "user_login", "membership_state", "github_org", "Sessions", "session_seconds_left"]

SESSION_COOKIE = "rm_session"
STATE_COOKIE = "rm_oauth_state"
SESSION_MAX_AGE_S = 8 * 3600            # section 14: valid 8 h
STATE_MAX_AGE_S = 600                   # an authorize round trip
RECHECK_S = 3600                        # section 14: membership re-checked at most hourly
MEMBER_ROLE = "member"                  # v1's one role: view + Run T0
AUTH_PREFIX = "/auth"
LOGIN_PATH, CALLBACK_PATH, LOGOUT_PATH = AUTH_PREFIX + "/login", AUTH_PREFIX + "/callback", AUTH_PREFIX + "/logout"
CSRF_FIELD = "csrf_token"
OAUTH_SCOPE = "read:org"
_HTTP_TIMEOUT_S = 20


class AuthError(Exception):
    """GitHub could not be asked (network, bad answer, unconfigured app). Never carries a token."""


@dataclass
class Identity:
    """Who a request is. `status`: member (active SoC-Labs member) or refused. `via`: session (signed cookie)
    or fake (the FakeMembership test seam). `sid` binds the CSRF token to one session."""
    login: str
    status: str
    via: str
    sid: str = ""
    checked_at: str = ""
    iat: str = ""                                               # sign-in time; the session ends 8 h after it
    attempt_at: str = ""                                        # last re-check GitHub could not answer
    refreshed: dict | None = field(default=None, repr=False)   # a re-checked session to write back

    def session(self, **changes: str) -> dict:
        """This identity as a session payload (for write-back), with `changes` applied."""
        d = {"login": self.login, "status": self.status, "sid": self.sid, "checked_at": self.checked_at,
             "iat": self.iat}
        if self.attempt_at:
            d["attempt_at"] = self.attempt_at
        d.update(changes)
        return {k: v for k, v in d.items() if v}


def github_org(config: Mapping[str, Any]) -> str:
    return (config.get("hosts", {}).get("github", {}) or {}).get("org") or "SoC-Labs"


def roles_for(config: Mapping[str, Any], login: str) -> set[str]:
    """roles.default plus roles."<login>" (interfaces.md section 1). An empty table gives no roles."""
    table = config.get("roles") or {}
    return set(table.get("default") or []) | set(table.get(login) or [])


# ---------------------------------------------------------------------------------------------------------
# GitHub calls (read-only; the code exchange is the declared read-post)
# ---------------------------------------------------------------------------------------------------------
def _github(config: Config) -> dict:
    gh = config.host("github")
    if not gh.get("web") or not gh.get("api"):
        raise ConfigError("%s: [hosts.github] web and api are required for sign-in" % config.path)
    return gh


def authorize_url(config: Config, state: str) -> str:
    """GitHub's authorize URL: scope read:org only, the configured client id, and `state`."""
    client_id = config.require("credentials", "github_oauth", "client_id")
    query = urllib.parse.urlencode({"client_id": client_id, "scope": OAUTH_SCOPE, "state": state,
                                    "allow_signup": "false"})
    return "%s/login/oauth/authorize?%s" % (_github(config)["web"].rstrip("/"), query)


def exchange_code(config: Config, code: str, client: httpx.Client | None = None) -> str:
    """The user's token for an OAuth code. Read-post (WRITE_CALLS): GitHub stores nothing it did not already
    hold. The caller drops the token after the membership check."""
    gh = _github(config)
    client_id = config.require("credentials", "github_oauth", "client_id")
    secret = config.read_secret("github_oauth")
    url = gh["web"].rstrip("/") + "/login/oauth/access_token"
    own = client is None
    client = client or httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=False)
    try:
        r = client.post(url, data={"client_id": client_id, "client_secret": secret, "code": code},
                        headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise AuthError("the OAuth code exchange failed: %s" % type(e).__name__) from None
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise AuthError("the OAuth code exchange answered HTTP %d" % r.status_code)
    try:
        token = r.json().get("access_token")
    except ValueError:
        token = None
    if not token:
        raise AuthError("the OAuth code exchange returned no token (expired or reused code)")
    return token


def _get(config: Config, path: str, token: str, client: httpx.Client | None = None) -> httpx.Response:
    url = _github(config)["api"].rstrip("/") + "/" + path.lstrip("/")
    own = client is None
    client = client or httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=False)
    try:
        return client.get(url, headers={"Authorization": "Bearer %s" % token,
                                        "Accept": "application/vnd.github+json"})
    except httpx.HTTPError as e:
        raise AuthError("GET %s failed: %s" % (path, type(e).__name__)) from None
    finally:
        if own:
            client.close()


def user_login(config: Config, token: str, client: httpx.Client | None = None) -> str:
    """`GET user` with the user's token: the login it belongs to."""
    r = _get(config, "user", token, client)
    if r.status_code != 200:
        raise AuthError("GET user answered HTTP %d" % r.status_code)
    login = (r.json() or {}).get("login")
    if not login:
        raise AuthError("GET user returned no login")
    return login


def membership_state(config: Config, token: str, client: httpx.Client | None = None) -> str | None:
    """`GET user/memberships/orgs/<org>` with the user's token: `active`, `pending`, or None (not a member)."""
    org = github_org(config)
    r = _get(config, "user/memberships/orgs/%s" % org, token, client)
    if r.status_code in (403, 404):
        return None
    if r.status_code != 200:
        raise AuthError("GET user/memberships/orgs/%s answered HTTP %d" % (org, r.status_code))
    return (r.json() or {}).get("state")


def is_member(config: Config, login: str, client: httpx.Client | None = None) -> bool:
    """Section 14 re-check: `GET orgs/<org>/members/<login>` with the service's read token (github_read).
    204 is a member; 404 (or a redirect, which GitHub gives a non-member requester) is not. Anything else
    raises AuthError: an unanswered question is never read as "member"."""
    org = github_org(config)
    token = config.read_secret("github_read")
    r = _get(config, "orgs/%s/members/%s" % (org, urllib.parse.quote(login, safe="")), token, client)
    if r.status_code == 204:
        return True
    if r.status_code in (302, 404):
        return False
    raise AuthError("GET orgs/%s/members/<login> answered HTTP %d" % (org, r.status_code))


# ---------------------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------------------
def _age_s(stamp: str | None) -> float | None:
    """Seconds since a contract timestamp; None if it is missing or unreadable."""
    try:
        return (clock.now() - clock.parse(stamp)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return None


def session_seconds_left(d: Mapping[str, Any]) -> int | None:
    """Seconds until a session payload ends (8 h after its `iat`); None if it has no readable `iat`. A sign-in
    time in the future (beyond a minute of clock skew) is not a session."""
    age = _age_s(d.get("iat"))
    if age is None or age < -60:
        return None
    return int(SESSION_MAX_AGE_S - age)


class Sessions:
    """Signed cookies: the session ({login, status, sid, checked_at}) and the OAuth state. The session secret
    is `session.secret_file`; without it nobody can sign in (the login page says so)."""

    def __init__(self, secret: str):
        self._session = URLSafeTimedSerializer(secret, salt="nanosoc-rm session")
        self._state = URLSafeTimedSerializer(secret, salt="nanosoc-rm oauth state")
        self._csrf_key = hashlib.sha256(("csrf|" + secret).encode()).digest()

    @classmethod
    def from_config(cls, config: Config) -> "Sessions | None":
        if "secret_file" not in (config.get("session") or {}):
            return None
        return cls(config.read_secret("session"))

    def dump_session(self, login: str, status: str, sid: str | None = None, checked_at: str | None = None,
                     iat: str | None = None, attempt_at: str | None = None) -> str:
        """The signed session. `iat` (default now) is the sign-in time: pass it through on every re-sign."""
        now = clock.iso()
        d = {"login": login, "status": status, "sid": sid or secrets.token_hex(16),
             "checked_at": checked_at or now, "iat": iat or now}
        if attempt_at:
            d["attempt_at"] = attempt_at
        return self._session.dumps(d)

    def load_session(self, value: str) -> dict | None:
        """The session, or None: bad signature, malformed, or more than 8 h since sign-in (`iat`), however
        recently the cookie was re-signed."""
        try:
            d = self._session.loads(value, max_age=SESSION_MAX_AGE_S)
        except BadSignature:
            return None
        if not isinstance(d, dict) or d.get("status") not in ("member", "refused") or not d.get("login"):
            return None
        left = session_seconds_left(d)
        if left is None or left <= 0:
            return None
        return d

    def dump_state(self, state: str, next_path: str) -> str:
        return self._state.dumps({"state": state, "next": next_path})

    def load_state(self, value: str) -> dict | None:
        try:
            d = self._state.loads(value, max_age=STATE_MAX_AGE_S)
        except BadSignature:
            return None
        return d if isinstance(d, dict) and d.get("state") else None

    def csrf(self, login: str, sid: str) -> str:
        return hmac.new(self._csrf_key, ("%s|%s" % (login, sid)).encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------------------------------------
# Authenticators: identify(request cookies, config) -> Identity | None
# ---------------------------------------------------------------------------------------------------------
class GitHubAuth:
    """The real sign-in: the signed session cookie set by /auth/callback, re-checked hourly."""
    requires_csrf = True

    def __init__(self, check_member: Callable[[Config, str], bool] = None):
        self._check_member = check_member or is_member

    def identify(self, cookies: Mapping[str, str], config: Config, sessions: "Sessions | None") -> Identity | None:
        if sessions is None or SESSION_COOKIE not in cookies:
            return None
        d = sessions.load_session(cookies[SESSION_COOKIE])
        if d is None:
            return None
        ident = Identity(login=d["login"], status=d["status"], via="session", sid=d.get("sid", ""),
                         checked_at=d.get("checked_at", ""), iat=d.get("iat", ""), attempt_at=d.get("attempt_at", ""))
        if ident.status != "member":
            return ident
        checked, attempted = _age_s(ident.checked_at), _age_s(ident.attempt_at)
        due = (checked is None or checked > RECHECK_S) and (attempted is None or attempted > RECHECK_S)
        if due:
            try:
                still = self._check_member(config, ident.login)
            except (AuthError, ConfigError):
                # GitHub could not be asked. The session ends 8 h after sign-in, so a member keeps access through
                # an outage and a removed member keeps it at most until then. The attempt is written back, so the
                # next one is an hour away: an outage does not put a GitHub call in front of every request.
                ident.attempt_at = clock.iso()
                ident.refreshed = ident.session()
                return ident
            ident.checked_at = clock.iso()
            ident.attempt_at = ""
            if not still:
                ident.status = "refused"
            ident.refreshed = ident.session()
        return ident


class FakeMembership:
    """Test seam (interfaces.md section 14): every request is signed in as `login`, an active member. Roles
    still come from the config. No cookie is involved, so no CSRF check applies."""
    requires_csrf = False

    def __init__(self, login: str):
        if not login:
            raise ValueError("FakeMembership needs a login")
        self.login = login

    def identify(self, cookies: Mapping[str, str], config: Config, sessions: "Sessions | None") -> Identity:
        now = clock.iso()
        return Identity(login=self.login, status="member", via="fake", sid="fake", checked_at=now, iat=now)

    def __repr__(self) -> str:
        return "FakeMembership(login=%r)" % self.login
