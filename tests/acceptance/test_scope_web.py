"""Acceptance tests: scope, web and security (A1-A7, H1, H2, H3, H6). Gate 2 skeletons.

Spec, fixtures, and why each test cannot pass vacuously: docs/acceptance/scope_web.md.
Contracts: docs/contracts/README.md, docs/contracts/interfaces.md, schemas/*.schema.json.

Status: every acceptance test carries NOT_BUILT (xfail, raises=ImportError, strict): XFAIL until the
package exists, red on any other error, red as XPASS(strict) once it passes, so its builder removes the
marker (tests/conftest.py, interfaces.md section 17.4). Imports of the package under test (nanosoc_rm)
sit inside the test bodies, so this file collects before the package exists. The checker-level
negative controls run now, as test_selfcheck_* (coverage review High 3).

Pattern (PR6)
    check_<id>(...) -> list[str]     one pure checker per AT; [] means the requirement is met
    test_AT_<ID>                     gathers the evidence through the contracted interface and
                                     asserts check_<id>(...) == []
    test_AT_<ID>_negative_control    gives the SAME checker a deliberately broken input and asserts
                                     it names the problem. _nc_<id>_checker() is the part that needs
                                     no implementation: it first shows the checker passes on a
                                     consistent input, then that it fails on the broken one.
Modes
    offline      local bare repos and local fake GitHub/GitLab HTTP servers (FakeHost)
    live         on the service VM: NANOSOC_RM_CONFIG, NANOSOC_RM_TEST_MEMBER; marked `live`
    live + Run   also starts one real compat-ci pipeline; under PR8 a person runs these
A live test without its inputs FAILS; it never skips and never passes (as test_ci_errata.py).
Deselect with -m "not live" off the VM.

Nothing here holds a secret or a hostname. Tokens in offline tests are the literal strings
"fake-...", accepted by the fake servers only.
"""
from __future__ import annotations

import ast
import base64
import datetime as dt
import functools
import html as htmlmod
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import zlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

NOT_BUILT = pytest.mark.xfail(raises=ImportError, strict=True, reason="not implemented: gate 2")

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
EXAMPLES = SCHEMAS / "examples"
INVALID = EXAMPLES / "invalid"
REQUIREMENTS_MD = REPO / "docs" / "REQUIREMENTS.md"
SRC_PKG = REPO / "src" / "nanosoc_rm"

# ---------------------------------------------------------------------------------------------
# Real fixtures (observed 2026-09-30 with read-only calls; scope_web.md "Real data used")
# ---------------------------------------------------------------------------------------------
RC4 = "eth-rc4"
RC4_COMMIT = "ab73e40924317e45e7b3b941f52a7ed35f20832e"   # v1.0.0, silicon/eth-chiplet-r0p0
RC4_PIN_COUNT = 44                                        # B2 golden
RC4_SILICON_ROWS = 13                                     # B5 golden rows in observation.example.jsonl:
RC4_SILICON_PINNED = 11                                   #   11 on the pinned host, all covered by keep/eth-rc4/*,
                                                          #   2 on the canonical host, uncovered (interfaces.md section 7)
RC4_DRIFT = {"tidelink", "ahb_qspi", "asic-flow"}         # B6 golden components in rc4's tree
FIX_A = "036d3811e630c89b2344b56aa617b40a99cd652b"         # nanosoc_gen, SL-DEF-0001 form A fix
DEFECT = "SL-DEF-0001"
ERRATUM = "ETH-R0P0-ERR-001"
CI_PROJECT = "soclabs/nanosoc-compat-ci"
CI_PROJECT_ID = 4242                     # CR-3: the fake compat-ci project's numeric id
GITHUB_ORG = "SoC-Labs"
GITLAB_GROUP = "soclabs"
NON_MEMBER = "octocat"                                    # GET orgs/SoC-Labs/members/octocat -> 404
OBSERVED_TOTALS = {"github": 57, "gitlab": 101}           # for the record; tests read the host's total
FOREIGN_REFS = {                                          # advertised, neither heads nor tags (H6)
    ("github", "SoC-Labs/NanoSoC-Ethernet-Chiplet"): "refs/pull/",
    ("github", "SoC-Labs/AHB-QSPI"): "refs/environments/",
}
RC4_T0_EXPECTED_FAIL = {"t0.pins.fetchable", "t0.urls.no-ssh-public"}  # raw verdicts; waivers may make them WAIVED (GATE2 Q1)
EXAMPLE_NONCE = "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f"     # the t0_result in observation.example.jsonl
READ_ONLY_POSTS = ("login/oauth/access_token",          # a POST that writes nothing: OAuth code exchange (§14)
                   "v1/messages")                       # CR-14 (Q4): the optional Claude rewrite, a read-post
B1_CLASSES = {"soc_ref", "example", "generator", "arch_tech", "ip_tech", "toolkit", "infra", "legacy"}
LEAK_STRINGS = (RC4_COMMIT, RC4_COMMIT[:7], RC4, DEFECT, ERRATUM, "keep/eth-rc4")
RED_STATES = {"LOCAL_URL", "NO_GITLINK", "UNREADABLE", "REWRITTEN", "PR_ONLY", "ORPHAN", "MISSING"}
DRIFT_RELATIONS = {"DIVERGED", "AHEAD", "BEHIND", "UNRELATED"}   # H1 host drift (interfaces.md section 13)

# The routes interfaces.md §13 contracts (parameters normalised to {}).
CONTRACT_ROUTES = {
    ("GET", "/"), ("GET", "/configurations/{}"), ("GET", "/components/{}"), ("GET", "/contains"),
    ("GET", "/silicon-protection"), ("GET", "/defects"), ("GET", "/defects/{}"), ("GET", "/errata/{}"),
    ("GET", "/runs/{}"), ("POST", "/configurations/{}/runs"),
    ("GET", "/api/v1/configurations"), ("GET", "/api/v1/configurations/{}/pins"),
    ("GET", "/api/v1/components/{}/hosts"), ("GET", "/api/v1/contains/{}"),
    ("GET", "/api/v1/silicon-protection"), ("GET", "/api/v1/runs"), ("GET", "/api/v1/mirrors"),
    ("GET", "/api/v1/inventory"), ("GET", "/api/v1/alerts"), ("POST", "/api/v1/configurations/{}/runs"),
}

# ---------------------------------------------------------------------------------------------
# Names beyond the schemas, as interfaces.md section 17 fixes them (the gaps of scope_web.md, resolved
# there). Change them HERE, not in the tests.
# ---------------------------------------------------------------------------------------------
ASSUMED = {
    # section 17.2: the app factory and test auth seam shared by all three AT files.
    "create_app": "nanosoc_rm.web:create_app",            # create_app(config_path, auth=None) -> FastAPI
    "fake_membership": "nanosoc_rm.auth:FakeMembership",  # FakeMembership(login=...): signed in as a member
    "load_config": "nanosoc_rm.config:load_config",       # load_config(path) -> config
    "is_member": "nanosoc_rm.auth:is_member",             # G3: is_member(config, login) -> bool
    "write_calls": "nanosoc_rm.contracts:WRITE_CALLS",    # G10
    "gates": "nanosoc_rm.t0.gates:GATES",                 # interfaces.md §9: the shared gate table
    "put": "nanosoc_rm.db:put",                           # interfaces.md §12; finds the db via $NANOSOC_RM_CONFIG
    # section 17.1: config keys.
    "cfg_data_dir": ("service", "data_dir"),
    "cfg_registry": ("registry", "path"),
    "login_path": "/auth/login",                          # G3
    "callback_path": "/auth/callback",                    # G3
    "auth_prefix": "/auth",                               # G3: login, callback, logout live here
    "static_prefix": "/static",                           # G3
    "csrf_field": "csrf_token",                           # G3
    "facts_route": "/facts/{subject}",                    # G5
    "credentials_cli": ("credentials", "check"),          # G9
    "run_action": "run_t0",                               # G11
    "phase_gates": "docs/phase-gates.yaml",               # G12
}

# Page hooks (G4). The contracts fix routes (interfaces.md §13), not markup; tests read only these.
HOOK = {
    "age": "data-age-seconds",
    "oldest": "data-oldest-observed-at",
    "fact": "data-subject",
    "fact_at": "data-observed-at",
    "source": "data-source-for",
    "configuration": "data-configuration",
    "item": "data-item",
    "run": "data-run-t0",
}
DASHBOARD_ITEMS = ("t0-result", "red-pins", "open-errata", "host-drift", "runner-status", "data-age")

# Later-phase surfaces (A7). CR-15 (2026-10-12): they may be BUILT before their entry gate; what the gate still
# governs is ENABLING each phase's automation (LATER_AUTOMATION: the scheduler must not run it).
LATER_PHASES = ("Phase 2", "Phase 3", "Phase 4", "Phase 5")
LATER_AUTOMATION = {"Phase 2": ["nightly_matrix"], "Phase 3": ["tool_release"], "Phase 4": ["bump_bot"],
                    "Phase 5": ["soc_release"]}
CR15_BUILD_FROM = "2026-10-12T00:00:00Z"
LATER_SURFACES = {
    "Phase 2": {"routes": ["/matrix"], "cli": ["matrix"]},
    "Phase 3": {"routes": [], "cli": ["release", "changelog"]},
    "Phase 4": {"routes": ["/bumps"], "cli": ["bump"]},
    "Phase 5": {"routes": ["/releases", "/trains", "/forks"], "cli": ["lock", "forks"]},
}

# Allowed recorded scopes per v1 credential (A2, H3; interfaces.md §1).
V1_CREDENTIALS = {
    "github_read": {"contents:read", "metadata:read", "members:read"},
    "gitlab_read": {"read_api", "read_repository"},
    "ci_trigger": {"trigger"},
    "github_oauth": {"read:org"},
}
LATER_CREDENTIALS = {"bump_writer", "bot_tag"}          # Phase 4 (H3)


def resolve(key):
    mod, _, attr = ASSUMED[key].partition(":")
    return getattr(importlib.import_module(mod), attr)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@functools.lru_cache(maxsize=None)
