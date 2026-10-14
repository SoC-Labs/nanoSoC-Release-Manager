"""Coverage angle (rob/coverage): the error paths of nanosoc_rm.auth that the GUI suite never drives.

Malformed password hashes, the sign-in throttle window, session payloads that must be refused, the local session
secret, the GitHub calls' non-200 answers (through httpx.MockTransport: no network), and the hourly membership
re-check with GitHub down. Each test names the mutation it was shown to catch."""
from __future__ import annotations

import base64
import datetime as dt
import stat

import httpx
import pytest
from itsdangerous import URLSafeTimedSerializer

from nanosoc_rm import auth, clock
from nanosoc_rm.config import Config, ConfigError

FAST = {"n": 2 ** 10, "r": 1, "p": 1}            # the smallest cost verify_password accepts, so the tests are quick


def _cfg(tmp_path, **doc) -> Config:
    doc.setdefault("service", {"data_dir": str(tmp_path / "data")})
    return Config(doc, tmp_path / "rm.toml")


def _gh_cfg(tmp_path) -> Config:
    (tmp_path / "oauth").write_text("oauth-secret\n")
    (tmp_path / "read").write_text("read-token\n")
    return _cfg(tmp_path, hosts={"github": {"web": "https://gh.invalid/", "api": "https://api.gh.invalid",
                                            "org": "Org"}},
                credentials={"github_oauth": {"client_id": "cid", "token_file": str(tmp_path / "oauth")},
                             "github_read": {"token_file": str(tmp_path / "read")}})


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ----- password hashes ---------------------------------------------------------------------------------------
def test_hash_round_trip_and_empty_password_refused():
    h = auth.hash_password("pw", **FAST)
    assert auth.verify_password("pw", h) and not auth.verify_password("pW", h)
    assert not auth.verify_password("", h) and not auth.verify_password(None, h)
    with pytest.raises(ValueError):
        auth.hash_password("")


def test_a_malformed_hash_never_matches():
    # mutation caught: without the n/r/p range check, the n=2 hash below (a correct but trivially cheap hash
    # someone wrote into the config) would match.
    good = auth.hash_password("pw", salt=b"s" * 16, **FAST)
    name, n, r, p, salt, dk = good.split("$")
    cheap_dk = base64.b64encode(__import__("hashlib").scrypt(b"pw", salt=b"s" * 16, n=2, r=1, p=1, dklen=32)).decode()
    bad = [
        "", "scrypt", good + "$extra", good.replace("scrypt$", "bcrypt$", 1),
        "$".join([name, "x", r, p, salt, dk]),                 # n not a number
        "$".join([name, "2", "1", "1", salt, cheap_dk]),       # n below 2**10: a cheap hash is not accepted
        "$".join([name, str(2 ** 21), r, p, salt, dk]),        # n above 2**20
        "$".join([name, n, "0", p, salt, dk]), "$".join([name, n, r, "17", salt, dk]),
        "$".join([name, n, r, p, "!!notb64", dk]), "$".join([name, n, r, p, salt, "!!"]),
    ]
    for h in bad:
        assert auth.verify_password("pw", h) is False, h


def test_check_local_password_unknown_login_and_wrong_password(tmp_path):
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": [{"login": "ann", "hash": auth.hash_password("pw", **FAST)},
                                                          "not-a-table", {"hash": "scrypt$x"}]})
    assert set(auth.local_users(cfg)) == {"ann"}                 # entries without a login are not users
    assert auth.check_local_password(cfg, "ann", "pw")
    assert not auth.check_local_password(cfg, "ann", "nope")
    assert not auth.check_local_password(cfg, "bob", "pw")      # unknown login: the dummy hash, never True


def test_roles_local_user_default_and_explicit(tmp_path):
    users = [{"login": "ann", "hash": "scrypt$"}, {"login": "bob", "hash": "scrypt$", "roles": ["admin"]}]
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": users}, roles={"default": ["viewer"], "bob": ["x"]})
    assert auth.roles_for(cfg, "ann") == {"viewer", auth.MEMBER_ROLE}
    assert auth.roles_for(cfg, "bob") == {"viewer", "x", "admin"}
    assert auth.roles_for(cfg, "nobody") == {"viewer"}
    assert auth.roles_for(_cfg(tmp_path), "ann") == set()       # no table: no roles, so refused
    assert auth.same_site(cfg) == "strict" and auth.same_site(_cfg(tmp_path)) == "lax"


