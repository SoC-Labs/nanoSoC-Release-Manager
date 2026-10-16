"""Web hardening from the 2026-10-16 evaluation (implementation.md gaps 5 and 6, report section 3.4 risk 5):
security response headers on every answer, the per-source-address sign-in throttle, the styled 500 page, and a
census proving the templates carry no inline script the CSP would block. Each check has a control that shows
it can fail (PR6)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth
from nanosoc_rm.web import app as appmod
import test_rob_security as rob
from test_rob_security import login_token, sign_in, signed_in

local = rob.local                                         # the robustness suite's local-mode fixture, reused

PKG = Path(appmod.__file__).resolve().parents[1]


# ----- headers ---------------------------------------------------------------------------------------------------
def _headers_ok(r, *, hsts: bool) -> list[str]:
    bad = []
    csp = r.headers.get("content-security-policy", "")
    if "script-src 'self'" not in csp or "frame-ancestors 'none'" not in csp or "unsafe-eval" in csp:
        bad.append("csp %r" % csp)
    if r.headers.get("x-frame-options") != "DENY":
        bad.append("x-frame-options")
    if r.headers.get("x-content-type-options") != "nosniff":
        bad.append("nosniff")
    if ("strict-transport-security" in r.headers) != hsts:
        bad.append("hsts %s" % r.headers.get("strict-transport-security"))
    return bad


def test_every_kind_of_answer_carries_the_security_headers(local):
    anon = TestClient(local["app"])
    answers = {"login form": anon.get("/auth/login"), "static": anon.get("/static/rm.css"),
               "redirect to sign-in": anon.get("/", follow_redirects=False), "api 401": anon.get("/api/v1/configurations")}
    c = signed_in(local["app"])
    answers.update({"page": c.get("/"), "404": c.get("/jobs/999999")})
    bad = {name: _headers_ok(r, hsts=False) for name, r in answers.items() if _headers_ok(r, hsts=False)}
    assert not bad, bad


def test_hsts_only_over_tls(local):
    r = TestClient(local["app"], base_url="https://testserver").get("/auth/login")
    assert _headers_ok(r, hsts=True) == [], r.headers
    assert "max-age=" in r.headers["strict-transport-security"]
    local["app"].state.rm.tls = True                      # serve --tls-cert: HSTS even when the scope reads http
    assert _headers_ok(TestClient(local["app"]).get("/auth/login"), hsts=True) == []


def test_control_the_header_check_goes_red_without_the_headers(local, monkeypatch):
    monkeypatch.setattr(appmod, "_BASE_HEADERS", ())
    r = TestClient(local["app"]).get("/auth/login")
    assert set(_headers_ok(r, hsts=False)) >= {"x-frame-options", "nosniff"}


# ----- no inline script (what makes script-src 'self' safe to send) -----------------------------------------------
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>|\son[a-z]+\s*=\s*[\"']|javascript:", re.I)


def inline_scripts(root: Path) -> list[str]:
    hits = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in (".html", ".py") or "__pycache__" in p.parts:
            continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if _INLINE.search(line) and "noqa: inline-ok" not in line:
                hits.append("%s:%d" % (p.relative_to(root), n))
    return hits


def test_no_template_or_page_builder_has_an_inline_script_or_handler():
    assert inline_scripts(PKG) == []


def test_control_a_planted_inline_handler_is_found(tmp_path):
    (tmp_path / "t.html").write_text('<select onchange="this.form.submit()"></select>\n')
    (tmp_path / "u.html").write_text("<script>\nalert(1)\n</script>\n")
    (tmp_path / "v.py").write_text('X = "<a href=\'javascript:go()\'>"\n')
    (tmp_path / "w.html").write_text('<script src="/static/rm.js" defer></script>\n')
    assert inline_scripts(tmp_path) == ["t.html:1", "u.html:1", "v.py:1"]


# ----- sign-in throttle per source address --------------------------------------------------------------------------
def _fail_from(client, n: int) -> None:
    for i in range(n):                                    # n different unknown logins: the per-login limit never trips
        token = login_token(client)
        r = client.post("/auth/login", data={"csrf_token": token, "login": "guess-%d" % i, "password": "x"},
                        follow_redirects=False)
        assert r.status_code == 401, (i, r.status_code)


def test_one_address_is_held_to_the_limit_across_logins(local):
    attacker = TestClient(local["app"], client=("10.9.9.9", 4000))
    _fail_from(attacker, auth.IP_LIMIT)
    r = sign_in(attacker, "carol")                        # even a right password: this address is held
    assert r.status_code == 429 and "this address" in r.text
    assert auth.SESSION_COOKIE not in r.cookies
    other = TestClient(local["app"], client=("10.1.1.1", 4000))
    assert sign_in(other, "carol").status_code == 303     # another host is not held


def test_a_good_sign_in_gives_back_its_own_slot_only(local):
    c = TestClient(local["app"], client=("10.2.2.2", 4000))
    _fail_from(c, auth.IP_LIMIT - 1)
    for _ in range(3):
        assert sign_in(c, "alice").status_code == 303      # good checks never fill the address's window
    r = sign_in(c, "carol", password="wrong")
    assert r.status_code == 401                            # the IP_LIMIT-th failure
    assert sign_in(c, "alice").status_code == 429          # the failures were not wiped by the sign-ins


def test_control_without_the_address_throttle_the_attacker_gets_in(local):
    local["app"].state.rm.ip_throttle = auth.Throttle(limit=10 ** 6)
    attacker = TestClient(local["app"], client=("10.9.9.9", 4000))
    _fail_from(attacker, auth.IP_LIMIT)
    assert sign_in(attacker, "carol").status_code == 303


# ----- the styled 500 page ---------------------------------------------------------------------------------------------
def _boom_app(local):
    app = local["app"]

    def boom():
        raise RuntimeError("planted fault with a secret-ish detail")
    app.add_api_route("/boom", boom)
    app.add_api_route("/api/v1/boom", boom)
    return app


def test_an_unhandled_error_is_the_services_own_500_page(local):
    c = signed_in(_boom_app(local))
    r = c.get("/boom")
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html")
    assert 'href="/static/rm.css"' in r.text and "<h1>Problem</h1>" in r.text and "RuntimeError" in r.text
    assert "secret-ish" not in r.text and "Traceback" not in r.text
    assert r.text != "Internal Server Error" and _headers_ok(r, hsts=False) == []
    j = c.get("/api/v1/boom")
    assert j.status_code == 500 and "RuntimeError" in j.json()["error"]


def test_a_500_after_a_post_never_claims_nothing_changed():
    """A GET that raised changed nothing; a POST that raised may have written before it failed, so its page must
    not say "nothing was changed" (review of wave-g/quickfix)."""
    get = appmod._server_error({"type": "http", "method": "GET", "path": "/x"}, RuntimeError("x")).body.decode()
    post = appmod._server_error({"type": "http", "method": "POST", "path": "/x"}, RuntimeError("x")).body.decode()
    assert "nothing was changed" in get
    assert "nothing was changed" not in post and "partly applied" in post


def test_control_without_the_handler_the_answer_is_the_bare_text(local, monkeypatch):
    app = _boom_app(local)
    c = signed_in(app)
    monkeypatch.setattr(appmod.Guard, "__call__", appmod.Guard._http)   # the guard without its handlers
    r = c.get("/boom")
    assert r.status_code == 500 and "<h1>Problem</h1>" not in r.text


@pytest.mark.parametrize("path", ["/boom"])
def test_a_500_never_echoes_a_hostile_path(local, path):
    c = signed_in(_boom_app(local))
    r = c.get(path + '?"><script>x</script>')
    assert "<script>x" not in r.text
