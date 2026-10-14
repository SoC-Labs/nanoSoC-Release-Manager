"""Robustness, security angle (ROB-security): the web app in local mode and GitHub mode, driven with the TestClient.

What this file holds, and how each check is kept honest:
    route census      every route the app serves (included routers and plug-ins too) refuses an anonymous request,
                      and every write refuses a missing, wrong or foreign CSRF token. A positive control opens a
                      route (or switches CSRF off) and shows the census goes red.
    sessions          a sign-in mints a new session id; logout revokes; a session's CSRF token is bound to it.
    throttle          the per-login sign-in limit, sequential and concurrent.
    path traversal    every download, patch and file route with hostile path segments and query values never
                      serves a planted secret; a control shows the detector finds the marker when it is served.
    injection         hostile pins, refs, component names and job fields never reach a job's argv as an option
                      of their own; a control shows the detector flags an injected option.
    XSS               reflected (every GET route, every path parameter, the common query parameters) and stored
                      (alert messages, registry records); a control echo route shows the scanner goes red.
    headers           CR/LF and off-site `next` never reach the Location header.
    secrets           token files, the session secret, password hashes and credential-bearing URLs never reach a
                      page, a job log or an API answer.
    isolation         gui.sqlite state (inbox marks, review basket) is per login.

A test that exposes a defect is xfail(strict=True) with reason "ROB-security-<n>: ...", so the suite stays green
and the defect stays recorded: when it is fixed, the XPASS fails the run and the marker comes off.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import types
import urllib.parse
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request                     # module level: FastAPI resolves the string annotation
from starlette.responses import HTMLResponse

from nanosoc_rm import auth, db, jobs
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app, routers
from webfix import make_registry, write_config

PASSWORDS = {"alice": "correct horse battery", "carol": "another long secret"}
HASHES = {login: auth.hash_password(pw, salt=b"0123456789abcdef") for login, pw in PASSWORDS.items()}
MARK = "ROBSECRET-7f3a9c"                                 # planted wherever a secret lives; must never be served
XSS = '<script>rob(1)</script>"><img src=x onerror=rob(2)>'
XSS_HITS = ("<script>rob(1)", "<img src=x onerror=rob(2)")


# ---------------------------------------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------------------------------------
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
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    return {"cfg": cfg, "registry": reg, "data": tmp_path / "data", "app": create_app(cfg), "tmp": tmp_path}


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


def signed_in(app, login="alice"):
    c = TestClient(app, raise_server_exceptions=False)          # a crash is an answer (500) the scans report
    assert sign_in(c, login).status_code == 303
    return c


def _fill(path: str, value: str = "x") -> str:
    """A concrete URL for a route path: every {param} (or {param:path}) becomes `value`."""
    return re.sub(r"\{[^}]+\}", value, path)


def census(app):
    """(path, methods) of every route the app serves, included routers and plug-ins flattened."""
    return [(p, set(m)) for p, m, _ in routers.route_table(app)]


def _open(path: str) -> bool:
    return path.startswith(("/auth", "/static"))


def _still_signed_in(client) -> None:
    """A scan that signed itself out half way (GET /auth/logout) would read login redirects from then on and
    pass on nothing: every signed-in scan ends with this."""
    assert client.get("/", follow_redirects=False).status_code == 200, "the scan lost its session"


# ---------------------------------------------------------------------------------------------------------
# 1. Route census: auth on every route, CSRF on every write
# ---------------------------------------------------------------------------------------------------------
LEAKS = ("eth-rc4", "eth-main", "tidelink", "ab73e40924317e45e7b3b941f52a7ed35f20832e")


def anonymous_offenders(app) -> list:
    """Routes an anonymous request gets anything but a refusal from (or a refusal that carries data)."""
    c = TestClient(app, raise_server_exceptions=False)
    bad = []
    for path, methods in census(app):
        if _open(path):
            continue
        url = _fill(path, "eth-rc4")
        for m in sorted(methods - {"HEAD"}):
            r = c.request(m, url, follow_redirects=False)
            want = (303, 401) if m == "GET" and not path.startswith("/api") else (401,)
            if r.status_code not in want or any(s in r.text for s in LEAKS):
                bad.append((m, path, r.status_code))
    return bad


def test_every_route_refuses_an_anonymous_request(local):
    app = local["app"]
    paths = {p for p, _ in census(app)}
    # the census must see the plug-in and workspace routers, or it proves nothing about them
    assert {"/matrix/{configuration}/run", "/releases/{configuration}/{version}/files/{name}",
            "/components/{id}/review/patch.diff", "/inbox/done", "/bumps/p/{pid}/patch.diff"} <= paths
    assert anonymous_offenders(app) == []


def test_control_the_anonymous_census_goes_red_on_an_open_route(local, monkeypatch):
    from nanosoc_rm.web import app as appmod
    monkeypatch.setattr(appmod, "_OPEN_PREFIXES", appmod._OPEN_PREFIXES + ("/matrix", "/inbox"))
    offenders = anonymous_offenders(create_app(local["cfg"]))
    assert any(p.startswith("/matrix") for _, p, _ in offenders) and any(p == "/inbox/done" for _, p, _ in offenders)


def test_a_refused_github_session_gets_403_on_every_route(service, tmp_path, fake_github):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=fake_github.url, registry=reg)
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/auth/login", follow_redirects=False)
    st = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    assert c.get("/auth/callback", params={"code": "bob", "state": st}, follow_redirects=False).status_code == 403
    bad = []
    for path, methods in census(app):
        if _open(path):
            continue
        for m in sorted(methods - {"HEAD"}):
            r = c.request(m, _fill(path, "eth-rc4"), follow_redirects=False)
            if r.status_code != 403 or any(s in r.text for s in LEAKS):
                bad.append((m, path, r.status_code))
    assert bad == []


def csrf_offenders(app, client, foreign_token: str, only=None) -> list:
    """Write routes that answer anything but 403 CSRF to a POST with no, a wrong, or another session's token."""
    bad = []
    for path, methods in census(app):
        if _open(path) or (only is not None and path not in only):
            continue
        for m in sorted(methods - {"GET", "HEAD", "OPTIONS"}):
            url = _fill(path, "eth-rc4")
            for data, headers in (({}, {}), ({"csrf_token": "0" * 64}, {}), ({"csrf_token": foreign_token}, {}),
                                  ({}, {"X-CSRF-Token": foreign_token}), ({"csrf_token": ""}, {"X-CSRF-Token": ""})):
                r = client.request(m, url, data=data, headers=headers, follow_redirects=False)
                if r.status_code != 403 or "CSRF" not in r.text:
                    bad.append((m, path, r.status_code, sorted(data), sorted(headers)))
    return bad