# ----- throttle ----------------------------------------------------------------------------------------------
def test_throttle_blocks_at_the_limit_and_forgets_after_the_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    t = auth.Throttle(limit=3, window_s=60)
    for _ in range(2):
        t.fail("ann")
    assert not t.blocked("ann")
    t.fail("ann")
    assert t.blocked("ann") and not t.blocked("bob")             # per login
    now[0] += 59.9
    assert t.blocked("ann")
    now[0] += 0.2                                                # the window has passed
    assert not t.blocked("ann")
    for _ in range(3):
        t.fail("ann")
    t.clear("ann")
    assert not t.blocked("ann")


# ----- session payloads --------------------------------------------------------------------------------------
def _iso(delta_s: float) -> str:
    return (clock.now() + dt.timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_session_seconds_left_edges():
    assert auth.session_seconds_left({}) is None
    assert auth.session_seconds_left({"iat": "yesterday"}) is None
    assert auth.session_seconds_left({"iat": _iso(+300)}) is None          # sign-in in the future: not a session
    assert auth.session_seconds_left({"iat": _iso(+30)}) is not None       # within the minute of clock skew
    left = auth.session_seconds_left({"iat": _iso(-3600)})
    assert auth.SESSION_MAX_AGE_S - 3700 < left <= auth.SESSION_MAX_AGE_S - 3600


def test_load_session_refuses_bad_payloads():
    s = auth.Sessions("k" * 32)
    raw = URLSafeTimedSerializer("k" * 32, salt="nanosoc-rm session")
    assert s.load_session("garbage") is None
    assert s.load_session(auth.Sessions("other").dump_session("ann", "member")) is None   # another key
    assert s.load_session(raw.dumps(["not", "a", "dict"])) is None
    assert s.load_session(raw.dumps({"login": "ann", "status": "admin", "iat": _iso(0)})) is None
    assert s.load_session(raw.dumps({"login": "", "status": "member", "iat": _iso(0)})) is None
    assert s.load_session(raw.dumps({"login": "ann", "status": "member"})) is None         # no iat
    # re-signed just now, but signed in 9 h ago: the session has ended (iat is never moved)
    assert s.load_session(s.dump_session("ann", "member", iat=_iso(-9 * 3600))) is None
    ok = s.load_session(s.dump_session("ann", "refused", attempt_at=_iso(0), hash_tag="t"))
    assert ok["status"] == "refused" and ok["attempt_at"] and ok["hash_tag"] == "t"


def test_oauth_state_cookie():
    s = auth.Sessions("k" * 32)
    assert s.load_state(s.dump_state("abc", "/x")) == {"state": "abc", "next": "/x"}
    assert s.load_state(s.dump_state("", "/x")) is None
    assert s.load_state("tampered") is None
    assert s.csrf("ann", "s1") != s.csrf("ann", "s2") != auth.Sessions("other").csrf("ann", "s2")


def test_identity_session_payload_drops_empty_fields():
    i = auth.Identity(login="ann", status="member", via="session", sid="s", checked_at="c", iat="i")
    assert i.session() == {"login": "ann", "status": "member", "sid": "s", "checked_at": "c", "iat": "i"}
    assert i.session(status="refused", sid="")["status"] == "refused" and "sid" not in i.session(sid="")
    i.attempt_at = "a"
    assert i.session()["attempt_at"] == "a"


# ----- the local session secret ------------------------------------------------------------------------------
def test_local_secret_is_created_once_private_and_reused(tmp_path):
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": []})
    first = auth._local_secret(cfg)
    path = tmp_path / "data" / "session.secret"
    assert len(first) == 64 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth._local_secret(cfg) == first
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".session.secret.")]   # no temp left
    assert isinstance(auth.Sessions.from_config(cfg), auth.Sessions)
    assert auth.Sessions.from_config(_cfg(tmp_path)) is None     # GitHub mode without secret_file: no sign-in


def test_local_secret_unreadable_is_a_config_error(tmp_path):
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": []})
    (tmp_path / "data" / "session.secret").mkdir(parents=True)  # a directory: IsADirectoryError, an OSError
    with pytest.raises(ConfigError, match="session secret is unreadable"):
        auth._local_secret(cfg)


@pytest.mark.xfail(strict=True, reason="ROB-coverage-1: an empty <data_dir>/session.secret becomes the signing key "
                                       "'' and forged session cookies are accepted")
