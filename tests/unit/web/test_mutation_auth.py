"""Mutation-killing tests for auth (robustness angle: mutation), and one defect it surfaced.

Scripted AST mutation over nanosoc_rm/auth.py (2026-10-14) against test_web_auth.py. Each test names the
mutant it kills. Pure functions are tested directly; nothing here starts the web app."""
from __future__ import annotations

import pytest

from nanosoc_rm import auth


def _local(users, roles=None):
    return {"auth": {"mode": "local", "users": users}, "roles": roles or {}}


@pytest.mark.xfail(strict=True, reason="ROB-mutation-1: a local user with an explicit `roles = []` is still a member")
def test_a_local_user_with_an_explicit_empty_role_list_has_no_role():
    """Section 14: "No role means refused". roles_for reads `user.get("roles") or [MEMBER_ROLE]`, so an explicit
    empty list (an admin taking a user's access away without deleting the entry) falls back to `member`."""
    cfg = _local([{"login": "alice", "hash": "scrypt$x", "roles": []}])
    assert auth.roles_for(cfg, "alice") == set()


def test_a_local_user_without_roles_is_a_member_and_listed_roles_replace_it():
    """The documented default (GUI.md: no [roles] needed, a local user is a member). Kills `or -> and`."""
    assert auth.roles_for(_local([{"login": "alice", "hash": "h"}]), "alice") == {"member"}
    assert auth.roles_for(_local([{"login": "alice", "hash": "h", "roles": ["admin"]}]), "alice") == {"admin"}
    assert auth.roles_for(_local([{"login": "alice", "hash": "h"}]), "bob") == set()
    assert auth.roles_for({"auth": {"mode": "github", "users": [{"login": "alice", "hash": "h"}]}}, "alice") == set()


# ---- passwords -------------------------------------------------------------------------------------------------
def _parts(encoded):
    return encoded.split("$")


def test_hash_round_trip_and_wrong_password():
    h = auth.hash_password("pw", n=2 ** 10)
    assert auth.verify_password("pw", h) and not auth.verify_password("pW", h)
    assert auth.verify_password("pw", auth.hash_password("pw", salt=b"s" * 16, n=2 ** 10))


def test_an_empty_password_is_refused():
    with pytest.raises(ValueError):
        auth.hash_password("")