def test_every_write_route_refuses_a_missing_wrong_or_foreign_csrf_token(local):
    app = local["app"]
    writes = routers.writes(app)
    assert len([w for w in writes if not _open(w[1])]) >= 20, sorted(writes)
    carol = signed_in(app, "carol")
    foreign = page_csrf(carol)
    alice = signed_in(app, "alice")
    assert page_csrf(alice) != foreign
    assert csrf_offenders(app, alice, foreign) == []
    assert jobs.jobs(load_config(local["cfg"])) == []                   # nothing was queued on the way


def test_control_the_csrf_census_goes_red_when_the_check_is_off(local, monkeypatch):
    monkeypatch.setattr(auth.ConfiguredAuth, "requires_csrf", False)
    app = create_app(local["cfg"])
    foreign = page_csrf(signed_in(app, "carol"))
    # only routes whose handler is cheap and local once it runs (a refresh route would reach the network)
    offenders = csrf_offenders(app, signed_in(app, "alice"), foreign, only={"/inbox/done", "/compat/jobs"})
    assert any(p == "/inbox/done" for _, p, *_ in offenders) and any(p == "/compat/jobs" for _, p, *_ in offenders)


def test_the_csrf_token_of_a_signed_out_session_is_dead(local):
    """A token is bound to its session id: after sign-out and a new sign-in (same login), the old token fails."""
    c = signed_in(local["app"])
    old = page_csrf(c)
    assert c.post("/auth/logout", data={"csrf_token": old}, follow_redirects=False).status_code == 303
    assert sign_in(c).status_code == 303
    r = c.post("/mirrors/sync", data={"csrf_token": old}, follow_redirects=False)
    assert r.status_code == 403 and "CSRF" in r.text
    assert c.post("/mirrors/sync", data={"csrf_token": page_csrf(c)}, follow_redirects=False).status_code == 303


async def _asgi(app, target: str, method: str = "GET", cookies: str = "") -> tuple[int, bytes]:
    """One request straight through ASGI with a RAW target, as uvicorn hands it on: `path` is the percent-decoded
    raw path with NO dot-segment normalisation (an HTTP client would remove `..` before sending)."""
    out = {"status": None, "body": b""}
    raw, _, query = target.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": urllib.parse.unquote(raw), "raw_path": raw.encode("latin-1"),
             "query_string": query.encode("latin-1"), "root_path": "",
             "headers": [(b"host", b"testserver")] + ([(b"cookie", cookies.encode())] if cookies else []),
             "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
    try:
        await app(scope, receive, send)
    except Exception:                                    # noqa: BLE001 - the app crashed: a 500 for the scans
        out["status"] = out["status"] or 500
    return out["status"], out["body"]


@pytest.mark.parametrize("raw", ["/static/../api/v1/alerts", "/auth/../api/v1/configurations",
                                 "/auth/..%2f..%2fapi/v1/alerts", "/static/%2e%2e/api/v1/mirrors",
                                 "/static/../../api/v1/configurations", "/auth/../configurations/eth-rc4",
                                 "/static//../api/v1/alerts", "/auth/./../facts/x"])
