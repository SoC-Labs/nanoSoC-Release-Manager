"""A fresh checkout's plain pytest is green (evaluation 2026-10-16, "fresh-checkout pytest"): the root conftest
puts the interpreter's bin/ on PATH, pytest-xdist is a [dev] dependency, and the not-live suite has a CI workflow."""
from __future__ import annotations

import importlib.util
import os
import shutil
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _root_conftest():
    spec = importlib.util.spec_from_file_location("rm_root_conftest", REPO / "tests" / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_console_script_is_on_path_without_setting_it():
    assert shutil.which("nanosoc-rm") is not None, os.environ.get("PATH")


def test_venv_on_path_prepends_only_a_bin_that_holds_the_script(tmp_path):
    venv_on_path = _root_conftest().venv_on_path
    good, empty = tmp_path / "venv" / "bin", tmp_path / "bare" / "bin"
    good.mkdir(parents=True)
    empty.mkdir(parents=True)
    (good / "nanosoc-rm").write_text("#!/bin/sh\n")
    env = {"PATH": "/usr/bin:/bin"}
    assert venv_on_path(env, good) and env["PATH"] == "%s:/usr/bin:/bin" % good
    assert not venv_on_path(env, good) and env["PATH"].count(str(good)) == 1     # once, not twice
    env2 = {"PATH": "/usr/bin"}
    assert not venv_on_path(env2, empty) and env2["PATH"] == "/usr/bin"          # the control: nothing to find


def test_xdist_is_a_dev_dependency():
    dev = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("pytest-xdist") for d in dev), dev


def test_the_ci_workflow_runs_the_not_live_suite():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "tests.yml").read_text())
    steps = [s.get("run", "") for job in wf["jobs"].values() for s in job["steps"]]
    assert any("pip install" in s and ".[dev]" in s for s in steps), steps
    test_steps = [s for s in steps if "pytest" in s]
    assert test_steps and all('-m "not live"' in s for s in test_steps), steps
    assert any("check_examples.py" in s for s in steps) and any("pyflakes" in s for s in steps), steps
    perms = wf.get("permissions")
    assert perms == {"contents": "read"}, perms                 # the workflow can read the repo, nothing more