@pytest.mark.parametrize("field, value, ok", [
    (1, str(2 ** 10), True), (1, str(2 ** 10 // 2), False), (1, str(2 ** 21), False),
    (2, "1", True), (2, "32", True), (2, "0", False), (2, "33", False),
    (3, "16", True), (3, "0", False), (3, "17", False),
    (0, "bcrypt", False), (5, "!!notbase64", False),
])
def test_cost_bounds_of_a_stored_hash(field, value, ok):
    """verify_password refuses costs outside [2^10, 2^20] x [1, 32] x [1, 16] and a non-scrypt name. Kills each
    `<=` -> `<` and the `name != scrypt` mutants. A bound is honoured by rehashing with that cost."""
    p = _parts(auth.hash_password("pw", salt=b"s" * 16, n=2 ** 10, r=1, p=1))
    p[field] = value
    if ok:
        n, r, pp = int(p[1]), int(p[2]), int(p[3])
        p = _parts(auth.hash_password("pw", salt=b"s" * 16, n=n, r=r, p=pp))
    assert auth.verify_password("pw", "$".join(p)) is ok


def test_unknown_local_login_never_matches():
    h = auth.hash_password("pw", n=2 ** 10)
    cfg = _local([{"login": "alice", "hash": h}])
    assert auth.check_local_password(cfg, "alice", "pw") is True
    assert auth.check_local_password(cfg, "alice", "no") is False
    assert auth.check_local_password(cfg, "bob", "pw") is False


def test_hash_tag_is_sixteen_hex():
    t = auth.hash_tag("scrypt$x")
    assert len(t) == 16 and t != auth.hash_tag("scrypt$y")


# ---- throttle, session clock ------------------------------------------------------------------------------------
def test_throttle_blocks_at_the_limit_inside_the_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    t = auth.Throttle(limit=3, window_s=10)
    for _ in range(2):
        t.fail("a")
    assert not t.blocked("a")
    t.fail("a")
    assert t.blocked("a") and not t.blocked("b")
    now[0] += 10                                   # exactly window_s later: the fails have aged out (`<`)
    assert not t.blocked("a")
    t.fail("a"), t.fail("a"), t.fail("a")
    t.clear("a")
    assert not t.blocked("a")


def test_session_seconds_left(monkeypatch):
    import datetime as dt
    from nanosoc_rm import clock
    now = clock.parse("2026-10-14T12:00:00Z")
    monkeypatch.setattr(clock, "now", lambda: now)
    iso = lambda s: clock.iso(now - dt.timedelta(seconds=s))
    assert auth.session_seconds_left({"iat": iso(0)}) == auth.SESSION_MAX_AGE_S
    assert auth.session_seconds_left({"iat": iso(100)}) == auth.SESSION_MAX_AGE_S - 100
    assert auth.session_seconds_left({"iat": iso(-60)}) == auth.SESSION_MAX_AGE_S + 60    # skew allowed
    assert auth.session_seconds_left({"iat": iso(-61)}) is None
    assert auth.session_seconds_left({}) is None and auth.session_seconds_left({"iat": "garbage"}) is None


def test_load_session_refuses_bad_status_login_and_age(monkeypatch):
    import datetime as dt
    from nanosoc_rm import clock
    s = auth.Sessions("k" * 32)
    good = s.dump_session("alice", "member")
    assert s.load_session(good)["login"] == "alice"
    assert s.load_session(s.dump_session("alice", "refused"))["status"] == "refused"
    assert s.load_session(s.dump_session("alice", "admin")) is None
    assert s.load_session(s.dump_session("", "member")) is None
    assert s.load_session(good + "x") is None
    old = clock.iso(clock.now() - dt.timedelta(seconds=auth.SESSION_MAX_AGE_S))
    assert s.load_session(s.dump_session("alice", "member", iat=old)) is None              # `left <= 0`
    assert auth.Sessions("other" * 8).load_session(good) is None


def test_csrf_binds_login_and_sid():
    s = auth.Sessions("k" * 32)
    assert s.csrf("a", "1") == s.csrf("a", "1") != s.csrf("a", "2") != s.csrf("b", "1")
    assert auth.Sessions("j" * 32).csrf("a", "1") != s.csrf("a", "1")


def test_oauth_state_round_trip():
    s = auth.Sessions("k" * 32)
    assert s.load_state(s.dump_state("st", "/x")) == {"state": "st", "next": "/x"}
    assert s.load_state(s.dump_state("", "/x")) is None and s.load_state("junk") is None


def test_same_site_and_org_defaults():
    assert auth.same_site({"auth": {"mode": "local"}}) == "strict" and auth.same_site({}) == "lax"
    assert auth.github_org({}) == "SoC-Labs" and auth.github_org({"hosts": {"github": {"org": "X"}}}) == "X"


def test_identity_session_payload_drops_empty_fields():
    i = auth.Identity(login="a", status="member", via="session", sid="s", checked_at="c", iat="i")
    assert i.session() == {"login": "a", "status": "member", "sid": "s", "checked_at": "c", "iat": "i"}
    i.attempt_at = "t"
    assert i.session(status="refused")["attempt_at"] == "t" and i.session(status="refused")["status"] == "refused"


# ---- local mode: secret, revocation, LocalAuth ------------------------------------------------------------------
@pytest.fixture
def local_cfg(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    h = auth.hash_password("pw", n=2 ** 10)
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\nhash = "%s"\n'
                 % (tmp_path / "data", h))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p), h


def test_the_local_secret_is_made_once_0600_and_reused(local_cfg):
    """_local_secret: kills `return value -> None` (both), the negated `if value`, `token_hex(32) -> 33` (length)
    and the 0o600 mode constant."""
    cfg, _h = local_cfg
    s1 = auth._local_secret(cfg)
    path = cfg.data_dir / "session.secret"
    assert len(s1) == 64 and path.read_text() == s1 + "\n" and (path.stat().st_mode & 0o777) == 0o600
    assert auth._local_secret(cfg) == s1
    assert auth.Sessions.from_config(cfg) is not None
    assert not list(cfg.data_dir.glob(".session.secret.*"))


@pytest.mark.xfail(strict=True, reason="ROB-mutation-3: an empty <data_dir>/session.secret becomes the session "
                                       "signing key ('') instead of being replaced")
def test_an_empty_local_secret_file_is_never_used_as_the_key(local_cfg):
    """auth.py _local_secret: an empty file is skipped (`if value`), a new secret is written to a temp file, the
    os.link onto the existing (empty) path raises FileExistsError, and the re-read returns ''. Sessions('') then
    signs and accepts cookies with an empty key. Repro: `: > <data_dir>/session.secret`, start the service."""
    cfg, _h = local_cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "session.secret").write_text("")
    assert auth._local_secret(cfg) != ""