def test_an_open_prefix_with_dot_segments_never_reaches_a_guarded_route(local, raw):
    """The guard lets /auth/* and /static/* through by prefix. A raw path that climbs out of them must not be
    routed to a guarded handler (a real server such as uvicorn does not normalise dot segments)."""
    status, body = asyncio.run(_asgi(local["app"], raw))
    assert status in (303, 401, 404, 405), (raw, status, body[:200])
    assert not any(s.encode() in body for s in LEAKS), (raw, body[:300])


# ---------------------------------------------------------------------------------------------------------
# 2. Sessions: fixation, logout revocation
# ---------------------------------------------------------------------------------------------------------
def _sid(client):
    sessions = auth.Sessions.from_config(load_config(client.app.state.rm.config_path))
    return sessions.load_session(client.cookies.get(auth.SESSION_COOKIE))


def test_sign_in_mints_a_new_session_and_ignores_a_planted_one(local):
    """Session fixation: a cookie planted in the victim's browser (the attacker's own valid session) is replaced
    on sign-in by a new session with a new id; the planted session stays the attacker's and is not upgraded."""
    attacker = signed_in(local["app"], "carol")
    planted = attacker.cookies.get(auth.SESSION_COOKIE)
    carol_sid = _sid(attacker)["sid"]
    victim = TestClient(local["app"])
    victim.cookies.set(auth.SESSION_COOKIE, planted, domain="testserver.local", path="/")   # as the server sets it
    assert sign_in(victim, "alice").status_code == 303
    d = _sid(victim)
    assert d["login"] == "alice" and d["sid"] != carol_sid
    again = TestClient(local["app"])
    again.cookies.set(auth.SESSION_COOKIE, planted)
    assert _sid(again)["login"] == "carol"                               # nothing the victim did reached it
    # a second sign-in of the same browser mints yet another id (no reuse of the previous one)
    assert sign_in(victim, "alice").status_code == 303 and _sid(victim)["sid"] != d["sid"]


def test_logout_needs_the_sessions_csrf_token(local):
    c = signed_in(local["app"])
    copied = c.cookies.get(auth.SESSION_COOKIE)
    r = c.post("/auth/logout", data={}, follow_redirects=False)            # a forged form: no token
    assert r.status_code == 403
    still = TestClient(local["app"])
    still.cookies.set(auth.SESSION_COOKIE, copied)
    assert still.get("/", follow_redirects=False).status_code == 200


def test_local_logout_revokes_every_copy_of_the_cookie(local):
    c = signed_in(local["app"])
    copied = c.cookies.get(auth.SESSION_COOKIE)
    assert c.post("/auth/logout", data={"csrf_token": page_csrf(c)}, follow_redirects=False).status_code == 303
    for method in ("GET", "POST"):
        thief = TestClient(local["app"])
        thief.cookies.set(auth.SESSION_COOKIE, copied)
        r = thief.request(method, "/api/v1/alerts" if method == "GET" else "/mirrors/sync", follow_redirects=False)
        assert r.status_code == 401, (method, r.status_code)


def test_logout_accepts_the_token_as_a_header(local):
    """POST /auth/logout takes the session's CSRF token from X-CSRF-Token as the other session POSTs do."""
    c = signed_in(local["app"])
    copied = c.cookies.get(auth.SESSION_COOKIE)
    r = c.post("/auth/logout", headers={"X-CSRF-Token": page_csrf(c)}, follow_redirects=False)
    assert r.status_code == 303
    thief = TestClient(local["app"])
    thief.cookies.set(auth.SESSION_COOKIE, copied)
    assert thief.get("/api/v1/alerts", follow_redirects=False).status_code == 401


def test_an_empty_secret_never_makes_a_signer():
    """ROB-coverage-1 backstop: Sessions('') is refused whatever path built it ('' is a key anyone can sign with)."""
    with pytest.raises(auth.ConfigError):
        auth.Sessions("")


def test_github_logout_revokes_a_copied_cookie(service):
    c = TestClient(create_app(service["cfg"]))
    r = c.get("/auth/login", follow_redirects=False)
    st = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    assert c.get("/auth/callback", params={"code": "alice", "state": st}, follow_redirects=False).status_code == 303
    copied = c.cookies.get(auth.SESSION_COOKIE)
    form = c.get("/auth/logout")                                          # GET only shows the sign-out button
    assert form.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    assert c.post("/auth/logout", data={"csrf_token": token}).status_code == 200
    thief = TestClient(create_app(service["cfg"]))
    thief.cookies.set(auth.SESSION_COOKIE, copied)
    assert thief.get("/api/v1/alerts").status_code == 401


