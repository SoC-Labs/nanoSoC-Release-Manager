"""Coverage angle (rob/coverage): CSRF tokens that are not ASCII (local sign-in, CR-16).

`hmac.compare_digest` raises TypeError when either str holds a non-ASCII character. A CSRF token is attacker
input (a form field), so a wrong token of any alphabet must be a 403, never an unhandled 500."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth
from nanosoc_rm.web import create_app

PASSWORD = "a long enough password"


@pytest.fixture
def local(tmp_path, monkeypatch):
    h = auth.hash_password(PASSWORD, n=2 ** 10, r=1, p=1)
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "ann"\n'
                   'hash = "%s"\n' % (tmp_path / "data", h))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    with TestClient(create_app(cfg), raise_server_exceptions=False) as c:
        yield c


def _form_token(c) -> str:
    r = c.get("/auth/login")
    assert r.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)


def _sign_in(c):
    r = c.post("/auth/login", data={"csrf_token": _form_token(c), "login": "ann", "password": PASSWORD},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def test_login_with_a_wrong_or_missing_ascii_token_is_403(local):
    _form_token(local)
    for token in ("", "x" * 64):
        r = local.post("/auth/login", data={"csrf_token": token, "login": "ann", "password": PASSWORD},
                       follow_redirects=False)
        assert r.status_code == 403 and "expired" in r.text


def test_a_signed_in_post_with_a_wrong_ascii_token_is_403(local):
    _sign_in(local)
    r = local.post("/jobs/1/cancel", data={"csrf_token": "x" * 64}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.xfail(strict=True, reason="ROB-coverage-9: a non-ASCII csrf_token makes hmac.compare_digest raise "
                                       "TypeError: HTTP 500 instead of 403 (web/app.py login_post and the CSRF "
                                       "middleware)")
@pytest.mark.parametrize("where", ["login", "signed-in post"])
def test_a_non_ascii_csrf_token_is_403_not_500(local, where):
    if where == "login":
        _form_token(local)
        r = local.post("/auth/login", data={"csrf_token": "\u00e9", "login": "ann", "password": PASSWORD},
                       follow_redirects=False)
    else:
        _sign_in(local)
        r = local.post("/jobs/1/cancel", data={"csrf_token": "\u00e9"}, follow_redirects=False)
    assert r.status_code == 403