def test_an_unreadable_local_secret_is_a_config_error(local_cfg):
    """Kills `Raise -> pass` on the OSError branch (a directory where the file should be)."""
    from nanosoc_rm.config import ConfigError
    cfg, _h = local_cfg
    (cfg.data_dir / "session.secret").mkdir(parents=True)
    with pytest.raises(ConfigError, match="unreadable"):
        auth._local_secret(cfg)


def test_revocation_round_trip(local_cfg):
    """revoke_session / session_revoked: kills the drop-not guards, `return False -> None`, `is not None -> is`."""
    cfg, _h = local_cfg
    assert auth.session_revoked(cfg, "s1") is False                      # no database yet
    auth.revoke_session(cfg, "", 100)                                     # no sid: nothing written
    assert not (cfg.data_dir / "sessions.sqlite").exists()
    auth.revoke_session(cfg, "s1", 100)
    assert auth.session_revoked(cfg, "s1") is True and auth.session_revoked(cfg, "s2") is False


def _cookie(cfg, h, sessions=None, **kw):
    s = sessions or auth.Sessions.from_config(cfg)
    args = dict(login="alice", status="member", hash_tag=auth.hash_tag(h))
    args.update(kw)
    return {auth.SESSION_COOKIE: s.dump_session(**args)}, s


def test_local_auth_identifies_only_a_current_member_session(local_cfg):
    """LocalAuth.identify: every refusal branch, then the accepted identity. Kills the and/or and negated-test
    mutants on lines 500-510 and `return Identity(...) -> None`."""
    cfg, h = local_cfg
    la = auth.LocalAuth()
    cookies, s = _cookie(cfg, h)
    ident = la.identify(cookies, cfg, s)
    assert (ident.login, ident.status, ident.via) == ("alice", "member", "local") and ident.sid
    assert la.identify(cookies, cfg, None) is None                                    # no sessions
    assert la.identify({}, cfg, s) is None                                            # no cookie
    assert la.identify(_cookie(cfg, h, s, status="refused")[0], cfg, s) is None       # not a member session
    assert la.identify(_cookie(cfg, h, s, login="bob")[0], cfg, s) is None            # not a configured user
    assert la.identify(_cookie(cfg, h, s, hash_tag="0" * 16)[0], cfg, s) is None      # password changed
    auth.revoke_session(cfg, ident.sid, 100)
    assert la.identify(cookies, cfg, s) is None                                       # signed out
    assert auth.ConfiguredAuth().identify(_cookie(cfg, h, s)[0], cfg, s).via == "local"


def test_dump_session_carries_attempt_and_hash_tag_only_when_given():
    s = auth.Sessions("k" * 32)
    d = s.load_session(s.dump_session("a", "member", attempt_at="2026-10-14T00:00:00Z", hash_tag="t"))
    assert d["attempt_at"] == "2026-10-14T00:00:00Z" and d["hash_tag"] == "t"
    d = s.load_session(s.dump_session("a", "member"))
    assert "attempt_at" not in d and "hash_tag" not in d


