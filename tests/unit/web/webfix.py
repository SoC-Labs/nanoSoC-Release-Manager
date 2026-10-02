"""Helpers and fixtures for the web, auth, phase-gate and trace unit tests (imported by conftest.py).

`fake_github`: a local HTTP server standing in for GitHub's OAuth and membership API (the same answers the
acceptance tests' fake gives, which AT-H2's live probe checks against GitHub itself). `service`: a config
pointing at it, a data dir seeded with the contract's example observations, and a registry checkout.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"
ORG = "SoC-Labs"


class FakeGitHub:
    """OAuth code exchange, GET user, GET user/memberships/orgs/<org>, GET orgs/<org>/members/<login>.
    states: login -> active | pending | None (not a member). The code IS the login; the token is fake-token-<login>."""

    def __init__(self, states: dict):
        self.states = dict(states)
        self.log: list[tuple[str, str]] = []
        self.member_check_status: int | None = None        # force an answer for orgs/<org>/members/<login>
        outer = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, status, payload=None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _token(self):
                a = self.headers.get("Authorization") or ""
                return a.split(None, 1)[1] if " " in a else ""

            def do_POST(self):
                u = urllib.parse.urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                outer.log.append(("POST", u.path))
                if u.path == "/login/oauth/access_token":
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
                    if form.get("client_secret") != "fake-github_oauth":
                        return self._reply(401, {"error": "bad client secret"})
                    return self._reply(200, {"access_token": "fake-token-%s" % form.get("code"), "scope": "read:org"})
                self._reply(404, {"message": "Not Found"})

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                outer.log.append(("GET", u.path))
                tok = self._token()
                if u.path == "/user":
                    if not tok.startswith("fake-token-"):
                        return self._reply(401, {"message": "Bad credentials"})
                    return self._reply(200, {"login": tok[len("fake-token-"):]})
                if u.path == "/user/memberships/orgs/%s" % ORG:
                    st = outer.states.get(tok[len("fake-token-"):])
                    return self._reply(404, {"message": "Not Found"}) if st is None else \
                        self._reply(200, {"state": st, "role": "member"})
                if u.path.startswith("/orgs/%s/members/" % ORG):
                    if tok != "fake-github_read":
                        return self._reply(401, {"message": "Bad credentials"})
                    if outer.member_check_status is not None:
                        return self._reply(outer.member_check_status, {})
                    login = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
                    return self._reply(204) if outer.states.get(login) == "active" else self._reply(404, {})
                self._reply(404, {"message": "Not Found"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_github():
    gh = FakeGitHub({"alice": "active", "bob": None, "carol": "pending", "dave": "active"})
    yield gh
    gh.close()


def _git(*args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_AUTHOR_DATE": "2026-09-30T12:00:00Z",
           "GIT_COMMITTER_DATE": "2026-09-30T12:00:00Z"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_registry(root: Path, *, extra_configs=(), drop_defect=False) -> Path:
    """A registry checkout from the contract examples (README section 1 layout)."""
    from nanosoc_rm import contracts
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", ".", cwd=root)

    def load(name):
        return contracts.load_yaml((EXAMPLES / name).read_text())
    elig = load("eligibility.example.yaml")
    elig["configurations"] += list(extra_configs)
    files = {"catalogue.yaml": load("component-catalogue.example.yaml"), "eligibility.yaml": elig,
             "imports.yaml": load("import-ledger.example.yaml")}
    if not drop_defect:                    # the erratum cites the defect (README section 3 rule 4): both or neither
        files["defects/SL-DEF-0001.yaml"] = load("defect.SL-DEF-0001.example.yaml")
        files["errata/eth-rc4/ERR-001.yaml"] = load("erratum.ETH-R0P0-ERR-001.example.yaml")
    for rel in [p.relative_to(root) for p in root.rglob("*.yaml")]:
        if str(rel) not in files:
            (root / rel).unlink()
    for rel, doc in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(doc, indent=1) + "\n")
    _git("add", "-A", ".", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "registry", cwd=root)
    return root


def write_config(path: Path, *, data: Path, github_url: str, registry: Path | None = None, roles=None,
                 session=True) -> Path:
    tok = path.parent / "tokens"
    tok.mkdir(parents=True, exist_ok=True)
    for n in ("github_read", "github_oauth", "session"):
        (tok / n).write_text("fake-%s\n" % n)
    lines = ["[service]", 'data_dir = "%s"' % data,
             "[hosts.github]", 'web = "%s"' % github_url, 'api = "%s"' % github_url, 'org = "%s"' % ORG,
             "[credentials.github_read]", 'token_file = "%s"' % (tok / "github_read"), 'scope = ["members:read"]',
             "[credentials.github_oauth]", 'client_id = "fake-client-id"', 'token_file = "%s"' % (tok / "github_oauth"),
             'scope = ["read:org"]',
             "[ci]", 'project = "soclabs/nanosoc-compat-ci"', "allowed_runner_ids = [9001]"]
    if registry is not None:
        lines += ["[registry]", 'repo = "soclabs/nanosoc-registry"', 'ref = "main"', 'path = "%s"' % registry]
    if session:
        lines += ["[session]", 'secret_file = "%s"' % (tok / "session")]
    lines += ["[roles]"]
    for k, v in ({"default": ["member"]} if roles is None else roles).items():
        lines.append('"%s" = %s' % (k, json.dumps(v)))
    path.write_text("\n".join(lines) + "\n")
    return path


def seed(cfg_path: Path, *names: str) -> None:
    from nanosoc_rm import contracts, db
    for name in names or ("observation.example.jsonl", "pin-state.example.jsonl"):
        for _, doc in contracts.docs_in(EXAMPLES / name):
            db.put(doc, config=cfg_path)


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory):
    """The example observations stored once through db.put; each test gets a copy of the file."""
    base = tmp_path_factory.mktemp("seed")
    cfg = base / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n' % (base / "data"))
    seed(cfg)
    return base / "data" / "rm.sqlite"


@pytest.fixture
def service(tmp_path, fake_github, monkeypatch, seeded_db):
    """Config + seeded store; $NANOSOC_RM_CONFIG names it. No registry checkout configured."""
    import shutil
    cfg = write_config(tmp_path / "rm.toml", data=tmp_path / "data", github_url=fake_github.url)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    return {"cfg": cfg, "tmp": tmp_path, "github": fake_github}


@pytest.fixture
def registry_spy(monkeypatch):
    """The real registry loader (nanosoc_rm.registry.load_registry), as the web calls it, with its calls recorded
    in `.calls`. To plant a problem the loader refuses, write a stray file under defects/ (README section 3
    rule 7: every file under defects/ is a record): `plant_problem(reg)`."""
    from nanosoc_rm.registry import load_registry as real
    from nanosoc_rm.web import registry_facts
    spy = types.SimpleNamespace(calls=[])

    def load_registry(path):
        spy.calls.append(Path(path))
        return real(path)
    monkeypatch.setattr(registry_facts, "load_registry", load_registry)
    return spy


PLANTED = "defects/BROKEN.txt"


def plant_problem(reg: Path, *, commit: bool = True) -> None:
    (reg / PLANTED).write_text("not a record\n")
    if commit:
        _git("add", PLANTED, cwd=reg)
        _git("commit", "-q", "-m", "broken", cwd=reg)


def git(*args, cwd):
    return _git(*args, cwd=cwd)


# ---------------------------------------------------------------------------------------------------------
# Reading the page hooks (interfaces.md section 13)
# ---------------------------------------------------------------------------------------------------------
from html.parser import HTMLParser  # noqa: E402


class Hooks(HTMLParser):
    """Every start tag's attributes, in order (`elements`), plus each tag's text by element index."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[dict] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append(dict({k: ("" if v is None else v) for k, v in attrs}, _tag=tag))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def with_attr(self, name: str) -> list[dict]:
        return [e for e in self.elements if name in e]

    def facts(self) -> list[tuple[str, str]]:
        return [(e["data-subject"], e.get("data-observed-at")) for e in self.with_attr("data-subject")]

    def links(self) -> dict[str, str]:
        return {e["data-source-for"]: e.get("href") for e in self.with_attr("data-source-for")}