# ---------------------------------------------------------------------------------------------------------
# 3. Sign-in throttle
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", [" alice", "alice ", "\talice\n"])
def test_the_throttle_cannot_be_dodged_by_padding_the_login(local, variant):
    c = TestClient(local["app"])
    for _ in range(5):
        assert sign_in(c, password="nope").status_code == 401
    token = login_token(c)
    r = c.post("/auth/login", data={"csrf_token": token, "login": variant, "password": PASSWORDS["alice"]},
               follow_redirects=False)
    assert r.status_code == 429, (variant, r.status_code)
    assert auth.SESSION_COOKIE not in r.cookies


async def _concurrent_wrong_passwords(app, n: int) -> list[int]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        async def one():
            r = await ac.post("/auth/login", data={"csrf_token": "t" * 32, "login": "alice", "password": "wrong"},
                              cookies={auth.LOGIN_CSRF_COOKIE: "t" * 32})
            return r.status_code
        return list(await asyncio.gather(*(one() for _ in range(n))))


def test_concurrent_guesses_are_held_to_the_limit(local):
    codes = asyncio.run(_concurrent_wrong_passwords(local["app"], 30))
    assert set(codes) <= {401, 429}, codes
    assert codes.count(401) <= 5, "%d password checks in one window (limit 5)" % codes.count(401)


def test_successful_sign_ins_are_never_throttled(local):
    """Review of ROB-security-3: attempt() reserves a slot before the password check, so a successful sign-in must
    give its slot back (clear). Otherwise a user who signs in six times in five minutes is locked out."""
    for _ in range(6):
        c = TestClient(local["app"])
        assert sign_in(c).status_code == 303
    c = TestClient(local["app"])
    for _ in range(4):
        assert sign_in(c, password="nope").status_code == 401
    assert sign_in(c).status_code == 303                                  # the fifth check, right: in
    for _ in range(4):
        assert sign_in(c, password="nope").status_code == 401             # the success reset the count
    assert sign_in(c).status_code == 303


def test_the_throttle_forgets_logins_whose_window_has_passed(monkeypatch):
    clock_s = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock_s[0])
    t = auth.Throttle(limit=5, window_s=300)
    for i in range(5000):
        t.fail("nobody-%d" % i)
    clock_s[0] += 301                                                     # every window has passed
    t.fail("one-more")
    t.blocked("someone")
    assert len(t._fails) <= 10, len(t._fails)


# ---------------------------------------------------------------------------------------------------------
# 4. Path traversal in every download, patch and file route
# ---------------------------------------------------------------------------------------------------------
HOSTILE = ["..", "%2e%2e", "..%2f..%2fsession.secret", "%2e%2e%2f%2e%2e%2fsession.secret", "..%5c..%5csession.secret",
           "session.secret", "%2fetc%2fpasswd"]
DOWNLOADS = ["/releases/{c}/{v}/files/{n}", "/releases/{c}/{v}/download.zip", "/releases/{c}/{v}/errata",
             "/releases/{c}/{v}", "/quality/{c}/comments.diff", "/quality/{c}/readme.diff", "/quality/{c}",
             "/components/{c}/review/patch.diff", "/bumps/p/{c}/patch.diff", "/facts/{c}", "/bumps/facts/{c}",
             "/jobs/{c}/log", "/matrix/{c}/cell", "/static/{c}", "/versions/{c}"]


def _plant_secrets(data: Path) -> None:
    (data / "session.secret").write_text(MARK + "-session\n")
    (data.parent / "session.secret").write_text(MARK + "-parent\n")
    rel = data / "releases"
    rel.mkdir(exist_ok=True)
    (rel / "session.secret").write_text(MARK + "-releases\n")
    (data / "release-inputs").mkdir(exist_ok=True)
    (data / "release-inputs" / "secret.txt").write_text(MARK + "-inputs\n")


def traversal_urls() -> list[str]:
    """Each download route with each hostile value in each of its path parameters, and once with hostile query
    values (?commit= on the review patch, ?ref= on quality, ?component= on a cell)."""
    urls = []
    for tmpl in DOWNLOADS:
        names = re.findall(r"\{(\w)\}", tmpl)
        for bad in HOSTILE:
            for slot in names:
                vals = {"c": "eth-rc4", "v": "1.0.0", "n": "draft.json", slot: bad}
                urls.append(re.sub(r"\{(\w)\}", lambda m: vals[m.group(1)], tmpl))
        q = HOSTILE[2]
        urls.append(re.sub(r"\{(\w)\}", lambda m: {"c": "tidelink", "v": "1.0.0", "n": "draft.json"}[m.group(1)], tmpl)
                    + "?commit=%s&ref=%s&component=%s" % (q, q, q))
    return list(dict.fromkeys(urls))