def test_fake_membership_needs_a_login():
    with pytest.raises(ValueError):
        auth.FakeMembership("")
    f = auth.FakeMembership("x")
    assert f.identify({}, None, None).login == "x" and repr(f) == "FakeMembership(login='x')"
    assert (auth.FakeMembership.requires_csrf, auth.LocalAuth.requires_csrf, auth.GitHubAuth.requires_csrf,
            auth.ConfiguredAuth.requires_csrf) == (False, True, True, True)


# ---- GitHub calls (httpx.MockTransport), GitHubAuth re-check ----------------------------------------------------
GH = "https://gh.example.invalid"


@pytest.fixture
def gh_cfg(tmp_path):
    from nanosoc_rm.config import load_config
    from webfix import write_config
    return load_config(write_config(tmp_path / "rm.toml", data=tmp_path / "data", github_url=GH))


def _client(handler):
    import httpx
    seen = []

    def h(request):
        seen.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(h)), seen


def _resp(status, body=None, text=None):
    import httpx
    if text is not None:
        return lambda r: httpx.Response(status, text=text)
    return lambda r: httpx.Response(status, json=body)


def test_authorize_url(gh_cfg):
    import urllib.parse
    u = auth.authorize_url(gh_cfg, "st")
    base, q = u.split("?", 1)
    assert base == GH + "/login/oauth/authorize"
    assert urllib.parse.parse_qs(q) == {"client_id": ["fake-client-id"], "scope": ["read:org"], "state": ["st"],
                                        "allow_signup": ["false"]}


def test_github_needs_web_and_api(gh_cfg):
    from nanosoc_rm.config import ConfigError
    gh_cfg["hosts"]["github"].pop("api")
    with pytest.raises(ConfigError, match="web and api are required"):
        auth.user_login(gh_cfg, "t", _client(_resp(200, {"login": "a"}))[0])


def test_exchange_code(gh_cfg):
    c, seen = _client(_resp(200, {"access_token": "tok"}))
    assert auth.exchange_code(gh_cfg, "code", c) == "tok"
    assert str(seen[0].url) == GH + "/login/oauth/access_token" and seen[0].method == "POST"
    for handler, words in ((_resp(500, {}), "HTTP 500"), (_resp(200, {}), "no token"),
                           (_resp(200, text="<html>"), "no token"), (_resp(201, {"access_token": "t"}), "HTTP 201")):
        with pytest.raises(auth.AuthError, match=words):
            auth.exchange_code(gh_cfg, "code", _client(handler)[0])


def test_network_failures_are_auth_errors(gh_cfg):
    import httpx

    def boom(r):
        raise httpx.ConnectError("down")
    with pytest.raises(auth.AuthError, match="exchange failed: ConnectError"):
        auth.exchange_code(gh_cfg, "code", _client(boom)[0])
    with pytest.raises(auth.AuthError, match="GET user failed: ConnectError"):
        auth.user_login(gh_cfg, "t", _client(boom)[0])


def test_user_login(gh_cfg):
    c, seen = _client(_resp(200, {"login": "alice"}))
    assert auth.user_login(gh_cfg, "tok", c) == "alice"
    assert str(seen[0].url) == GH + "/user" and seen[0].headers["Authorization"] == "Bearer tok"
    for handler, words in ((_resp(401, {}), "HTTP 401"), (_resp(201, {"login": "a"}), "HTTP 201"),
                           (_resp(200, {}), "no login")):
        with pytest.raises(auth.AuthError, match=words):
            auth.user_login(gh_cfg, "tok", _client(handler)[0])


@pytest.mark.parametrize("status, body, want", [(200, {"state": "active"}, "active"), (200, {"state": "pending"},
                                                "pending"), (403, {}, None), (404, {}, None)])
def test_membership_state(gh_cfg, status, body, want):
    c, seen = _client(_resp(status, body))
    assert auth.membership_state(gh_cfg, "tok", c) == want
    assert seen[0].url.path.endswith("/user/memberships/orgs/%s" % auth.github_org(gh_cfg))