def _contracts():
    """schemas/check_examples.py: the contracts' own loader and validators (PR9: reuse)."""
    spec = importlib.util.spec_from_file_location("check_examples", SCHEMAS / "check_examples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schemas, registry = mod.load_schemas()
    return mod, schemas, registry


def validate(schema_name, doc, pointer=None):
    mod, schemas, registry = _contracts()
    v = mod.validator_for(schemas, registry, schema_name, pointer)
    return ["%s: %s" % ("/".join(map(str, e.absolute_path)) or "<root>", e.message)
            for e in v.iter_errors(doc)]


def load_docs(path):
    """YAML (strict: no timestamps, no duplicate keys), JSON or JSONL, as check_examples reads them."""
    mod = _contracts()[0]
    return [d for _, d in mod.docs_in(str(path))]


def example(name):
    return load_docs(EXAMPLES / name)


def as_doc(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError("not a contract document: %r" % (obj,))


# Read once at import: offline tests point $NANOSOC_RM_CONFIG at their own config (monkeypatch), and a
# live step inside such a test (the H2 realism probe) must still see the service's real one.
_LIVE_ENV = {k: os.environ.get(k) for k in ("NANOSOC_RM_CONFIG", "NANOSOC_RM_TEST_MEMBER")}


def live_env():
    """(config path, member login) on the service VM. Without them the test FAILS: a live acceptance
    test never passes, and never skips, without the evidence it names (PR5, PR6)."""
    cfg, member = _LIVE_ENV["NANOSOC_RM_CONFIG"], _LIVE_ENV["NANOSOC_RM_TEST_MEMBER"]
    if not cfg or not Path(cfg).is_file() or not member:
        pytest.fail("live input missing: NANOSOC_RM_CONFIG must name the service config and "
                    "NANOSOC_RM_TEST_MEMBER a SoC-Labs member's GitHub login (run on the service VM)")
    return Path(cfg), member


def read_config(path):
    import tomllib  # Python >= 3.11, as the package requires
    return tomllib.loads(Path(path).read_text())


def _cfg_key(cfg, key):
    node = cfg
    for part in ASSUMED[key]:
        node = node[part]
    return node


def data_dir(cfg):
    """<data> of interfaces.md §3 and §12 (mirrors/, rm.sqlite). Key ASSUMED (G2)."""
    return Path(_cfg_key(cfg, "cfg_data_dir"))


def registry_dir(cfg):
    """The registry's local checkout (interfaces.md §1 `registry`). Key ASSUMED (G2)."""
    return Path(_cfg_key(cfg, "cfg_registry"))


def token(cfg, name):
    return Path(cfg["credentials"][name]["token_file"]).read_text().strip()


def http_get(url, *, tok=None, style="Bearer"):
    """Read-only GET. Returns (status, headers, body bytes). Never logs the token."""
    req = urllib.request.Request(url, method="GET")
    if tok:
        req.add_header("Authorization", "%s %s" % (style, tok))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def enc(path):
    return urllib.parse.quote(path, safe="")


def cli(config_path, *args, env_extra=None, timeout=1800):
    """Run the contracted console script. Returns (exit code, JSON-line docs, CompletedProcess)."""
    exe = shutil.which("nanosoc-rm")
    assert exe, "nanosoc-rm is not on PATH (interfaces.md §0.5: console script)"
    env = dict(os.environ, NANOSOC_RM_CONFIG=str(config_path))
    env.update(env_extra or {})
    p = subprocess.run([exe, *args], capture_output=True, text=True, env=env, timeout=timeout)
    docs = [json.loads(line) for line in p.stdout.splitlines() if line.strip()] if "--json" in args else []
    return p.returncode, docs, p


def sqlite_rows(db_path, sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def latest_observations(db_path):
    """subject -> latest observation doc, read from the contracted `observations` table (§12)."""
    rows = sqlite_rows(db_path, "SELECT subject, observed_at, doc FROM observations")
    out = {}
    for r in rows:
        if r["subject"] not in out or utc(r["observed_at"]) > utc(out[r["subject"]]["observed_at"]):
            out[r["subject"]] = json.loads(r["doc"])
    return out


# ---------------------------------------------------------------------------------------------
# git: fixture repos, a PATH shim that logs every git argv, and argv classification
# ---------------------------------------------------------------------------------------------
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rm-test", "GIT_AUTHOR_EMAIL": "rm-test@example.invalid",
    "GIT_COMMITTER_NAME": "rm-test", "GIT_COMMITTER_EMAIL": "rm-test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
}


def git(*args, cwd=None, date=None):
    env = dict(os.environ, **GIT_ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def git_init(path, bare=False):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", *(["--bare"] if bare else []), str(path))
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=path)
    return path


def commit(work, name, text, msg, date="2026-09-30T12:00:00Z"):
    f = work / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git("add", name, cwd=work)
    git("commit", "-q", "-m", msg, cwd=work, date=date)
    return git("rev-parse", "HEAD", cwd=work)


def git_shim(tmp):
    """A `git` first on PATH that logs argv (fields split by \\037) and then runs the real git."""
    real = shutil.which("git")
    d = tmp / "git-shim"
    d.mkdir(exist_ok=True)
    log = tmp / "git-argv.log"
    (d / "git").write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do printf '%%s\\037' \"$a\" >> '%s'; done\n"
        "printf '\\n' >> '%s'\n"
        "exec '%s' \"$@\"\n" % (log, log, real))
    (d / "git").chmod(0o755)
    return {"PATH": "%s%s%s" % (d, os.pathsep, os.environ.get("PATH", ""))}, log


def shim_calls(log):
    if not Path(log).exists():
        return []
    return [line.split("\037")[:-1] for line in Path(log).read_text().split("\n") if line]


_GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def git_subcommand(argv):
    """The subcommand of a git argv (without the leading 'git'); None if it cannot be found."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a is None:
            return None
        if a in _GIT_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def git_log_problems(calls):
    """A2/A6 at run time: git never writes to a host."""
    probs = []
    for argv in calls:
        sub = git_subcommand(argv)
        if sub in ("push", "send-pack", "receive-pack"):
            probs.append("git %s ran: %s" % (sub, " ".join(argv)))
    return probs


# ---------------------------------------------------------------------------------------------
# FakeHost: a local HTTP server standing in for a GitHub or GitLab API (offline tests)
# ---------------------------------------------------------------------------------------------
class FakeHost:
    """Serves registered routes on 127.0.0.1 and records every request (method, path, query, body)."""

    def __init__(self):
        self.routes, self.log = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                u = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                query = urllib.parse.parse_qs(u.query)
                outer.log.append({"method": self.command, "path": u.path, "query": query,
                                  "headers": dict(self.headers), "body": body})
                for method, rx, fn in outer.routes:
                    m = rx.fullmatch(u.path)
                    if method == self.command and m:
                        status, headers, payload = fn(m, query, self.headers, body)
                        break
                else:
                    status, headers, payload = 404, {}, {"message": "404 Not Found"}
                data = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "content-type" not in {k.lower() for k in headers}:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _serve

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, method, pattern, fn):
        self.routes.insert(0, (method, re.compile(pattern), fn))   # later registrations win

    def writes(self):
        return [(e["method"], e["path"]) for e in self.log if e["method"] not in ("GET", "HEAD")]

    def gets(self):
        return [e["path"] for e in self.log if e["method"] == "GET"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_github():
    h = FakeHost()
    yield h
    h.close()


@pytest.fixture
def fake_gitlab():
    h = FakeHost()
    yield h
    h.close()


def _bearer(headers):
    a = headers.get("Authorization") or ""
    return a.split(None, 1)[1] if " " in a else a


def _form_or_json(body):
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}


def paged(items, query, *, style, cap=100):
    """Answer one page the way the host does (GitLab: X-Total headers; GitHub: Link header). `cap` is the
    largest page the host serves whatever per_page asks for (CR-4: lets a control force GitHub paging)."""
    per = min(int((query.get("per_page") or ["30" if style == "github" else "20"])[0]), cap)
    page = int((query.get("page") or ["1"])[0])
    chunk = items[(page - 1) * per: page * per]
    pages = max(1, -(-len(items) // per))
    headers = {}
    if style == "gitlab":
        headers.update({"X-Total": str(len(items)), "X-Total-Pages": str(pages), "X-Page": str(page),
                        "X-Per-Page": str(per), "X-Next-Page": str(page + 1) if page < pages else ""})
    if page < pages:
        headers["Link"] = '<?page=%d&per_page=%d>; rel="next"' % (page + 1, per)
    return 200, headers, chunk


def github_identities(h, states):
    """Fake GitHub OAuth + membership. states: login -> 'active' | 'pending' | None (not a member)."""
    def access_token(m, q, hd, body):
        code = _form_or_json(body).get("code") or (q.get("code") or [""])[0]
        return 200, {}, {"access_token": "fake-token-%s" % code, "token_type": "bearer", "scope": "read:org"}

    def user(m, q, hd, body):
        tok = _bearer(hd)
        if not tok.startswith("fake-token-"):
            return 401, {}, {"message": "Bad credentials"}
        return 200, {}, {"login": tok[len("fake-token-"):], "id": 1}

    def membership(m, q, hd, body):
        login = _bearer(hd)[len("fake-token-"):]
        st = states.get(login)
        if st is None:
            return 404, {}, {"message": "Not Found"}
        return 200, {}, {"state": st, "role": "member", "organization": {"login": GITHUB_ORG}}

    def member_check(m, q, hd, body):
        return (204, {}, None) if states.get(m.group(1)) == "active" else (404, {}, {"message": "Not Found"})

    h.route("POST", r"/login/oauth/access_token", access_token)
    h.route("GET", r"/user", user)
    h.route("GET", r"/user/memberships/orgs/%s" % GITHUB_ORG, membership)
    h.route("GET", r"/orgs/%s/members/([^/]+)" % GITHUB_ORG, member_check)


def github_listing(h, repos, cap=100):
    """repos: list of (path, visibility). GET orgs/<org>/repos (paged, at most `cap` per page) and GET orgs/<org>
    (totals)."""
    items = [{"full_name": p, "name": p.split("/")[-1], "private": v != "public", "visibility": v,
              "default_branch": "main", "fork": False, "archived": False} for p, v in repos]
    h.route("GET", r"/orgs/%s/repos" % GITHUB_ORG, lambda m, q, hd, b: paged(items, q, style="github", cap=cap))
    h.route("GET", r"/orgs/%s" % GITHUB_ORG, lambda m, q, hd, b: (200, {}, {
        "login": GITHUB_ORG, "public_repos": sum(1 for _, v in repos if v == "public"),
        "total_private_repos": sum(1 for _, v in repos if v != "public")}))


def gitlab_listing(h, projects):
    items = [{"id": 100 + i, "path_with_namespace": p, "visibility": v, "default_branch": "main",
              "archived": False} for i, (p, v) in enumerate(projects)]
    h.route("GET", r"/api/v4/groups/%s/projects" % GITLAB_GROUP, lambda m, q, hd, b: paged(items, q, style="gitlab"))


def gitlab_token_self(h, scopes, expires_at="2027-06-30"):
    h.route("GET", r"/api/v4/personal_access_tokens/self", lambda m, q, hd, b: (200, {}, {
        "id": 1, "name": "poller", "scopes": list(scopes), "expires_at": expires_at,
        "active": True, "revoked": False}))


def gitlab_ci(h, *, trigger_status=201, foreign_nonce=None, job_status="success"):
    """Fake compat-ci: trigger, pipeline, jobs, variables and an rm-bundle artifact built from the
    contract example bundle, re-stamped with the requested nonce and ids."""
    state = {"pipelines": {}}
    proj = r"/api/v4/projects/([^/]+)"

    def trigger(m, q, hd, body):
        if trigger_status >= 400:
            return trigger_status, {}, {"message": "%d" % trigger_status}
        form = _form_or_json(body)
        req = form.get("variables[RM_REQUEST]") or (form.get("variables") or {}).get("RM_REQUEST")
        pid = 1000 + len(state["pipelines"])
        state["pipelines"][pid] = {"project": urllib.parse.unquote(m.group(1)), "ref": form.get("ref"),
                                   "request": req, "job": pid + 1000}
        return 201, {}, {"id": pid, "ref": form.get("ref"), "status": "created", "source": "trigger"}

    def pipeline(m, q, hd, body):
        p = state["pipelines"].get(int(m.group(2)))
        if not p:
            return 404, {}, {"message": "404"}
        return 200, {}, {"id": int(m.group(2)), "project_id": CI_PROJECT_ID, "ref": p["ref"], "source": "trigger",
                         "status": job_status, "sha": "0" * 39 + "1"}

    def jobs(m, q, hd, body):
        p = state["pipelines"].get(int(m.group(2)))
        return (200, {}, [{"id": p["job"], "name": "t0", "status": job_status, "failure_reason": None,
                           "started_at": "2026-10-01T02:03:30Z", "finished_at": "2026-10-01T02:05:02Z",
                           "runner": {"id": 1, "description": "rm-t0", "online": True}}]) if p else (404, {}, {})

    def variables(m, q, hd, body):
        p = state["pipelines"].get(int(m.group(2)))
        if not p:
            return 404, {}, {}
        value = p["request"]
        if foreign_nonce:
            r = json.loads(value)
            r["nonce"] = foreign_nonce
            value = json.dumps(r, separators=(",", ":"))
        return 200, {}, [{"key": "RM_REQUEST", "value": value, "variable_type": "env_var"}]

    def artifacts(m, q, hd, body):
        jid = int(m.group(2))
        for pid, p in state["pipelines"].items():
            if p["job"] == jid:
                return 200, {"Content-Type": "application/zip"}, bundle_zip(json.loads(p["request"]), pid, jid)
        return 404, {}, {}

    h.route("POST", proj + r"/trigger/pipeline", trigger)
    h.route("GET", proj + r"/pipelines/(\d+)", pipeline)
    h.route("GET", proj + r"/pipelines/(\d+)/jobs", jobs)
    h.route("GET", proj + r"/pipelines/(\d+)/variables", variables)
    h.route("GET", proj + r"/jobs/(\d+)/artifacts", artifacts)
    return state


def bundle_zip(request, pipeline_id, job_id):
    prov = json.loads((EXAMPLES / "result-bundle.provenance.example.json").read_text())
    nonce = request["nonce"]
    prov["run_nonce"] = nonce
    prov["request"] = {k: request[k] for k in ("configuration", "repo", "commit", "upgrade_from", "canonical_urls")}
    prov["ci"].update(pipeline_id=pipeline_id, job_id=job_id, harness_sha="0" * 39 + "1")
    prov["captured"]["superproject_commit"] = request["commit"]
    rows = (EXAMPLES / "result-bundle.verdicts.example.tsv").read_text().splitlines()
    tsv = "".join("\t".join(r.split("\t")[:4] + [nonce]) + "\n" for r in rows if r)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("rm-bundle/provenance.json", json.dumps(prov, indent=1))
        z.writestr("rm-bundle/verdicts.tsv", tsv)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------
# Offline estate, registry and service config
# ---------------------------------------------------------------------------------------------
def local_estate(root):
    """Bare repos standing in for both hosts (interfaces.md §8: a local bare repo stands in for a host).

    gitlab/soclabs/ip.git     main (2 commits), tag v0.1.0, refs/merge-requests/1/head (MR-only commit)
    gitlab/alice/ip.git       a user's fork of ip: one extra commit on main (A6)
    github/SoC-Labs/soc.git   main pins ip@v0.1.0 and the fork's extra commit; refs/pull/1/head (PR-only)
    """
    hosts = {"github": root / "github", "gitlab": root / "gitlab"}
    ip_w = git_init(root / "work" / "ip")
    c1 = commit(ip_w, "rtl/ip.v", "module ip; endmodule\n", "ip v0.1.0", "2026-09-01T10:00:00Z")
    git("tag", "-a", "v0.1.0", "-m", "v0.1.0", cwd=ip_w)
    c2 = commit(ip_w, "rtl/ip.v", "module ip(input a); endmodule\n", "ip: add a", "2026-09-02T10:00:00Z")
    git("checkout", "-q", "-b", "mr", c2, cwd=ip_w)
    c3 = commit(ip_w, "rtl/ip.v", "module ip(input a, b); endmodule\n", "MR only", "2026-09-03T10:00:00Z")
    git("checkout", "-q", "-b", "fork-main", c2, cwd=ip_w)
    f1 = commit(ip_w, "rtl/ip.v", "module ip(input a, x); endmodule\n", "fork only", "2026-09-04T10:00:00Z")
    ip = git_init(hosts["gitlab"] / "soclabs" / "ip.git", bare=True)
    git("push", "-q", str(ip), "%s:refs/heads/main" % c2, "refs/tags/v0.1.0", "mr:refs/merge-requests/1/head", cwd=ip_w)
    fork = git_init(hosts["gitlab"] / "alice" / "ip.git", bare=True)
    git("push", "-q", str(fork), "fork-main:refs/heads/main", "refs/tags/v0.1.0", cwd=ip_w)

    soc_w = git_init(root / "work" / "soc")
    gl = "file://%s" % hosts["gitlab"]
    (soc_w / ".gitmodules").write_text(
        '[submodule "ip"]\n\tpath = ip\n\turl = %s/soclabs/ip.git\n'
        '[submodule "ip-fork"]\n\tpath = ip-fork\n\turl = %s/alice/ip.git\n\tbranch = main\n' % (gl, gl))
    git("add", ".gitmodules", cwd=soc_w)
    git("update-index", "--add", "--cacheinfo", "160000,%s,ip" % c1, cwd=soc_w)
    git("update-index", "--add", "--cacheinfo", "160000,%s,ip-fork" % f1, cwd=soc_w)
    git("commit", "-q", "-m", "soc: pin ip and a fork", cwd=soc_w, date="2026-09-05T10:00:00Z")
    s1 = git("rev-parse", "HEAD", cwd=soc_w)
    git("checkout", "-q", "-b", "pr", cwd=soc_w)
    p1 = commit(soc_w, "README.md", "pr\n", "PR only", "2026-09-06T10:00:00Z")
    soc = git_init(hosts["github"] / "SoC-Labs" / "soc.git", bare=True)
    git("push", "-q", str(soc), "%s:refs/heads/main" % s1, "pr:refs/pull/1/head", cwd=soc_w)
    return {"hosts": hosts, "ip": ip, "fork": fork, "soc": soc, "c1": c1, "c2": c2, "c3": c3, "f1": f1,
            "s1": s1, "p1": p1}


ESTATE_COMPONENTS = [
    {"id": "estate-soc", "class": "soc_ref", "canonical": {"host": "github", "path": "SoC-Labs/soc"}},
    {"id": "estate-ip", "class": "ip_tech", "canonical": {"host": "gitlab", "path": "soclabs/ip"}},
]
ESTATE_CONFIG = {"id": "estate-main", "component": "estate-soc", "ref": {"branch": "main"}}


def write_registry(root, *, catalogue, eligibility, defects=(), errata=(), imports=None):
    """A registry checkout (README §1 layout). JSON is valid YAML, so no emitter is needed."""
    git_init(root)

    def dump(rel, doc):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(doc, indent=1) + "\n")

    dump("catalogue.yaml", catalogue)
    dump("eligibility.yaml", eligibility)
    for d in defects:
        dump("defects/%s.yaml" % d["id"], d)
    for e in errata:
        dump("errata/%s/ERR-%s.yaml" % (e["configuration"], e["id"].rsplit("-", 1)[1]), e)
    if imports:
        dump("imports.yaml", imports)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "fixture registry", cwd=root, date="2026-09-30T12:00:00Z")
    return root


def fixture_registry(root, *, extra_components=(), drop=()):
    """The contract examples as a registry (real rc4 data), plus the offline estate."""
    cat = example("component-catalogue.example.yaml")[0]
    cat["components"] = [c for c in cat["components"] if c["id"] not in drop] + list(ESTATE_COMPONENTS) + list(extra_components)
    elig = example("eligibility.example.yaml")[0]
    elig["configurations"].append(dict(ESTATE_CONFIG))
    return write_registry(root, catalogue=cat, eligibility=elig,
                          defects=example("defect.SL-DEF-0001.example.yaml"),
                          errata=example("erratum.ETH-R0P0-ERR-001.example.yaml"),
                          imports=example("import-ledger.example.yaml")[0])


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(v)


def to_toml(d, prefix=""):
    scalars = ["%s = %s" % (k, _toml_value(v)) for k, v in d.items() if not isinstance(v, dict)]
    out = "\n".join(scalars) + ("\n" if scalars else "")
    for k, v in d.items():
        if isinstance(v, dict):
            out += "\n[%s%s]\n" % (prefix, k) + to_toml(v, "%s%s." % (prefix, k))
    return out


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def write_config(path, *, data, registry, github, gitlab, git_bases=None, overrides=None):
    """Service config for offline tests (keys: interfaces.md section 17.1; `git` is the optional fetch base)."""
    tok = path.parent / "tokens"
    tok.mkdir(parents=True, exist_ok=True)
    for n in ("github_read", "gitlab_read", "ci_trigger", "github_oauth", "session"):
        (tok / n).write_text("fake-%s\n" % n)
    gb = git_bases or {}
    cfg = {
        "service": {"data_dir": str(data)},
        "hosts": {
            "github": {"web": github.url, "api": github.url, "ssh": "git@github.invalid:",
                       "git": gb.get("github", github.url), "org": GITHUB_ORG},
            "gitlab": {"web": gitlab.url, "api": gitlab.url + "/api/v4", "ssh": "git@gitlab.invalid:",
                       "git": gb.get("gitlab", gitlab.url), "group": GITLAB_GROUP},
        },
        "registry": {"repo": "soclabs/nanosoc-registry", "ref": "main", "path": str(registry)},
        # CR-3: verify step 1 needs the project's numeric id and the runner allowlist (interfaces.md section 1).
        "ci": {"project": CI_PROJECT, "project_id": CI_PROJECT_ID, "ref": "main", "job": "t0", "allowed_refs": ["main"],
               "allowed_runner_ids": [1], "fetch_timeout_minutes": 120},
        "credentials": {
            "github_read": {"token_file": str(tok / "github_read"), "scope": ["contents:read", "metadata:read", "members:read"], "expires": "2027-06-30"},
            "gitlab_read": {"token_file": str(tok / "gitlab_read"), "scope": ["read_api", "read_repository"], "expires": "2027-06-30"},
            "ci_trigger": {"token_file": str(tok / "ci_trigger"), "scope": ["trigger"], "project": CI_PROJECT, "expires": "2027-06-30"},
            "github_oauth": {"client_id": "fake-client-id", "token_file": str(tok / "github_oauth"), "scope": ["read:org"], "expires": ""},
        },
        "session": {"secret_file": str(tok / "session")},
        "stores": {"disk": {"path": str(data / "store")}},
        # interfaces.md section 1: `default` applies to every signed-in member; a login entry adds to it.
        "roles": {"default": ["member"]},
        "schedule": {"poll_minutes": 15, "nightly_utc": "02:00", "fetch_minutes": 2},
    }
    overrides = dict(overrides or {})
    if "roles" in overrides:                 # replaced, never merged: H2 needs an empty role table
        cfg["roles"] = overrides.pop("roles")
    _merge(cfg, overrides)
    path.write_text(to_toml(cfg))
    return path


def offline_service(tmp, github, gitlab, monkeypatch, *, overrides=None, drop=(), seed=True,
                    trigger_status=201, foreign_nonce=None, members=None, prime=False):
    """The service in-process on the example registry + estate, with fake hosts. Returns a dict.
    $NANOSOC_RM_CONFIG points at the offline config for the rest of the test (in-process calls such
    as db.put find the database through it, as test_pins.py assumes)."""
    estate = local_estate(tmp / "estate")
    registry = fixture_registry(tmp / "registry", drop=drop)
    gb = {h: "file://%s" % p for h, p in estate["hosts"].items()}
    cfg_path = write_config(tmp / "config.toml", data=tmp / "data", registry=registry, github=github,
                            gitlab=gitlab, git_bases=gb, overrides=overrides)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg_path))
    github_identities(github, members or {"alice": "active", "bob": None, "carol": "pending"})
    github_listing(github, [("SoC-Labs/soc", "public")])
    gitlab_listing(gitlab, [("soclabs/ip", "public")])
    gitlab_token_self(gitlab, ["read_api", "read_repository"])
    ci = gitlab_ci(gitlab, trigger_status=trigger_status, foreign_nonce=foreign_nonce)
    shim_env, shim_log = git_shim(tmp)
    if seed:
        put = resolve("put")
        for name in ("observation.example.jsonl", "pin-state.example.jsonl"):
            for doc in example(name):
                put(doc)
    svc = {"cfg_path": cfg_path, "estate": estate, "registry": registry, "shim_env": shim_env,
           "shim_log": shim_log, "ci": ci, "db": tmp / "data" / "rm.sqlite"}
    if prime:
        # §10 Trigger step 1 resolves the commit from the mirror, and canonical_urls from the walk.
        for args in (("mirror", "sync"), ("walk", ESTATE_CONFIG["id"], "--json")):
            cli(cfg_path, *args, env_extra=shim_env)
    svc["app"] = resolve("create_app")(cfg_path)
    return svc


def member_client(cfg_path, login):
    """In-process client signed in as SoC-Labs member `login` (G1: create_app's `auth` seam, as
    test_ci_errata.py's member_client). Roles still come from the config (H2)."""
    from fastapi.testclient import TestClient
    return TestClient(resolve("create_app")(cfg_path, auth=resolve("fake_membership")(login=login)))


def sign_in(client, login):
    """Drive the OAuth flow against the fake GitHub. Returns (authorize query, callback response)."""
    r = client.get(ASSUMED["login_path"], follow_redirects=False)
    assert r.status_code in (302, 303, 307), "login must redirect to GitHub's authorize URL"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    state = (q.get("state") or [""])[0]
    return q, client.get(ASSUMED["callback_path"], params={"code": login, "state": state}, follow_redirects=False)


# ---------------------------------------------------------------------------------------------
# HTML: read the page hooks (G4)
# ---------------------------------------------------------------------------------------------
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class PageScan(HTMLParser):
    """Facts, source links, data age, dashboard items (with their text) and forms of one page."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ages, self.facts, self.links, self.forms = [], [], {}, []
        self.items = {}                     # (configuration, item) -> text
        self._stack, self._form = [], None
        self.feed(text)
        self.close()

    def _conf(self):
        for e in reversed(self._stack):
            if e["conf"]:
                return e["conf"]
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: ("" if v is None else v) for k, v in attrs}
        conf = a.get(HOOK["configuration"]) or self._conf()
        if HOOK["age"] in a:
            self.ages.append((a[HOOK["age"]], a.get(HOOK["oldest"])))
        if HOOK["fact"] in a:
            self.facts.append((a[HOOK["fact"]], a.get(HOOK["fact_at"]), conf))
        if tag == "a" and HOOK["source"] in a:
            self.links.setdefault(a[HOOK["source"]], a.get("href"))
        item = a.get(HOOK["item"])
        if item:
            self.items.setdefault((conf, item), "")
        if tag == "form":
            self._form = {"action": a.get("action"), "method": (a.get("method") or "get").lower(),
                          "fields": {}, "run": HOOK["run"] in a, "configuration": conf, "disabled": False}
        if self._form is not None and tag in ("input", "button"):
            if a.get("name"):
                self._form["fields"][a["name"]] = a.get("value", "")
            if "disabled" in a and (tag == "button" or a.get("type") == "submit"):
                self._form["disabled"] = True
        if tag not in _VOID:
            self._stack.append({"tag": tag, "conf": a.get(HOOK["configuration"]), "item": (conf, item) if item else None})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self._stack and self._stack[-1]["tag"] == tag:
            self._stack.pop()

    def handle_endtag(self, tag):
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        for e in self._stack:
            if e["item"]:
                self.items[e["item"]] += data


def first_int(text):
    m = re.search(r"-?\d+", text or "")
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------------------------------------
# Checkers, one per AT
# ---------------------------------------------------------------------------------------------
def catalogue_index(cat):
    """(host, path) -> (component id, role, class); and the repos listed more than once (A1 rule 1)."""
    idx, dup = {}, []
    for c in cat["components"]:
        for role, repos in (("canonical", [c["canonical"]]), ("copy", c.get("copies", [])),
                            ("alias", c.get("aliases", []))):
            for r in repos:
                k = (r["host"], r["path"])
                if k in idx:
                    dup.append(k)
                idx[k] = (c["id"], role, c.get("class"))
    for e in cat.get("external", []):
        k = (e["repo"]["host"], e["repo"]["path"])
        if k in idx:
            dup.append(k)
        idx[k] = (e["id"], "external", None)
    for g in cat.get("ignored", []):
        k = (g["repo"]["host"], g["repo"]["path"])
        if k in idx:
            dup.append(k)
        idx[k] = (None, "ignored", None)
    return idx, dup


def check_a1(docs, host_totals, cat):
    """A1: every repo on both hosts inventoried and classified; one canonical per component."""
    probs = []
    idx, dup = catalogue_index(cat)
    probs += ["catalogue: %s:%s belongs to more than one entry" % k for k in dup]
    seen = {}
    for d in docs:
        errs = validate("observation", d)
        if errs:
            probs.append("invalid observation %s: %s" % (d.get("subject"), errs[0]))
            continue
        if d["kind"] != "repo_inventory":
            probs.append("unexpected kind %s" % d["kind"])
            continue
        f = d["fact"]
        k = (f["repo"]["host"], f["repo"]["path"])
        if k in seen:
            probs.append("inventoried twice: %s:%s" % k)
        seen[k] = f
        if f["role"] == "unclassified":
            probs.append("unclassified: %s:%s" % k)
            continue
        want = idx.get(k)
        if want is None:
            probs.append("%s:%s has role %s but is not in the catalogue" % (k + (f["role"],)))
            continue
        if (f["component"], f["role"]) != want[:2]:
            probs.append("%s:%s reported as %s of %s; catalogue says %s of %s"
                         % (k + (f["role"], f["component"], want[1], want[0])))
        if want[1] not in ("external", "ignored") and want[2] not in B1_CLASSES:
            probs.append("%s:%s: component %s has no B1 class" % (k + (want[0],)))
    for host, total in sorted(host_totals.items()):
        n = sum(1 for k in seen if k[0] == host)
        if total is None:
            probs.append("%s: the host's own repo total is not visible to this token" % host)
        elif n != total:
            probs.append("%s: %d repos inventoried, the host reports %d" % (host, n, total))
    for c in cat["components"]:
        k = (c["canonical"]["host"], c["canonical"]["path"])
        rows = [kk for kk, f in seen.items() if f.get("component") == c["id"] and f["role"] == "canonical"]
        if len(rows) > 1:
            probs.append("%s: %d canonical repos, want exactly 1" % (c["id"], len(rows)))
        if k not in seen:
            probs.append("%s: canonical %s:%s not found on its host" % ((c["id"],) + k))
    return probs


HTTP_WRITE_METHODS = {"post", "put", "patch", "delete"}
HTTP_REQUEST_CALLS = {"request", "stream", "build_request"}          # method is the first argument
EXEC_CALLS = {"run", "Popen", "check_call", "check_output", "call"}
HTTP_LIBS = ("httpx", "requests", "urllib.request", "urllib3", "aiohttp", "http.client")
GIT_WRITES = ("push", "send-pack", "receive-pack")


def _const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _argv(node):
    """A literal argv list as strings, with None for any element that is not a string constant."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_const(e) for e in node.elts]
    return None


def _kw(node, name):
    return next((k.value for k in node.keywords if k.arg == name), None)


def _is_module(root, dotted):
    """Is `dotted` (relative to the package root; '' = the root) a module or package of this package?"""
    p = root.joinpath(*dotted.split(".")) if dotted else root
    return p.with_suffix(".py").is_file() or (p / "__init__.py").is_file()


def _imports(tree, root, py):
    """(does the file import an HTTP client library?, local names bound to MODULES of this package).
    Only module names count as internal: an object imported from the package (a shared client) does
    not, so `client.post(...)` on it is still censused."""
    http, internal = False, set()
    here = list(py.parent.relative_to(root).parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(HTTP_LIBS):
                    http = True
                if a.name.split(".")[0] == root.name:
                    rest = a.name.split(".", 1)[1] if "." in a.name else ""
                    if a.asname is None:
                        internal.add(root.name)
                    elif _is_module(root, rest):
                        internal.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(HTTP_LIBS) or mod == "urllib":
                http = True
            if node.level > 0:
                up = here[:len(here) - (node.level - 1)] if node.level > 1 else here
                base = ".".join(up + ([mod] if mod else []))
            elif mod.split(".")[0] == root.name:
                base = mod.split(".", 1)[1] if "." in mod else ""
            else:
                continue
            for a in node.names:
                if _is_module(root, "%s.%s" % (base, a.name) if base else a.name):
                    internal.add(a.asname or a.name)
    return http, internal


def _receiver_root(f):
    v = f.value if isinstance(f, ast.Attribute) else None
    while isinstance(v, ast.Attribute):
        v = v.value
    return v.id if isinstance(v, ast.Name) else None


def write_call_census(root):
    """A2/A6: every call site that can write outside the service's disk, or that cannot be resolved.
    Each entry: {file, line, kind (http | exec | parse), method, target, http_file}. An unresolvable
    one has method '?'. A method call on a module of this package (db.put, db.delete) is internal and
    is not an HTTP call: that module's own file is censused."""
    out = []
    root = Path(root)
    for py in sorted(root.rglob("*.py")):
        rel = str(py.relative_to(root.parent))
        try:
            tree = ast.parse(py.read_text(), filename=rel)
        except SyntaxError as e:
            out.append({"file": rel, "line": e.lineno or 0, "kind": "parse", "method": "?", "target": str(e),
                        "http_file": True})
            continue
        http_file, internal = _imports(tree, root, py)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            first = node.args[0] if node.args else None
            entry = {"file": rel, "line": node.lineno, "http_file": http_file}
            if isinstance(f, ast.Attribute) and _receiver_root(f) in internal:
                continue                                          # a call into this package
            if name in HTTP_WRITE_METHODS and isinstance(f, ast.Attribute):
                out.append(dict(entry, kind="http", method=name.upper(), target=_const(first) or "?"))
            elif name in HTTP_REQUEST_CALLS and isinstance(f, ast.Attribute):
                m = _const(first) or _const(_kw(node, "method"))
                if m is None:
                    out.append(dict(entry, kind="http", method="?", target="unresolved method"))
                elif m.upper() not in ("GET", "HEAD"):
                    tgt = _const(node.args[1]) if len(node.args) > 1 else _const(_kw(node, "url"))
                    out.append(dict(entry, kind="http", method=m.upper(), target=tgt or "?"))
            elif name == "Request":                               # urllib: Request(url, data=None, ..., method=None)
                mk = _kw(node, "method")
                has_data = len(node.args) > 1 or _kw(node, "data") is not None
                m = _const(mk) if mk is not None else ("POST" if has_data else "GET")
                if m is None:
                    out.append(dict(entry, kind="http", method="?", target="unresolved method"))
                elif m.upper() not in ("GET", "HEAD"):
                    out.append(dict(entry, kind="http", method=m.upper(), target=_const(first) or "?"))
            elif name == "create_subprocess_exec" or name in EXEC_CALLS or (
                    name in ("system", "popen") and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name) and f.value.id == "os"):
                if name in EXEC_CALLS and not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                                               and f.value.id in ("subprocess", "sp")):
                    continue                                      # obj.run(), a bare run(): not subprocess
                if name == "create_subprocess_exec":
                    argv = [_const(a) for a in node.args] or None # argv as separate positionals
                elif name in ("system", "popen"):
                    argv = None
                else:
                    argv = _argv(first)
                if not argv or argv[0] is None:
                    out.append(dict(entry, kind="exec", method="?", target="unresolved argv"))
                elif argv[0] in ("gh", "glab"):
                    out.append(dict(entry, kind="exec", method=argv[0], target=" ".join(a or "?" for a in argv)))
                elif argv[0] == "git":
                    sub = git_subcommand(argv[1:])
                    if sub is None:
                        out.append(dict(entry, kind="exec", method="?", target="git with unresolved subcommand"))
                    elif sub in GIT_WRITES:
                        out.append(dict(entry, kind="exec", method="git " + sub, target=" ".join(a or "?" for a in argv)))
    return out


# Allowlist entry effects (gap G10). A census entry passes only if an entry of the same file, kind and
# method covers it, and the entry's effect is one of these:
#   write       the one outbound write of v1: POST trigger/pipeline (A2). Needs a target guard.
#   read-post   a POST that writes nothing; its target must be in READ_ONLY_POSTS (OAuth code exchange).
#   local       a call the census mistakes for HTTP (sqlite .delete) in a file that imports no HTTP library.
#   git-runner  the one dynamic git runner; its guard refuses GIT_WRITES at run time (AT-A6 proves it).
EFFECTS = ("write", "read-post", "local", "git-runner")


def allowed(entry, allowlist):
    for a in allowlist:
        if not (entry["file"].endswith(a["file"]) and entry["kind"] == a["kind"] and entry["method"] == a["method"]):
            continue
        if a.get("effect") == "local" and entry.get("http_file"):
            continue                                              # a file with an HTTP client cannot claim "local"
        if entry["target"] in ("?", a["target"]) or a["target"] in entry["target"]:
            return True
    return False


def allowlist_problems(allowlist):
    """The declared allowlist itself: exactly one write (the trigger), everything else read-only."""
    probs = []
    writes = [a for a in allowlist if a.get("effect") == "write"]
    if len(writes) != 1 or writes[0]["method"] != "POST" or "trigger/pipeline" not in writes[0]["target"]:
        probs.append("the v1 write allowlist must hold exactly one write: the CI pipeline trigger")
    for a in allowlist:
        if a.get("effect") not in EFFECTS:
            probs.append("allowlist entry %s:%s %s has effect %r" % (a["file"], a["method"], a["target"], a.get("effect")))
        if not a.get("reason"):
            probs.append("allowlist entry %s:%s %s gives no reason" % (a["file"], a["method"], a["target"]))
        if a.get("effect") == "read-post" and not any(t in a["target"] for t in READ_ONLY_POSTS):
            probs.append("read-post %s is not a known read-only POST %s" % (a["target"], list(READ_ONLY_POSTS)))
    if sum(1 for a in allowlist if a.get("effect") == "git-runner") > 1:
        probs.append("more than one git runner is allowlisted: every git call must go through one")
    return probs


def check_a2(census, allowlist, credentials, *, ci_project, gitlab_self=None, runs=(), actions=()):
    """A2: one write call (the trigger); read-only credentials plus one CI-scoped trigger; Runs logged."""
    probs = []
    for c in census:
        if not allowed(c, allowlist):
            probs.append("write call not allowlisted: %s:%s %s %s" % (c["file"], c["line"], c["method"], c["target"]))
    probs += allowlist_problems(allowlist)
    trig = [a for a in allowlist if a.get("effect") == "write"]
    if trig and not any(allowed(c, trig) for c in census):
        probs.append("the census did not find the trigger call itself: the census is blind")
    names = set(credentials)
    if names != set(V1_CREDENTIALS):
        probs.append("credentials %s, want exactly %s" % (sorted(names), sorted(V1_CREDENTIALS)))
    for name, cred in sorted(credentials.items()):
        scope = set(cred.get("scope") or [])
        if not scope:
            probs.append("%s: no scope recorded" % name)
        extra = scope - V1_CREDENTIALS.get(name, set())
        if extra:
            probs.append("%s: scope %s is beyond read access" % (name, sorted(extra)))
    if credentials.get("ci_trigger", {}).get("project") != ci_project:
        probs.append("ci_trigger is scoped to %r, not %s" % (credentials.get("ci_trigger", {}).get("project"), ci_project))
    if gitlab_self is not None:
        live = set(gitlab_self.get("scopes") or [])
        if not live or live - V1_CREDENTIALS["gitlab_read"]:
            probs.append("gitlab_read: the host reports scopes %s" % sorted(live))
        if live != set(credentials.get("gitlab_read", {}).get("scope") or []):
            probs.append("gitlab_read: host scopes %s differ from the record" % sorted(live))
        if not gitlab_self.get("active") or gitlab_self.get("revoked"):
            probs.append("gitlab_read: the host reports the token inactive or revoked")
    manual = [r for r in runs if r["trigger"] == "manual"]
    if not manual:
        probs.append("no manual Run in the log yet: run AT-A4 or the H1 demo first")
    for r in runs:
        if not r.get("triggered_by"):
            probs.append("run %s has no triggered_by" % r["nonce"])
        if r["trigger"] == "schedule" and r["triggered_by"] != "schedule":
            probs.append("scheduled run %s says triggered_by %r" % (r["nonce"], r["triggered_by"]))
    for r in manual:
        hits = [a for a in actions if a["login"] == r["triggered_by"] and r["configuration"] in (a["target"] or "")
                and abs((utc(a["at"]) - utc(r["triggered_at"])).total_seconds()) <= 5]
        if len(hits) != 1:
            probs.append("run %s by %s has %d actions rows, want 1" % (r["nonce"], r["triggered_by"], len(hits)))
    return probs


def check_a3_page(route, text, stored, fetched_at, fetch):
    """A3 on one page. stored: subject -> latest observation; fetch(href) -> body or None."""
    p = PageScan(text)
    probs = []
    if len(p.ages) != 1:
        probs.append("%s: %d data-age elements, want exactly 1" % (route, len(p.ages)))
    if not p.facts:
        probs.append("%s: no facts rendered, so nothing is proven" % route)
    for subject, observed_at, _ in p.facts:
        if not observed_at:
            probs.append("%s: fact %s shows no observed_at" % (route, subject))
        href = p.links.get(subject)
        if not href:
            probs.append("%s: fact %s has no source link" % (route, subject))
            continue
        body = fetch(href)
        if body is None:
            probs.append("%s: the source link of %s is broken (%s)" % (route, subject, href))
            continue
        if subject.startswith("registry_record:"):
            if " show " not in body:
                probs.append("%s: registry fact %s does not link to its git show command" % (route, subject))
            continue
        obs = stored.get(subject)
        if obs is None:
            probs.append("%s: fact %s is not a stored observation" % (route, subject))
            continue
        if observed_at and utc(observed_at) != utc(obs["observed_at"]):
            probs.append("%s: %s shows %s, the store has %s" % (route, subject, observed_at, obs["observed_at"]))
        if not obs.get("sources"):
            probs.append("%s: %s has no sources in the store" % (route, subject))
        for src in obs.get("sources", []):
            cmd = src["command"]
            if cmd not in body and htmlmod.escape(cmd) not in body and htmlmod.escape(cmd, quote=False) not in body:
                probs.append("%s: the source page of %s does not show `%s`" % (route, subject, cmd))
    shown = [at for _, at, _ in p.facts if at]
    if len(p.ages) == 1 and shown:
        age, oldest = p.ages[0]
        want_oldest = min(shown, key=utc)
        if not oldest or utc(oldest) != utc(want_oldest):
            probs.append("%s: data age is measured from %s, the oldest fact shown is %s" % (route, oldest, want_oldest))
        else:
            want = (fetched_at - utc(oldest)).total_seconds()
            try:
                if abs(float(age) - want) > 5:
                    probs.append("%s: data age %ss, want %.0fs" % (route, age, want))
            except ValueError:
                probs.append("%s: data age %r is not a number of seconds" % (route, age))
    return probs


def check_a4(run, pipeline, jobs, variables, *, configuration, commit, login):
    """A4: the result shown belongs to the pipeline that pressing Run started in the CI project."""
    probs = []
    f = run["fact"]
    r = f["run"]
    if f["configuration"] != configuration or f["commit"] != commit:
        probs.append("run is for %s@%s, want %s@%s" % (f["configuration"], f["commit"][:7], configuration, commit[:7]))
    if r["trigger"] != "manual" or r["triggered_by"] != login:
        probs.append("run says %s by %s, want manual by %s" % (r["trigger"], r["triggered_by"], login))
    if r["pipeline_id"] is None or pipeline is None:
        probs.append("no pipeline for nonce %s: nothing was started in the CI project" % r["nonce"])
        return probs
    if pipeline.get("id") != r["pipeline_id"]:
        probs.append("pipeline %s is not the run's %s" % (pipeline.get("id"), r["pipeline_id"]))
    if pipeline.get("source") != "trigger":
        probs.append("pipeline source is %r, want trigger" % pipeline.get("source"))
    t0 = [j for j in jobs if j.get("name") == "t0"]
    if len(t0) != 1:
        probs.append("pipeline has %d jobs named t0, want 1" % len(t0))
    req = {v["key"]: v["value"] for v in variables}.get("RM_REQUEST")
    if req is None:
        probs.append("pipeline has no RM_REQUEST variable")
    else:
        q = json.loads(req)
        if q.get("nonce") != r["nonce"]:
            probs.append("RM_REQUEST nonce %s is not the run's nonce %s" % (q.get("nonce"), r["nonce"]))
        if q.get("configuration") != configuration or q.get("commit") != commit:
            probs.append("RM_REQUEST asks for %s@%s" % (q.get("configuration"), (q.get("commit") or "")[:7]))
    if f["acceptance"] == "pending":
        probs.append("the result has not appeared: the run is still pending")
    else:
        if t0 and r["job_id"] != t0[0]["id"]:
            probs.append("the result carries job %s, the pipeline's t0 job is %s" % (r["job_id"], t0[0]["id"]))
        if f["verdict"] is None:
            probs.append("the fetched run has no verdict")
    return probs


def route_table(app):
    """Every route the app serves: (path, methods, static directory or None)."""
    out = []
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is None:
            continue
        directory = getattr(getattr(r, "app", None), "directory", None)
        out.append((path, set(getattr(r, "methods", None) or ()), directory))
    return out


def norm_route(path):
    return re.sub(r"\{[^}]+\}", "{}", path)


def fill(path, nonce=EXAMPLE_NONCE, subject=None):
    samples = [
        ("/configurations/", {"id": RC4, "configuration": RC4, "configuration_id": RC4}),
        ("/components/", {"id": "tidelink", "component": "tidelink", "component_id": "tidelink"}),
        ("/defects/", {"id": DEFECT, "defect": DEFECT}),
        ("/errata/", {"configuration": RC4, "id": RC4}),
        ("/runs/", {"nonce": nonce, "id": nonce}),
        ("/contains/", {"sha": FIX_A}),
        ("/facts/", {"subject": subject or "silicon_protection:%s:nanosoc_gen:d04de2ec25d79c1fc3223f853ddb6725cbf2831c:gitlab" % RC4}),
    ]
    table = next((vals for prefix, vals in samples if prefix in path), {})
    return re.sub(r"\{([^}:]+)(:[^}]*)?\}",
                  lambda m: urllib.parse.quote(str(table.get(m.group(1), "x")), safe=":"), path)


def is_under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def sweep(client, app, who, *, methods=None, skip=()):
    """Request every route in the app's table as one identity. `skip`: route prefixes this identity
    does not request (a signed-in client must not hit /auth/logout mid-sweep)."""
    results = []
    for path, meths, directory in route_table(app):
        if directory is not None or is_under(path, skip):
            continue
        for m in sorted((meths - {"HEAD", "OPTIONS"}) or {"GET"}):
            if methods and m not in methods:
                continue
            r = client.request(m, fill(path), follow_redirects=False)
            results.append({"route": path, "method": m, "who": who, "status": r.status_code,
                            "location": r.headers.get("location", ""), "body": r.text[:200000]})
    return results


MEMBER_MUST_OPEN = {("GET", "/"), ("GET", "/silicon-protection"), ("GET", "/defects"), ("GET", "/api/v1/configurations")}


def check_a5(results, *, member, login_path, exempt=(), required=CONTRACT_ROUTES, must_open=MEMBER_MUST_OPEN):
    """A5: every route refuses anyone who is not a signed-in SoC-Labs member, and leaks nothing.
    Routes under `exempt` (sign-in) are held only to the no-leak rule."""
    probs = []
    swept = {(r["method"], norm_route(r["route"])) for r in results}
    for m, route in sorted(required - swept):
        probs.append("contracted route %s %s is not in the app's route table" % (m, route))
    if not any(r["who"] == member for r in results):
        probs.append("no member requests: the sweep did not run")
    opened = {(r["method"], r["route"]) for r in results if r["who"] == member and r["status"] == 200}
    for m, route in sorted(must_open - opened):
        if any(r["who"] == member for r in results):
            probs.append("member could not open %s %s" % (m, route))
    for r in results:
        key = "%s %s" % (r["method"], r["route"])
        if r["who"] == member:
            # A member is never refused; a 404 for a sample id is not a privacy question (H1 checks pages).
            if r["method"] == "GET" and (r["status"] in (401, 403) or r["status"] >= 500):
                probs.append("member got %d on %s" % (r["status"], key))
            continue
        leak = [s for s in LEAK_STRINGS if s in r["body"]]
        if leak:
            probs.append("%s saw %r on %s" % (r["who"], leak[0], key))
        if is_under(r["route"], exempt):
            continue
        if r["who"] == "anonymous":
            to_login = r["status"] in (302, 303, 307) and urllib.parse.urlsplit(r["location"]).path == login_path
            if not (r["status"] in (401, 403) or to_login):
                probs.append("anonymous got %d on %s" % (r["status"], key))
        elif r["status"] != 403:
            probs.append("non-member %s got %d on %s, want 403" % (r["who"], r["status"], key))
    return probs


# A6 holds in every phase, so it looks only at what A6 forbids: writes to a user's fork, and writes that
# touch a silicon release (coverage review Low 3). A Phase 4 write to an org repo's branch is not an A6
# matter (A2 forbids it in v1; E5 and H3 bound it later). A git write whose target cannot be resolved
# statically cannot be proven not to be a fork, so it is flagged unless it is the guarded git runner.
FORK_OR_YANK = [re.compile(p) for p in (
    r"/forks?\b",                                          # creating or writing through a fork endpoint
    r"repos/(?!SoC-Labs/)[A-Za-z0-9_.-]+/",                  # a GitHub repo outside the org: a user's fork
    r"projects/(?!soclabs(%2F|/))[^ ]+",                     # a GitLab project outside the group
    r"silicon/", r"\byank",                                 # a silicon tag or release; withdrawing a release
    r"^git (push|send-pack)\b.*\?",                          # a git write to a target nobody can check
)]


def check_a6(census, allowlist):
    """A6: no call site writes to a fork or touches a silicon release; every allowlisted write is guarded."""
    probs = []
    trigger = [a for a in allowlist if a.get("effect") == "write"]
    for c in census:
        text = "%s %s" % (c["method"], c["target"])
        if trigger and allowed(c, trigger):
            continue          # the one contracted write, the CI trigger; its guard is checked below
        if any(rx.search(text) for rx in FORK_OR_YANK):
            probs.append("fork or silicon write at %s:%s: %s" % (c["file"], c["line"], text))
        elif c["method"] == "?" and not allowed(c, allowlist):
            probs.append("unresolvable call at %s:%s cannot be proven read-only" % (c["file"], c["line"]))
    for a in allowlist:
        if a.get("effect") in ("write", "git-runner") and not a.get("guard"):
            probs.append("allowlisted %s %s %s declares no guard" % (a.get("effect"), a["method"], a["target"]))
    return probs


def requirement_phases(text):
    """ID -> phase ('v1', 'Process', 'All', 'Phase 2'...), from the REQUIREMENTS.md headings."""
    out = {}
    for m in re.finditer(r"^###\s+([A-Z]+\d+)\s+·\s+(?:Must|Should)\s+·\s+([^*\n]+?)\s*(?:\*.*)?$", text, re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


def git_commits(repo):
    """Every commit: sha, commit time, message, and the files it changed."""
    raw = git("log", "--format=%x1e%H%x1f%cI%x1f%B%x1f", "--name-only", cwd=repo)
    out = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        sha, when, msg, files = rec.split("\x1f", 3)
        out.append({"sha": sha, "at": when, "message": msg,
                    "files": [f for f in files.split("\n") if f.strip()]})
    return out


IMPLEMENTATION_PREFIXES = ("src/",)      # contracts, schemas, docs and tests for later phases are gate-2 work (§15)


def check_a7(gates_doc, phase_of, commits, routes, cli_commands, changes=None, automation=()):
    """A7: each later phase has a written, checked entry gate before its automation is enabled.
    Only commits that change implementation (src/) count: PR3 writes later phases' formats and
    tests before any code (interfaces.md §15), and that is not starting the phase.
    CR-5: a register `exemptions` entry {commit, ids, cr} takes its ids out of that one commit, and only
    while `changes` (docs/CHANGES.md) has a row for that CR naming the commit's short sha.
    CR-15: build and enable are separate. An implementation commit from CR15_BUILD_FROM on that names a
    later-phase id is building, not starting the phase; routes and commands may exist. The breach is an
    enabled automation (`automation`: the scheduler's job names) of a phase whose gate is not met."""
    probs = []
    commits = [c for c in commits if any(f.startswith(IMPLEMENTATION_PREFIXES) for f in c.get("files", []))
               and utc(c["at"]) < utc(CR15_BUILD_FROM)]
    rows = [r for r in (changes or "").splitlines() if r.startswith("|") and r.count("|") > 2]
    exempt = {}
    for e in (gates_doc or {}).get("exemptions") or []:
        sha, cr = str(e.get("commit") or ""), str(e.get("cr") or "")
        if len(sha) == 40 and cr and any(r.split("|")[1].strip() == cr and sha[:7] in r for r in rows):
            exempt.setdefault(sha, set()).update(e.get("ids") or [])
    gates = {g.get("phase"): g for g in (gates_doc or {}).get("phases", [])}
    # CR-3: an id stands alone; a review label such as `CI-E1` is not requirement E1.
    ids_rx = re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(sorted(phase_of, key=len, reverse=True)))
    for ph in LATER_PHASES:
        g = gates.get(ph)
        if not g:
            probs.append("%s: no entry gate written down" % ph)
            met, checked_at = False, None
        else:
            crit = g.get("entry_gate") or []
            if not crit:
                probs.append("%s: the entry gate has no criteria" % ph)
            for c in crit:
                if not c.get("criterion") or not c.get("check"):
                    probs.append("%s: criterion %r has no check" % (ph, c.get("criterion")))
            met = g.get("status") in ("gate-met", "started")
            checked = g.get("checked") or {}
            checked_at = checked.get("at")
            if met and not (checked_at and checked.get("by")):
                probs.append("%s: marked %s without checked.at and checked.by" % (ph, g.get("status")))
        ph_ids = {i for i, p in phase_of.items() if p == ph}
        named = lambda c: set(ids_rx.findall(c["message"])) - exempt.get(c["sha"], set())
        naming = [c for c in commits if named(c) & ph_ids]
        if naming:
            first = min(naming, key=lambda c: utc(c["at"]))
            if not met or not checked_at or utc(first["at"]) < utc(checked_at):
                probs.append("%s: implementation commit %s (%s) names %s before the entry gate was checked"
                             % (ph, first["sha"][:7], first["at"], sorted(named(first) & ph_ids)))
        if not met:
            governed = (g or {}).get("automation")
            for name in (governed if isinstance(governed, list) else LATER_AUTOMATION[ph]):
                if name in automation:
                    probs.append("%s: automation `%s` is enabled before its entry gate" % (ph, name))
    return probs


def _run_form_ok(form, conf):
    return (form["method"] == "post" and form["action"] == "/configurations/%s/runs" % conf
            and bool(form["fields"].get(ASSUMED["csrf_field"])))


def check_h1(pages, *, configurations, cli_subjects, counts=None, mentions=None):
    """H1: dashboard items and Run buttons; each page shows exactly what its CLI command gives.
    pages: route -> html; cli_subjects: route -> set of subjects; counts: (conf, item) -> int;
    mentions: (conf, item) -> set of words the cell must contain."""
    probs = []
    dash = pages.get("/")
    if dash is None:
        return ["dashboard / was not fetched"]
    p = PageScan(dash)
    for c in configurations:
        missing = [i for i in DASHBOARD_ITEMS if (c, i) not in p.items]
        if missing:
            probs.append("dashboard: %s lacks %s" % (c, ", ".join(missing)))
        forms = [f for f in p.forms if f["run"] and f["configuration"] == c]
        if len(forms) != 1:
            probs.append("dashboard: %s has %d Run T0 buttons, want 1" % (c, len(forms)))
        elif not _run_form_ok(forms[0], c):
            probs.append("dashboard: the Run T0 form of %s is not a CSRF-protected POST to /configurations/%s/runs" % (c, c))
        page = pages.get("/configurations/%s" % c)
        if page is not None:
            cforms = [f for f in PageScan(page).forms if f["run"]]
            if len(cforms) != 1 or not _run_form_ok(cforms[0], c):
                probs.append("/configurations/%s: no working Run T0 button" % c)
    for key, n in (counts or {}).items():
        got = first_int(p.items.get(key, ""))
        if got != n:
            probs.append("dashboard: %s %s shows %r, want %d" % (key[0], key[1], got, n))
    for key, words in (mentions or {}).items():
        text = p.items.get(key, "")
        for w in sorted(words):
            if w not in text:
                probs.append("dashboard: %s %s does not name %s" % (key[0], key[1], w))
    for route, want in cli_subjects.items():
        if not want:
            probs.append("%s: the CLI gives no facts, so the page proves nothing" % route)
            continue
        text = pages.get(route)
        if text is None:
            probs.append("%s: not fetched" % route)
            continue
        got = {s for s, _, _ in PageScan(text).facts}
        kinds = {s.split(":", 1)[0] for s in want}
        lacking = sorted(want - got)
        extra = sorted(s for s in got - want if s.split(":", 1)[0] in kinds)
        if lacking:
            probs.append("%s: lacks %d facts the CLI gives, e.g. %s" % (route, len(lacking), lacking[0]))
        if extra:
            probs.append("%s: shows %d facts the CLI does not give, e.g. %s" % (route, len(extra), extra[0]))
    return probs


def cookie_texts(value):
    """The readable forms of a signed session cookie (itsdangerous or Starlette): raw and decoded parts."""
    out = [value]
    for part in value.replace("|", ".").split("."):
        compressed = part.startswith(".")
        try:
            raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            out.append((zlib.decompress(raw) if compressed else raw).decode("utf-8", "replace"))
            try:
                out.append(zlib.decompress(raw).decode("utf-8", "replace"))
            except zlib.error:
                pass
        except (ValueError, zlib.error):
            pass
    return out


def check_h2(logins, sweeps, *, members, tokens=(), cookies=(), db_bytes=b"", actions=(), runs_by=()):
    """H2: only active members sign in and open pages; the user's token is kept nowhere; writes logged."""
    probs = []
    for who, o in sorted(logins.items()):
        if who in members and not o["session"]:
            probs.append("member %s could not sign in (%s)" % (who, o["status"]))
        if who not in members and o["session"]:
            probs.append("non-member %s was given a session" % who)
        if o.get("scope") is not None and o["scope"] != "read:org":
            probs.append("the authorize URL asks for scope %r, want read:org" % o["scope"])
    for who, results in sorted(sweeps.items()):
        opened = [r for r in results if r["status"] == 200]
        if who in members and not opened:
            probs.append("member %s opened no page" % who)
        if who not in members and opened:
            probs.append("non-member %s opened %d routes, e.g. %s %s" % (who, len(opened), opened[0]["method"], opened[0]["route"]))
    for t in tokens:
        if any(t in text for c in cookies for text in cookie_texts(c)):
            probs.append("a GitHub user token is inside the session cookie")
        if t.encode() in db_bytes:
            probs.append("a GitHub user token is stored in the database")
    for login in runs_by:
        if not any(a["login"] == login and a["action"] == ASSUMED["run_action"] for a in actions):
            probs.append("the Run by %s is not in actions" % login)
    return probs


def check_h3(rows, *, today, ci_project, alerts=(), gitlab_self=None, github_headers=None,
             oauth_scope=None, config=None, phase4_met=False):
    """H3: each credential's scope is recorded, within its role, checked where the host allows. H3
    holds in every phase: before the Phase 4 gate the set is exactly the 4 v1 credentials; after it,
    the Phase 4 credentials may exist too (their own scope checks arrive with Phase 4)."""
    probs = []
    names = {r["name"] for r in rows}
    allowed_names = set(V1_CREDENTIALS) | (LATER_CREDENTIALS if phase4_met else set())
    if not set(V1_CREDENTIALS) <= names or names - allowed_names:
        probs.append("credentials %s, want exactly %s" % (sorted(names), sorted(allowed_names)))
    if not phase4_met:
        for n in sorted(names & LATER_CREDENTIALS):
            probs.append("%s exists before Phase 4" % n)
    rows = [r for r in rows if r["name"] in V1_CREDENTIALS]
    by = {r["name"]: r for r in rows}
    for r in rows:
        name, rec = r["name"], set(r.get("recorded_scope") or [])
        if not rec:
            probs.append("%s: scope not recorded" % name)
        extra = rec - V1_CREDENTIALS.get(name, set())
        if extra:
            probs.append("%s: recorded scope %s is beyond its role" % (name, sorted(extra)))
        live = r.get("live_scope")
        if r.get("verdict") == "ok" and live is None and name != "github_oauth":
            probs.append("%s: reported ok without a live check" % name)
        if r.get("verdict") == "unverified" and not r.get("reason"):
            probs.append("%s: unverified without a reason" % name)
        if live is not None and set(live) != rec:
            probs.append("%s: live scope %s differs from the record %s" % (name, sorted(live), sorted(rec)))
        exp = r.get("expires")
        if not exp and name != "github_oauth":
            probs.append("%s: expiry not recorded" % name)
        if exp:
            left = (dt.date.fromisoformat(exp[:10]) - today).days
            if left <= 14 and not any(name in (a.get("subject") or "") and "expir" in (a.get("kind") or "")
                                      and not a.get("cleared_at") for a in alerts):
                probs.append("%s expires in %d days and no expiry alert is raised" % (name, left))
    if config is not None and config.get("credentials", {}).get("ci_trigger", {}).get("project") != ci_project:
        probs.append("ci_trigger is not scoped to %s" % ci_project)
    if gitlab_self is not None:
        g = by.get("gitlab_read", {})
        if set(gitlab_self.get("scopes") or []) != set(g.get("live_scope") or []):
            probs.append("gitlab_read: the tool reports %s, GitLab says %s" % (g.get("live_scope"), gitlab_self.get("scopes")))
        if gitlab_self.get("expires_at") and g.get("expires") and gitlab_self["expires_at"][:10] != g["expires"][:10]:
            probs.append("gitlab_read: recorded expiry %s, GitLab says %s" % (g.get("expires"), gitlab_self["expires_at"]))
    if github_headers is not None:
        if "x-oauth-scopes" in github_headers:
            probs.append("github_read is a classic token (%s): a classic token cannot be read-only on private repos"
                         % github_headers["x-oauth-scopes"])
        gexp = github_headers.get("github-authentication-token-expiration")
        if gexp and by.get("github_read", {}).get("expires") and gexp[:10] != by["github_read"]["expires"][:10]:
            probs.append("github_read: recorded expiry %s, GitHub says %s" % (by["github_read"]["expires"], gexp))
    if oauth_scope is not None and oauth_scope != "read:org":
        probs.append("the OAuth authorize URL asks for %r, want read:org" % oauth_scope)
    return probs


MIRROR_REF = re.compile(r"refs/r/(github|gitlab)/[^/]+/(heads|tags)/.+")


def check_h6_refs(mirror_git_dir):
    """H6: a mirror holds only heads and tags, in the contracted namespace (interfaces.md §3)."""
    refs = git("for-each-ref", "--format=%(refname)", cwd=mirror_git_dir).splitlines()
    probs = ["%s: ref %s is not a mirrored head or tag" % (Path(mirror_git_dir).name, r)
             for r in refs if not MIRROR_REF.fullmatch(r)]
    if not refs:
        probs.append("%s: the mirror holds no refs" % Path(mirror_git_dir).name)
    return probs


def check_h6_status(docs, *, expected, now, max_age_s=20 * 60):
    """H6: every expected repo has an ok mirror no older than max_age_s."""
    probs, seen = [], {}
    for d in docs:
        errs = validate("observation", d)
        if errs:
            probs.append("invalid mirror_status %s: %s" % (d.get("subject"), errs[0]))
            continue
        f = d["fact"]
        seen[(f["repo"]["host"], f["repo"]["path"])] = f
    for k in sorted(set(expected) - set(seen)):
        probs.append("%s:%s has no mirror" % k)
    for k, f in sorted(seen.items()):
        if not f["ok"]:
            probs.append("%s:%s: the last fetch failed: %s" % (k + (f["error"],)))
        if f["last_success_at"] is None:
            probs.append("%s:%s was never fetched" % k)
            continue
        age = (now - utc(f["last_success_at"])).total_seconds()
        if age > max_age_s:
            probs.append("%s:%s: mirror age %ds exceeds %ds" % (k + (age, max_age_s)))
    return probs


def check_h6_table(text, docs, *, now, slack_s=120):
    """H6: the `mirror status` table prints an age (seconds) per repo, consistent with last_success_at."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines or "age" not in lines[0]:
        return ["mirror status: the table has no `age` column"]
    col = lines[0].index("age")
    rows = {}
    for cells in lines[1:]:
        for d in docs:
            if d["fact"]["repo"]["path"] in cells and d["fact"]["repo"]["host"] in cells:
                rows[(d["fact"]["repo"]["host"], d["fact"]["repo"]["path"])] = cells
    probs = []
    for d in docs:
        k = (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"])
        cells = rows.get(k)
        if cells is None or len(cells) <= col or not cells[col].isdigit():
            probs.append("%s:%s: no age printed" % k)
            continue
        want = (now - utc(d["fact"]["last_success_at"])).total_seconds()
        if abs(int(cells[col]) - want) > slack_s:
            probs.append("%s:%s: age %ss printed, last_success_at says %.0fs" % (k + (cells[col], want)))
    return probs


def check_h6_cadence(first, second):
    """H6: every mirror advanced between two snapshots taken more than one poll apart."""
    a = {(d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]): d["fact"]["last_success_at"] for d in first}
    b = {(d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]): d["fact"]["last_success_at"] for d in second}
    return ["%s:%s was not refreshed between the snapshots" % k for k in sorted(a)
            if not b.get(k) or not a[k] or utc(b[k]) <= utc(a[k])]


# ---------------------------------------------------------------------------------------------
# Synthetic inputs for the checker-level controls
# ---------------------------------------------------------------------------------------------
def inv_doc(host, path, component, role, visibility="public"):
    return {"schema": 1, "kind": "repo_inventory", "subject": "repo_inventory:%s:%s" % (host, path),
            "observed_at": "2026-09-30T18:05:04Z",
            "sources": [{"kind": "%s_api" % host, "command": "GET listing of %s" % path}],
            "snapshot": {"id": "repo_inventory:%s@2026-09-30T18:05:04Z" % host, "scope": "repo_inventory:%s" % host},
            "fact": {"repo": {"host": host, "path": path}, "component": component, "role": role,
                     "visibility": visibility, "default_branch": "main"}}


def fixture_catalogue_and_listing():
    """The example catalogue padded with legacy fillers to the real 2026-09-30 totals (57 + 101)."""
    cat = example("component-catalogue.example.yaml")[0]
    listing = []
    for c in cat["components"]:
        listing.append((c["canonical"]["host"], c["canonical"]["path"], c["id"], "canonical"))
        listing += [(r["host"], r["path"], c["id"], "copy") for r in c.get("copies", [])]
    listing += [(g["repo"]["host"], g["repo"]["path"], None, "ignored") for g in cat.get("ignored", [])]
    for host, prefix in (("github", "SoC-Labs/filler-"), ("gitlab", "soclabs/filler-")):
        n = OBSERVED_TOTALS[host] - sum(1 for x in listing if x[0] == host)
        for i in range(n):
            cid = "filler-%s-%03d" % (host, i)
            cat["components"].append({"id": cid, "class": "legacy", "canonical": {"host": host, "path": "%s%03d" % (prefix, i)}})
            listing.append((host, "%s%03d" % (prefix, i), cid, "canonical"))
    return cat, listing


def fixture_page(route, observations, *, now, configurations=(), items=None, run_forms=True,
                 drop_link=None, drop_age=False, age_from="oldest"):
    """A page with the G4 hooks, built from real observations (the checker-level controls' input)."""
    ats = [o["observed_at"] for o in observations]
    ref = (min if age_from == "oldest" else max)(ats, key=utc) if ats else iso(now)
    out = ["<html><body>"]
    if not drop_age:
        out.append('<p %s="%d" %s="%s">data age</p>' % (HOOK["age"], (now - utc(ref)).total_seconds(), HOOK["oldest"], ref))
    for c in configurations:
        out.append('<tr %s="%s">' % (HOOK["configuration"], c))
        for i in (items or {}).get(c, DASHBOARD_ITEMS):
            out.append('<td %s="%s">%s</td>' % (HOOK["item"], i, htmlmod.escape(str((items or {}).get((c, i), "")))))
        if run_forms:
            out.append('<td><form %s method="post" action="/configurations/%s/runs"><input type="hidden" name="%s" value="x">'
                       '<button type="submit">Run T0</button></form></td>' % (HOOK["run"], c, ASSUMED["csrf_field"]))
        out.append("</tr>")
    for o in observations:
        s = o["subject"]
        out.append('<div %s="%s" %s="%s">%s' % (HOOK["fact"], htmlmod.escape(s), HOOK["fact_at"], o["observed_at"], htmlmod.escape(s)))
        if s != drop_link:
            out.append(' <a %s="%s" href="/facts/%s">source</a>' % (HOOK["source"], htmlmod.escape(s), urllib.parse.quote(s, safe="")))
        out.append("</div>")
    out.append("</body></html>")
    return "".join(out)


def fixture_source_pages(observations):
    return {"/facts/%s" % urllib.parse.quote(o["subject"], safe=""):
            "<ul>%s</ul>" % "".join("<li><code>%s</code></li>" % htmlmod.escape(s["command"]) for s in o["sources"])
            for o in observations}


def silicon_observations():
    return [d for d in example("observation.example.jsonl") if d["kind"] == "silicon_protection"]


# ---------------------------------------------------------------------------------------------
# Checker-level negative controls (no implementation needed; each runs a baseline first)
# ---------------------------------------------------------------------------------------------
def _nc_A1_checker():
    cat, listing = fixture_catalogue_and_listing()
    docs = [inv_doc(h, p, c, r) for h, p, c, r in listing]
    totals = dict(OBSERVED_TOTALS)
    assert check_a1(docs, totals, cat) == [], "baseline: a consistent inventory must pass"
    broken = [dict(d, fact=dict(d["fact"], component=None, role="unclassified"))
              if d["fact"]["repo"]["path"] == "soclabs/secworks-sha256-project" else d for d in docs]
    assert "unclassified: gitlab:soclabs/secworks-sha256-project" in check_a1(broken, totals, cat)
    gl = [d for d in docs if d["fact"]["repo"]["host"] == "gitlab"]
    cut = [d for d in docs if d is not gl[-1]]
    assert "gitlab: 100 repos inventoried, the host reports 101" in check_a1(cut, totals, cat)
    dup = json.loads(json.dumps(cat))
    dup["components"].append({"id": "tidelink-old", "class": "legacy", "canonical": {"host": "gitlab", "path": "soclabs/tidelink"}})
    assert "catalogue: gitlab:soclabs/tidelink belongs to more than one entry" in check_a1(docs, totals, dup)
    wrong = [dict(d, fact=dict(d["fact"], role="canonical")) if d["fact"]["repo"]["path"] == "soclabs/ahb_qspi" else d for d in docs]
    assert any("soclabs/ahb_qspi reported as canonical" in p for p in check_a1(wrong, totals, cat))


# A minimal package shaped like interfaces.md §0.5: the trigger (the one write), the OAuth code exchange
# (a POST that writes nothing), a db module whose put()/delete() are not HTTP, a caller of db.put in a
# file that also imports httpx, and the one git runner.
PLANTED_TRIGGER = (
    "import httpx\n"
    "from nanosoc_rm import db\n"
    "def trigger(client, pid, form, obs):\n"
    "    db.put(obs)\n"
    "    return client.post(f'projects/{pid}/trigger/pipeline', data=form)\n")
PLANTED_AUTH = (
    "import httpx\n"
    "def exchange(client, code):\n"
    "    return client.post('login/oauth/access_token', data={'code': code})\n")
PLANTED_DB = (
    "import sqlite3\n"
    "class Store:\n"
    "    def delete(self, subject):\n"
    "        pass\n"
    "def put(obs):\n"
    "    Store().delete(obs['subject'])\n")
PLANTED_GIT = (
    "import subprocess\n"
    "WRITES = ('push', 'send-pack', 'receive-pack')\n"
    "def run_git(*args):\n"
    "    if any(a in WRITES for a in args):\n"
    "        raise PermissionError('git write refused')\n"
    "    return subprocess.run(['git', *args], capture_output=True, text=True)\n")
PLANTED_WRITES = (
    "import subprocess, httpx\n"
    "from nanosoc_rm.ci.trigger import trigger as client\n"
    "def sneak(client, fork_url):\n"
    "    client.post('repos/SoC-Labs/X/git/refs', json={})\n"
    "    client.post('repos/alice/ip/git/refs', json={})\n"
    "    subprocess.run(['git', 'push', fork_url, 'HEAD'])\n"
    "    client.request('DELETE', 'repos/SoC-Labs/X/git/refs/tags/silicon/eth-chiplet-r0p0')\n"
    "def dyn(args):\n"
    "    subprocess.run(args)\n")
V1_ALLOWLIST = [
    {"file": "nanosoc_rm/ci/trigger.py", "kind": "http", "method": "POST", "target": "trigger/pipeline",
     "effect": "write", "guard": "target is config ci.project only", "reason": "Run T0 (A2, A4)"},
    {"file": "nanosoc_rm/auth.py", "kind": "http", "method": "POST", "target": "login/oauth/access_token",
     "effect": "read-post", "reason": "GitHub OAuth code exchange (H2, §14)"},
    {"file": "nanosoc_rm/db.py", "kind": "http", "method": "DELETE", "target": "?",
     "effect": "local", "reason": "sqlite row delete, not HTTP"},
    {"file": "nanosoc_rm/gitrun.py", "kind": "exec", "method": "?", "target": "git with unresolved subcommand",
     "effect": "git-runner", "guard": "refuses push, send-pack, receive-pack at run time", "reason": "every git call"},
]


def planted_package(tmp, extra=None):
    pkg = tmp / "pkg" / "nanosoc_rm"
    (pkg / "ci").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "ci" / "__init__.py").write_text("")
    (pkg / "ci" / "trigger.py").write_text(PLANTED_TRIGGER)
    (pkg / "auth.py").write_text(PLANTED_AUTH)
    (pkg / "db.py").write_text(PLANTED_DB)
    (pkg / "gitrun.py").write_text(PLANTED_GIT)
    if extra:
        (pkg / "planted.py").write_text(extra)
    return pkg


def _v1_credentials():
    return {"github_read": {"scope": ["contents:read", "metadata:read", "members:read"], "expires": "2027-06-30"},
            "gitlab_read": {"scope": ["read_api", "read_repository"], "expires": "2027-06-30"},
            "ci_trigger": {"scope": ["trigger"], "project": CI_PROJECT, "expires": "2027-06-30"},
            "github_oauth": {"scope": ["read:org"]}}


def _nc_A2_checker(tmp):
    runs = [{"nonce": "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f", "configuration": RC4, "trigger": "manual",
             "triggered_by": "alice", "triggered_at": "2026-10-01T02:03:10Z"},
            {"nonce": "9c1e4f7a2b6d4e0f8a3c5b7d9e1f2a4c", "configuration": RC4, "trigger": "schedule",
             "triggered_by": "schedule", "triggered_at": "2026-10-01T02:00:00Z"}]
    actions = [{"at": "2026-10-01T02:03:11Z", "login": "alice", "action": "run_t0", "target": RC4, "result": "ok"}]
    live = {"scopes": ["read_api", "read_repository"], "active": True, "revoked": False}
    clean = planted_package(tmp / "clean")
    census = write_call_census(clean)
    kinds = {(c["file"], c["method"]) for c in census}
    assert kinds == {("nanosoc_rm/ci/trigger.py", "POST"), ("nanosoc_rm/auth.py", "POST"),
                     ("nanosoc_rm/db.py", "DELETE"), ("nanosoc_rm/gitrun.py", "?")}, \
        "baseline census: db.put (a package module) is internal, the rest are found: %s" % sorted(kinds)
    assert check_a2(census, V1_ALLOWLIST, _v1_credentials(), ci_project=CI_PROJECT, gitlab_self=live,
                    runs=runs, actions=actions) == [], "baseline: a trigger-only package must pass"
    dirty = write_call_census(planted_package(tmp / "dirty", PLANTED_WRITES))
    probs = check_a2(dirty, V1_ALLOWLIST, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=actions)
    assert any("git push" in p for p in probs), probs
    assert any("POST repos/SoC-Labs/X/git/refs" in p for p in probs), probs   # an object imported from the package is not exempt
    assert any("DELETE repos/SoC-Labs/X/git/refs/tags" in p for p in probs), probs
    assert any("unresolved argv" in p for p in probs), probs
    blind = check_a2([], V1_ALLOWLIST, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=actions)
    assert "the census did not find the trigger call itself: the census is blind" in blind
    two = V1_ALLOWLIST + [dict(V1_ALLOWLIST[0], file="nanosoc_rm/bump.py", target="repos/x/pulls")]
    assert "the v1 write allowlist must hold exactly one write: the CI pipeline trigger" in \
        check_a2(census, two, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=actions)
    graphql = V1_ALLOWLIST + [dict(V1_ALLOWLIST[1], target="graphql")]
    assert any(p.startswith("read-post graphql is not a known read-only POST") for p in
               check_a2(census, graphql, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=actions))
    (clean / "db.py").write_text("import httpx\n" + PLANTED_DB)           # "local" is refused in an HTTP file
    probs = check_a2(write_call_census(clean), V1_ALLOWLIST, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=actions)
    assert any(p.startswith("write call not allowlisted: nanosoc_rm/db.py") for p in probs), probs
    (clean / "db.py").write_text(PLANTED_DB)
    creds = _v1_credentials()
    creds["gitlab_read"]["scope"] = ["api"]
    creds["ci_trigger"]["project"] = "soclabs/nanosoc-registry"
    probs = check_a2(census, V1_ALLOWLIST, creds, ci_project=CI_PROJECT, runs=runs, actions=actions)
    assert "gitlab_read: scope ['api'] is beyond read access" in probs
    assert any(p.startswith("ci_trigger is scoped to 'soclabs/nanosoc-registry'") for p in probs)
    probs = check_a2(census, V1_ALLOWLIST, _v1_credentials(), ci_project=CI_PROJECT, runs=runs, actions=[])
    assert "run 5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f by alice has 0 actions rows, want 1" in probs


def _nc_A3_checker():
    obs = silicon_observations()
    assert len(obs) == RC4_SILICON_ROWS and sum(1 for o in obs if o["fact"]["pinned"]) == RC4_SILICON_PINNED
    stored = {o["subject"]: o for o in obs}
    now = utc("2026-10-01T12:00:00Z")
    sources = fixture_source_pages(obs)
    fetch = sources.get
    good = fixture_page("/silicon-protection", obs, now=now)
    assert check_a3_page("/silicon-protection", good, stored, now, fetch) == [], "baseline must pass"
    s0 = obs[0]["subject"]
    probs = check_a3_page("/silicon-protection", fixture_page("/silicon-protection", obs, now=now, drop_link=s0), stored, now, fetch)
    assert "/silicon-protection: fact %s has no source link" % s0 in probs
    probs = check_a3_page("/silicon-protection", fixture_page("/silicon-protection", obs, now=now, drop_age=True), stored, now, fetch)
    assert "/silicon-protection: 0 data-age elements, want exactly 1" in probs
    newest = dict(obs[-1], observed_at="2026-10-03T00:00:01Z")     # CR-2: newer than every re-derived row
    mixed = obs[:-1] + [newest]
    stored2 = dict(stored, **{newest["subject"]: newest})
    probs = check_a3_page("/silicon-protection", fixture_page("/silicon-protection", mixed, now=now, age_from="newest"),
                          stored2, now, fixture_source_pages(mixed).get)
    assert any("data age is measured from 2026-10-03T00:00:01Z" in p for p in probs), probs
    sourceless = dict(obs[1], sources=[])
    probs = check_a3_page("/silicon-protection", good, dict(stored, **{obs[1]["subject"]: sourceless}), now, fetch)
    assert "/silicon-protection: %s has no sources in the store" % obs[1]["subject"] in probs
    assert check_a3_page("/", "<html></html>", {}, now, fetch) == [
        "/: 0 data-age elements, want exactly 1", "/: no facts rendered, so nothing is proven"]


def _a4_consistent():
    nonce = "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f"
    run = json.loads(json.dumps([d for d in example("observation.example.jsonl") if d["kind"] == "t0_result"][0]))
    run["fact"].update(acceptance="accepted", reason=None, verdict="FAIL", bundle={"store": "ci", "path": "rm-bundle"})
    run["fact"]["run"].update(triggered_by="alice")
    req = json.dumps({"schema": 1, "nonce": nonce, "configuration": RC4, "repo": {"host": "github", "path": "SoC-Labs/NanoSoC-Ethernet-Chiplet"},
                      "commit": RC4_COMMIT, "upgrade_from": None, "canonical_urls": {}})
    return run, {"id": 1001, "source": "trigger", "ref": "main"}, [{"id": 2002, "name": "t0"}], [{"key": "RM_REQUEST", "value": req}]


def _nc_A4_checker():
    run, pipe, jobs, var = _a4_consistent()
    kw = dict(configuration=RC4, commit=RC4_COMMIT, login="alice")
    assert check_a4(run, pipe, jobs, var, **kw) == [], "baseline must pass"
    nopipe = json.loads(json.dumps(run))
    nopipe["fact"]["run"]["pipeline_id"] = None
    assert check_a4(nopipe, None, [], [], **kw) == ["no pipeline for nonce 5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f: nothing was started in the CI project"]
    foreign = [{"key": "RM_REQUEST", "value": var[0]["value"].replace("5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f", "0" * 32)}]
    assert any(p.startswith("RM_REQUEST nonce 000") for p in check_a4(run, pipe, jobs, foreign, **kw))
    assert "pipeline has 2 jobs named t0, want 1" in check_a4(run, pipe, jobs * 2, var, **kw)
    pending = json.loads(json.dumps(run))
    pending["fact"].update(acceptance="pending", verdict=None)
    assert "the result has not appeared: the run is still pending" in check_a4(pending, pipe, jobs, var, **kw)
    other = json.loads(json.dumps(run))
    other["fact"]["run"]["job_id"] = 9999
    assert "the result carries job 9999, the pipeline's t0 job is 2002" in check_a4(other, pipe, jobs, var, **kw)


def _a5_results(extra_open=None):
    res = []
    for m, route in sorted(CONTRACT_ROUTES):
        if m == "GET":
            res.append({"route": route, "method": m, "who": "alice", "status": 200, "location": "", "body": RC4_COMMIT})
        res.append({"route": route, "method": m, "who": "bob", "status": 403, "location": "", "body": "Forbidden"})
        res.append({"route": route, "method": m, "who": "anonymous", "status": 302, "location": "/auth/login?next=/", "body": ""})
    if extra_open:
        res.append({"route": extra_open, "method": "GET", "who": "bob", "status": 200, "location": "", "body": RC4_COMMIT})
    return res


def _nc_A5_checker():
    kw = dict(member="alice", login_path=ASSUMED["login_path"])
    assert check_a5(_a5_results(), **kw) == [], "baseline must pass"
    probs = check_a5(_a5_results("/negctl/open"), **kw)
    assert "non-member bob got 200 on GET /negctl/open, want 403" in probs
    assert "bob saw %r on GET /negctl/open" % RC4_COMMIT in probs
    assert "no member requests: the sweep did not run" in check_a5([r for r in _a5_results() if r["who"] != "alice"], **kw)
    missing = [r for r in _a5_results() if r["route"] != "/silicon-protection"]
    assert "contracted route GET /silicon-protection is not in the app's route table" in check_a5(missing, **kw)


def _nc_A6_checker(tmp):
    clean = write_call_census(planted_package(tmp / "clean"))
    assert check_a6(clean, V1_ALLOWLIST) == [], "baseline must pass"
    probs = check_a6(write_call_census(planted_package(tmp / "dirty", PLANTED_WRITES)), V1_ALLOWLIST)
    assert any("git push" in p for p in probs), probs                          # to an unresolvable (fork) URL
    assert any("git/refs/tags/silicon/eth-chiplet-r0p0" in p for p in probs), probs
    assert any("repos/alice/ip/git/refs" in p for p in probs), probs           # a user's fork
    # An org repo's branch write is A2's business in v1, not A6's: A6 stays true in Phase 4.
    assert not any("repos/SoC-Labs/X/git/refs " in p or p.endswith("repos/SoC-Labs/X/git/refs") for p in probs), probs
    unguarded = [dict(V1_ALLOWLIST[0], guard="")] + V1_ALLOWLIST[1:]
    assert "allowlisted write POST trigger/pipeline declares no guard" in check_a6(clean, unguarded)
    # The git runner's guard is real: it refuses a push at run time (loaded from the planted package).
    spec = importlib.util.spec_from_file_location("planted_gitrun", tmp / "clean" / "pkg" / "nanosoc_rm" / "gitrun.py")
    gitrun = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gitrun)
    with pytest.raises(PermissionError):
        gitrun.run_git("push", "origin", "HEAD")
    rc4 = next(c for c in example("eligibility.example.yaml")[0]["configurations"] if c["id"] == RC4)
    assert validate("eligibility", {"schema": 1, "configurations": [rc4]}) == [], "baseline: the real rc4 entry is valid"
    assert validate("eligibility", {"schema": 1, "configurations": [dict(rc4, yanked=True)]}), "a yank field must be rejected"
    assert validate("eligibility", {"schema": 1, "configurations": [dict(rc4, frozen=False)]}), "silicon must stay frozen"