def traversal_hits(client, urls=None) -> list:
    """(url, status) of every raw request (signed in as `client`) that served the marker, or crashed (a 500 is a
    robustness defect too; a 503 is a deliberate answer). Raw, so a literal `..` reaches the app as a server
    would pass it."""
    cookie = "%s=%s" % (auth.SESSION_COOKIE, client.cookies.get(auth.SESSION_COOKIE))

    async def run():
        out = []
        for url in urls if urls is not None else traversal_urls():
            status, body = await _asgi(client.app, url, cookies=cookie)
            if status is None or status == 500 or MARK.encode() in body:
                out.append((url, status))
        return out
    return asyncio.run(run())


def test_no_download_or_patch_route_serves_a_file_outside_its_folder(local):
    _plant_secrets(local["data"])
    c = signed_in(local["app"])
    assert traversal_hits(c) == []


def test_control_the_traversal_detector_sees_a_served_marker(local):
    """The detector reads bodies: a job log that holds the marker (a route that legitimately serves a file) is
    found, so an empty traversal list means the marker was really not served."""
    _plant_secrets(local["data"])
    c = signed_in(local["app"])
    cfg = load_config(local["cfg"])
    job = jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, "alice")
    d = jobs.job_dir(cfg, job["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.txt").write_text(MARK + "\n")
    log = "/jobs/%d/log" % job["id"]
    assert traversal_hits(c, [log, "/jobs/%d/log" % (job["id"] + 1)]) == [(log, 200)]


def test_quality_path_and_ref_never_reach_a_directory_or_git_option_from_the_web(local, monkeypatch):
    """`quality --path` is CLI-only: a web target is a [quality] paths name or a catalogue component, never a
    directory, and a hostile ref never reaches git as an option."""
    from nanosoc_rm.quality import source as qsource
    calls = []
    real = qsource._git

    def spy(repo, *args, **kw):
        calls.append((str(repo), args))
        return real(repo, *args, **kw)
    monkeypatch.setattr(qsource, "_git", spy)
    c = signed_in(local["app"])
    cookie = "%s=%s" % (auth.SESSION_COOKIE, c.cookies.get(auth.SESSION_COOKIE))
    for target in ("%2ftmp", "..", "%2e%2e%2f%2e%2e", "~", "%2fetc", "..%2f..%2f..%2ftmp"):
        status, body = asyncio.run(_asgi(local["app"], "/quality/%s" % target, cookies=cookie))
        assert status == 404 and b"not a directory" not in body, (target, status, body[:200])
    for ref in ("--output=/tmp/rob", "-h", "--git-dir=/tmp", "HEAD --all"):
        c.get("/quality/tidelink", params={"ref": ref}, follow_redirects=False)
    assert not [a for _, args in calls for a in args if str(a).startswith("-") and "rob" in str(a)]
    assert jobs.jobs(load_config(local["cfg"]), limit=100) == [] or all(
        not str((j["params"] or {}).get("ref") or "").startswith("-") for j in jobs.jobs(load_config(local["cfg"])))


# ---------------------------------------------------------------------------------------------------------
# 5. Command and argument injection into job argv
# ---------------------------------------------------------------------------------------------------------
VALUE_OPTS = {"--pin", "--tier", "--keep-work", "--component", "--column", "--kind", "--archive", "--gds", "--netlist",
              "--waiver", "--ref"}
FLAGS = {"--store", "--json", "--force-tier", "--replace"}


def injected_options(argv: list[str]) -> list[str]:
    """Tokens of a `nanosoc-rm` argv that the CLI parser would read as an option the tool did not mean to pass:
    any token starting with '-' that is not a known flag and not the value of a known value option."""
    bad, skip = [], False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok in VALUE_OPTS:
            skip = True
        elif tok.startswith("-") and tok not in FLAGS:
            bad.append(tok)
    return bad


def test_control_the_injection_detector_flags_an_option_in_a_positional_slot():
    assert injected_options(["compat", "--keep-work=/tmp/x", "--json"]) == ["--keep-work=/tmp/x"]
    assert injected_options(["compat", "eth-rc4", "--pin", "--keep-work=/x", "--json"]) == []   # a value, not an option


SHA = "036d3811e630c89b2344b56aa617b40a99cd652b"
HOSTILE_POSTS = [
    ("/compat/jobs", {"configuration": "--help"}),
    ("/compat/jobs", {"configuration": "-x"}),
    ("/compat/jobs", {"configuration": "eth-rc4", "pins_extra": "--keep-work=/tmp/rob=%s\n-x=%s" % (SHA, SHA)}),
    ("/compat/jobs", {"configuration": "eth-rc4", "pin": "a=%s --store" % SHA}),
    ("/compat/jobs", {"configuration": "eth-rc4", "pin": "a=$(id)"}),
    ("/compat/jobs", {"configuration": "eth-rc4", "tier": "t1 --force-tier"}),
    ("/compat/jobs", {"configuration": "eth-rc4", "tier": "--keep-work"}),
    ("/compat/jobs", {"configuration": "eth-rc4; id"}),
    ("/mirrors/sync", {"component": "--all"}),
    ("/mirrors/sync", {"component": "-h"}),
    ("/mirrors/sync", {"component": "tidelink\n--host"}),
    ("/configurations/--help/walk", {}),
    ("/configurations/-x/runs", {}),
    ("/releases/jobs", {"configuration": "eth-rc4", "version": "1.0.0", "waivers": "a=b\n--replace=x"}),
    ("/releases/jobs", {"configuration": "eth-rc4", "version": "1.0.0 --replace"}),
    ("/releases/jobs", {"configuration": "-x", "version": "1.0.0"}),
    ("/releases/jobs", {"configuration": "eth-rc4", "version": "1.0.0", "archive": "../../rm.toml"}),
    ("/releases/jobs", {"configuration": "eth-rc4", "version": "1.0.0", "kind": "--json"}),
    ("/releases/forks/jobs", {"component": "--all"}),
    ("/releases/eth-rc4/--x/verify", {}),
    ("/matrix/eth-rc4/run", {"component": "--json", "column": "latest-tag"}),
    ("/matrix/--help/run", {"column": "pinned"}),
    ("/matrix/eth-rc4/run", {"column": "--all"}),
    ("/versions/refresh", {"component": "--all"}),
    ("/versions/-x/draft", {}),
    ("/bumps/scan", {"configuration": "--all"}),
    ("/compat/jobs", {"configuration": "eth-rc4", "pin": "a/b=%s" % SHA}),          # a legitimate job: the
]                                                                                   # detector sees a real argv


def test_hostile_job_fields_never_become_an_option_or_a_file_argument(local):
    c = signed_in(local["app"])
    token = page_csrf(c)
    cfg = load_config(local["cfg"])
    for path, data in HOSTILE_POSTS:
        r = c.post(path, data=dict(data, csrf_token=token), follow_redirects=False)
        assert r.status_code < 500, (path, data, r.status_code, r.text[:200])
    bad = []
    queued = jobs.jobs(cfg, limit=200)
    assert queued, "no job was queued at all: the argv check below would read nothing"
    for j in queued:
        argv = jobs.cli_args(j["kind"], j["params"])
        bad += [(j["kind"], j["params"], t) for t in injected_options(argv)]
        # a release input can only be a file under <data_dir>/release-inputs
        for k in ("archive", "gds", "netlist"):
            v = (j["params"] or {}).get(k)
            assert not v or str(v).startswith(str(cfg.data_dir / "release-inputs")), (j["params"], k)
        assert "$(" not in json.dumps(j["params"]) and "\n" not in "".join(argv), j
    assert bad == []


# ---------------------------------------------------------------------------------------------------------
# 6. XSS: reflected and stored
# ---------------------------------------------------------------------------------------------------------
QUERY_KEYS = ("q", "next", "ref", "configuration", "component", "sha", "kind", "scope", "verdict", "commit", "cat",
              "show", "target", "column", "i", "job", "signed_out", "version", "subject")


def xss_hits(client, app, prefix: str = "/") -> list:
    """(url, status) of every GET under `prefix` whose HTML carries the payload unescaped, or that crashed."""
    hits = []
    enc = urllib.parse.quote(XSS, safe="")
    for path, methods in census(app):
        if "GET" not in methods or path.startswith("/static") or path == auth.LOGOUT_PATH \
                or not path.startswith(prefix):
            continue
        urls = [_fill(path, enc)] if "{" in path else []
        urls.append(_fill(path, "eth-rc4") + "?" + urllib.parse.urlencode({k: XSS for k in QUERY_KEYS}))
        for url in urls:
            r = client.get(url, follow_redirects=False)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 500 or ("html" in ctype and any(h in r.text for h in XSS_HITS)):
                hits.append((url[:120], r.status_code))
    _still_signed_in(client)
    return hits


def test_no_route_reflects_markup_from_its_path_or_query(local):
    c = signed_in(local["app"])
    assert xss_hits(c, local["app"]) == []


def test_control_the_xss_scanner_flags_a_raw_echo(local, monkeypatch):
    from fastapi import APIRouter
    router = APIRouter(prefix="/quality")

    def echo(request: Request):
        return HTMLResponse("<p>%s</p>" % request.query_params.get("q", ""))
    router.add_api_route("/echo", echo, methods=["GET"])
    mod = types.ModuleType("nanosoc_rm.quality.web")
    mod.router = router
    monkeypatch.setitem(sys.modules, "nanosoc_rm.quality.web", mod)
    app = create_app(local["cfg"])
    hits = xss_hits(signed_in(app), app, prefix="/quality")
    assert any(u.startswith("/quality/echo") for u, _ in hits), hits


def test_the_login_form_escapes_the_login_and_next_it_echoes(local):
    c = TestClient(local["app"])
    token = login_token(c)
    r = c.post("/auth/login", data={"csrf_token": token, "login": XSS, "password": "x", "next": XSS})
    assert r.status_code == 401 and not any(h in r.text for h in XSS_HITS)
    r = c.get("/auth/login", params={"next": "/" + XSS})
    assert not any(h in r.text for h in XSS_HITS)


def _stored_payloads(cfg_path: Path, reg: Path) -> None:
    """Plant the payload where people and repos write text: an alert message, a component id in an alert, and the
    registry's erratum and defect free text (titles, conditions, workaround rules)."""
    cfg = load_config(cfg_path)
    db.raise_alert("mirror_failed", "tidelink", "fetch failed: %s" % XSS,
                   [{"kind": "git", "command": "git fetch %s" % XSS}], config=cfg)
    db.raise_alert("mirror_failed", "rob-comp", "x " + XSS, [{"kind": "git", "command": "git ls-remote"}], config=cfg)
    for rel in ("defects/SL-DEF-0001.yaml", "errata/eth-rc4/ERR-001.yaml"):
        p = reg / rel
        doc = json.loads(p.read_text())

        def taint(o):
            if isinstance(o, dict):
                return {k: (v + " " + XSS if isinstance(v, str) and k in (
                    "title", "conditions", "rules", "evidence", "summary", "description", "notes", "detail")
                    else taint(v)) for k, v in o.items()}
            if isinstance(o, list):
                return [taint(v) for v in o]
            return o
        p.write_text(json.dumps(taint(doc), indent=1) + "\n")
    import subprocess
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "commit", "-qam", "payload " + XSS], cwd=reg, env=env, check=True)


