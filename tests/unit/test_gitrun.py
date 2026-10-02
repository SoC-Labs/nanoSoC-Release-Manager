"""nanosoc_rm.gitrun: the one git runner refuses writes before git starts, never prompts, records its argv."""
from __future__ import annotations

import os
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nanosoc_rm import gitrun
from nanosoc_rm.gitrun import GitRefused, check_argv, hardened_env, run_git

ALLOWED = [
    "fetch --no-tags --prune --filter=blob:none https://example.invalid/x.git +refs/heads/*:refs/r/k/heads/*",
    "-C /m.git fetch --no-tags https://example.invalid/x.git +refs/tags/*:refs/r/k/tags/*",
    "--git-dir=/m.git for-each-ref --contains abc refs/r/k/heads",
    "-C /m.git for-each-ref --count=1 --points-at abc refs/r/k/tags",
    "ls-remote --heads https://example.invalid/x.git",
    "-C /m.git rev-list --count a..b",
    "-C /m.git merge-base --is-ancestor a b",
    "-C /m.git cat-file -p abc:.gitmodules",
    "-C /m.git ls-tree abc -- path",
    "-C /m.git rev-parse FETCH_HEAD^{commit}",
    "-C /m.git reflog show refs/r/k/heads/main",
    "-C /m.git reflog refs/r/k/heads/main",
    "init -q --bare /m.git",
    "-C /m.git config core.logAllRefUpdates always",
    "-C /m.git config gc.reflogExpire never",
    "-C /m.git config --get remote.origin.url",
    "-C /m.git config remote.origin.url",
    ["-c", "credential.helper=!f(){ echo password=$RM_TOKEN; }; f", "fetch", "https://example.invalid/p.git"],
    "clone --filter=blob:none https://example.invalid/x.git /tmp/c",
    "-C /tmp/c checkout -q abc",
    "-C /tmp/c submodule update --init --recursive",
    "-C /tmp/c submodule sync --recursive",
    "-C /tmp/s add -A",
    "-C /tmp/s commit -q -m scan",
    "-C /m.git tag --list keep/*",
    "-C /m.git tag --points-at abc",
    "-C /m.git tag -l --contains abc",
    "-C /m.git branch -r --contains abc",
    "-C /m.git branch --list",
    "-C /m.git remote -v",
    "-C /m.git remote get-url origin",
    "-C /m.git symbolic-ref --short HEAD",
    "ls-remote ssh://git@example.invalid:22/x.git",
    "version",
]

REFUSED = [
    "push origin HEAD",
    "-C /m.git push --force https://example.invalid/x.git main",
    "send-pack https://example.invalid/x.git main",
    "receive-pack /m.git",
    "-C /m.git tag v1.0.0",
    "-C /m.git tag -d keep/eth-rc4/tidechart",
    "-C /m.git tag -a -m msg v2",
    "-C /m.git tag -fa v2",
    "-C /m.git branch topic",
    "-C /m.git branch -D main",
    "-C /m.git remote add fork https://example.invalid/alice/ip.git",
    "-C /m.git remote set-url origin https://example.invalid/alice/ip.git",
    "-C /m.git remote remove origin",
    "-C /m.git config remote.origin.url https://example.invalid/fork.git",
    "-C /m.git config remote.origin.pushurl https://example.invalid/fork.git",
    "-C /m.git config --unset remote.origin.url",
    "-C /m.git config url.https://x/.insteadOf https://y/",
    "-C /m.git config credential.helper store",
    "-C /m.git config alias.sync push",
    "-C /m.git config --global core.logAllRefUpdates always",
    "-C /m.git config --file /etc/gitconfig a.b c",
    "-c alias.sync=!git push sync",
    "-c core.hooksPath=/tmp/hooks fetch https://example.invalid/x.git",
    "-c protocol.ext.allow=always fetch ext::sh",
    "-C /m.git update-ref -d refs/r/k/heads/main",
    "-C /m.git reflog expire --expire=now --all",
    "-C /m.git reflog delete refs/r/k/heads/main@{0}",
    "-C /m.git symbolic-ref HEAD refs/heads/other",
    "-C /tmp/c submodule foreach git push",
    "fetch --upload-pack=evil https://example.invalid/x.git",
    "ls-remote -u evil https://example.invalid/x.git",
    "clone -u evil https://example.invalid/x.git /tmp/c",
    "clone --template=/tmp/hooks https://example.invalid/x.git /tmp/c",
    "clone -c core.hooksPath=/tmp/h https://example.invalid/x.git /tmp/c",
    "fetch ext::sh",
    "fetch https://user:tok3n@example.invalid/x.git",
    "--exec-path=/tmp/evil fetch https://example.invalid/x.git",
    "--config-env=core.sshCommand=X fetch https://example.invalid/x.git",
    "--bogus-option status",
    "gc --prune=now",
    "filter-branch --all",
    "-C /m.git",
    "",
]