def _nc_A7_checker(tmp):
    phase_of = requirement_phases(REQUIREMENTS_MD.read_text())
    assert phase_of.get("D3") == "Phase 2" and phase_of.get("A7") == "All" and phase_of.get("G3") == "Phase 5"
    repo = git_init(tmp / "toolrepo")
    commit(repo, "README.md", "x\n", "docs: requirements (PR1)", "2026-09-30T10:00:00Z")
    # Gate-2 work names later-phase IDs legitimately (formats fixed now, interfaces.md §15): not flagged.
    commit(repo, "schemas/release-lock.schema.json", "{}\n", "contracts: release-lock for G3, manifest for C11",
           "2026-10-01T10:00:00Z")
    commit(repo, "src/matrix.py", "x\n", "feat(matrix): D3 compatibility matrix view", "2026-10-05T10:00:00Z")
    gates = {"phases": [
        {"phase": ph, "entry_gate": [{"criterion": "stated in proposal §9", "check": "a command or evidence path"}],
         "status": "not-started", "checked": {}} for ph in LATER_PHASES]}
    probs = check_a7(gates, phase_of, git_commits(repo), routes=["/"], cli_commands=["walk"])
    assert any(p.startswith("Phase 2: implementation commit") and "['D3']" in p for p in probs), probs
    assert not [p for p in probs if "Phase 3" in p or "Phase 5" in p], "a contracts-only commit is not implementation: %s" % probs
    met = json.loads(json.dumps(gates))
    met["phases"][0].update(status="gate-met", checked={"at": "2026-10-04T09:00:00Z", "by": "david", "evidence": "x"})
    assert check_a7(met, phase_of, git_commits(repo), routes=["/"], cli_commands=["walk"]) == [], "baseline must pass"
    # CR-15: a /releases route and a `lock` command may be built; an enabled nightly matrix may not
    assert check_a7(met, phase_of, git_commits(repo), routes=["/", "/releases"], cli_commands=["walk", "lock"]) == []
    probs = check_a7(gates, phase_of, [], routes=["/"], cli_commands=["walk"], automation=["poll", "nightly_matrix"])
    assert probs == ["Phase 2: automation `nightly_matrix` is enabled before its entry gate"], probs
    assert check_a7(met, phase_of, [], ["/"], ["walk"], automation=["nightly_matrix"]) == []
    assert "Phase 2: no entry gate written down" in check_a7({"phases": met["phases"][1:]}, phase_of, [], [], [])
    # CR-5: an exemption for the D3 commit counts only with a CHANGES.md row for its CR naming that commit
    d3 = next(c for c in git_commits(repo) if "D3" in c["message"])
    ex = dict(gates, exemptions=[{"commit": d3["sha"], "ids": ["D3"], "cr": "CR-5"}])
    row = "| CR-5 | 2026-10-10 | D7 | %s names it | why | David |" % d3["sha"][:7]
    assert not [p for p in check_a7(ex, phase_of, git_commits(repo), ["/"], ["walk"], changes=row) if "Phase 2" in p]
    for changes in (None, row.replace("CR-5", "CR-9"), row.replace(d3["sha"][:7], "0000000")):
        assert any("['D3']" in p for p in check_a7(ex, phase_of, git_commits(repo), ["/"], ["walk"], changes=changes))
    # CR-15: the same D3 commit made after the build-allowed date is building, not starting Phase 2
    commit(repo, "src/matrix2.py", "x\n", "feat(matrix): D3 matrix cells", "2026-10-13T10:00:00Z")
    late = [c for c in git_commits(repo) if "cells" in c["message"]]
    assert check_a7(gates, phase_of, late, ["/"], ["walk"]) == []