def test_stored_text_from_alerts_and_registry_records_is_escaped_on_every_page(local):
    _stored_payloads(local["cfg"], local["registry"])
    c = signed_in(local["app"])
    shown = 0
    urls = [_fill(p, "eth-rc4") for p, m in census(local["app"]) if "GET" in m and not _open(p)]
    urls += ["/defects/SL-DEF-0001", "/components/tidelink", "/facts/alert:mirror_failed:rob-comp",
             "/search?q=rob-comp", "/components/rob-comp"]
    for url in dict.fromkeys(urls):
        r = c.get(url, follow_redirects=False)
        if "html" not in r.headers.get("content-type", ""):
            continue
        assert not any(h in r.text for h in XSS_HITS), url
        shown += "rob(1)" in r.text                                        # the payload reached the page, escaped
    _still_signed_in(c)
    assert shown >= 3, "the payload reached %d pages: the planting did not take, so this proves nothing" % shown


# ---------------------------------------------------------------------------------------------------------
# 7. Header injection and open redirect
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("nxt", ["/x\r\nSet-Cookie: rob=1", "/x%0d%0aSet-Cookie:%20rob=1", "//evil.example/",
                                 "/\\evil.example", "https://evil.example/", "/\t/evil.example", "\\/evil.example",
                                 " //evil.example", "/%2f%2fevil.example", "/x\nLocation: //evil.example"])