def test_an_empty_local_secret_file_is_never_the_signing_key(tmp_path):
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": []})
    path = tmp_path / "data" / "session.secret"
    path.parent.mkdir(parents=True)
    path.write_text("\n")                                        # truncated by hand, a full disk, a bad restore
    try:
        secret = auth._local_secret(cfg)
    except ConfigError:
        return                                                   # refusing to start is also a correct answer
    assert secret.strip(), "the session secret is empty: anyone can sign a session cookie"


# ----- revocation --------------------------------------------------------------------------------------------
def test_revoke_session_and_lookup(tmp_path):
    cfg = _cfg(tmp_path)
    assert not auth.session_revoked(cfg, "s1")                  # no database yet: nothing revoked, nothing created
    assert not (tmp_path / "data" / "sessions.sqlite").exists()
    auth.revoke_session(cfg, "", 100)                            # no sid: a no-op
    assert not (tmp_path / "data" / "sessions.sqlite").exists()
    auth.revoke_session(cfg, "s1", -50)                          # an already-ended session is still kept a minute
    assert auth.session_revoked(cfg, "s1") and not auth.session_revoked(cfg, "s2")


# ----- GitHub calls ------------------------------------------------------------------------------------------
def test_github_host_must_be_configured(tmp_path):
    with pytest.raises(ConfigError, match="web and api"):
        auth._github(_cfg(tmp_path, hosts={"github": {"web": "https://x"}}))
    cfg = _gh_cfg(tmp_path)
    url = auth.authorize_url(cfg, "st8")
    assert url.startswith("https://gh.invalid/login/oauth/authorize?") and "scope=read%3Aorg" in url
    assert "state=st8" in url and "client_id=cid" in url and "secret" not in url
    assert auth.github_org(cfg) == "Org" and auth.github_org(_cfg(tmp_path)) == "SoC-Labs"


@pytest.mark.parametrize("status,body,words", [
    (500, b"{}", "HTTP 500"),
    (200, b"not json", "no token"),
    (200, b'{"error": "bad_verification_code"}', "no token"),
    (200, b'{"access_token": ""}', "no token"),
])
def test_exchange_code_failures_never_carry_the_secret(tmp_path, status, body, words):
    cfg = _gh_cfg(tmp_path)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, content=body)
    with pytest.raises(auth.AuthError, match=words) as ei:
        auth.exchange_code(cfg, "code", client=_client(handler))
    assert "oauth-secret" not in str(ei.value)
    assert seen[0].method == "POST" and b"client_secret=oauth-secret" in seen[0].content


def test_exchange_code_network_error_and_success(tmp_path):
    cfg = _gh_cfg(tmp_path)

    def boom(req):
        raise httpx.ConnectError("down", request=req)
    with pytest.raises(auth.AuthError, match="ConnectError"):
        auth.exchange_code(cfg, "code", client=_client(boom))
    ok = _client(lambda req: httpx.Response(200, json={"access_token": "tok"}))
    assert auth.exchange_code(cfg, "code", client=ok) == "tok"