def _nc_H1_checker():
    obs = silicon_observations()
    now = utc("2026-10-01T12:00:00Z")
    confs = [RC4, "eth-main"]
    items = {(RC4, "red-pins"): 0, (RC4, "open-errata"): 1, (RC4, "host-drift"): "tidelink ahb_qspi asic-flow"}
    dash = fixture_page("/", [], now=now, configurations=confs, items=items)
    sp = fixture_page("/silicon-protection", obs, now=now)
    conf_pages = {"/configurations/%s" % c: fixture_page("/", [], now=now, configurations=[c]) for c in confs}
    pages = dict({"/": dash, "/silicon-protection": sp}, **conf_pages)
    want = {"/silicon-protection": {o["subject"] for o in obs}}
    kw = dict(configurations=confs, cli_subjects=want, counts={(RC4, "red-pins"): 0, (RC4, "open-errata"): 1},
              mentions={(RC4, "host-drift"): RC4_DRIFT})
    assert check_h1(pages, **kw) == [], "baseline must pass"
    no_runner = dict(pages, **{"/": dash.replace('%s="runner-status"' % HOOK["item"], 'data-x="runner-status"')})
    assert "dashboard: eth-rc4 lacks runner-status" in check_h1(no_runner, **kw)
    one_row = dict(pages, **{"/": fixture_page("/", [], now=now, configurations=[RC4], items=items)})
    assert "dashboard: eth-main has 0 Run T0 buttons, want 1" in check_h1(one_row, **kw)
    no_csrf = dict(pages, **{"/": dash.replace('name="%s"' % ASSUMED["csrf_field"], 'name="other"')})
    assert any("is not a CSRF-protected POST" in p for p in check_h1(no_csrf, **kw))
    short = dict(pages, **{"/silicon-protection": fixture_page("/silicon-protection", obs[1:], now=now)})
    assert any(p.startswith("/silicon-protection: lacks 1 facts") for p in check_h1(short, **kw))
    invented = dict(obs[0], subject="silicon_protection:%s:invented:%s" % (RC4, "0" * 40))
    extra = dict(pages, **{"/silicon-protection": fixture_page("/silicon-protection", obs + [invented], now=now)})
    assert any(p.startswith("/silicon-protection: shows 1 facts the CLI does not give") for p in check_h1(extra, **kw))
    wrong = dict(pages, **{"/": fixture_page("/", [], now=now, configurations=confs, items={**items, (RC4, "red-pins"): 3})})
    assert "dashboard: eth-rc4 red-pins shows 3, want 0" in check_h1(wrong, **kw)