@pytest.mark.parametrize("argv", ALLOWED, ids=str)
def test_allowed(argv):
    check_argv(argv if isinstance(argv, list) else shlex.split(argv))


@pytest.mark.parametrize("argv", REFUSED)
def test_refused(argv):
    with pytest.raises(GitRefused):
        check_argv(shlex.split(argv))


def test_refusal_is_a_permission_error_and_git_never_starts(monkeypatch):
    # AT-A6's planted runner raises PermissionError on a push; the real one keeps that contract.
    assert issubclass(GitRefused, PermissionError)
    started = []
    monkeypatch.setattr(gitrun.subprocess, "run", lambda *a, **k: started.append(a))
    for args in (("push", "origin", "HEAD"), ("send-pack", "x"), ("receive-pack", "x")):
        with pytest.raises(PermissionError):
            run_git(*args)
    assert started == [], "git was started for a refused argv"


def test_runs_and_records_the_exact_argv(tmp_path):
    repo = tmp_path / "r"
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    assert run_git("init", "-q", str(repo), isolate=True).ok
    (repo / "f").write_text("x\n")
    assert run_git("add", "f", cwd=repo, isolate=True).ok
    assert run_git("commit", "-q", "-m", "one two", cwd=repo, env=ident, isolate=True).ok
    r = run_git("rev-parse", "HEAD", cwd=repo, isolate=True).check()
    assert len(r.stdout.strip()) == 40
    assert r.argv == ("git", "-C", str(repo), "rev-parse", "HEAD")
    assert r.command == shlex.join(r.argv) and r.source() == {"kind": "git", "command": r.command}
    bad = run_git("rev-parse", "no-such-ref", cwd=repo, isolate=True)
    assert not bad.ok and bad.returncode != 0
    with pytest.raises(gitrun.GitError, match="rev-parse no-such-ref"):
        bad.check()
    blob = run_git("cat-file", "-p", "HEAD:f", cwd=repo, isolate=True, text=False)
    assert blob.stdout == b"x\n"


def test_timeout_is_a_result_not_a_hang():
    r = run_git("version", timeout=1e-6)
    assert r.timed_out and r.returncode == -1 and not r.ok


def test_hardened_env(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere.git")
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/askpass")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/routing.gitconfig")   # tests route fetches this way (section 3)
    env = hardened_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert "GIT_DIR" not in env and "SSH_ASKPASS" not in env and "GIT_ASKPASS" not in env
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert env["GIT_CONFIG_GLOBAL"] == "/tmp/routing.gitconfig"         # inherited by the service
    iso = hardened_env(isolate=True)
    assert iso["GIT_CONFIG_GLOBAL"] == os.devnull and iso["GIT_CONFIG_NOSYSTEM"] == "1"   # the T0 runner
    assert hardened_env({"GIT_SSH_COMMAND": "ssh -o BatchMode=yes -i k"})["GIT_SSH_COMMAND"].endswith("-i k")


def test_no_prompt_for_credentials():
    # A remote that asks for a password makes git fail at once ("terminal prompts disabled"), never wait.
    class Auth(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Auth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = run_git("ls-remote", "http://127.0.0.1:%d/private.git" % srv.server_address[1], timeout=60, isolate=True)
    finally:
        srv.shutdown()
    assert not r.ok and not r.timed_out
    assert "terminal prompts disabled" in r.stderr, r.stderr
