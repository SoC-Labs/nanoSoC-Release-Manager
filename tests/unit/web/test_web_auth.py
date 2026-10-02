"""Sign-in, membership, roles, sessions and CSRF (interfaces.md section 14; A5, H2)."""
from __future__ import annotations

import base64
import json
import urllib.parse
import zlib

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.routing import Route

from nanosoc_rm import auth, clock
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app
from webfix import write_config

RC4 = "ab73e40924317e45e7b3b941f52a7ed35f20832e"


def sign_in(client, login, *, state=None):
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    st = q["state"][0] if state is None else state
    return q, client.get("/auth/callback", params={"code": login, "state": st}, follow_redirects=False)


def replace_cookie(client, name, value):
    """Swap a cookie the server set for `value`, keeping its domain and path (one cookie, not two)."""
    old = [c for c in client.cookies.jar if c.name == name]
    assert len(old) == 1, old
    client.cookies.delete(name, domain=old[0].domain, path=old[0].path)
    client.cookies.set(name, value, domain=old[0].domain, path=old[0].path)


def cookie_text(value):
    """Every readable form of the signed cookie (the payload is base64, maybe zlib)."""
    out = [value]
    for part in value.split("."):
        raw = part.lstrip(".")
        try:
            b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError:
            continue
        out.append(b.decode("utf-8", "replace"))
        try:
            out.append(zlib.decompress(b).decode("utf-8", "replace"))
        except zlib.error:
            pass
    return out


# ----- login and callback -----------------------------------------------------------------------------------
def test_login_asks_github_for_read_org_only_with_state(service):
    c = TestClient(create_app(service["cfg"]))
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    u = urllib.parse.urlsplit(r.headers["location"])
    q = urllib.parse.parse_qs(u.query)
    assert u.path == "/login/oauth/authorize" and u.netloc == urllib.parse.urlsplit(service["github"].url).netloc
    assert q["scope"] == ["read:org"] and q["client_id"] == ["fake-client-id"] and len(q["state"][0]) >= 24
    assert auth.STATE_COOKIE in c.cookies


def test_active_member_gets_a_session_and_opens_pages(service):
    c = TestClient(create_app(service["cfg"]))
    _, cb = sign_in(c, "alice")
    assert cb.status_code == 303 and cb.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/api/v1/configurations").status_code == 200


def test_login_returns_to_the_local_page_asked_for_only(service):
    c = TestClient(create_app(service["cfg"]))
    r = c.get("/silicon-protection", follow_redirects=False)
    assert r.status_code == 303 and urllib.parse.urlsplit(r.headers["location"]).path == "/auth/login"
    nxt = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["next"][0]
    r = c.get("/auth/login", params={"next": nxt}, follow_redirects=False)
    st = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    cb = c.get("/auth/callback", params={"code": "alice", "state": st}, follow_redirects=False)
    assert cb.headers["location"] == "/silicon-protection"
    c2 = TestClient(create_app(service["cfg"]))
    r = c2.get("/auth/login", params={"next": "//evil.example/x"}, follow_redirects=False)
    st = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    assert c2.get("/auth/callback", params={"code": "alice", "state": st}, follow_redirects=False).headers["location"] == "/"