def _nc_H2_checker():
    members = {"alice"}
    logins = {"alice": {"status": 302, "session": True, "scope": "read:org"},
              "bob": {"status": 403, "session": False, "scope": "read:org"},
              "carol": {"status": 403, "session": False, "scope": "read:org"}}
    ok = [{"route": "/", "method": "GET", "status": 200}]
    no = [{"route": "/", "method": "GET", "status": 403}]
    sweeps = {"alice": ok, "bob": no, "carol": no}
    cookie = base64.urlsafe_b64encode(json.dumps({"login": "alice"}).encode()).decode().rstrip("=") + ".sig"
    actions = [{"login": "alice", "action": ASSUMED["run_action"], "target": "estate-main"}]
    kw = dict(members=members, tokens=["fake-token-alice"], cookies=[cookie], db_bytes=b"...", actions=actions, runs_by=["alice"])
    assert check_h2(logins, sweeps, **kw) == [], "baseline must pass"
    assert "non-member bob opened 1 routes, e.g. GET /" in check_h2(logins, dict(sweeps, bob=ok), **kw)
    assert "non-member carol was given a session" in check_h2(dict(logins, carol={"status": 302, "session": True}), sweeps, **kw)
    leaky = base64.urlsafe_b64encode(json.dumps({"login": "alice", "gh": "fake-token-alice"}).encode()).decode().rstrip("=") + ".sig"
    assert "a GitHub user token is inside the session cookie" in check_h2(logins, sweeps, **dict(kw, cookies=[leaky]))
    assert "the Run by alice is not in actions" in check_h2(logins, sweeps, **dict(kw, actions=[]))
    assert "the authorize URL asks for scope 'repo', want read:org" in check_h2(dict(logins, alice=dict(logins["alice"], scope="repo")), sweeps, **kw)