@pytest.mark.parametrize("status", [500, 201, 401])
def test_membership_state_that_decides_nothing_raises(gh_cfg, status):
    with pytest.raises(auth.AuthError):
        auth.membership_state(gh_cfg, "tok", _client(_resp(status, {"state": "active"}))[0])


@pytest.mark.parametrize("status, want", [(204, True), (302, False), (404, False)])
def test_is_member(gh_cfg, status, want):
    import httpx
    c, seen = _client(lambda r: httpx.Response(status))
    assert auth.is_member(gh_cfg, "al ice", c) is want
    assert seen[0].url.raw_path.decode().endswith("/members/al%20ice")
    assert seen[0].headers["Authorization"] == "Bearer fake-github_read"


@pytest.mark.parametrize("status", [200, 205, 500, 403])
def test_is_member_never_reads_an_unexpected_answer_as_member(gh_cfg, status):
    import httpx
    with pytest.raises(auth.AuthError):
        auth.is_member(gh_cfg, "a", _client(lambda r: httpx.Response(status))[0])


def _gh_cookie(sessions, ago_checked=0, ago_attempt=None, status="member"):
    import datetime as dt
    from nanosoc_rm import clock
    t = lambda s: clock.iso(clock.now() - dt.timedelta(seconds=s))
    return {auth.SESSION_COOKIE: sessions.dump_session("alice", status, checked_at=t(ago_checked),
                                                       attempt_at=t(ago_attempt) if ago_attempt is not None else None)}


@pytest.mark.parametrize("checked, attempted, asks", [
    (0, None, False), (auth.RECHECK_S - 5, None, False), (auth.RECHECK_S + 5, None, True),
    (auth.RECHECK_S + 5, auth.RETRY_S - 5, False), (auth.RECHECK_S + 5, auth.RETRY_S + 5, True),
])
def test_github_recheck_is_due_hourly_and_retried_after_ten_minutes(gh_cfg, checked, attempted, asks):
    """`due = (checked is None or > RECHECK_S) and (attempted is None or > RETRY_S)`: kills the and/or and the
    negated `if due`."""
    calls = []
    ga = auth.GitHubAuth(check_member=lambda cfg, login: calls.append(login) or True)
    s = auth.Sessions("k" * 32)
    ident = ga.identify(_gh_cookie(s, checked, attempted), gh_cfg, s)
    assert ident.status == "member" and bool(calls) is asks
    assert (ident.refreshed is not None) is asks


def test_github_recheck_outcomes(gh_cfg):
    s = auth.Sessions("k" * 32)
    old = _gh_cookie(s, auth.RECHECK_S + 5)
    ident = auth.GitHubAuth(check_member=lambda c, l: False).identify(old, gh_cfg, s)
    assert ident.status == "refused" and ident.refreshed["status"] == "refused"

    def down(c, l):
        raise auth.AuthError("down")
    ident = auth.GitHubAuth(check_member=down).identify(old, gh_cfg, s)
    assert ident.status == "member" and ident.attempt_at and ident.refreshed["attempt_at"] == ident.attempt_at
    ident = auth.GitHubAuth(check_member=lambda c, l: True).identify(_gh_cookie(s, auth.RECHECK_S + 5,
                                                                               auth.RETRY_S + 5), gh_cfg, s)
    assert ident.attempt_at == "" and "attempt_at" not in ident.refreshed
    calls = []
    refused = auth.GitHubAuth(check_member=lambda c, l: calls.append(l)).identify(
        _gh_cookie(s, auth.RECHECK_S + 5, status="refused"), gh_cfg, s)
    assert refused.status == "refused" and calls == []
    assert auth.GitHubAuth().identify({}, gh_cfg, s) is None
    assert auth.GitHubAuth().identify(old, gh_cfg, None) is None
    assert auth.GitHubAuth().identify({auth.SESSION_COOKIE: "junk"}, gh_cfg, s) is None
    assert auth.ConfiguredAuth(github=auth.GitHubAuth(check_member=lambda c, l: True)).identify(
        old, gh_cfg, s).via == "session"


