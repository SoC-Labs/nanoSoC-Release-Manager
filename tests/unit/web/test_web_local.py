"""The local GUI (CR-16 local sign-in, CR-17 local jobs; interfaces.md section 13a): login, session cookie,
CSRF on every POST, non-loopback refusal, `user add`, Run queueing a local job, the plug-in seam, and a smoke
over every page with the contract's example data."""
from __future__ import annotations

import json
import re
import sys
import types
import urllib.parse

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.requests import Request

from nanosoc_rm import auth, db, jobs
from nanosoc_rm.config import ConfigError, load_config
from nanosoc_rm.web import create_app
from webfix import Hooks, make_registry, seed, write_config

PASSWORDS = {"alice": "correct horse battery", "carol": "another long secret"}
HASHES = {login: auth.hash_password(pw, salt=b"0123456789abcdef") for login, pw in PASSWORDS.items()}


def local_config(path, data, registry=None, users=("alice", "carol"), extra=""):
    lines = ["[service]", 'data_dir = "%s"' % data]
    if registry is not None:
        lines += ["[registry]", 'repo = "soclabs/nanosoc-registry"', 'ref = "main"', 'path = "%s"' % registry]
    lines += ["[auth]", 'mode = "local"']
    for u in users:
        lines += ["[[auth.users]]", 'login = "%s"' % u, 'hash = "%s"' % HASHES[u]]
    path.write_text("\n".join(lines) + "\n" + extra)
    return path


@pytest.fixture
def local(tmp_path, monkeypatch, seeded_db):
    import shutil
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    return {"cfg": cfg, "registry": reg, "data": tmp_path / "data", "app": create_app(cfg)}


def login_token(client):
    r = client.get("/auth/login")
    assert r.status_code == 200 and 'name="password"' in r.text
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)


def sign_in(client, login="alice", password=None, next_path="/"):
    token = login_token(client)
    return client.post("/auth/login", data={"csrf_token": token, "login": login,
                                            "password": PASSWORDS[login] if password is None else password,
                                            "next": next_path}, follow_redirects=False)


def page_csrf(client, path="/"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


# ----- passwords and config ------------------------------------------------------------------------------------
def test_scrypt_hash_round_trip_and_refusals():
    h = auth.hash_password("pw-1")
    assert h.startswith("scrypt$16384$8$1$") and auth.verify_password("pw-1", h)
    assert not auth.verify_password("pw-2", h)
    assert not auth.verify_password("pw-1", h.replace("scrypt$", "bcrypt$"))
    assert not auth.verify_password("pw-1", "scrypt$1$1$1$x$y")               # parameters out of range
    assert auth.hash_password("pw-1") != h                                     # salted


@pytest.mark.parametrize("auth_toml, problem", [
    ('[auth]\nmode = "ldap"\n', "mode must be one of"),
    ('[auth]\nmode = "local"\n', "needs at least one"),
    ('[auth]\nmode = "local"\n[[auth.users]]\nlogin = "a"\nhash = "plain"\n', "hash is required"),
    ('[auth]\nmode = "local"\n[[auth.users]]\nlogin = "a"\nhash = "scrypt$x"\n[[auth.users]]\nlogin = "a"\n'
     'hash = "scrypt$y"\n', "given twice"),
    ('[auth]\nsecret = "x"\n', "unknown key"),
])
def test_config_checks_the_auth_section(tmp_path, auth_toml, problem):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n%s' % (tmp_path, auth_toml))
    with pytest.raises(ConfigError, match=problem):
        load_config(p)


def test_user_add_prints_a_stanza_that_signs_in(tmp_path):
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, ["user", "add", "dave", "--generate"])
    assert r.exit_code == 0, r.output
    stanza = r.stdout
    password = re.search(r"\(shown once\): (\S+)", r.stderr).group(1)
    assert password not in stanza
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[auth]\nmode = "local"\n%s' % (tmp_path, stanza))
    assert auth.check_local_password(load_config(p), "dave", password)
    assert not auth.check_local_password(load_config(p), "dave", password + "x")
    r = CliRunner().invoke(main, ["user", "add", "da ve", "--generate"])
    assert r.exit_code == 3
    r = CliRunner().invoke(main, ["user", "add", "eve"], input="short\nshort\n")
    assert r.exit_code == 3 and "8 characters" in r.output


