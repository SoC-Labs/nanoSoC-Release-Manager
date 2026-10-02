"""Fixtures and helpers shared by the ci, credentials and schedule unit tests.

Imported by name (pytest puts this directory on sys.path). There is deliberately no conftest.py here: a second
`conftest` module would shadow tests/unit/conftest.py for `from conftest import ...`. Each test module binds the
fixtures it uses: `ce = ci_helpers.ce`.

The acceptance files' own fixtures and oracles are loaded from tests/acceptance (never copied), so a unit test and
the acceptance test it stands in for share one definition of the genuine case, the tampers and the checkers."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"


def clean_scan(_bundle):
    return SimpleNamespace(verdict="clean", findings=[], files_scanned=3, armed={"arm.banner": True})


GIT_ENV = {"GIT_AUTHOR_NAME": "rm-test", "GIT_AUTHOR_EMAIL": "rm-test@example.invalid",
           "GIT_COMMITTER_NAME": "rm-test", "GIT_COMMITTER_EMAIL": "rm-test@example.invalid",
           "GIT_AUTHOR_DATE": "2026-09-30T12:00:00Z", "GIT_COMMITTER_DATE": "2026-09-30T12:00:00Z",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def git(*args, cwd=None, input=None):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, input=input, capture_output=True, text=True,
                       env=dict(os.environ, **GIT_ENV), timeout=120)
    if p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), p.stderr))
    return p.stdout.strip()


def mirror_commit(mirror: Path, refs: list[str], message: str = "fixture") -> str:
    """A commit in bare repo `mirror` (created on first use), pointed at by each of `refs`."""
    if not mirror.exists():
        git("init", "-q", "--bare", mirror)
    tree = git("--git-dir", mirror, "mktree", input="")
    sha = git("--git-dir", mirror, "commit-tree", tree, "-m", message)
    for ref in refs:
        git("--git-dir", mirror, "update-ref", ref, sha)
    return sha


def jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (EXAMPLES / name).read_text().splitlines() if line.strip()]


def _load(name: str):
    spec = importlib.util.spec_from_file_location("at_%s" % name, REPO / "tests" / "acceptance" / ("%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod              # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def ce():
    """tests/acceptance/test_ci_errata.py: genuine_case, D5_TAMPERS, write_bundle, write_config, the oracles."""
    return _load("test_ci_errata")


@pytest.fixture(scope="session")
def sw():
    """tests/acceptance/test_scope_web.py: write_call_census, check_a2, check_a4, check_h3, FakeHost."""
    return _load("test_scope_web")


@pytest.fixture
def ci_config(tmp_path, monkeypatch, ce):
    """AT-D5's service config ($NANOSOC_RM_CONFIG) over the contract example registry. Returns the Config."""
    from nanosoc_rm.config import load_config
    path = ce.write_config(tmp_path, monkeypatch)
    ce.example_registry(tmp_path / "registry")
    return load_config(path)


@pytest.fixture
def scanner_clean(monkeypatch):
    """nanosoc_rm.scan is built on another branch: verify's check 7 sees this clean scanner instead."""
    from nanosoc_rm.ci import verify
    monkeypatch.setattr(verify, "_scan_dir", lambda: clean_scan)
    return clean_scan
