"""CR-PR1: optional read-only credentials for private mirrors (interfaces.md section 3, Private repos).

Hermetic: a smart-HTTP git host on loopback (git http-backend behind Basic auth for the private repos) plays the
https side; git's url.insteadOf sends the host's ssh base to the same repos on disk, standing in for a key the
host accepts. Nothing here reaches the network.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest
from click.testing import CliRunner
from mw_fixtures import load_at

from nanosoc_rm import db, gitrun
from nanosoc_rm.config import ConfigError, load_config
from nanosoc_rm.mirror import private_read
from nanosoc_rm.mirror.sync import failure_message, sync_component
from nanosoc_rm.web import alertgroups

SRC = Path(__file__).resolve().parents[3] / "src" / "nanosoc_rm"
SSH_BASE = "git@gitlab.example.invalid:"


class _GitHttp(http.server.BaseHTTPRequestHandler):
    """Smart HTTP over git http-backend. A path containing `priv` needs Basic auth with the server's token."""
    protocol_version = "HTTP/1.0"
    root: str = ""
    token: str = ""
    requests: list = []

    def _serve(self):
        u = urllib.parse.urlsplit(self.path)
        auth = self.headers.get("Authorization", "")
        type(self).requests.append((self.command, u.path, bool(auth)))
        want = "Basic " + base64.b64encode(("oauth2:%s" % self.token).encode()).decode()
        if "priv" in u.path and auth != want:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
        env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": self.root, "GIT_HTTP_EXPORT_ALL": "1",
               "REQUEST_METHOD": self.command, "PATH_INFO": urllib.parse.unquote(u.path), "QUERY_STRING": u.query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""), "GIT_CONFIG_NOSYSTEM": "1",
               "HOME": self.root}
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=60).stdout
        head, _, rest = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.split(b"\r\n"):
            k, _, v = line.decode().partition(":")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(rest)))
        self.end_headers()
        self.wfile.write(rest)

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


def _git(*args, env=None):
    return load_at("test_pins").git(*args, env=env)