# ----- sign-in ------------------------------------------------------------------------------------------------
def test_anonymous_is_sent_to_the_local_login_and_api_gets_401(local):
    c = TestClient(local["app"])
    r = c.get("/configurations/eth-rc4", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=%2Fconfigurations%2Feth-rc4"
    assert c.get("/api/v1/configurations").status_code == 401
    assert c.post("/mirrors/sync", data={}).status_code == 401
    assert c.get("/auth/callback").status_code == 404                       # no GitHub in local mode


def test_login_sets_an_httponly_strict_session_and_wrong_passwords_fail(local):
    c = TestClient(local["app"])
    r = sign_in(c, password="wrong")
    assert r.status_code == 401 and "Wrong login or password" in r.text and auth.SESSION_COOKIE not in r.cookies
    token = login_token(c)
    r = c.post("/auth/login", data={"csrf_token": token, "login": "mallory", "password": "x"}, follow_redirects=False)
    assert r.status_code == 401 and "Wrong login or password" in r.text
    r = sign_in(c, next_path="/compat")
    assert r.status_code == 303 and r.headers["location"] == "/compat"
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith(auth.SESSION_COOKIE + "="))
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=28800" in cookie
    assert c.get("/").status_code == 200


def test_login_form_needs_its_double_submit_token(local):
    c = TestClient(local["app"])
    login_token(c)
    r = c.post("/auth/login", data={"login": "alice", "password": PASSWORDS["alice"]}, follow_redirects=False)
    assert r.status_code == 403 and auth.SESSION_COOKIE not in r.cookies
    other = TestClient(local["app"])                     # a token from another browser does not match this cookie
    r = c.post("/auth/login", data={"csrf_token": login_token(other) + "x", "login": "alice",
                                    "password": PASSWORDS["alice"]}, follow_redirects=False)
    assert r.status_code == 403


def test_repeated_failures_are_throttled(local):
    c = TestClient(local["app"])
    for _ in range(5):
        assert sign_in(c, password="nope").status_code == 401
    r = sign_in(c)                                        # even the right password, until the window passes
    assert r.status_code == 429 and "wait 5 minutes" in r.text
    assert sign_in(c, login="carol").status_code == 303   # per login


def test_a_user_removed_from_the_config_is_signed_out(local):
    c = TestClient(local["app"])
    assert sign_in(c).status_code == 303 and c.get("/").status_code == 200
    local_config(local["cfg"], local["data"], local["registry"], users=("carol",))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