# ---- remaining survivors: documented constants and small guards -------------------------------------------------
def test_section_14_numbers():
    """The numbers interfaces.md section 14 / CR-16 state. Kills the constant mutants that every other test reads
    symbolically (8 h session, 10 min state, hourly re-check, 10 min retry, scrypt cost, throttle)."""
    assert (auth.SESSION_MAX_AGE_S, auth.STATE_MAX_AGE_S, auth.RECHECK_S, auth.RETRY_S) == (8 * 3600, 600, 3600, 600)
    assert (auth.SCRYPT_N, auth.SCRYPT_R, auth.SCRYPT_P, auth.SCRYPT_DKLEN) == (2 ** 14, 8, 1, 32)
    assert auth._SCRYPT_MAXMEM == 64 * 1024 * 1024 and auth._HTTP_TIMEOUT_S == 20
    t = auth.Throttle()
    assert (t.limit, t.window_s) == (5, 300.0)
    import base64
    name, n, r, p, salt, dk = auth.hash_password("pw").split("$")
    assert (name, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(base64.b64decode(salt)) == 16 and len(base64.b64decode(dk)) == 32


@pytest.mark.parametrize("kw", [{"n": 2 ** 9}, {"n": 2 ** 10, "r": 33}, {"n": 2 ** 10, "p": 17}])
def test_a_hash_made_outside_the_cost_bounds_never_verifies(kw):
    """Even with the right password: kills `and -> or` in the bounds check."""
    assert auth.verify_password("pw", auth.hash_password("pw", **kw)) is False


def test_local_users_skips_entries_without_a_login():
    cfg = {"auth": {"users": [{"hash": "h"}, "alice", {"login": "", "hash": "h"}, {"login": "bob", "hash": "h"}]}}
    assert list(auth.local_users(cfg)) == ["bob"]


def test_secret_and_revocation_work_when_the_data_dir_exists(local_cfg):
    """mkdir(exist_ok=True) on an existing data_dir. Kills `True -> False` in revoke_session and _local_secret."""
    cfg, _h = local_cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    assert len(auth._local_secret(cfg)) == 64
    auth.revoke_session(cfg, "s1", 10)
    assert auth.session_revoked(cfg, "s1")


def test_a_passed_http_client_is_never_closed(gh_cfg):
    """`own = client is None`: kills `is -> is not` and the negated `if own` (a closed client fails the 2nd call)."""
    c, seen = _client(_resp(200, {"login": "alice", "access_token": "t"}))
    assert auth.user_login(gh_cfg, "t", c) == auth.user_login(gh_cfg, "t", c) == "alice"
    assert auth.exchange_code(gh_cfg, "x", c) == auth.exchange_code(gh_cfg, "x", c) == "t"
    assert len(seen) == 4 and not c.is_closed


def test_sessions_from_config(gh_cfg, tmp_path):
    from nanosoc_rm.config import load_config
    from webfix import write_config
    s = auth.Sessions.from_config(gh_cfg)
    assert isinstance(s, auth.Sessions)
    assert auth.Sessions("fake-session").load_session(s.dump_session("a", "member"))["login"] == "a"
    nosecret = load_config(write_config(tmp_path / "n.toml", data=tmp_path / "d2", github_url=GH, session=False))
    assert auth.Sessions.from_config(nosecret) is None


def test_configured_auth_defaults(gh_cfg):
    ca = auth.ConfiguredAuth()
    assert isinstance(ca.github, auth.GitHubAuth) and isinstance(ca.local, auth.LocalAuth)
    assert ca.identify({}, gh_cfg, auth.Sessions("k" * 32)) is None


def test_identity_repr_hides_the_refreshed_payload():
    i = auth.Identity(login="a", status="member", via="session", refreshed={"sid": "secret-sid"})
    assert "secret-sid" not in repr(i)