@pytest.mark.parametrize("who", ["bob", "carol"])
def test_non_member_and_pending_invitee_are_refused_everywhere(service, who):
    c = TestClient(create_app(service["cfg"]))
    _, cb = sign_in(c, who)
    assert cb.status_code == 403
    for path in ("/", "/silicon-protection", "/api/v1/configurations", "/facts/x", "/no-such-page"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 403, (who, path, r.status_code)
        assert RC4 not in r.text and "eth-rc4" not in r.text
    assert c.post("/configurations/eth-rc4/runs", data={"csrf_token": "x"}).status_code == 403


def test_forged_or_missing_state_is_refused(service):
    c = TestClient(create_app(service["cfg"]))
    _, cb = sign_in(c, "alice", state="forged")
    assert cb.status_code == 403
    fresh = TestClient(create_app(service["cfg"]))                  # no state cookie at all
    assert fresh.get("/auth/callback", params={"code": "alice", "state": "x"}, follow_redirects=False).status_code == 403
    assert not any(k == auth.SESSION_COOKIE for k in fresh.cookies)


def test_user_token_is_stored_nowhere(service):
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    c.get("/")
    texts = [t for v in c.cookies.values() for t in cookie_text(v)]
    assert any('"alice"' in t for t in texts), "the decoder must read the session payload (else the check is blind)"
    assert not any("fake-token-alice" in t for t in texts)
    dbfile = load_config(service["cfg"]).db_path
    assert b"fake-token-alice" not in dbfile.read_bytes()


def test_empty_role_table_refuses_a_member(service, tmp_path, fake_github):
    cfg = write_config(tmp_path / "noroles.toml", data=tmp_path / "data", github_url=fake_github.url, roles={})
    c = TestClient(create_app(cfg))
    _, cb = sign_in(c, "alice")
    assert cb.status_code == 403
    assert c.get("/", follow_redirects=False).status_code == 403


def test_roles_come_from_the_config_per_login(service, tmp_path, fake_github):
    cfg = write_config(tmp_path / "per.toml", data=tmp_path / "data", github_url=fake_github.url,
                       roles={"alice": ["member"]})
    a, d = TestClient(create_app(cfg)), TestClient(create_app(cfg))
    assert sign_in(a, "alice")[1].status_code == 303
    assert sign_in(d, "dave")[1].status_code == 403                     # active member, but no role here
    assert a.get("/", follow_redirects=False).status_code == 200


def test_roles_are_read_per_request(service, tmp_path, fake_github):
    """A member signed in, then removed from the role table, is refused on the next request."""
    cfg = write_config(tmp_path / "live.toml", data=tmp_path / "data", github_url=fake_github.url)
    c = TestClient(create_app(cfg))
    sign_in(c, "alice")
    assert c.get("/", follow_redirects=False).status_code == 200
    write_config(cfg, data=tmp_path / "data", github_url=fake_github.url, roles={"default": []})
    assert c.get("/", follow_redirects=False).status_code == 403


def test_github_failure_is_502_and_no_session(service, monkeypatch):
    c = TestClient(create_app(service["cfg"]))
    r = c.get("/auth/login", follow_redirects=False)
    st = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    (service["tmp"] / "tokens" / "github_oauth").write_text("wrong-secret\n")
    cb = c.get("/auth/callback", params={"code": "alice", "state": st}, follow_redirects=False)
    assert cb.status_code == 502 and "wrong-secret" not in cb.text
    assert auth.SESSION_COOKIE not in c.cookies


def test_without_a_session_secret_nobody_signs_in(service, tmp_path, fake_github):
    cfg = write_config(tmp_path / "nosecret.toml", data=tmp_path / "data", github_url=fake_github.url, session=False)
    c = TestClient(create_app(cfg))
    assert c.get("/auth/login", follow_redirects=False).status_code == 503
    assert c.get("/", follow_redirects=False).status_code == 303          # still anonymous: to the login page


# ----- anonymous ---------------------------------------------------------------------------------------------
def test_anonymous_is_sent_to_login_or_refused(service):
    c = TestClient(create_app(service["cfg"]))
    r = c.get("/configurations/eth-rc4", follow_redirects=False)
    assert r.status_code == 303 and urllib.parse.urlsplit(r.headers["location"]).path == "/auth/login"
    assert "eth-rc4" not in r.text
    assert c.get("/api/v1/configurations").status_code == 401
    assert c.post("/configurations/eth-rc4/runs").status_code == 401
    assert c.post("/api/v1/configurations/eth-rc4/runs").status_code == 401


def test_tampered_session_cookie_is_anonymous(service):
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    value = c.cookies[auth.SESSION_COOKIE]
    replace_cookie(c, auth.SESSION_COOKIE, value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    assert c.get("/", follow_redirects=False).status_code == 303


def test_session_expires_after_eight_hours(service, monkeypatch):
    import itsdangerous.timed
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    assert c.get("/", follow_redirects=False).status_code == 200
    real = itsdangerous.timed.time.time
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: real() + auth.SESSION_MAX_AGE_S + 5)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_a_route_added_after_construction_is_guarded(service):
    app = create_app(service["cfg"])
    app.router.routes.append(Route("/late", lambda request: JSONResponse({"commit": RC4})))
    anon = TestClient(app)
    assert anon.get("/late", follow_redirects=False).status_code == 303
    bob = TestClient(app)
    sign_in(bob, "bob")
    r = bob.get("/late")
    assert r.status_code == 403 and RC4 not in r.text
    alice = TestClient(app)
    sign_in(alice, "alice")
    assert alice.get("/late").status_code == 200


def test_static_is_open_and_holds_no_data_or_templates(service):
    app = create_app(service["cfg"])
    mounts = [r for r in app.routes if getattr(getattr(r, "app", None), "directory", None)]
    assert [m.path for m in mounts] == ["/static"]
    r = TestClient(app).get("/static/rm.css")
    assert r.status_code == 200 and "{{" not in r.text and "{%" not in r.text and "eth-rc4" not in r.text


# ----- membership re-check -------------------------------------------------------------------------------------
def _age_session(client, cfg, hours):
    sessions = auth.Sessions.from_config(load_config(cfg))
    d = sessions.load_session(client.cookies[auth.SESSION_COOKIE])
    old = clock.iso(clock.now() - __import__("datetime").timedelta(hours=hours))
    replace_cookie(client, auth.SESSION_COOKIE, sessions.dump_session(d["login"], d["status"], d["sid"], old))


def test_membership_is_rechecked_hourly_and_a_removed_member_is_refused(service):
    gh = service["github"]
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    _age_session(c, service["cfg"], 0.5)
    n = len([x for x in gh.log if x[1].startswith("/orgs/")])
    assert c.get("/", follow_redirects=False).status_code == 200
    assert len([x for x in gh.log if x[1].startswith("/orgs/")]) == n, "re-checked before an hour passed"
    gh.states["alice"] = None                                          # removed from the org
    _age_session(c, service["cfg"], 2)
    assert c.get("/", follow_redirects=False).status_code == 403
    gh.states["alice"] = "active"                                      # GitHub would now say yes again...
    assert c.get("/", follow_redirects=False).status_code == 403, \
        "...but the refusal was written back to the session: she signs in again to be let in"


def test_recheck_that_github_cannot_answer_keeps_the_session(service):
    gh = service["github"]
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    gh.member_check_status = 500
    _age_session(c, service["cfg"], 2)
    assert c.get("/", follow_redirects=False).status_code == 200


def test_is_member_reads_the_org_membership_with_the_service_token(service):
    config = load_config(service["cfg"])
    assert auth.is_member(config, "alice") is True
    assert auth.is_member(config, "bob") is False
    assert auth.is_member(config, "carol") is False                    # pending is not a member
    service["github"].member_check_status = 500
    with pytest.raises(auth.AuthError):
        auth.is_member(config, "alice")


def test_roles_for():
    cfg = {"roles": {"default": ["member"], "alice": ["admin"]}}
    assert auth.roles_for(cfg, "alice") == {"member", "admin"} and auth.roles_for(cfg, "x") == {"member"}
    assert auth.roles_for({"roles": {}}, "alice") == set()


# ----- CSRF ----------------------------------------------------------------------------------------------------
def _form(html, cid):
    import re
    m = re.search(r'<form data-run-t0 method="post" action="/configurations/%s/runs".*?name="csrf_token" value="([0-9a-f]+)"'
                  % re.escape(cid), html, re.S)
    return m.group(1) if m else None


def test_session_post_needs_its_csrf_token(service, monkeypatch, fake_registry_module):
    from nanosoc_rm.web import views
    calls = []

    def fake_run(config, cid, login):
        calls.append((cid, login))
        raise views.TriggerUnavailable("test: trigger not called")
    monkeypatch.setattr(views, "run_t0", fake_run)
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    token = _form(c.get("/").text, "eth-rc4")
    assert token, "the dashboard has no Run form with a CSRF token"
    assert c.post("/configurations/eth-rc4/runs", data={}).status_code == 403
    assert c.post("/configurations/eth-rc4/runs", data={"csrf_token": "0" * 64}).status_code == 403
    assert c.post("/api/v1/configurations/eth-rc4/runs").status_code == 403
    assert calls == []
    assert c.post("/configurations/eth-rc4/runs", data={"csrf_token": token}).status_code == 503
    assert c.post("/api/v1/configurations/eth-rc4/runs", headers={"X-CSRF-Token": token}).status_code == 503
    assert calls == [("eth-rc4", "alice"), ("eth-rc4", "alice")]
    other = TestClient(create_app(service["cfg"]))
    sign_in(other, "dave")
    assert other.post("/configurations/eth-rc4/runs", data={"csrf_token": token}).status_code == 403, \
        "a token is bound to its session"


def test_fake_membership_is_a_member_still_subject_to_roles(service, tmp_path, fake_github):
    c = TestClient(create_app(service["cfg"], auth=auth.FakeMembership(login="bob")))
    assert c.get("/", follow_redirects=False).status_code == 200      # the seam trusts whoever it names
    cfg = write_config(tmp_path / "none.toml", data=tmp_path / "data", github_url=fake_github.url, roles={})
    c = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    assert c.get("/", follow_redirects=False).status_code == 403
    with pytest.raises(ValueError):
        auth.FakeMembership(login="")


def test_logout_ends_the_session(service):
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "alice")
    assert c.get("/auth/logout").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 303


def test_refusal_bodies_echo_nothing(service):
    c = TestClient(create_app(service["cfg"]))
    sign_in(c, "bob")
    r = c.get("/configurations/eth-rc4-secret-path")
    assert r.status_code == 403 and "secret-path" not in r.text
    assert json.loads(TestClient(create_app(service["cfg"])).get("/api/v1/runs").text)["error"]