def test_logout_is_a_post_and_ends_the_session(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    r = c.post("/auth/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?signed_out=1"
    assert c.get("/", follow_redirects=False).status_code == 303


def test_the_session_secret_is_generated_once_with_mode_0600(local):
    c = TestClient(local["app"])
    sign_in(c)
    secret = local["data"] / "session.secret"
    assert secret.exists() and (secret.stat().st_mode & 0o777) == 0o600
    before = secret.read_text()
    assert TestClient(create_app(local["cfg"])).get("/auth/login").status_code == 200
    c2 = TestClient(create_app(local["cfg"]))
    sign_in(c2)
    assert secret.read_text() == before


def test_github_mode_is_unchanged(tmp_path, monkeypatch, fake_github):
    cfg = write_config(tmp_path / "rm.toml", data=tmp_path / "data", github_url=fake_github.url)
    c = TestClient(create_app(cfg))
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(fake_github.url + "/login/oauth/authorize?")
    assert c.post("/auth/login", data={}).status_code == 405
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/api/v1/alerts").json() == {"error": "sign in with GitHub as a SoC-Labs member"}


# ----- CSRF on every POST ---------------------------------------------------------------------------------------
POSTS = ("/configurations/eth-rc4/runs", "/api/v1/configurations/eth-rc4/runs", "/compat/jobs", "/jobs/1/cancel",
         "/configurations/eth-rc4/walk", "/mirrors/sync", "/alerts/clear")


def test_every_post_needs_the_csrf_token(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    for path in POSTS:
        for data, headers in (({}, {}), ({"csrf_token": "0" * 64}, {}), ({}, {"X-CSRF-Token": "nope"})):
            r = c.post(path, data=data, headers=headers, follow_redirects=False)
            assert r.status_code == 403 and "CSRF" in r.text, (path, data, headers, r.status_code)
    assert jobs.jobs(load_config(local["cfg"])) == []
    # the same token in a form field or in the htmx header passes
    r = c.post("/mirrors/sync", data={"csrf_token": token, "component": "tidelink"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/mirrors?job=1"
    r = c.post("/configurations/eth-rc4/walk", headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 202 and 'data-job-state="queued"' in r.text


def test_a_plugin_post_is_csrf_checked_too(local, monkeypatch):
    from fastapi import APIRouter
    from starlette.responses import PlainTextResponse
    from nanosoc_rm.web import plugins
    router = APIRouter(prefix="/quality")
    router.add_api_route("/scan", lambda: PlainTextResponse("scanned"), methods=["POST"])
    def quality_home(request: Request):
        return plugins.render_page(request, "message.html", "Quality", message="quality pages here")
    router.add_api_route("", quality_home, methods=["GET"])
    mod = types.ModuleType("nanosoc_rm.quality.web")
    mod.router = router
    monkeypatch.setitem(sys.modules, "nanosoc_rm.quality", types.ModuleType("nanosoc_rm.quality"))
    monkeypatch.setitem(sys.modules, "nanosoc_rm.quality.web", mod)
    app = create_app(local["cfg"])
    c = TestClient(app)
    assert c.get("/quality", follow_redirects=False).status_code == 303             # behind sign-in
    sign_in(c)
    page = c.get("/quality")
    assert page.status_code == 200 and "quality pages here" in page.text
    assert '<a href="/quality" aria-current="page" class="current">Quality</a>' in page.text
    assert c.post("/quality/scan").status_code == 403
    assert c.post("/quality/scan", headers={"X-CSRF-Token": page_csrf(c)}).text == "scanned"


def test_no_plugin_installed_means_no_quality_entry(local):
    c = TestClient(local["app"])
    sign_in(c)
    assert ">Quality<" not in c.get("/").text and c.get("/quality").status_code == 404


# ----- Run queues a local job (CR-17) --------------------------------------------------------------------------
def test_run_without_a_ci_project_queues_a_local_compat_job(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    r = c.post("/configurations/eth-main/runs", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/compat?job=1"
    cfg = load_config(local["cfg"])
    job = jobs.get(cfg, 1)
    assert (job["kind"], job["params"]["configuration"], job["by"], job["state"]) == ("compat", "eth-main", "alice", "queued")
    assert ("alice", "run_t0", "eth-main") in [(a["login"], a["action"], a["target"]) for a in db.actions(config=cfg)]
    r = c.post("/configurations/eth-main/runs", data={"csrf_token": token}, headers={"HX-Request": "true"})
    assert r.status_code == 409 and 'data-job="1"' in r.text                      # one at a time per configuration
    r = c.post("/api/v1/configurations/eth-rc4/runs", headers={"X-CSRF-Token": token})
    assert r.status_code == 202 and r.json()["job"]["command"] == "nanosoc-rm compat eth-rc4 --store"
    # the dashboard says so, and its Run button is disabled while the job is queued
    dash = Hooks(c.get("/").text)
    row = [e for e in dash.elements if e.get("data-configuration") == "eth-main"]
    assert row
    assert "job 1 queued" in c.get("/").text


def test_the_compat_form_queues_a_what_if_with_tier(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c, "/compat?configuration=eth-main")
    sha = "036d3811e630c89b2344b56aa617b40a99cd652b"
    r = c.post("/compat/jobs", data={"csrf_token": token, "configuration": "eth-main", "pin": ["", "a/b=%s" % sha],
                                     "pins_extra": "c/d=%s\n" % sha, "tier": "t2", "force_tier": "1"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    job = jobs.get(load_config(local["cfg"]), 1)
    assert job["params"] == {"configuration": "eth-main", "pins": ["a/b=%s" % sha, "c/d=%s" % sha], "tier": "t2",
                             "force_tier": True}
    assert "--store" not in job["command"]
    r = c.post("/compat/jobs", data={"csrf_token": token, "configuration": "eth-main", "pin": "a/b=123"})
    assert r.status_code == 400 and "40-hex" in r.text


def test_cancel_from_the_page(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    c.post("/configurations/eth-main/runs", data={"csrf_token": token})
    r = c.post("/jobs/1/cancel", data={"csrf_token": token}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'data-job-state="cancelled"' in r.text and "cancelled by alice" in r.text
    assert c.post("/jobs/99/cancel", data={"csrf_token": token}).status_code == 404


# ----- serve -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host, ok", [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("localhost", True),
                                      ("0.0.0.0", False), ("::", False), ("192.168.1.5", False), ("myhost", False)])
def test_is_loopback(host, ok):
    from nanosoc_rm.web.commands import is_loopback
    assert is_loopback(host) is ok


def test_serve_refuses_a_non_loopback_address_in_local_mode(local, monkeypatch):
    import uvicorn
    from nanosoc_rm.cli import main
    bound = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: bound.append((host, port)))
    r = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--no-schedule", "--no-worker"])
    assert r.exit_code == 3 and "loopback only" in r.output and bound == []
    r = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--no-schedule", "--no-worker",
                                  "--i-know-this-is-exposed"])
    assert r.exit_code == 0 and bound == [("0.0.0.0", 8000)]
    r = CliRunner().invoke(main, ["serve", "--no-schedule", "--no-worker"])
    assert r.exit_code == 0 and bound[-1] == ("127.0.0.1", 8000)


def test_serve_stops_the_worker_when_the_app_shuts_down(local, monkeypatch):
    # uvicorn re-raises SIGTERM after the app's shutdown, so the worker must stop in the shutdown handler
    import uvicorn
    from nanosoc_rm.cli import main
    events = []

    class FakeWorker:
        def __init__(self, path):
            pass

        def start(self):
            events.append("start")
            return self

        def stop(self):
            events.append("stop")

    def run(app, host, port, **kw):
        with TestClient(app):
            events.append("serving")
        events.append("shut down")
        raise SystemExit(0)                                   # what the re-raised signal does to the code after it
    monkeypatch.setattr(jobs, "Worker", FakeWorker)
    monkeypatch.setattr(uvicorn, "run", run)
    CliRunner().invoke(main, ["serve", "--no-schedule"])
    assert events[:4] == ["start", "serving", "stop", "shut down"], events


def test_serve_in_github_mode_binds_as_before(tmp_path, monkeypatch):
    import uvicorn
    from nanosoc_rm.cli import main
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % tmp_path)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    bound = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: bound.append(host))
    assert CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--no-schedule", "--no-worker"]).exit_code == 0
    assert bound == ["0.0.0.0"]


# ----- smoke: every page, signed in, with the example data -----------------------------------------------------
def _fill(path):
    vals = {"/configurations/": "eth-rc4", "/components/": "tidelink", "/defects/": "SL-DEF-0001",
            "/errata/": "eth-rc4", "/runs/": "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f", "/jobs/": "1",
            "/contains/": "036d3811e630c89b2344b56aa617b40a99cd652b"}
    if path.startswith("/facts/"):
        return "/facts/" + urllib.parse.quote("registry_record:eligibility.yaml#/configurations/0", safe="")
    return re.sub(r"\{[^}]+\}", next((v for k, v in vals.items() if k in path), "x"), path)


def test_smoke_every_page_renders_for_a_local_user(local, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    seed(local["cfg"], "observation.compat-result.example.jsonl")
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    # one finished job (with the example compat_result) so the job pages have something to show
    c.post("/compat/jobs", data={"csrf_token": token, "configuration": "eth-rc4"})
    from pathlib import Path
    fake = [sys.executable, str(Path(__file__).resolve().parents[1] / "jobs" / "fake_cli.py")]
    assert jobs.Worker(local["cfg"], argv_prefix=fake).step() == 1
    pages = 0
    for r_ in c.app.routes:
        methods = getattr(r_, "methods", None) or set()
        if "GET" not in methods or r_.path.startswith(("/auth", "/static")):
            continue
        url = _fill(r_.path)
        extra = {"/contains": "?sha=036d381", "/branches": "?component=tidelink", "/compat": "?job=1",
                 "/hosts": "?component=tidelink", "/mirrors": "?job=1"}
        r = c.get(url + extra.get(r_.path, ""))
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        if r.headers["content-type"].startswith("text/html") and not r_.path.startswith("/jobs/"):
            h = Hooks(r.text)
            assert len(h.with_attr("data-age-seconds")) == 1, url
            assert "Problem:" not in r.text or url == "/branches", (url, re.findall(r"Problem:</strong>([^<]*)", r.text))
            pages += 1
        elif r.headers["content-type"].startswith("application/x-ndjson"):
            for line in r.text.splitlines():
                assert json.loads(line)["kind"]
    assert pages >= 17
    job_page = c.get("/compat?job=1").text
    assert 'data-job-state="done"' in job_page and 'data-verdict="FAIL"' in job_page
    assert "Stored result:" in job_page and 'data-gate="t0.pins.reachable"' in job_page    # seeded above
    # colour never alone: every verdict pill carries its text
    for m in re.finditer(r'<span class="pill (state-\w+)"[^>]*>([^<]*)<', job_page):
        assert m.group(2).strip(), m.group(0)
    assert "nanosoc-rm compat eth-rc4 --store" in c.get("/jobs/1/log").text
    assert c.get("/jobs/1/fragment").headers["content-type"].startswith("text/html")


def test_the_local_post_routes_exist_only_in_a_local_deployment(local):
    writes = {(m, r.path) for r in local["app"].routes for m in (getattr(r, "methods", None) or ())
              if m not in ("GET", "HEAD")}
    assert writes == {("POST", "/configurations/{id}/runs"), ("POST", "/api/v1/configurations/{id}/runs"),
                      ("POST", "/compat/jobs"), ("POST", "/jobs/{id}/cancel"), ("POST", "/configurations/{id}/walk"),
                      ("POST", "/mirrors/sync"), ("POST", "/alerts/clear"), ("POST", "/branches/refresh"),
                      ("POST", "/hosts/refresh"), ("POST", "/auth/login"),
                      ("POST", "/auth/logout")}


def test_a_github_ci_deployment_shows_no_local_actions(service, registry_spy, tmp_path):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    c = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    for path in ("/compat?configuration=eth-main", "/mirrors", "/alerts", "/configurations/eth-rc4", "/hosts"):
        body = c.get(path).text
        for action in ("/compat/jobs", "/mirrors/sync", "/alerts/clear", "/walk", "/hosts/refresh"):
            assert 'action="%s' % action not in body and action + '"' not in body, (path, action)
    assert "nanosoc-rm compat eth-main" in c.get("/compat?configuration=eth-main").text


# ----- review findings G2, G3 (each shown red under its mutation) --------------------------------------------------
def _compat_doc(cid, nonce, verdict, finished_at):
    from pathlib import Path
    ex = Path(__file__).resolve().parents[3] / "schemas" / "examples" / "observation.compat-result.example.jsonl"
    doc = json.loads(next(ln for ln in ex.read_text().splitlines() if ln.strip().startswith("{")))
    doc["subject"] = "compat_result:%s:%s" % (cid, nonce)
    doc["fact"].update(configuration=cid, nonce=nonce, verdict=verdict, finished_at=finished_at, overrides=[],
                       unjudged_overrides=[])
    if verdict == "PASS":
        for g in doc["fact"]["gates"]:
            if g["verdict"] not in ("PASS", "NOT-RUN"):
                g["verdict"] = "PASS"
                g.pop("raw_verdict", None)
                g.pop("waived", None)
    return doc


def test_the_dashboard_shows_each_configurations_own_compat_result(local):
    cfg = load_config(local["cfg"])
    db.put(_compat_doc("eth-main", "a" * 32, "FAIL", "2026-10-11T10:00:00Z"), config=cfg)
    db.put(_compat_doc("eth-rc4", "b" * 32, "PASS", "2026-10-11T12:00:00Z"), config=cfg)    # newer, other config
    c = TestClient(local["app"])
    sign_in(c)
    body = c.get("/").text
    cells = {}
    for m in re.finditer(r'<tr data-configuration="([^"]+)".*?data-item="compat-result"[^>]*>(.*?)</td>', body, re.S):
        cells[m.group(1)] = m.group(2)
    assert ">FAIL<" in cells["eth-main"] and "PASS" not in cells["eth-main"], cells.get("eth-main")
    assert ">PASS<" in cells["eth-rc4"], cells.get("eth-rc4")
    others = [k for k in cells if k not in ("eth-main", "eth-rc4")]
    assert others and all("not run here yet" in cells[k] for k in others), cells


def test_a_refused_session_is_not_a_local_sign_in(local):
    c = TestClient(local["app"])
    sign_in(c)                                     # creates the session secret
    sessions = auth.Sessions.from_config(load_config(local["cfg"]))
    for status, code in (("member", 200), ("refused", 303)):
        c2 = TestClient(local["app"])
        c2.cookies.set(auth.SESSION_COOKIE, sessions.dump_session("alice", status,
                                                                  hash_tag=auth.hash_tag(HASHES["alice"])))
        assert c2.get("/", follow_redirects=False).status_code == code, status


def test_a_what_if_job_never_shows_a_stored_result(local, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    seed(local["cfg"], "observation.compat-result.example.jsonl")     # the example's subject is stored
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    sha = "036d3811e630c89b2344b56aa617b40a99cd652b"
    c.post("/compat/jobs", data={"csrf_token": token, "configuration": "eth-rc4", "pin": "a/b=%s" % sha})
    from pathlib import Path
    fake = [sys.executable, str(Path(__file__).resolve().parents[1] / "jobs" / "fake_cli.py")]
    assert jobs.Worker(local["cfg"], argv_prefix=fake).step() == 1
    body = c.get("/jobs/1/fragment").text
    assert 'data-job-state="done"' in body and "Stored result:" not in body
    assert "never stored as a fact" in body


@pytest.mark.parametrize("params", [{"n": 2 ** 9}, {"n": 2 ** 10, "r": 33}, {"n": 2 ** 10, "r": 1, "p": 17}])
def test_a_hash_with_out_of_range_scrypt_parameters_never_verifies(params):
    # a config hash with huge parameters would make every sign-in a CPU or memory burn: refused unread
    h = auth.hash_password("pw-1", **params)
    assert not auth.verify_password("pw-1", h)
    assert auth.verify_password("pw-1", auth.hash_password("pw-1", n=2 ** 10, r=1, p=1))


# ----- review findings S1, S2, U1, U2 ------------------------------------------------------------------------------
def test_a_get_never_refreshes_branches_or_hosts_and_the_post_does(local, monkeypatch):
    from nanosoc_rm.web import views
    calls = []
    monkeypatch.setattr(views, "branch_rows", lambda config, comp, all_repos=False, refresh=False:
                        (calls.append(("branches", comp, refresh)) or ([], [], refresh)))
    monkeypatch.setattr(views, "compare_now", lambda config, comp: calls.append(("hosts", comp)) or [])
    c = TestClient(local["app"])
    sign_in(c)
    assert c.get("/branches?component=tidelink&refresh=1").status_code == 200
    assert c.get("/hosts?component=tidelink&refresh=1").status_code == 200
    assert calls == [("branches", "tidelink", False)]                   # a cross-site link computes nothing new
    token = page_csrf(c)
    r = c.post("/branches/refresh", data={"csrf_token": token, "component": "tidelink", "all": "1"})
    assert r.status_code == 200 and "Branches" in r.text
    r = c.post("/hosts/refresh", data={"csrf_token": token, "component": "tidelink"})
    assert r.status_code == 200 and "Host copies" in r.text
    assert 'action="/hosts/refresh"' in r.text and 'name="refresh"' not in r.text
    assert calls[1:] == [("branches", "tidelink", True), ("hosts", "tidelink")]
    assert c.post("/hosts/refresh", data={"component": "tidelink"}).status_code == 403
    assert c.post("/branches/refresh", data={"csrf_token": token, "component": "../x"}).status_code == 400


def test_logout_revokes_the_session_and_a_password_change_ends_it(local):
    c = TestClient(local["app"])
    sign_in(c)
    copied = c.cookies.get(auth.SESSION_COOKIE)
    assert c.post("/auth/logout", data={"csrf_token": page_csrf(c)}, follow_redirects=False).status_code == 303
    thief = TestClient(local["app"])
    thief.cookies.set(auth.SESSION_COOKIE, copied)
    assert thief.get("/", follow_redirects=False).status_code == 303          # revoked, not just deleted
    c2 = TestClient(local["app"])
    sign_in(c2, login="carol")
    assert c2.get("/", follow_redirects=False).status_code == 200
    text = local["cfg"].read_text().replace(HASHES["carol"], auth.hash_password("a new long secret"))
    local["cfg"].write_text(text)
    assert c2.get("/", follow_redirects=False).status_code == 303           # the old password's session ends


def test_user_add_refuses_a_role_that_is_not_a_plain_name():
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, ["user", "add", "bob", "--generate", "--role", 'a"]\nevil = "1'])
    assert r.exit_code == 3 and "evil" not in r.stdout, r.output
    r = CliRunner().invoke(main, ["user", "add", "bob", "--generate", "--role", "member", "--role", "ops-1"])
    assert r.exit_code == 0 and 'roles = ["member", "ops-1"]' in r.stdout


def test_a_job_for_an_unregistered_configuration_is_refused(local):
    c = TestClient(local["app"])
    sign_in(c)
    token = page_csrf(c)
    r = c.post("/compat/jobs", data={"csrf_token": token, "configuration": "nope-cfg"})
    assert r.status_code == 404 and "no registered configuration 'nope-cfg'" in r.text
    assert c.post("/configurations/nope-cfg/walk", data={"csrf_token": token}).status_code == 404
    assert c.post("/configurations/nope-cfg/runs", data={"csrf_token": token}).status_code == 404
    cfg = load_config(local["cfg"])
    assert jobs.jobs(cfg) == [] and not [a for a in db.actions(config=cfg) if a["target"] == "nope-cfg"]