def test_sign_in_never_redirects_off_site_or_splits_the_header(local, nxt):
    c = TestClient(local["app"])
    r = sign_in(c, next_path=nxt)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "\r" not in loc and "\n" not in loc and "rob=1" not in r.headers.get("set-cookie", "")
    u = urllib.parse.urlsplit(loc.replace("\\", "/").replace("\t", ""))
    assert not u.netloc and not u.scheme and not loc.replace("\\", "/").replace("\t", "").lstrip().startswith("//"), loc


def test_job_redirects_and_downloads_carry_no_hostile_header_text(local):
    c = signed_in(local["app"])
    token = page_csrf(c)
    for path in ("/configurations/eth-rc4%0d%0aSet-Cookie:%20rob=1/walk", "/configurations/eth-rc4%0aX:1/walk"):
        r = c.post(path, data={"csrf_token": token}, follow_redirects=False)
        assert "rob=1" not in r.headers.get("set-cookie", "") and "x" not in {k.lower() for k in r.headers if k == "X"}
        assert r.status_code in (400, 404), (path, r.status_code)


# ---------------------------------------------------------------------------------------------------------
# 8. Secrets never reach a page, a log or an API answer
# ---------------------------------------------------------------------------------------------------------
def _every_get(client, app, values=("eth-rc4",)) -> dict[str, str]:
    out = {}
    for path, methods in census(app):
        if "GET" not in methods or path.startswith("/static") or path == auth.LOGOUT_PATH:
            continue
        for v in values:
            url = _fill(path, v)
            if url in out:
                continue
            out[url] = client.get(url, follow_redirects=False).text
    _still_signed_in(client)
    return out