def _h3_rows():
    return [{"name": "github_read", "recorded_scope": ["contents:read", "metadata:read", "members:read"], "live_scope": None,
             "expires": "2027-06-30", "verdict": "unverified", "reason": "fine-grained tokens cannot report their permissions"},
            {"name": "gitlab_read", "recorded_scope": ["read_api", "read_repository"], "live_scope": ["read_api", "read_repository"],
             "expires": "2027-06-30", "verdict": "ok", "reason": None},
            {"name": "ci_trigger", "recorded_scope": ["trigger"], "live_scope": None, "expires": "2027-06-30",
             "verdict": "unverified", "reason": "a trigger token cannot be listed with read scope"},
            {"name": "github_oauth", "recorded_scope": ["read:org"], "live_scope": None, "expires": "", "verdict": "ok", "reason": None}]


def _nc_H3_checker():
    today = dt.date(2026, 10, 1)
    gl_self = {"scopes": ["read_api", "read_repository"], "expires_at": "2027-06-30", "active": True}
    kw = dict(today=today, ci_project=CI_PROJECT, gitlab_self=gl_self, github_headers={}, oauth_scope="read:org",
              config={"credentials": {"ci_trigger": {"project": CI_PROJECT}}})
    assert check_h3(_h3_rows(), **kw) == [], "baseline must pass"
    rows = _h3_rows()
    rows[1]["recorded_scope"] = ["api"]
    assert "gitlab_read: recorded scope ['api'] is beyond its role" in check_h3(rows, **kw)
    rows = _h3_rows()
    rows[1]["live_scope"] = ["read_api", "write_repository"]
    assert any(p.startswith("gitlab_read: live scope ['read_api', 'write_repository'] differs") for p in check_h3(rows, **kw))
    rows = _h3_rows()
    rows[0]["verdict"] = "ok"
    assert "github_read: reported ok without a live check" in check_h3(rows, **kw)
    assert any("classic token" in p for p in check_h3(_h3_rows(), **dict(kw, github_headers={"x-oauth-scopes": "repo, read:org"})))
    rows = _h3_rows()
    rows[2]["expires"] = "2026-10-11"
    assert "ci_trigger expires in 10 days and no expiry alert is raised" in check_h3(rows, **kw)
    assert check_h3(rows, **dict(kw, alerts=[{"kind": "token_expiry", "subject": "credential:ci_trigger", "cleared_at": None}])) == []
    rows = _h3_rows() + [{"name": "bump_writer", "recorded_scope": ["write_repository"], "expires": "2027-01-01", "verdict": "ok", "live_scope": ["write_repository"]}]
    assert "bump_writer exists before Phase 4" in check_h3(rows, **kw)
    assert check_h3(rows, **dict(kw, phase4_met=True)) == [], "after the Phase 4 gate the bump writer may exist"


def _nc_H6_checker(tmp):
    est = local_estate(tmp / "estate")
    bad = tmp / "by-mirror.git"
    git("clone", "-q", "--mirror", str(est["ip"]), str(bad))
    probs = check_h6_refs(bad)
    assert "by-mirror.git: ref refs/merge-requests/1/head is not a mirrored head or tag" in probs, probs
    bad2 = tmp / "soc-by-mirror.git"
    git("clone", "-q", "--mirror", str(est["soc"]), str(bad2))
    assert "soc-by-mirror.git: ref refs/pull/1/head is not a mirrored head or tag" in check_h6_refs(bad2)
    key = "refs/r/gitlab/%s" % enc("soclabs/ip")
    refspec = ["+refs/heads/*:%s/heads/*" % key, "+refs/tags/*:%s/tags/*" % key]
    url = "file://%s" % est["ip"]
    # The fetch as the FIRST draft of interfaces.md section 3 wrote it, without --no-tags: tag auto-follow
    # also writes refs/tags/* OUTSIDE the per-repo namespace, so one host's tags squat the shared namespace
    # (gap G19, now fixed in the contract). Caught.
    literal = git_init(tmp / "literal-s3.git", bare=True)
    git("fetch", "-q", "--prune", url, *refspec, cwd=literal)
    assert "literal-s3.git: ref refs/tags/v0.1.0 is not a mirrored head or tag" in check_h6_refs(literal)
    # With --no-tags (the contracted fetch) only the contracted namespace is written: the baseline passes.
    good = git_init(tmp / "estate-ip.git", bare=True)
    git("fetch", "-q", "--no-tags", "--prune", url, *refspec, cwd=good)
    assert check_h6_refs(good) == [], "baseline: the contracted refspec with --no-tags must pass"
    absent = subprocess.run(["git", "cat-file", "-e", est["c3"]], cwd=good, env=dict(os.environ, **GIT_ENV),
                            capture_output=True)
    assert absent.returncode != 0, "the MR-only commit must not be in a heads-and-tags mirror"
    now = utc("2026-10-01T12:00:00Z")
    fresh = [{"schema": 1, "kind": "mirror_status", "subject": "mirror_status:gitlab:soclabs/ip", "observed_at": "2026-10-01T11:50:00Z",
              "sources": [{"kind": "git", "command": "git -C mirrors/estate-ip.git fetch --no-tags --prune --filter=blob:none <url> ..."}],
              "snapshot": {"id": "mirror_status:estate-ip@2026-10-01T11:50:00Z", "scope": "mirror_status:estate-ip"},
              "fact": {"repo": {"host": "gitlab", "path": "soclabs/ip"}, "component": "estate-ip", "ok": True,
                       "last_success_at": "2026-10-01T11:50:00Z", "error": None}}]
    expected = {("gitlab", "soclabs/ip")}
    assert check_h6_status(fresh, expected=expected, now=now) == [], "baseline must pass"
    stale = [dict(fresh[0], fact=dict(fresh[0]["fact"], last_success_at="2026-10-01T11:20:00Z"))]
    assert "gitlab:soclabs/ip: mirror age 2400s exceeds 1200s" in check_h6_status(stale, expected=expected, now=now)
    failed = [dict(fresh[0], fact=dict(fresh[0]["fact"], ok=False, error="fatal: repository not found"))]
    assert "gitlab:soclabs/ip: the last fetch failed: fatal: repository not found" in check_h6_status(failed, expected=expected, now=now)
    assert "github:SoC-Labs/soc has no mirror" in check_h6_status(fresh, expected=expected | {("github", "SoC-Labs/soc")}, now=now)
    later = [dict(fresh[0], fact=dict(fresh[0]["fact"], last_success_at="2026-10-01T12:05:00Z"))]
    assert check_h6_cadence(fresh, later) == [], "baseline: an advanced mirror passes"
    assert check_h6_cadence(fresh, fresh) == ["gitlab:soclabs/ip was not refreshed between the snapshots"]


# =============================================================================================
# Acceptance tests
# =============================================================================================


def run_form(html, configuration):
    """The Run T0 form of one configuration on a page, or None: inside that configuration's
    data-configuration element (dashboard), or posting to its runs route (configuration page)."""
    action = "/configurations/%s/runs" % configuration
    return next((f for f in PageScan(html).forms
                 if f["run"] and (f["configuration"] == configuration or f["action"] == action)), None)


def refs_of(bare):
    return git("for-each-ref", "--format=%(objectname) %(refname)", cwd=bare)


# ----- A1 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A1")
@pytest.mark.live
@NOT_BUILT
def test_AT_A1():
    cfg_path, _ = live_env()
    cfg = read_config(cfg_path)
    # Given the live GitHub org SoC-Labs and GitLab group soclabs (with subgroups), read with the
    #   poller's read-only tokens, and the registry's catalogue.yaml at main.
    cat = load_docs(registry_dir(cfg) / "catalogue.yaml")[0]
    # When `nanosoc-rm inventory --json` and `nanosoc-rm inventory --unclassified --json` run.
    rc, docs, p = cli(cfg_path, "inventory", "--json")
    assert rc in (0, 1), p.stderr
    rc_u, flagged, p_u = cli(cfg_path, "inventory", "--unclassified", "--json")
    # Then each host's count equals the host's own total, read from a DIFFERENT endpoint than the
    #   tool's listing (57 and 101 on 2026-09-30; the test reads today's)...
    st, _, body = http_get("%s/orgs/%s" % (cfg["hosts"]["github"]["api"], GITHUB_ORG), tok=token(cfg, "github_read"))
    org = json.loads(body) if st == 200 else {}
    gh_total = (org["public_repos"] + org["total_private_repos"]) if "total_private_repos" in org else None
    st, hdrs, _ = http_get("%s/groups/%s/projects?include_subgroups=true&per_page=1" % (cfg["hosts"]["gitlab"]["api"],
                                                                                         GITLAB_GROUP), tok=token(cfg, "gitlab_read"))
    gl_total = int(hdrs["x-total"]) if st == 200 and "x-total" in hdrs else None
    # ...every row is valid and classified with its catalogue role, no repo is listed twice, and
    #   each component has exactly one canonical repo, found on its host.
    assert check_a1(docs, {"github": gh_total, "gitlab": gl_total}, cat) == []
    # And the flag list is empty with exit 0 (interfaces.md §3: exit 1 if any repo is unclassified).
    assert flagged == [] and rc_u == 0, p_u.stdout


@pytest.mark.requirement("A1")
def test_AT_A1_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: a consistent inventory passes; unclassified, truncated at a page boundary,
    # duplicated in the catalogue and mis-roled each fail.
    _nc_A1_checker()
    # System level: fake hosts serve the real totals (57 + 101, paged as the hosts page them, so
    # GitLab needs 2 pages at per_page=100), and the catalogue has secworks-sha256-project removed.
    # CR-4: GitHub serves at most 25 per page, so its 57 repos need 3 pages and the Link header is exercised.
    cat, listing = fixture_catalogue_and_listing()
    cat["components"] = [c for c in cat["components"] if c["id"] != "secworks-sha256-project"]
    github_listing(fake_github, [(p, "public") for h, p, _, _ in listing if h == "github"], cap=25)
    gitlab_listing(fake_gitlab, [(p, "public") for h, p, _, _ in listing if h == "gitlab"])
    registry = write_registry(tmp_path / "registry", catalogue=cat, eligibility=example("eligibility.example.yaml")[0],
                              defects=example("defect.SL-DEF-0001.example.yaml"),
                              errata=example("erratum.ETH-R0P0-ERR-001.example.yaml"),
                              imports=example("import-ledger.example.yaml")[0])
    cfg_path = write_config(tmp_path / "config.toml", data=tmp_path / "data", registry=registry,
                            github=fake_github, gitlab=fake_gitlab)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg_path))
    # Then the tool flags exactly that repo, exits 1, and check_a1 names it.
    rc, flagged, p = cli(cfg_path, "inventory", "--unclassified", "--json")
    assert rc == 1, "an unclassified repo must make `inventory --unclassified` exit 1"
    assert [d["subject"] for d in flagged] == ["repo_inventory:gitlab:soclabs/secworks-sha256-project"]
    rc, docs, _ = cli(cfg_path, "inventory", "--json")
    assert check_a1(docs, dict(OBSERVED_TOTALS), cat) == ["unclassified: gitlab:soclabs/secworks-sha256-project"]
    assert any(e["query"].get("page") == ["2"] for e in fake_gitlab.log), "the tool never asked for GitLab's page 2"
    assert any(e["query"].get("page") == ["3"] for e in fake_github.log), "the tool never followed GitHub's Link to page 3"


# ----- A2 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A2")
@pytest.mark.live
@NOT_BUILT
def test_AT_A2():
    cfg_path, _ = live_env()
    cfg = read_config(cfg_path)
    # Given the deployed package, the config's credentials, and runs/actions after >= 1 manual Run
    #   (AT-A4 or the H1 demo).
    pkg = Path(importlib.util.find_spec("nanosoc_rm").submodule_search_locations[0])
    # When the write-call census runs over the package (every HTTP write method, every non-GET
    #   request, every git push/send-pack, every gh/glab, every argv or method it cannot resolve)...
    census = write_call_census(pkg)
    allowlist = list(resolve("write_calls"))
    # ...GitLab describes the poller token itself...
    st, _, body = http_get("%s/personal_access_tokens/self" % cfg["hosts"]["gitlab"]["api"], tok=token(cfg, "gitlab_read"))
    assert st == 200, "GitLab must describe the poller token (GET personal_access_tokens/self)"
    # ...and the runs and actions tables are read.
    db = data_dir(cfg) / "rm.sqlite"
    runs = sqlite_rows(db, "SELECT nonce, configuration, trigger, triggered_by, triggered_at FROM runs")
    actions = sqlite_rows(db, "SELECT at, login, action, target, result FROM actions")
    # Then exactly one write call exists (POST trigger/pipeline) and it is found; the config holds
    #   exactly the 4 v1 credentials, read-only except the trigger, which is scoped to compat-ci;
    #   GitLab's live scopes equal the record; every manual Run has exactly one actions row.
    assert check_a2(census, allowlist, dict(cfg["credentials"]), ci_project=cfg["ci"]["project"],
                    gitlab_self=json.loads(body), runs=runs, actions=actions) == []
    assert cfg["ci"]["project"] == CI_PROJECT


@pytest.mark.requirement("A2")
def test_AT_A2_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: planted push/POST/DELETE, an unresolvable argv, a second write, an unknown
    # read-post, a "local" claim in an HTTP file, scope `api`, a trigger for another project and a
    # Run without its actions row are each caught; a package shaped like §0.5 passes.
    _nc_A2_checker(tmp_path / "checker")
    # System level: one real (offline) Run, then its actions row deleted from a copy of the database.
    svc = offline_service(tmp_path / "svc", fake_github, fake_gitlab, monkeypatch, prime=True)
    client = member_client(svc["cfg_path"], "alice")
    form = run_form(client.get("/").text, ESTATE_CONFIG["id"])
    assert form, "the dashboard has no Run T0 form for %s" % ESTATE_CONFIG["id"]
    assert client.post(form["action"], data=form["fields"]).status_code in (200, 201, 202)
    copy = tmp_path / "copy.sqlite"
    shutil.copy(svc["db"], copy)
    con = sqlite3.connect(copy)
    con.execute("DELETE FROM actions")
    con.commit()
    con.close()
    runs = sqlite_rows(copy, "SELECT nonce, configuration, trigger, triggered_by, triggered_at FROM runs")
    assert [r["trigger"] for r in runs] == ["manual"], runs
    probs = check_a2(write_call_census(SRC_PKG), list(resolve("write_calls")), read_config(svc["cfg_path"])["credentials"],
                     ci_project=CI_PROJECT, runs=runs, actions=[])
    assert any(p.endswith("by alice has 0 actions rows, want 1") for p in probs), probs


# ----- A3 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A3")
@pytest.mark.live
@NOT_BUILT
def test_AT_A3():
    cfg_path, member = live_env()
    cfg = read_config(cfg_path)
    db = data_dir(cfg) / "rm.sqlite"
    # Given the live service in-process as a member, and the store's latest observation per subject.
    client = member_client(cfg_path, member)
    app = client.app
    stored = latest_observations(db)
    assert stored, "the store is empty: nothing to show"
    runs = sqlite_rows(db, "SELECT nonce FROM runs ORDER BY triggered_at DESC LIMIT 1")
    nonce = runs[0]["nonce"] if runs else EXAMPLE_NONCE
    subject = next(s for s in stored if s.startswith("silicon_protection:"))
    fetch = lambda href: (lambda r: r.text if r.status_code == 200 else None)(client.get(href))
    # When every HTML GET route in the app's own route table is fetched, with parameters from live
    #   data (eth-rc4, tidelink, 036d381 + nanosoc_gen, SL-DEF-0001, the latest nonce, a real subject).
    problems, pages = [], 0
    for path, methods, directory in route_table(app):
        if directory is not None or "GET" not in methods or path.startswith("/api/") \
                or is_under(path, (ASSUMED["auth_prefix"],)):
            continue
        url = fill(path, nonce=nonce, subject=subject)
        if path == "/contains":
            url = "/contains?sha=%s&component=nanosoc_gen" % FIX_A[:7]
        r = client.get(url)
        fetched_at = now_utc()
        if not r.headers.get("content-type", "").startswith("text/html"):
            continue
        pages += 1
        # Then: exactly one data age, measured from the oldest fact shown; every fact links to a
        #   page showing its exact commands; every fact's time equals the store's.
        problems += check_a3_page(url, r.text, stored, fetched_at, fetch)
    assert pages >= 9, "fewer HTML pages than the v1 set (interfaces.md §13)"
    # And every JSON API line is a valid observation, which carries sources and observed_at by schema
    # (configurations and alerts return registry_record and alert observations: interfaces.md section 13).
    for path, methods, _ in route_table(app):
        if path.startswith("/api/v1/") and "GET" in methods:
            r = client.get(fill(path, nonce=nonce, subject=subject))
            for line in r.text.splitlines():
                if line.strip():
                    problems += ["%s: %s" % (path, e) for e in validate("observation", json.loads(line))]
    # And db.put refuses a fact without observed_at (the contract's own golden, interfaces.md §16).
    with pytest.raises(Exception):
        resolve("put")(load_docs(INVALID / "observation.without-observed-at.yaml")[0])
    assert problems == []


