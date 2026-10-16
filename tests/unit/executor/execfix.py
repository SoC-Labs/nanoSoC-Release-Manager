"""Fixtures for the remote executor tests (interfaces.md section 27): a service config with one agent, a hermetic
mirror holding a fake tier, an agent config, and an in-process server (FastAPI TestClient over create_app).

No network, no lab host, no EDA tool: the "tier" is a shell script committed to a throwaway repo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nanosoc_rm.config import load_config
from nanosoc_rm.executor import queue as Q
from nanosoc_rm.executor import sandbox, tokens
from nanosoc_rm.executor.agentconfig import load_agent_config
from nanosoc_rm.executor.client import ExecutorClient

BWRAP_OK = sandbox.bwrap_usable()
needs_bwrap = pytest.mark.skipif(not BWRAP_OK, reason="bubblewrap cannot create a sandbox on this host")

FAKE_TIER = r"""#!/bin/bash
# The hermetic fake tier: what it does is chosen by MODE (the task's env).
echo "fake tier: mode=${MODE:-pass} tree=$TREE"
case "${MODE:-pass}" in
  pass)    echo "PASS 3"; exit 0 ;;
  skipped) echo "PASS 3"; echo "WRAPPER: SKIPPED"; exit 0 ;;
  fail)    echo "lint error"; exit 1 ;;
  env)     env | sort; exit 0 ;;
  probe)   cat "$PROBE" && echo "READ-OK"; exit 0 ;;
  sleep)   sleep 60; exit 0 ;;
  flood)   head -c 3000000 /dev/zero | tr '\0' 'x'; exit 0 ;;
  verdicts) printf '2026-10-16T10:00:00Z\tPASS\tlint-wrapper\twrapper linted\n' > "$NANOSOC_RM_VERDICTS"; exit 0 ;;
  pin)     cat "$TREE/sub/PIN.txt"; exit 0 ;;
esac
"""
CONTROL = "exit 1"


def git(*args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": "/usr/bin:/bin", "GIT_AUTHOR_DATE": "2026-10-16T09:00:00Z",
           "GIT_COMMITTER_DATE": "2026-10-16T09:00:00Z"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(work: Path, files: dict[str, str]) -> str:
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", ".", cwd=work)
    for name, text in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if name.endswith(".sh"):
            p.chmod(0o755)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "fixture", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


class Rig:
    """Everything one test needs: .config (service), .agent (agent config), .client, .app_client, .commit."""

    def __init__(self, root: Path, *, labels=("verilator", "vcs"), mode="auto", lease_seconds=60, extra=""):
        self.root = root
        self.token = tokens.new_token()
        self.other_token = tokens.new_token()
        data = root / "data"
        (root / "service.toml").write_text(
            '[service]\ndata_dir = "%s"\n\n[executor]\nlease_seconds = %d\nmax_attempts = 2\n\n'
            '[[executor.agents]]\nname = "eda-1"\ntoken_sha256 = "%s"\nlabels = %s\n\n'
            '[[executor.agents]]\nname = "eda-2"\ntoken_sha256 = "%s"\nlabels = ["verilator"]\n%s'
            % (data, lease_seconds, tokens.digest(self.token), json.dumps(list(labels)),
               tokens.digest(self.other_token), extra))
        self.config_path = root / "service.toml"
        src = root / "src"
        self.pin_commit = make_repo(src / "pinrepo", {"PIN.txt": "pinned content\n"})
        self.commit = make_repo(src / "rootrepo", {"tier.sh": FAKE_TIER, "README": "fixture root\n"})
        self.mirrors = root / "mirrors"
        self.mirrors.mkdir()
        git("clone", "-q", "--bare", str(src / "rootrepo"), str(self.mirrors / "fixture-root.git"), cwd=root)
        git("clone", "-q", "--bare", str(src / "pinrepo"), str(self.mirrors / "fixture-pin.git"), cwd=root)
        agent_dir = root / "agent"
        agent_dir.mkdir()
        self.token_file = agent_dir / "token"
        fd = os.open(str(self.token_file), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token + "\n")
        (agent_dir / "agent.toml").write_text(
            '[agent]\nname = "eda-1"\nserver = "http://localhost"\ntoken_file = "%s"\nmirror_root = "%s"\n'
            'scratch_dir = "%s"\nlabels = %s\npoll_seconds = 1\nheartbeat_seconds = 1\nmax_log_bytes = 1000000\n'
            'max_upload_bytes = 4096\n\n[sandbox]\nmode = "%s"\n'
            % (self.token_file, self.mirrors, root / "scratch", json.dumps(list(labels)), mode))
        self.agent_path = agent_dir / "agent.toml"

    @property
    def config(self):
        return load_config(self.config_path)

    @property
    def agent(self):
        return load_agent_config(self.agent_path)

    def app(self):
        from nanosoc_rm.web.app import create_app
        return create_app(self.config_path)

    def http(self) -> TestClient:
        return TestClient(self.app(), base_url="http://localhost")

    def client(self, http=None) -> ExecutorClient:
        return ExecutorClient(self.agent, self.token, http=http or self.http())

    def task(self, **run) -> dict:
        r = {"tier": "t1", "command": "./tier.sh", "budget_s": 60}
        r.update(run)
        return {"schema": 1, "subject": {"configuration": None, "component": "fixture-root", "capability": "lint"},
                "mirror": "fixture-root", "commit": self.commit, "pins": [], "run": r, "labels": ["verilator"],
                "requested_by": "david"}

    def enqueue(self, task=None, **run) -> int:
        return Q.enqueue(self.config, task or self.task(**run))