def test_local_mode_secrets_never_reach_a_page_or_api_answer(local):
    data = local["data"]
    token_file = local["tmp"] / "gitlab-read.token"
    token_file.write_text(MARK + "-token\n")
    token_file.chmod(0o600)
    extra = ('[hosts.gitlab]\nweb = "https://gitlab.invalid"\napi = "https://gitlab.invalid/api/v4"\n'
             'ssh = "git@gitlab.invalid:"\ngroup = "soclabs"\n'
             'private_read = { method = "token", token_file = "%s" }\n' % token_file)
    local_config(local["cfg"], data, local["registry"], extra=extra)
    app = create_app(local["cfg"])
    c = signed_in(app)
    secret = (data / "session.secret").read_text().strip()
    pages = _every_get(c, app)
    assert len(pages) >= 30
    for url, text in pages.items():
        for s in (MARK, secret, HASHES["alice"], HASHES["carol"], PASSWORDS["alice"], "scrypt$"):
            assert s not in text, (url, s[:12])


def test_github_mode_tokens_and_session_secret_never_reach_a_page(service, tmp_path, fake_github):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=fake_github.url, registry=reg)
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    c = TestClient(app)
    pages = _every_get(c, app)
    assert len(pages) >= 30
    for url, text in pages.items():
        for s in ("fake-github_read", "fake-github_oauth", "fake-session", "fake-token-"):
            assert s not in text, (url, s)


CRED_URL = "https://oauth2:%s@gitlab.invalid/soclabs/x.git" % MARK


def test_a_credential_url_in_an_alert_is_redacted_everywhere_it_is_shown(local):
    db.raise_alert("mirror_failed", "tidelink", "fetch %s failed" % CRED_URL,
                   [{"kind": "git", "command": "git ls-remote <gitlab>"}], config=load_config(local["cfg"]))
    c = signed_in(local["app"])
    assert MARK not in c.get("/alerts").text                               # the page that redacts
    shown = ("/", "/alerts", "/components/tidelink", "/mirrors", "/facts/alert:mirror_failed:tidelink",
             "/api/v1/alerts", "/search?q=tidelink")
    leaks = [u for u in shown if MARK in c.get(u).text]
    assert leaks == []


def test_a_job_log_carries_no_token_from_the_config(local, monkeypatch):
    """The worker hands a child the config path, never a token: a job's log and command show no secret."""
    monkeypatch.setenv("FAKE_MODE", "ok")
    token_file = local["tmp"] / "gitlab-read.token"
    token_file.write_text(MARK + "-token\n")
    extra = ('[hosts.gitlab]\nweb = "https://gitlab.invalid"\napi = "https://gitlab.invalid/api/v4"\n'
             'ssh = "git@gitlab.invalid:"\ngroup = "soclabs"\n'
             'private_read = { method = "token", token_file = "%s" }\n' % token_file)
    local_config(local["cfg"], local["data"], local["registry"], extra=extra)
    c = signed_in(local["app"])
    c.post("/mirrors/sync", data={"csrf_token": page_csrf(c), "component": "tidelink"})
    fake = [sys.executable, str(Path(__file__).resolve().parents[1] / "jobs" / "fake_cli.py")]
    assert jobs.Worker(local["cfg"], argv_prefix=fake).step() == 1
    cfg = load_config(local["cfg"])
    job = jobs.get(cfg, 1)
    assert job["state"] in ("done", "failed", "infra"), job["state"]
    text = c.get("/jobs/1/log").text + c.get("/jobs/1/fragment").text + json.dumps(job)
    assert "nanosoc-rm mirror sync" in text and MARK not in text


# ---------------------------------------------------------------------------------------------------------
# 9. Per-user isolation of gui.sqlite
# ---------------------------------------------------------------------------------------------------------
def test_inbox_marks_are_per_login_through_real_sessions(local):
    from nanosoc_rm.web import uistate
    alice, carol = signed_in(local["app"], "alice"), signed_in(local["app"], "carol")
    page = alice.get("/").text
    forms = re.findall(r'<form[^>]*action="/inbox/done".*?</form>', page, re.S)
    assert forms, "the inbox shows no card with a Mark done form: nothing to isolate"
    card = re.search(r'name="card" value="([^"]+)"', forms[0]).group(1)
    fp = re.search(r'name="fp" value="([^"]+)"', forms[0]).group(1)
    r = alice.post("/inbox/done", data={"csrf_token": page_csrf(alice), "card": card, "fp": fp},
                   follow_redirects=False)
    assert r.status_code == 303
    cfg = load_config(local["cfg"])
    assert card in uistate.card_state(cfg, "alice") and card not in uistate.card_state(cfg, "carol")
    assert 'name="card" value="%s"' % card in carol.get("/").text            # carol still sees the card
    # carol's undo of alice's card changes nothing of alice's
    carol.post("/inbox/undo", data={"csrf_token": page_csrf(carol), "card": card}, follow_redirects=False)
    assert card in uistate.card_state(cfg, "alice")
    assert (uistate.path(cfg).stat().st_mode & 0o777) == 0o600