@pytest.mark.requirement("A3")
def test_AT_A3_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: a page of the 11 real silicon rows passes; a missing link, a missing data age,
    # an age measured from the newest fact, and a stored fact with no sources are each caught.
    _nc_A3_checker()
    # System level: a row with sources: [] inserted directly (bypassing put) must not pass the audit.
    svc = offline_service(tmp_path / "svc", fake_github, fake_gitlab, monkeypatch)
    obs = silicon_observations()
    bad = dict(obs[0], subject=obs[0]["subject"].replace("tidechart", "tidechart-negctl"), sources=[])
    con = sqlite3.connect(svc["db"])
    con.execute("INSERT INTO observations (kind, subject, observed_at, doc) VALUES (?, ?, ?, ?)",
                (bad["kind"], bad["subject"], bad["observed_at"], json.dumps(bad)))
    con.commit()
    con.close()
    client = member_client(svc["cfg_path"], "alice")
    r = client.get("/silicon-protection")
    stored = latest_observations(svc["db"])
    fetch = lambda href: (lambda x: x.text if x.status_code == 200 else None)(client.get(href))
    probs = check_a3_page("/silicon-protection", r.text, stored, now_utc(), fetch)
    shown = {s for s, _, _ in PageScan(r.text).facts}
    assert {o["subject"] for o in obs} <= shown, "baseline: the 11 real rows are shown"
    assert bad["subject"] not in shown or any(bad["subject"] in p for p in probs), \
        "a fact without sources was shown and the audit did not catch it"
    # And put refuses the invalid example.
    # CR-4: refused as an observation (ObservationRefused), not any crash.
    from nanosoc_rm.db import ObservationRefused
    with pytest.raises(ObservationRefused):
        resolve("put")(load_docs(INVALID / "observation.without-observed-at.yaml")[0])


# ----- A4 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A4")
@pytest.mark.live
@NOT_BUILT
def test_AT_A4(tmp_path, monkeypatch):
    cfg_path, member = live_env()   # live + Run: starts one real compat-ci pipeline (PR8: a person runs it)
    cfg = read_config(cfg_path)
    shim_env, shim_log = git_shim(tmp_path)
    monkeypatch.setenv("PATH", shim_env["PATH"])
    # Given the live service in-process as a member; eth-rc4 registered at ab73e40; compat-ci has
    #   job t0 on main; every git argv the service runs is logged.
    client = member_client(cfg_path, member)
    # When the Run T0 form on /configurations/eth-rc4 is posted with its CSRF field...
    form = run_form(client.get("/configurations/%s" % RC4).text, RC4)
    assert form and not form["disabled"], "no enabled Run T0 button on eth-rc4 (a run already pending?)"
    r = client.post(form["action"], data=form["fields"])
    assert r.status_code in (200, 201, 202), r.text[:500]
    fact = next((s for s, _, _ in PageScan(r.text).facts if s.startswith("t0_result:%s:" % RC4)), None)
    assert fact, "pressing Run showed no t0_result fact"
    nonce = fact.rsplit(":", 1)[1]

    def this_run():
        _, docs, _ = cli(cfg_path, "t0", "runs", RC4, "--json")
        return next((d for d in docs if d["fact"]["run"]["nonce"] == nonce), None)

    run = this_run()
    assert run and run["fact"]["acceptance"] == "pending" and run["fact"]["run"]["pipeline_id"], run
    # ...and `t0 fetch` runs every 2 minutes until the run leaves pending (at most 45 minutes).
    deadline = time.time() + 45 * 60
    while run["fact"]["acceptance"] == "pending" and time.time() < deadline:
        time.sleep(120)
        cli(cfg_path, "t0", "fetch")
        run = this_run()
    # Then the CI API itself (read independently) shows the trigger pipeline on the configured ref,
    #   its one t0 job, and RM_REQUEST carrying this nonce and rc4's commit; the result the tool shows
    #   carries that job's id and a verdict.
    api, ci = cfg["hosts"]["gitlab"]["api"], enc(cfg["ci"]["project"])
    tok = token(cfg, "gitlab_read")
    pid = run["fact"]["run"]["pipeline_id"]
    pipeline = json.loads(http_get("%s/projects/%s/pipelines/%d" % (api, ci, pid), tok=tok)[2])
    jobs = json.loads(http_get("%s/projects/%s/pipelines/%d/jobs" % (api, ci, pid), tok=tok)[2])
    variables = json.loads(http_get("%s/projects/%s/pipelines/%d/variables" % (api, ci, pid), tok=tok)[2])
    assert check_a4(run, pipeline, jobs, variables, configuration=RC4, commit=RC4_COMMIT, login=member) == []
    assert pipeline.get("ref") == cfg["ci"]["ref"]
    if run["fact"]["acceptance"] == "accepted":
        gates = resolve("gates")
        ids = set(gates) if isinstance(gates, dict) else {g if isinstance(g, str) else getattr(g, "id", None) or g["id"]
                                                          for g in gates}
        assert {g["id"] for g in run["fact"]["gates"]} == {g for g in ids if not g.startswith("t0.registry.")}
        # Today's rc4 data (interfaces.md §9). Its raw verdicts FAIL on the known gates; with rc4's
        # t0_waivers registered (GATE2 Q1) those gates show WAIVED, keeping raw_verdict, and the run is
        # FAIL or WAIVED, never PASS (README section 6). AT-D2 checks the waivers item by item.
        raw = {g["id"]: g.get("raw_verdict", g["verdict"]) for g in run["fact"]["gates"]}
        assert RC4_T0_EXPECTED_FAIL <= {gid for gid, v in raw.items() if v == "FAIL"}
        assert all(g["verdict"] in ("FAIL", "WAIVED") for g in run["fact"]["gates"] if g["id"] in RC4_T0_EXPECTED_FAIL)
        assert run["fact"]["verdict"] in ("FAIL", "WAIVED")
    assert client.get("/runs/%s" % nonce).status_code == 200
    # And the service ran no tests itself: the T0 runner was never imported in this process, and no
    #   git clone or T0 probe (fetch --depth=1) ran.
    assert "nanosoc_rm.t0.runner" not in sys.modules
    calls = shim_calls(shim_log)
    assert not [c for c in calls if git_subcommand(c) == "clone" or (git_subcommand(c) == "fetch" and "--depth=1" in c)]


@pytest.mark.requirement("A4")
def test_AT_A4_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: no pipeline, a foreign nonce, two t0 jobs, still pending, another job's id: caught.
    _nc_A4_checker()
    # System level 1: the trigger endpoint answers 401 (a revoked trigger token).
    svc = offline_service(tmp_path / "a", fake_github, fake_gitlab, monkeypatch, trigger_status=401, prime=True)
    monkeypatch.setenv("PATH", svc["shim_env"]["PATH"])
    client = member_client(svc["cfg_path"], "alice")
    form = run_form(client.get("/configurations/%s" % ESTATE_CONFIG["id"]).text, ESTATE_CONFIG["id"])
    assert form, "baseline: the estate configuration has a Run T0 form"
    resp = client.post(form["action"], data=form["fields"])
    shown = [s for s, _, _ in PageScan(resp.text).facts if s.startswith("t0_result:%s:" % ESTATE_CONFIG["id"])]
    _, docs, _ = cli(svc["cfg_path"], "t0", "runs", ESTATE_CONFIG["id"], "--json")
    # interfaces.md section 10, Trigger step 5: a failed trigger is recorded, never swallowed, and never
    # left pending (which would disable the Run button for ever). A tool that swallowed the 401 and
    # stored a pending run, or nothing, fails here (coverage review Medium 5).
    assert len(docs) == 1, "pressing Run must record exactly one t0_result, even when the trigger fails: %s" % docs
    assert shown == [docs[0]["subject"]], "the Run response must show the recorded run: %s" % shown
    f = docs[0]["fact"]
    assert (f["acceptance"], f["run"]["pipeline_id"]) == ("no-bundle", None), f
    assert f["verdict"] in ("UNVERIFIED", "INFRA") and "401" in (f["reason"] or ""), f
    probs = check_a4(docs[0], None, [], [], configuration=ESTATE_CONFIG["id"], commit=svc["estate"]["s1"], login="alice")
    assert probs, "a trigger answered 401 must not satisfy A4"
    assert all(run["fact"]["acceptance"] != "accepted" for run in docs)
    # CR-3: probed in a fresh interpreter (in the merged suite other tests import the runner into this one):
    # the same failing trigger, driven through the CLI, never imports the T0 runner.
    probe = subprocess.run([sys.executable, "-c",
                            "import sys\nfrom nanosoc_rm.cli import main\n"
                            "try:\n    main(['t0', 'trigger', sys.argv[1], '--by', 'alice'])\nexcept SystemExit:\n    pass\n"
                            "sys.exit(9 if 'nanosoc_rm.t0.runner' in sys.modules else 0)", ESTATE_CONFIG["id"]],
                           capture_output=True, text=True, env=dict(os.environ, NANOSOC_RM_CONFIG=str(svc["cfg_path"])))
    assert probe.returncode == 0, "the service must not fall back to running T0 itself: %s" % probe.stderr[-2000:]
    assert not [c for c in shim_calls(svc["shim_log"]) if git_subcommand(c) == "fetch" and "--depth=1" in c]
    # System level 2: the pipeline's RM_REQUEST carries a foreign nonce; the fetched run is rejected
    # at verify step 2 (interfaces.md §10), and check_a4 names the mismatch.
    gh2, gl2 = FakeHost(), FakeHost()
    try:
        svc2 = offline_service(tmp_path / "b", gh2, gl2, monkeypatch, foreign_nonce="0" * 32, prime=True)
        cli(svc2["cfg_path"], "t0", "trigger", ESTATE_CONFIG["id"], "--by", "alice")
        cli(svc2["cfg_path"], "t0", "fetch")
        _, docs, _ = cli(svc2["cfg_path"], "t0", "runs", ESTATE_CONFIG["id"], "--json")
        assert len(docs) == 1, docs
        run = docs[0]
        assert run["fact"]["acceptance"] == "rejected" and "RM_REQUEST" in (run["fact"]["reason"] or ""), run["fact"]
        pid = run["fact"]["run"]["pipeline_id"]
        variables = json.loads(http_get("%s/api/v4/projects/%s/pipelines/%d/variables" % (gl2.url, enc(CI_PROJECT), pid))[2])
        probs = check_a4(run, {"id": pid, "source": "trigger"}, [{"id": pid + 1000, "name": "t0"}], variables,
                         configuration=ESTATE_CONFIG["id"], commit=svc2["estate"]["s1"], login=run["fact"]["run"]["triggered_by"])
        assert any(p.startswith("RM_REQUEST nonce 000") for p in probs), probs
    finally:
        gh2.close()
        gl2.close()


# ----- A5 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A5")
def test_AT_A5(tmp_path, fake_github, fake_gitlab, monkeypatch):
    from fastapi.testclient import TestClient
    # Given the offline service (example registry, the real rc4 observations seeded, fake GitHub)
    #   and identities alice (active member), bob (404: not a member) and anonymous.
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch)
    app = svc["app"]
    auth = (ASSUMED["auth_prefix"],)
    # When every route in the app's OWN route table is requested as each identity (alice: GET only;
    #   bob and anonymous: every method). Signed-in clients do not request /auth/* (logout).
    results = []
    for who in ("alice", "bob"):
        c = TestClient(app)
        sign_in(c, who)
        results += sweep(c, app, who, methods={"GET"} if who == "alice" else None, skip=auth)
    results += sweep(TestClient(app), app, "anonymous")
    # Then alice is never refused and opens the fixed pages; bob gets 403 on every route including
    #   /api and POST; anonymous gets 401/403 or the login redirect; no refused body leaks rc4 data;
    #   every contracted route (interfaces.md §13) is in the table.
    assert check_a5(results, member="alice", login_path=ASSUMED["login_path"], exempt=auth) == []
    # And the only unauthenticated surfaces are /auth/* and static files, which carry no data.
    for path, _, directory in route_table(app):
        if directory is not None:
            assert path == ASSUMED["static_prefix"]
            for f in Path(directory).rglob("*"):
                if f.is_file():
                    text = f.read_bytes().decode("utf-8", "replace")
                    assert "{{" not in text and "{%" not in text, "%s is a template, not a static asset" % f
                    assert not [s for s in LEAK_STRINGS if s in text], "%s carries data" % f
        else:
            assert not path.startswith(ASSUMED["static_prefix"] + "/")


@pytest.mark.requirement("A5")
def test_AT_A5_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    # Checker level: an open route, a sweep with no member, a missing contracted route: caught.
    _nc_A5_checker()
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch)
    auth = (ASSUMED["auth_prefix"],)
    # System level 1 (fires on any design): the app built with an auth that trusts bob, a non-member.
    broken = resolve("create_app")(svc["cfg_path"], auth=resolve("fake_membership")(login="bob"))
    probs = check_a5(sweep(TestClient(broken), broken, "bob", methods={"GET"}, skip=auth),
                     member="alice", login_path=ASSUMED["login_path"], exempt=auth)
    assert "non-member bob got 200 on GET /, want 403" in probs, probs
    # System level 2 (coverage): a raw route the test adds after construction, outside any FastAPI
    # dependency, is still swept, because the sweep reads the app's own route table. If it answers
    # 200 to bob, the checker names it.
    app = resolve("create_app")(svc["cfg_path"])
    app.router.routes.append(Route("/negctl/open", lambda request: JSONResponse({"commit": RC4_COMMIT})))
    c = TestClient(app)
    sign_in(c, "bob")
    results = sweep(c, app, "bob", skip=auth)
    opened = [r for r in results if r["route"] == "/negctl/open"]
    assert opened, "the sweep missed a route that is in the app's table"
    probs = check_a5(results, member="alice", login_path=ASSUMED["login_path"], exempt=auth)
    assert opened[0]["status"] != 200 or "non-member bob got 200 on GET /negctl/open, want 403" in probs


# ----- A6 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A6")
def test_AT_A6(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Given the package source (every phase), the schemas, and an offline estate whose superproject
    #   pins a user's fork (alice/ip) at a fork-only commit, with an MR ref and a PR ref.
    census = write_call_census(SRC_PKG)
    # When the census is matched against fork, tag, release, branch and yank patterns...
    assert check_a6(census, list(resolve("write_calls"))) == []
    # ...the real eth-rc4 eligibility entry is given a yank field, or un-frozen...
    rc4 = next(c for c in example("eligibility.example.yaml")[0]["configurations"] if c["id"] == RC4)
    assert validate("eligibility", {"schema": 1, "configurations": [rc4]}) == []
    assert validate("eligibility", {"schema": 1, "configurations": [dict(rc4, yanked=True)]})
    assert validate("eligibility", {"schema": 1, "configurations": [dict(rc4, frozen=False)]})
    # ...and the full v1 CLI cycle runs over the estate with every git argv logged.
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch, seed=False)
    est = svc["estate"]
    before = {k: refs_of(est[k]) for k in ("ip", "fork", "soc")}
    for args in (("mirror", "sync"), ("inventory", "--json"), ("walk", ESTATE_CONFIG["id"], "--json"),
                 ("hosts", "--json"), ("silicon-protection", "--json"), ("contains", est["c1"], "--json"),
                 ("t0", "trigger", ESTATE_CONFIG["id"], "--by", "alice"), ("t0", "fetch")):
        cli(svc["cfg_path"], *args, env_extra=svc["shim_env"])
    calls = shim_calls(svc["shim_log"])
    # Then git never pushed; the hosts saw only GETs plus the one trigger POST; the cycle did real
    #   work (it fetched, and read both APIs); and every estate repo, the fork included, is unchanged.
    assert git_log_problems(calls) == []
    assert any(git_subcommand(c) == "fetch" for c in calls), "the cycle fetched nothing: vacuous"
    assert fake_github.gets() and fake_gitlab.gets(), "the cycle read nothing from the APIs: vacuous"
    assert fake_github.writes() == []
    assert [w for w in fake_gitlab.writes() if not w[1].endswith("/trigger/pipeline")] == []
    assert {k: refs_of(est[k]) for k in before} == before


@pytest.mark.requirement("A6")
def test_AT_A6_negative_control(tmp_path):
    # Checker level: a planted fork push and a planted silicon tag delete are caught; an unguarded
    # write is caught; the git runner's guard refuses a push; eligibility with a yank field, or with
    # frozen: false on silicon, fails the real schema.
    _nc_A6_checker(tmp_path)
    # System level: the same checker over a copy of the real package with the planted module added.
    pkg = tmp_path / "pkgcopy" / "nanosoc_rm"
    shutil.copytree(SRC_PKG, pkg)
    (pkg / "negctl_planted.py").write_text(PLANTED_WRITES)
    probs = check_a6(write_call_census(pkg), list(resolve("write_calls")))
    assert any("negctl_planted.py" in p and "git push" in p for p in probs), probs
    assert any("negctl_planted.py" in p and "tags/silicon/eth-chiplet-r0p0" in p for p in probs), probs


# ----- A7 -------------------------------------------------------------------------------------
@pytest.mark.requirement("A7")
def test_AT_A7(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Given the phase-gate register (gap G12), the requirement -> phase map from REQUIREMENTS.md,
    #   the tool repo's log (PR7: commits name requirement IDs), and the app's routes and CLI commands.
    reg = REPO / ASSUMED["phase_gates"]
    assert reg.is_file(), "no phase-gate register: A7's entry gates are not written down (gap G12)"
    gates = load_docs(reg)[0]
    phase_of = requirement_phases(REQUIREMENTS_MD.read_text())
    assert phase_of.get("D3") == "Phase 2" and phase_of.get("I1") == "Phase 5", "REQUIREMENTS.md headings changed"
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch, seed=False)
    routes = [p for p, _, _ in route_table(svc["app"])]
    exe = shutil.which("nanosoc-rm")
    assert exe, "nanosoc-rm is not on PATH"
    helptext = subprocess.run([exe, "--help"], capture_output=True, text=True).stdout
    commands = re.findall(r"^\s{2}([a-z][a-z0-9-]*)\s", helptext.split("Commands:", 1)[-1], re.M)
    assert "walk" in commands, "could not read the CLI's command list: %r" % helptext[:300]
    # When check_a7 runs over them.
    # Then each later phase has a written entry gate whose every criterion has a check; no commit
    #   that changes src/ names a later-phase requirement before that phase's gate was checked; and
    #   no later-phase route or command exists before its gate.
    changes = (REPO / "docs" / "CHANGES.md").read_text()                  # CR-5: recorded exemptions
    from nanosoc_rm.schedule import JOBS                                  # CR-15: what the scheduler runs
    automation = [j.name for j in JOBS]
    assert "poll" in automation, "could not read the scheduler's jobs"
    assert check_a7(gates, phase_of, git_commits(REPO), routes, commands, changes=changes,
                    automation=automation) == []