def test_user_login_and_membership_answers(tmp_path):
    cfg = _gh_cfg(tmp_path)
    assert auth.user_login(cfg, "t", _client(lambda r: httpx.Response(200, json={"login": "ann"}))) == "ann"
    with pytest.raises(auth.AuthError, match="no login"):
        auth.user_login(cfg, "t", _client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(auth.AuthError, match="HTTP 401"):
        auth.user_login(cfg, "t", _client(lambda r: httpx.Response(401)))
    for code in (403, 404):
        assert auth.membership_state(cfg, "t", _client(lambda r, c=code: httpx.Response(c))) is None
    assert auth.membership_state(cfg, "t", _client(lambda r: httpx.Response(200, json={"state": "pending"}))) == "pending"
    with pytest.raises(auth.AuthError, match="HTTP 502"):
        auth.membership_state(cfg, "t", _client(lambda r: httpx.Response(502)))


def test_is_member_reads_only_204_as_member(tmp_path):
    cfg = _gh_cfg(tmp_path)
    urls = []

    def answer(code):
        def handler(req):
            urls.append(str(req.url))
            assert req.headers["Authorization"] == "Bearer read-token"
            return httpx.Response(code)
        return _client(handler)
    assert auth.is_member(cfg, "ann", answer(204)) is True
    assert auth.is_member(cfg, "ann", answer(404)) is False
    assert auth.is_member(cfg, "ann", answer(302)) is False
    for code in (200, 401, 500):                                 # an unanswered question is never "member"
        with pytest.raises(auth.AuthError):
            auth.is_member(cfg, "ann", answer(code))
    auth.is_member(cfg, "a/../b", answer(404))
    assert urls[-1].endswith("/orgs/Org/members/a%2F..%2Fb")       # the login cannot change the path


# ----- the hourly re-check -----------------------------------------------------------------------------------
def _cookie(sessions, **kw):
    return {auth.SESSION_COOKIE: sessions.dump_session(**kw)}


def test_github_recheck_outage_keeps_the_session_and_records_the_attempt(tmp_path):
    cfg, s = _gh_cfg(tmp_path), auth.Sessions("k" * 32)
    calls = []

    def down(config, login):
        calls.append(login)
        raise auth.AuthError("down")
    g = auth.GitHubAuth(check_member=down)
    ident = g.identify(_cookie(s, login="ann", status="member", checked_at=_iso(-2 * 3600)), cfg, s)
    assert ident.status == "member" and calls == ["ann"]
    assert ident.attempt_at and ident.refreshed["attempt_at"] == ident.attempt_at
    # the written-back attempt postpones the next GitHub call by RETRY_S (10 min), not by one request
    again = g.identify({auth.SESSION_COOKIE: s.dump_session(**ident.refreshed)}, cfg, s)
    assert calls == ["ann"] and again.refreshed is None
    later = g.identify(_cookie(s, login="ann", status="member", checked_at=_iso(-2 * 3600),
                               attempt_at=_iso(-auth.RETRY_S - 5)), cfg, s)
    assert calls == ["ann", "ann"] and later.attempt_at


def test_github_recheck_refuses_a_removed_member_and_skips_refused(tmp_path):
    cfg, s = _gh_cfg(tmp_path), auth.Sessions("k" * 32)
    calls = []
    g = auth.GitHubAuth(check_member=lambda c, login: calls.append(login) or False)
    ident = g.identify(_cookie(s, login="ann", status="member", checked_at=_iso(-2 * 3600)), cfg, s)
    assert ident.status == "refused" and ident.refreshed["status"] == "refused" and "attempt_at" not in ident.refreshed
    fresh = g.identify(_cookie(s, login="ann", status="member"), cfg, s)          # checked now: no call
    assert fresh.status == "member" and calls == ["ann"]
    refused = g.identify(_cookie(s, login="bob", status="refused", checked_at=_iso(-9000)), cfg, s)
    assert refused.status == "refused" and calls == ["ann"]                         # never re-asked
    assert g.identify({}, cfg, s) is None and g.identify(_cookie(s, login="a", status="member"), cfg, None) is None
    assert g.identify({auth.SESSION_COOKIE: "x"}, cfg, s) is None


def test_local_auth_refuses_removed_rehashed_revoked_and_refused(tmp_path):
    h = auth.hash_password("pw", **FAST)
    cfg = _cfg(tmp_path, auth={"mode": "local", "users": [{"login": "ann", "hash": h}]})
    s = auth.Sessions("k" * 32)
    la = auth.LocalAuth()
    good = _cookie(s, login="ann", status="member", sid="s1", hash_tag=auth.hash_tag(h))
    assert la.identify(good, cfg, s).via == "local"
    assert la.identify(_cookie(s, login="ann", status="member", sid="s1"), cfg, s) is None          # no hash tag
    assert la.identify(_cookie(s, login="ann", status="refused", hash_tag=auth.hash_tag(h)), cfg, s) is None
    assert la.identify(_cookie(s, login="bob", status="member", hash_tag=auth.hash_tag(h)), cfg, s) is None
    rehash = _cfg(tmp_path, auth={"mode": "local", "users": [{"login": "ann", "hash": h + "x"}]})
    assert la.identify(good, rehash, s) is None                                         # password changed
    auth.revoke_session(cfg, "s1", 100)
    assert la.identify(good, cfg, s) is None                                            # logged out
    assert la.identify(good, cfg, None) is None
    assert auth.ConfiguredAuth().identify(good, cfg, s) is None                         # routes to LocalAuth


def test_fake_membership_needs_a_login():
    with pytest.raises(ValueError):
        auth.FakeMembership("")
    f = auth.FakeMembership("ann")
    assert f.identify({}, None, None).via == "fake" and "ann" in repr(f)