@pytest.fixture
def estate(tmp_path, monkeypatch):
    """Two gitlab repos (soclabs/pub public, soclabs/priv private), a registry naming both, a service config
    writer and the loopback host. Yields a namespace; the server is shut down after the test."""
    root = tmp_path / "hosts" / "gitlab"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n[protocol \"file\"]\n\tallow = always\n"
                         "[init]\n\tdefaultBranch = main\n[url \"%s/\"]\n\tinsteadOf = %s\n"
                         % (root.as_uri(), SSH_BASE))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for k in ("SSH_ASKPASS", "GIT_ASKPASS"):
        env.pop(k, None)
    heads = {}
    for name in ("pub", "priv"):
        bare = root / "soclabs" / ("%s.git" % name)
        _git("init", "--quiet", "--bare", bare, env=env)
        _git("--git-dir", bare, "config", "uploadpack.allowFilter", "true", env=env)
        work = tmp_path / "work" / name
        _git("init", "--quiet", work, env=env)
        (work / "README").write_text("%s\n" % name)
        _git("-C", work, "add", "README", env=env)
        _git("-C", work, "commit", "--quiet", "-m", name, env=env)
        _git("-C", work, "push", "--quiet", bare.as_uri(), "main", env=env)
        heads[name] = _git("-C", work, "rev-parse", "HEAD", env=env)
    token = "tok-" + secrets.token_hex(12)
    handler = type("H", (_GitHttp,), {"root": str(root), "token": token, "requests": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    at = load_at("test_pins")
    catalogue = {"schema": 1, "components": [
        {"id": "pub", "class": "soc_ref", "canonical": {"host": "gitlab", "path": "soclabs/pub"}},
        {"id": "priv", "class": "ip_tech", "canonical": {"host": "gitlab", "path": "soclabs/priv"}}]}
    registry = at.write_registry(tmp_path / "registry", catalogue, {"schema": 1, "configurations": [
        {"id": "pub-main", "component": "pub", "ref": {"branch": "main"}}]},
                                 examples=False)
    for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"):
        monkeypatch.setenv(k, env[k])
    for k in ("SSH_ASKPASS", "GIT_ASKPASS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(k, raising=False)
    token_file = tmp_path / "gitlab-read.token"
    token_file.write_text(token + "\n")

    def configure(private_read_line: str | None = None) -> Path:
        cfg = tmp_path / "rm.toml"
        lines = ['[service]', 'data_dir = "%s"' % (tmp_path / "data"), '[registry]', 'path = "%s"' % registry,
                 'ref = "main"', '[hosts.gitlab]', 'web = "%s"' % base, 'api = "%s/api/v4"' % base,
                 'ssh = "%s"' % SSH_BASE, 'group = "soclabs"']
        if private_read_line:
            lines.append(private_read_line)
        cfg.write_text("\n".join(lines) + "\n")
        monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
        return cfg

    class NS:
        pass
    ns = NS()
    ns.tmp, ns.token, ns.token_file, ns.base, ns.heads, ns.configure = tmp_path, token, token_file, base, heads, \
        configure
    ns.handler = handler
    try:
        yield ns
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _status(docs, path):
    return next(d for d in docs if d["kind"] == "mirror_status" and d["fact"]["repo"]["path"] == path)


def _everything_stored(ns) -> bytes:
    data = ns.tmp / "data"
    return b"".join(p.read_bytes() for p in data.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------------------------------------
# Off by default: anonymous only, unchanged behaviour plus the new access fields
# ---------------------------------------------------------------------------------------------------------
def test_off_by_default_private_repo_is_unreadable_with_cause(estate):
    estate.configure()
    pub = _status(sync_component("pub"), "soclabs/pub")["fact"]
    priv = _status(sync_component("priv"), "soclabs/priv")["fact"]
    assert (pub["ok"], pub["access"], pub["cause"], pub["method"]) == (True, "anonymous", None, None)
    assert (priv["ok"], priv["access"], priv["cause"], priv["method"]) == (False, "unreadable", "auth", None)
    # no request ever carried credentials
    assert not any(authed for _m, _p, authed in estate.handler.requests)
    alert = next(a for a in db.alerts() if a["fact"]["about"] == "mirror_status:gitlab:soclabs/priv")
    assert "no private_read for gitlab" in alert["fact"]["message"]
    assert alertgroups.cause_of(alert)[0] == "private-anon"


# ---------------------------------------------------------------------------------------------------------
# ssh: the service user's existing key, BatchMode, fetch and ls-remote only
# ---------------------------------------------------------------------------------------------------------
def test_ssh_method_reads_the_private_repo_and_says_so(estate):
    estate.configure('private_read = "ssh"')
    docs = sync_component("priv")
    doc = _status(docs, "soclabs/priv")
    f = doc["fact"]
    assert (f["ok"], f["access"], f["cause"], f["method"]) == (True, "credentials", None, "ssh")
    cmds = [s["command"] for s in doc["sources"]]
    assert any(" fetch " in c and SSH_BASE + "soclabs/priv.git" in c and "--filter=blob:limit=1m" in c for c in cmds)
    assert any(" ls-remote --symref " in c and SSH_BASE in c for c in cmds)
    mirror = estate.tmp / "data" / "mirrors" / "priv.git"
    refs = _git("--git-dir", mirror, "for-each-ref", "--format=%(objectname) %(refname)")
    assert "%s refs/r/gitlab/soclabs%%2Fpriv/heads/main" % estate.heads["priv"] in refs
    # the public repo is still read anonymously: the method is used only after an anonymous failure
    pub = _status(sync_component("pub"), "soclabs/pub")
    assert (pub["fact"]["access"], pub["fact"]["method"]) == ("anonymous", None)
    assert not any(SSH_BASE in s["command"] for s in pub["sources"])
    # the stored row is a valid contract document
    assert db.latest("mirror_status", "mirror_status:gitlab:soclabs/priv")[0]["fact"]["access"] == "credentials"


def test_ssh_env_is_batchmode_and_marked():
    pr = private_read.PrivateRead("gitlab", "ssh", SSH_BASE, identity_file="/k/id_ed25519")
    env = pr.env()
    assert env[private_read.MARKER] == "ssh"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"] and "-i /k/id_ed25519" in env["GIT_SSH_COMMAND"]
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]
    assert pr.url("soclabs/x") == SSH_BASE + "soclabs/x.git"


# ---------------------------------------------------------------------------------------------------------
# token: a read-scope token file through a credential helper; never logged
# ---------------------------------------------------------------------------------------------------------
def test_token_method_reads_private_repo_and_never_records_the_token(estate):
    estate.configure('private_read = { method = "token", token_file = "%s" }' % estate.token_file)
    docs = sync_component("priv")
    f = _status(docs, "soclabs/priv")["fact"]
    assert (f["ok"], f["access"], f["method"]) == (True, "credentials", "token")
    assert any(authed for _m, p, authed in estate.handler.requests if "priv" in p)
    assert estate.token not in json.dumps(docs)
    assert estate.token.encode() not in _everything_stored(estate)
    out = CliRunner().invoke(_main(), ["mirror", "status", "--json"])
    assert estate.token not in out.output
    # a user-level store helper never sees it: the helper list is reset for the host's URL
    env = private_read.method(load_config(), "gitlab").env()
    keys = [(env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i]) for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert keys[0] == ("credential.%s.helper" % estate.base, "")
    assert all(estate.token not in v for _k, v in keys)


def test_refused_token_is_unreadable_and_alert_names_the_credentials(estate):
    estate.configure()
    sync_component("priv")                                   # anonymous failure first: private-anon alert
    bad = estate.tmp / "wrong.token"
    bad.write_text("not-the-token\n")
    estate.configure('private_read = { method = "token", token_file = "%s" }' % bad)
    f = _status(sync_component("priv"), "soclabs/priv")["fact"]
    assert (f["ok"], f["access"], f["cause"], f["method"]) == (False, "unreadable", "auth", "token")
    alert = next(a for a in db.alerts() if a["fact"]["about"] == "mirror_status:gitlab:soclabs/priv")
    # raised again because the way it fails changed: it now groups under the credentials cause
    assert "failed with read credentials (token)" in alert["fact"]["message"]
    assert alertgroups.cause_of(alert)[0] == "private-creds"
    assert "not-the-token" not in json.dumps(db.alerts())


def test_missing_token_file_is_a_config_cause_not_a_crash(estate):
    estate.configure('private_read = { method = "token", token_file = "%s" }' % (estate.tmp / "absent.token"))
    f = _status(sync_component("priv"), "soclabs/priv")["fact"]
    assert (f["ok"], f["access"], f["cause"], f["method"]) == (False, "unreadable", "config", None)
    assert "token file is unreadable" in f["error"] and "absent.token" not in f["error"]
    alert = next(a for a in db.alerts() if a["fact"]["about"] == "mirror_status:gitlab:soclabs/priv")
    assert alertgroups.cause_of(alert)[0] == "private-read-config"


# ---------------------------------------------------------------------------------------------------------
# gitrun: a credentialed environment reaches fetch and ls-remote only; writes stay refused
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ("push", "origin", "main"),
    ("clone", "https://example.invalid/x.git", "x"),
    ("cat-file", "-p", "HEAD"),
    ("submodule", "update", "--init"),
    ("fetch", "--recurse-submodules", "https://example.invalid/x.git"),
    ("-C", "/tmp", "show", "HEAD"),
])
def test_credentialed_env_refuses_everything_but_fetch_and_ls_remote(argv):
    with pytest.raises(gitrun.GitRefused):
        gitrun.run_git(*argv, env={gitrun.CREDENTIALED_MARKER: "ssh"})


@pytest.mark.parametrize("argv", [
    ("-C", "/m.git", "fetch", "--no-tags", "--filter=blob:limit=1m", "git@h:x.git", "+refs/heads/*:refs/r/k/heads/*"),
    ("-C", "/m.git", "ls-remote", "--symref", "git@h:x.git", "HEAD"),
])
def test_credentialed_reads_pass_the_guard(argv):
    gitrun.check_argv(argv)
    gitrun.check_credentialed(argv)


def test_private_read_env_always_carries_the_marker(estate):
    for line, kind in (('private_read = "ssh"', "ssh"),
                       ('private_read = { method = "token", token_file = "%s" }' % estate.token_file, "token")):
        estate.configure(line)
        assert private_read.method(load_config(), "gitlab").env()[gitrun.CREDENTIALED_MARKER] == kind


# ---------------------------------------------------------------------------------------------------------
# T0 stays anonymous: it judges public fetchability
# ---------------------------------------------------------------------------------------------------------
def test_t0_and_compat_never_reach_private_read():
    offenders = []
    for p in list((SRC / "t0").rglob("*.py")) + [SRC / "analysis" / "compat.py", SRC / "analysis" / "tierrun.py",
                                                  SRC / "matrix" / "cells.py"]:
        text = p.read_text()
        if "private_read" in text or "NANOSOC_RM_PRIVATE_READ" in text:
            offenders.append(p.relative_to(SRC).as_posix())
    assert offenders == []
    # and the only package code that builds a credentialed environment is the mirror sync
    importers = sorted(p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                       if re.search(r"import private_read\b|private_read import", p.read_text()))
    assert importers == ["mirror/commands.py", "mirror/sync.py"]           # classify() only in commands.py


# ---------------------------------------------------------------------------------------------------------
# Config shape
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("line, problem", [
    ('private_read = "password"', 'method must be "ssh" or "token"'),
    ('private_read = { method = "token" }', "needs token_file"),
    ('private_read = { method = "ssh", token_file = "t" }', "takes no token_file"),
    ('private_read = { method = "token", token_file = "t", identity_file = "k" }', "takes no identity_file"),
    ('private_read = { method = "ssh", password = "x" }', "unknown key 'password'"),
    ('private_read = 3', 'must be "ssh" or a table'),
])
def test_config_refuses_a_malformed_private_read(estate, line, problem):
    cfg = estate.configure(line)
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert problem in str(e.value)


def test_config_resolves_relative_files(estate):
    cfg = estate.configure('private_read = { method = "token", token_file = "gitlab-read.token" }')
    spec = load_config(cfg)["hosts"]["gitlab"]["private_read"]
    assert spec["token_file"] == str(estate.token_file)
    assert private_read.method(load_config(cfg), "github") is None


# ---------------------------------------------------------------------------------------------------------
# Causes, alert text, grouping, status CLI
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stderr, cause", [
    ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", "auth"),
    ("remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://h/x.git/'", "auth"),
    ("git@h: Permission denied (publickey).\nfatal: Could not read from remote repository.", "ssh-key"),
    ("Host key verification failed.\nfatal: Could not read from remote repository.", "ssh-host"),
    ("remote: Repository not found.\nfatal: repository 'https://h/x.git/' not found", "not-found"),
    ("fatal: unable to access 'https://h/x.git/': Could not resolve host: h", "network"),
    ("\ntimed out after 600s", "timeout"),
    ("fatal: something else", "other"),
])
def test_classify(stderr, cause):
    assert private_read.classify(stderr) == cause


