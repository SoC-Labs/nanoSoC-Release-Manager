"""Shared set-up for the property-based robustness tests (angle: property; hypothesis).

A helper module, not a conftest.py (see tests/unit/registry/registry_helpers.py for why). Importing it:

- skips the importing test module when hypothesis is not installed (it is not in the [dev] extra yet; install it
  into the venv with `pip install hypothesis`);
- loads the hypothesis profile: `rm` (default) is derandomized, so the suite is deterministic and an xfail(strict)
  defect test fails the same way on every run; RM_HYPOTHESIS_PROFILE=explore runs many random examples to hunt
  for new counterexamples. No example database is written (nothing lands in the worktree).

It also holds the reference oracles the tests compare against: git itself for .gitmodules (`git config -f`) and
for patches (`git apply`).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st  # noqa: E402

# Test modules import hypothesis through here, so the profile is always loaded before any strategy is built.
__all__ = ["assume", "budget", "example", "given", "settings", "st", "git", "git_config_modules", "git_config_write",
           "as_set", "GIT_ENV"]

_SUPPRESS = [HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.data_too_large]
settings.register_profile("rm", database=None, deadline=None, derandomize=True, max_examples=200,
                          suppress_health_check=_SUPPRESS)
settings.register_profile("explore", database=None, deadline=None, max_examples=3000, suppress_health_check=_SUPPRESS)
PROFILE = os.environ.get("RM_HYPOTHESIS_PROFILE", "rm")
settings.load_profile(PROFILE)


def budget(n: int):
    """Cap a slow test (one that runs git per example) at `n` examples in the default profile only, so the
    explore profile still searches it widely."""
    return settings(max_examples=n) if PROFILE == "rm" else settings()

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(*args: str, cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=GIT_ENV, timeout=60)
    if check and p.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr.decode(errors="replace")))
    return p


def git_config_modules(text: str) -> list[dict[str, str]] | None:
    """What git itself reads from `text` as a .gitmodules file: [{name, key: value}] for every submodule section
    that has at least one key; None when git refuses the file."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d, "gitmodules")
        f.write_bytes(text.encode("utf-8"))
        p = git("config", "-f", str(f), "--list", "-z", check=False)
    if p.returncode:
        return None
    mods: dict[str, dict[str, str]] = {}
    for ent in p.stdout.decode("utf-8").split("\0"):
        if not ent:
            continue
        key, nl, value = ent.partition("\n")
        if not nl:
            value = "true"                               # a bare key (git config's implicit boolean)
        if "." not in key:
            continue
        section, rest = key.split(".", 1)
        if section != "submodule" or "." not in rest:
            continue
        name, k = rest.rsplit(".", 1)
        mods.setdefault(name, {"name": name})[k] = value
    return list(mods.values())


def git_config_write(modules: list[tuple[str, dict[str, str]]]) -> str:
    """The .gitmodules text git's own writer produces for `modules` ([(name, {key: value})])."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d, "gitmodules")
        f.write_text("")
        for name, kv in modules:
            for k, v in kv.items():
                git("config", "-f", str(f), "submodule.%s.%s" % (name, k), v)
        return f.read_bytes().decode("utf-8")


def as_set(modules: list[dict[str, str]]) -> list[list[tuple[str, str]]]:
    """Order-free, comparable form; sections with no key are dropped (git does not list them)."""
    return sorted(sorted(m.items()) for m in modules if len(m) > 1)