@pytest.mark.requirement("A7")
def test_AT_A7_negative_control(tmp_path):
    # Checker level (the whole control, A7 being a process requirement): a D3 implementation commit
    # made before CR-15 and before the Phase 2 gate, an enabled nightly matrix before the Phase 2 gate, and a
    # missing Phase 2 gate are each caught; a contracts-only commit naming G3 and C11 is not, nor (CR-15) a
    # /releases route, a `lock` command or a D3 commit made after CR-15; the same repo with the Phase 2 gate
    # checked first passes.
    _nc_A7_checker(tmp_path)


# ----- H1 -------------------------------------------------------------------------------------
@pytest.mark.requirement("H1")
@pytest.mark.live
@NOT_BUILT
def test_AT_H1():
    cfg_path, member = live_env()   # live + Run: starts one real compat-ci pipeline (PR8)
    cfg = read_config(cfg_path)
    registry = registry_dir(cfg)
    # Given the live service in-process as a member, and the configurations the live registry registers.
    elig = load_docs(registry / "eligibility.yaml")[0]["configurations"]
    confs = [c["id"] for c in elig]
    frozen = {c["id"] for c in elig if c.get("frozen") or c.get("silicon")}
    client = member_client(cfg_path, member)
    # When the v1 pages are fetched...
    routes = ["/", "/components/tidelink", "/contains?sha=%s&component=nanosoc_gen" % FIX_A[:7],
              "/silicon-protection", "/defects", "/defects/%s" % DEFECT, "/errata/%s" % RC4]
    routes += ["/configurations/%s" % c for c in confs]
    pages = {}
    for r in routes:
        resp = client.get(r)
        assert resp.status_code == 200, "%s -> %d" % (r, resp.status_code)
        pages[r] = resp.text
    # ...and the CLI answers the same questions (PR4: two front ends over one function).
    subj = lambda docs: {d["subject"] for d in docs}
    _, pins, _ = cli(cfg_path, "pins", RC4, "--json")
    _, hosts, _ = cli(cfg_path, "hosts", "tidelink", "--json")
    _, contains, _ = cli(cfg_path, "contains", FIX_A[:7], "--component", "nanosoc_gen", "--json")
    _, silicon, _ = cli(cfg_path, "silicon-protection", "--json")
    rc4_silicon = [d for d in silicon if d["fact"]["configuration"] == RC4]
    pinned = [d for d in rc4_silicon if d["fact"]["pinned"]]
    assert len(pins) == RC4_PIN_COUNT and len(pinned) == RC4_SILICON_PINNED
    assert all(d["fact"]["covered"] for d in pinned)
    assert [d["fact"]["answer"] for d in contains if d["fact"]["configuration"] == RC4 and d["fact"]["host"] == "gitlab"] == ["no"]
    counts, mentions = {}, {}
    _, all_hosts, _ = cli(cfg_path, "hosts", "--json")
    drifting = {d["fact"]["component"] for d in all_hosts if d["fact"]["relation"] in DRIFT_RELATIONS}
    for c in confs:
        _, cpins, _ = cli(cfg_path, "pins", c, "--json")
        red = [p for p in cpins for h in p["fact"]["hosts"].values() if h["pinned"] and
               (h["state"] in RED_STATES or (h["state"] == "BRANCH_ONLY" and c in frozen))]
        counts[(c, "red-pins")] = len(red)
        edir = registry / "errata" / c
        errata = [load_docs(f)[0] for f in edir.glob("ERR-*.yaml")] if edir.is_dir() else []
        counts[(c, "open-errata")] = sum(1 for e in errata if e["status"] in ("open", "permanent"))
        mentions[(c, "host-drift")] = drifting & {p["fact"]["child"]["component"] for p in cpins}
    assert mentions[(RC4, "host-drift")] >= RC4_DRIFT and counts[(RC4, "open-errata")] >= 1
    # Then every configuration has all six dashboard items (counts equal to the CLI's and the
    #   registry's) and exactly one CSRF-protected Run T0 button, each configuration page has one,
    #   and every page shows exactly the facts its CLI command gives (44 pins, 11 silicon rows...).
    assert check_h1(pages, configurations=confs, counts=counts, mentions=mentions, cli_subjects={
        "/configurations/%s" % RC4: subj(pins), "/components/tidelink": subj(hosts),
        "/contains?sha=%s&component=nanosoc_gen" % FIX_A[:7]: subj(contains), "/silicon-protection": subj(silicon)}) == []
    assert DEFECT in pages["/defects"] and ERRATUM in pages["/errata/%s" % RC4]
    # And pressing Run on the dashboard shows the pending run, and the button is disabled (or gone)
    #   while that run is pending (interfaces.md §10).
    form = run_form(pages["/"], RC4)
    assert not form["disabled"], "a run is already pending on eth-rc4: wait for it before the H1 demo"
    r = client.post(form["action"], data=form["fields"])
    assert r.status_code in (200, 201, 202)
    assert any(s.startswith("t0_result:%s:" % RC4) for s, _, _ in PageScan(r.text).facts)
    again = run_form(client.get("/").text, RC4)
    assert again is None or again["disabled"], "the Run button must be disabled while a run is pending"


@pytest.mark.requirement("H1")
def test_AT_H1_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: a dashboard without runner-status, a configuration without a row, a Run form
    # without CSRF, a wrong red-pin count, and a silicon page missing or inventing a fact are each
    # caught; the consistent fixture pages pass.
    _nc_H1_checker()
    # System level: the REAL offline dashboard passes for the configurations it has, and the checker
    # fails it when asked for a configuration the registry does not hold.
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch)
    dash = member_client(svc["cfg_path"], "alice").get("/").text
    real = [RC4, ESTATE_CONFIG["id"]]
    assert check_h1({"/": dash}, configurations=real, cli_subjects={}) == [], "baseline: the real dashboard has every hook"
    probs = check_h1({"/": dash}, configurations=real + ["eth-ghost"], cli_subjects={})
    assert "dashboard: eth-ghost lacks %s" % ", ".join(DASHBOARD_ITEMS) in probs, probs


# ----- H2 -------------------------------------------------------------------------------------
@pytest.mark.requirement("H2")
@pytest.mark.live
@NOT_BUILT
def test_AT_H2(tmp_path, fake_github, fake_gitlab, monkeypatch):
    from fastapi.testclient import TestClient
    # Given the offline service, a fake GitHub (OAuth + membership), and identities alice (active),
    #   bob (404) and carol (pending: invited, not joined).
    svc = offline_service(tmp_path / "svc", fake_github, fake_gitlab, monkeypatch, prime=True)
    app = svc["app"]
    auth = (ASSUMED["auth_prefix"],)
    logins, sweeps, cookies, clients = {}, {}, [], {}
    # When each signs in through /auth/login -> the fake authorize URL -> /auth/callback, and sweeps
    #   every route...
    for who in ("alice", "bob", "carol"):
        c = TestClient(app)
        q, cb = sign_in(c, who)
        assert (q.get("client_id") or [""])[0] == "fake-client-id"
        assert (q.get("state") or [""])[0], "the authorize URL carries no state"
        logins[who] = {"status": cb.status_code, "session": bool(c.cookies) and cb.status_code in (302, 303, 307),
                       "scope": (q.get("scope") or [""])[0]}
        cookies += list(c.cookies.values())
        sweeps[who] = sweep(c, app, who, methods={"GET"}, skip=auth)
        clients[who] = c
    # ...a callback with a forged state is refused...
    forged = TestClient(app)
    forged.get(ASSUMED["login_path"], follow_redirects=False)
    assert forged.get(ASSUMED["callback_path"], params={"code": "alice", "state": "forged"},
                      follow_redirects=False).status_code in (400, 401, 403)
    # ...alice presses Run on the estate configuration...
    a = clients["alice"]
    form = run_form(a.get("/").text, ESTATE_CONFIG["id"])
    assert form, "alice's dashboard has no Run T0 form"
    assert a.post(form["action"], data=form["fields"]).status_code in (200, 201, 202)
    actions = sqlite_rows(svc["db"], "SELECT at, login, action, target, result FROM actions")
    # Then only alice gets a session and opens pages; the authorize URL asks for read:org only;
    #   alice's GitHub token is in neither the cookie nor the database; her Run is logged.
    assert check_h2(logins, sweeps, members={"alice"}, tokens=["fake-token-alice"], cookies=cookies,
                    db_bytes=svc["db"].read_bytes(), actions=actions, runs_by=["alice"]) == []
    # And roles come from the config: with an empty role table, a member is refused.
    gh2, gl2 = FakeHost(), FakeHost()
    try:
        svc2 = offline_service(tmp_path / "noroles", gh2, gl2, monkeypatch, overrides={"roles": {}})
        c = TestClient(svc2["app"])
        sign_in(c, "alice")
        assert c.get("/", follow_redirects=False).status_code == 403
    finally:
        gh2.close()
        gl2.close()
    # And the fake's answers match GitHub's real ones (fixture realism): the membership check, run
    #   with the service's real read token, says True for a real member and False for octocat.
    cfg_path, member = live_env()
    config = resolve("load_config")(cfg_path)
    assert resolve("is_member")(config, member) is True
    assert resolve("is_member")(config, NON_MEMBER) is False


@pytest.mark.requirement("H2")
def test_AT_H2_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    from fastapi.testclient import TestClient
    # Checker level: bob opening a page, carol given a session, a token in the cookie, a Run with no
    # actions row and a wrong OAuth scope are each caught; the consistent result passes.
    _nc_H2_checker()
    # System level: the fake answers `active` for bob while the test's own table says non-member.
    # The service lets him in, and check_h2 must report it: the test catches a service that trusts
    # a membership answer it should not have been given.
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch, members={"alice": "active", "bob": "active"})
    c = TestClient(svc["app"])
    q, cb = sign_in(c, "bob")
    logins = {"bob": {"status": cb.status_code, "session": bool(c.cookies) and cb.status_code in (302, 303, 307),
                      "scope": (q.get("scope") or [""])[0]}}
    probs = check_h2(logins, {"bob": sweep(c, svc["app"], "bob", methods={"GET"}, skip=(ASSUMED["auth_prefix"],))},
                     members={"alice"})
    assert "non-member bob was given a session" in probs, probs
    assert any(p.startswith("non-member bob opened") for p in probs), probs


# ----- H3 -------------------------------------------------------------------------------------
@pytest.mark.requirement("H3")
@pytest.mark.live
@NOT_BUILT
def test_AT_H3():
    from fastapi.testclient import TestClient
    cfg_path, _ = live_env()
    cfg = read_config(cfg_path)
    # Given the live config, the credentials check command (gap G9) and the alerts table.
    rc, docs, p = cli(cfg_path, *ASSUMED["credentials_cli"], "--json")
    assert rc in (0, 2), p.stderr   # 2 = some credential could not be checked (unverified)
    assert all(validate("observation", d) == [] and d["kind"] == "credential_status" for d in docs), docs
    rows = [d["fact"] for d in docs]
    # When GitLab and GitHub describe the tokens themselves, and the login redirect is read...
    st, _, body = http_get("%s/personal_access_tokens/self" % cfg["hosts"]["gitlab"]["api"], tok=token(cfg, "gitlab_read"))
    assert st == 200
    _, gh_headers, _ = http_get("%s/user" % cfg["hosts"]["github"]["api"], tok=token(cfg, "github_read"))
    loc = TestClient(resolve("create_app")(cfg_path)).get(ASSUMED["login_path"], follow_redirects=False).headers["location"]
    scope = (urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query).get("scope") or [""])[0]
    alerts = sqlite_rows(data_dir(cfg) / "rm.sqlite", "SELECT kind, subject, raised_at, cleared_at FROM alerts")
    gates = load_docs(REPO / ASSUMED["phase_gates"])[0]
    phase4 = next(g for g in gates["phases"] if g["phase"] == "Phase 4")
    phase4_met = phase4["status"] in ("gate-met", "started")
    # Then exactly the 4 v1 credentials exist, each recorded within its role, checked against the
    #   host where the host can say, never `ok` unchecked, with an expiry alert 14 days ahead, and
    #   no Phase 4 credential (bump writer, bot tag) exists yet.
    assert check_h3(rows, today=dt.date.today(), ci_project=CI_PROJECT, alerts=alerts, gitlab_self=json.loads(body),
                    github_headers=gh_headers, oauth_scope=scope, config=cfg, phase4_met=phase4_met) == []


@pytest.mark.requirement("H3")
def test_AT_H3_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: recorded `api`, live scopes that differ, `ok` unchecked, a classic token, a
    # 10-day expiry with no alert, and a bump_writer credential are each caught; a clean report passes.
    _nc_H3_checker()
    # System level: the fake GitLab says the poller token has `api`; the config records read_api.
    svc = offline_service(tmp_path, fake_github, fake_gitlab, monkeypatch, seed=False)
    gitlab_token_self(fake_gitlab, ["api"])
    rc, docs, _ = cli(svc["cfg_path"], *ASSUMED["credentials_cli"], "--json")
    assert rc == 1, "a poller token wider than its record must be a finding (exit 1)"
    rows = [d["fact"] for d in docs]
    # CR-4: the exit 1 must come from the gitlab_read row itself (the fixture's github_read is unrelated).
    gl = [r for r in rows if r["name"] == "gitlab_read"]
    assert len(gl) == 1 and gl[0]["verdict"] == "finding" and gl[0]["live_scope"] == ["api"], gl
    probs = check_h3(rows, today=dt.date(2026, 10, 1), ci_project=CI_PROJECT, gitlab_self={"scopes": ["api"]})
    assert any(p.startswith("gitlab_read: live scope ['api'] differs") for p in probs), probs


# ----- H6 -------------------------------------------------------------------------------------
@pytest.mark.requirement("H6")
@pytest.mark.live
@NOT_BUILT
def test_AT_H6():
    cfg_path, _ = live_env()
    cfg = read_config(cfg_path)
    cat = load_docs(registry_dir(cfg) / "catalogue.yaml")[0]
    idx, _ = catalogue_index(cat)
    expected = {k for k, v in idx.items() if v[1] in ("canonical", "copy", "external")}   # G15
    # Given the live service with its schedule running.
    # When `mirror status` is read twice, 16 minutes apart (one 15-minute poll plus margin)...
    _, first, _ = cli(cfg_path, "mirror", "status", "--json")
    t1 = now_utc()
    time.sleep(16 * 60)
    _, second, _ = cli(cfg_path, "mirror", "status", "--json")
    t2 = now_utc()
    # Then every catalogued repo is mirrored, ok and at most 20 minutes old in both snapshots, and
    #   every one advanced between them (the schedule really polls).
    assert check_h6_status(first, expected=expected, now=t1) == []
    assert check_h6_status(second, expected=expected, now=t2) == []
    assert check_h6_cadence(first, second) == []
    # And the tool itself reports each mirror's age (H6 done-when; interfaces.md section 3): the table
    # has an `age` column in seconds that equals now - last_success_at for every repo.
    _, _, table = cli(cfg_path, "mirror", "status")
    t3 = now_utc()
    assert check_h6_table(table.stdout, second, now=t3) == []
    # And mirrors hold heads and tags only: the two real repos that advertise refs/pull/* and
    #   refs/environments/* have none of them, and a fresh sync equals anonymous ls-remote's heads and tags.
    mirrors = data_dir(cfg) / "mirrors"
    for (host, path), prefix in sorted(FOREIGN_REFS.items()):
        component = idx[(host, path)][0]
        cli(cfg_path, "mirror", "sync", "--component", component)
        mirror = mirrors / ("%s.git" % component)
        assert check_h6_refs(mirror) == []
        key = "refs/r/%s/%s/" % (host, enc(path))
        mine = {}
        for line in git("for-each-ref", "--format=%(objectname) %(refname)", key, cwd=mirror).splitlines():
            sha, ref = line.split(" ", 1)
            mine["refs/" + ref[len(key):]] = sha
        theirs = {}
        url = "%s/%s.git" % (cfg["hosts"][host]["web"].rstrip("/"), path)
        for line in git("ls-remote", "--heads", "--tags", url).splitlines():
            sha, ref = line.split("\t", 1)
            if not ref.endswith("^{}"):
                theirs[ref] = sha
        assert not [r for r in theirs if r.startswith(prefix)], "ls-remote --heads --tags returned a foreign ref"
        assert mine == theirs


@pytest.mark.requirement("H6")
def test_AT_H6_negative_control(tmp_path, fake_github, fake_gitlab, monkeypatch):
    # Checker level: `git clone --mirror` picks up refs/pull/* and refs/merge-requests/*, and the §3
    # fetch without --no-tags writes refs/tags/*: each is caught. The contracted refspec with
    # --no-tags passes. Stale, failed, missing and un-advanced mirrors are caught.
    _nc_H6_checker(tmp_path / "checker")
    # System level: sync the estate, remove its host, sync again: the mirror reports ok: false with
    # the old last_success_at, and 16 minutes later the status check calls it stale.
    svc = offline_service(tmp_path / "svc", fake_github, fake_gitlab, monkeypatch, seed=False)
    cli(svc["cfg_path"], "mirror", "sync")
    _, before, _ = cli(svc["cfg_path"], "mirror", "status", "--json")
    ip = next(d for d in before if d["fact"]["repo"]["path"] == "soclabs/ip")
    assert ip["fact"]["ok"], "baseline: the first sync succeeds"
    assert check_h6_refs(data_dir(read_config(svc["cfg_path"])) / "mirrors" / "estate-ip.git") == []
    shutil.move(str(svc["estate"]["ip"]), str(tmp_path / "ip-gone.git"))
    cli(svc["cfg_path"], "mirror", "sync")
    _, after, _ = cli(svc["cfg_path"], "mirror", "status", "--json")
    ip2 = next(d for d in after if d["fact"]["repo"]["path"] == "soclabs/ip")
    assert ip2["fact"]["ok"] is False and ip2["fact"]["last_success_at"] == ip["fact"]["last_success_at"]
    later = utc(ip["fact"]["last_success_at"]) + dt.timedelta(minutes=16)
    probs = check_h6_status([ip2], expected={("gitlab", "soclabs/ip")}, now=later, max_age_s=15 * 60)
    assert any("mirror age" in p for p in probs) and any("the last fetch failed" in p for p in probs), probs


# =============================================================================================
# Self-checks: the checker-level negative controls, run now (coverage review High 3). Each first
# shows its checker passes on a consistent input, then that it names the problem in the broken one.
# They test this file's checkers, not the tool (PR6 at checker level). The system-level controls
# inside test_AT_*_negative_control run once the package exists.
# =============================================================================================
def test_selfcheck_A1():
    _nc_A1_checker()


def test_selfcheck_A2(tmp_path):
    _nc_A2_checker(tmp_path)


def test_selfcheck_A3():
    _nc_A3_checker()


def test_selfcheck_A4():
    _nc_A4_checker()


def test_selfcheck_A5():
    _nc_A5_checker()


def test_selfcheck_A6(tmp_path):
    _nc_A6_checker(tmp_path)


def test_selfcheck_A7(tmp_path):
    _nc_A7_checker(tmp_path)


def test_selfcheck_H1():
    _nc_H1_checker()


def test_selfcheck_H2():
    _nc_H2_checker()


def test_selfcheck_H3():
    _nc_H3_checker()


def test_selfcheck_H6(tmp_path):
    _nc_H6_checker(tmp_path)


def test_selfcheck_phase_gate_register():
    """A7: the register exists and is well formed, so AT-A7 fails today only on what is really
    missing (no phase has met its gate), never on a missing file."""
    gates = load_docs(REPO / ASSUMED["phase_gates"])[0]
    phase_of = requirement_phases(REQUIREMENTS_MD.read_text())
    assert [g["phase"] for g in gates["phases"]] == list(LATER_PHASES)
    for g in gates["phases"]:
        assert g["entry_gate"] and all(c.get("criterion") and c.get("check") for c in g["entry_gate"]), g["phase"]
        assert g["status"] == "not-met" and not g["checked"], g["phase"]
        assert all(phase_of.get(i) == g["phase"] for i in g["capabilities"]), g["phase"]
    later = {i for i, ph in phase_of.items() if ph in LATER_PHASES}
    assert later == {i for g in gates["phases"] for i in g["capabilities"]}
