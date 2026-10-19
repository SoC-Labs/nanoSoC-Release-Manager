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
  recorded (`attempt_at`), so GitHub is asked again 10 minutes later (K3 option a), not on every request.
- **Logout (both modes):** `POST /auth/logout` with the session's CSRF token revokes the session id
  (`<data_dir>/sessions.sqlite`) until it would have ended, so a copied cookie stops working too; `GET
  /auth/logout` only shows a sign-out button, so a cross-site link cannot sign anybody out.
- **CSRF:** every form carries `csrf_token`, an HMAC of the session id and login. It is checked on every POST
  authenticated by the session cookie (the only ambient credential a browser sends cross-site).
- **Test seam:** `create_app(config_path, auth=FakeMembership(login=...))` signs every request in as that
  login, as an active member. Roles still come from the config. There is no cookie, so there is no
  cross-site request to forge, and the CSRF check does not apply to it.

**Local mode.** `[auth] mode = "local"` replaces GitHub sign-in for a single-host deployment: named users
in `[[auth.users]]` (login, `hash` from `nanosoc-rm user add`: scrypt via hashlib, salted), a login form at
`/auth/login` (POST, with a double-submit CSRF cookie so nobody can sign a browser in as someone else), the same
signed session cookie (HttpOnly, SameSite=Strict, 8 h) and the same CSRF token on every POST. A user removed from
the config is signed out on the next request, and so is every session of a user whose hash changes. Roles: `roles.default`, `roles."<login>"` and the user's own
`roles` (default `["member"]` when the key is absent; an explicit `roles = []` adds none). Without `[session] secret_file`, the secret is generated once in
`<data_dir>/session.secret` (mode 0600). Failed sign-ins are throttled per login and per source address.

The OAuth exchange is a POST that writes nothing, declared `read-post` in `nanosoc_rm.contracts.WRITE_CALLS`.
Tokens never appear in a URL, a log, an error or a page (interfaces.md section 0.3).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
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
           "user_login", "membership_state", "github_org", "Sessions", "session_seconds_left", "LocalAuth",
           "ConfiguredAuth", "hash_password", "verify_password", "local_users", "check_local_password", "LOGIN_CSRF_COOKIE", "Throttle",
           "same_site"]

SESSION_COOKIE = "rm_session"
STATE_COOKIE = "rm_oauth_state"
SESSION_MAX_AGE_S = 8 * 3600            # section 14: valid 8 h
STATE_MAX_AGE_S = 600                   # an authorize round trip
RECHECK_S = 3600                        # section 14: membership re-checked at most hourly
RETRY_S = 600                           # section 14 (K3 a): a re-check GitHub could not answer is retried in 10 min
MEMBER_ROLE = "member"                  # v1's one role: view + Run T0
AUTH_PREFIX = "/auth"
LOGIN_PATH, CALLBACK_PATH, LOGOUT_PATH = AUTH_PREFIX + "/login", AUTH_PREFIX + "/callback", AUTH_PREFIX + "/logout"
CSRF_FIELD = "csrf_token"
LOGIN_CSRF_COOKIE = "rm_login_csrf"     # local mode: double-submit token of the login form
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 14, 8, 1, 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
OAUTH_SCOPE = "read:org"
_HTTP_TIMEOUT_S = 20


class AuthError(Exception):
    """GitHub could not be asked (network, bad answer, unconfigured app). Never carries a token."""


@dataclass
class Identity:
    """Who a request is. `status`: member (active SoC-Labs member) or refused. `via`: session (signed cookie),
    local (the local login form) or fake (the FakeMembership test seam). `sid` binds the CSRF token to one
    session."""
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
    roles = set(table.get("default") or []) | set(table.get(login) or [])
    if (config.get("auth") or {}).get("mode") == "local":
        user = local_users(config).get(login)
        if user is not None:
            own = user.get("roles")                     # absent: member; an explicit [] grants nothing
            roles |= set([MEMBER_ROLE] if own is None else own)
    return roles


def same_site(config: Mapping[str, Any]) -> str:
    """The session cookie's SameSite: Strict in local mode; Lax for GitHub sign-in, whose callback is a
    cross-site redirect."""
    return "strict" if (config.get("auth") or {}).get("mode") == "local" else "lax"


# ---------------------------------------------------------------------------------------------------------
# Local users
# ---------------------------------------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, *, salt: bytes | None = None, n: int = SCRYPT_N, r: int = SCRYPT_R,
                  p: int = SCRYPT_P) -> str:
    """`scrypt$<n>$<r>$<p>$<salt>$<hash>` (base64): what `[[auth.users]] hash` holds."""
    if not password:
        raise ValueError("an empty password is not a password")
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN,
                        maxmem=_SCRYPT_MAXMEM)
    return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, _b64(salt), _b64(dk))