def _fact(**kw):
    f = {"repo": {"host": "gitlab", "path": "soclabs/x"}, "component": "x", "ok": False, "last_success_at": None,
         "error": "fatal: could not read Username for 'https://h': terminal prompts disabled", "access": "unreadable",
         "cause": "auth", "method": None}
    f.update(kw)
    return f


def test_alert_groups_by_cause():
    def group(fact):
        return alertgroups.cause_of({"fact": {"alert_kind": "mirror_failed", "message": failure_message(fact)}})[0]
    assert group(_fact()) == "private-anon"
    assert group(_fact(method="ssh", cause="ssh-key", error="git@h: Permission denied (publickey).")) == \
        "private-creds"
    assert group(_fact(method="token")) == "private-creds"
    assert group(_fact(cause="config", error="[hosts.gitlab] private_read token file is empty")) == \
        "private-read-config"
    assert group(_fact(cause="network", error="fatal: unable to access: Could not resolve host: h")) == "unreachable"


def _main():
    from nanosoc_rm.cli import main
    return main


def test_mirror_status_access_column_and_summary(estate):
    estate.configure('private_read = "ssh"')
    sync_component("pub")
    sync_component("priv")
    runner = CliRunner()
    table = runner.invoke(_main(), ["mirror", "status"])
    lines = table.output.splitlines()
    assert lines[0].split()[:7] == ["host", "path", "component", "ok", "age", "last_success_at", "access"]
    cells = {ln.split()[1]: ln.split()[6] for ln in lines[1:]}
    assert cells == {"soclabs/priv": "credentials:ssh", "soclabs/pub": "anonymous"}
    summary = runner.invoke(_main(), ["mirror", "status", "--summary", "--json"])
    rows = [json.loads(ln) for ln in summary.output.splitlines()]
    assert [(r["access"], r["method"], r["count"]) for r in rows] == [("credentials", "ssh", 1),
                                                                       ("anonymous", None, 1)]