def verify_password(password: str, encoded: str) -> bool:
    """True when `password` matches `encoded` (hash_password's format). A malformed hash never matches."""
    try:
        name, n, r, p, salt, want = encoded.split("$")
        if name != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if not (2 ** 10 <= n <= 2 ** 20 and 1 <= r <= 32 and 1 <= p <= 16):
            return False
        want_b = base64.b64decode(want, validate=True)
        dk = hashlib.scrypt((password or "").encode("utf-8"), salt=base64.b64decode(salt, validate=True), n=n, r=r,
                            p=p, dklen=len(want_b), maxmem=_SCRYPT_MAXMEM)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(dk, want_b)


_DUMMY_HASH = None


def _dummy_hash() -> str:
    """A hash nobody's password matches, verified for an unknown login so its timing matches a known one."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


def local_users(config: Mapping[str, Any]) -> dict[str, dict]:
    """{login: [[auth.users]] entry} of the config (local mode)."""
    return {u["login"]: u for u in (config.get("auth") or {}).get("users") or [] if isinstance(u, dict)
            and u.get("login")}


def check_local_password(config: Mapping[str, Any], login: str, password: str) -> bool:
    user = local_users(config).get(login)
    ok = verify_password(password, user["hash"] if user else _dummy_hash())
    return bool(user) and ok


def hash_tag(encoded: str) -> str:
    """A short digest of a user's configured hash, carried in the session: a new password ends old sessions."""
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _revoked_db(config: Config) -> sqlite3.Connection:
    from nanosoc_rm.db import private_store
    con = sqlite3.connect(private_store(config.data_dir / "sessions.sqlite"), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS revoked (sid TEXT PRIMARY KEY, until REAL NOT NULL)")
    return con


def revoke_session(config: Config, sid: str, seconds_left: int) -> None:
    """Logout: refuse session `sid` from now until it would have ended anyway."""
    if not sid:
        return
    config.data_dir.mkdir(parents=True, exist_ok=True)
    con = _revoked_db(config)
    try:
        with con:
            con.execute("DELETE FROM revoked WHERE until < ?", (time.time(),))
            con.execute("INSERT OR REPLACE INTO revoked VALUES (?, ?)", (sid, time.time() + max(0, seconds_left) + 60))
    finally:
        con.close()


def session_revoked(config: Config, sid: str) -> bool:
    if not (config.data_dir / "sessions.sqlite").exists():
        return False
    con = _revoked_db(config)
    try:
        return con.execute("SELECT 1 FROM revoked WHERE sid = ?", (sid,)).fetchone() is not None
    finally:
        con.close()


IP_LIMIT = 20                     # failed sign-in checks per source address per window (per login: Throttle.limit)


class Throttle:
    """At most `limit` password checks per login in `window_s` that did not end in a sign-in; then that login is
    refused until the window passes. In memory: a restart forgets it.

    `attempt(key)` reserves a slot under the lock BEFORE the (slow) password check, so concurrent guesses cannot
    all pass the check before any failure is counted; a successful sign-in calls `clear(key)`.
    Logins whose window has passed are swept at most once per window, so unique login names cannot grow the
    table without bound."""

    def __init__(self, limit: int = 5, window_s: float = 300.0):
        self.limit, self.window_s = limit, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._swept = time.monotonic()

    def _recent(self, key: str, now: float) -> list[float]:
        """Call with the lock held. The key's failures inside the window; a key with none is dropped."""
        if now - self._swept >= self.window_s:
            self._fails = {k: v for k, v in self._fails.items() if v and now - v[-1] < self.window_s}
            self._swept = now
        fails = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        if fails:
            self._fails[key] = fails
        else:
            self._fails.pop(key, None)
        return fails

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.limit

    def attempt(self, key: str) -> bool:
        """Reserve one password check for `key`: False (and nothing reserved) when the limit is reached. The
        reservation counts as a failure until `clear(key)` (a successful sign-in) removes it."""
        with self._lock:
            now = time.monotonic()
            fails = self._recent(key, now)
            if len(fails) >= self.limit:
                return False
            self._fails[key] = fails + [now]
            return True

    def release(self, key: str) -> None:
        """Give back the newest reservation of `key` (a check that did not count as a failure), leaving its
        older failures in place. The per-address throttle uses it: one good sign-in from a host must not erase the
        failures that host made against other logins."""
        with self._lock:
            fails = self._fails.get(key)
            if fails:
                fails.pop()
                if not fails:
                    self._fails.pop(key, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


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


def _local_secret(config: Config) -> str:
    """Local mode without [session] secret_file: `<data_dir>/session.secret`, created once (0600). Deleting it
    signs everybody out."""
    path = config.data_dir / "session.secret"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ConfigError("%s: the session secret is unreadable (%s)" % (path, e.strerror)) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    tmp = path.with_name(".session.secret.%d.%d" % (os.getpid(), threading.get_ident()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    try:
        os.link(tmp, path)                       # first writer wins; a racing one reads the winner's secret
    except FileExistsError:
        value = path.read_text().strip()
        if not value:                            # an empty file (truncated, full disk): replace it atomically,
            os.replace(tmp, path)                # never sign with ''
            value = path.read_text().strip()
    finally:
        tmp.unlink(missing_ok=True)
    if not value:
        raise ConfigError("%s: the session secret is empty" % path)
    return value


class Sessions:
    """Signed cookies: the session ({login, status, sid, checked_at}) and the OAuth state. The session secret
    is `session.secret_file`; without it nobody can sign in (the login page says so)."""

    def __init__(self, secret: str):
        if not secret:
            raise ConfigError("an empty session secret signs nothing")   # '' is a known key
        self._session = URLSafeTimedSerializer(secret, salt="nanosoc-rm session")
        self._state = URLSafeTimedSerializer(secret, salt="nanosoc-rm oauth state")
        self._csrf_key = hashlib.sha256(("csrf|" + secret).encode()).digest()

    @classmethod
    def from_config(cls, config: Config) -> "Sessions | None":
        if "secret_file" not in (config.get("session") or {}):
            if config.auth_mode == "local":
                return cls(_local_secret(config))
            return None
        return cls(config.read_secret("session"))

    def dump_session(self, login: str, status: str, sid: str | None = None, checked_at: str | None = None,
                     iat: str | None = None, attempt_at: str | None = None, hash_tag: str | None = None) -> str:
        """The signed session. `iat` (default now) is the sign-in time: pass it through on every re-sign.
        `hash_tag` (local mode): hash_tag() of the user's configured hash at sign-in."""
        now = clock.iso()
        d = {"login": login, "status": status, "sid": sid or secrets.token_hex(16),
             "checked_at": checked_at or now, "iat": iat or now}
        if attempt_at:
            d["attempt_at"] = attempt_at
        if hash_tag:
            d["hash_tag"] = hash_tag
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
        if session_revoked(config, d.get("sid", "")):
            return None                                   # signed out: a copied cookie is refused too
        ident = Identity(login=d["login"], status=d["status"], via="session", sid=d.get("sid", ""),
                         checked_at=d.get("checked_at", ""), iat=d.get("iat", ""), attempt_at=d.get("attempt_at", ""))
        if ident.status != "member":
            return ident
        checked, attempted = _age_s(ident.checked_at), _age_s(ident.attempt_at)
        due = (checked is None or checked > RECHECK_S) and (attempted is None or attempted > RETRY_S)
        if due:
            try:
                still = self._check_member(config, ident.login)
            except (AuthError, ConfigError):
                # GitHub could not be asked. The session ends 8 h after sign-in, so a member keeps access through
                # an outage and a removed member keeps it at most until then. The attempt is written back, so the
                # next one is 10 minutes away (K3 a): an outage does not put a GitHub call in front of every request,
                # and a revoked member is caught soon after GitHub is back.
                ident.attempt_at = clock.iso()
                ident.refreshed = ident.session()
                return ident
            ident.checked_at = clock.iso()
            ident.attempt_at = ""
            if not still:
                ident.status = "refused"
            ident.refreshed = ident.session()
        return ident


class LocalAuth:
    """The signed session cookie set by the local login form. The login must still be a configured user:
    removing a user from [[auth.users]] signs them out on their next request."""
    requires_csrf = True

    def identify(self, cookies: Mapping[str, str], config: Config, sessions: "Sessions | None") -> Identity | None:
        if sessions is None or SESSION_COOKIE not in cookies:
            return None
        d = sessions.load_session(cookies[SESSION_COOKIE])
        if d is None or d.get("status") != "member":
            return None
        user = local_users(config).get(d["login"])
        if user is None or not hmac.compare_digest(str(d.get("hash_tag") or ""), hash_tag(user["hash"])):
            return None                                   # removed, or the password changed since sign-in
        if session_revoked(config, d.get("sid", "")):
            return None                                   # signed out: a copied cookie is refused too
        return Identity(login=d["login"], status="member", via="local", sid=d.get("sid", ""),
                        checked_at=d.get("checked_at", ""), iat=d.get("iat", ""))


class ConfiguredAuth:
    """The default authenticator: the config's [auth] mode, read per request. GitHub sign-in unless
    mode = "local"."""
    requires_csrf = True

    def __init__(self, github: "GitHubAuth | None" = None, local: LocalAuth | None = None):
        self.github = github or GitHubAuth()
        self.local = local or LocalAuth()

    def identify(self, cookies: Mapping[str, str], config: Config, sessions: "Sessions | None") -> Identity | None:
        impl = self.local if config.auth_mode == "local" else self.github
        return impl.identify(cookies, config, sessions)


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
