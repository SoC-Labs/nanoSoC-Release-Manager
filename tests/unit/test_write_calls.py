"""WRITE_CALLS against the package source, with AT-A2/AT-A6's own census (single-sourced: loaded from
tests/acceptance/test_scope_web.py, never copied). Run on every builder branch, so a new call site that the
census flags is caught in the commit that adds it, not at integration."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from nanosoc_rm.contracts import WRITE_CALLS

REPO = Path(__file__).resolve().parents[2]
SRC_PKG = REPO / "src" / "nanosoc_rm"


@pytest.fixture(scope="module")
def sw():
    spec = importlib.util.spec_from_file_location("scope_web_checkers", REPO / "tests" / "acceptance" / "test_scope_web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_censused_call_is_allowlisted(sw):
    census = sw.write_call_census(SRC_PKG)
    assert [c for c in census if not sw.allowed(c, WRITE_CALLS)] == []
    assert sw.allowlist_problems(WRITE_CALLS) == []
    assert sw.check_a6(census, WRITE_CALLS) == []
    runner = [c for c in census if c["file"] == "nanosoc_rm/gitrun.py"]
    assert [(c["kind"], c["method"], c["target"]) for c in runner] == [("exec", "?", "git with unresolved subcommand")], \
        "gitrun must hold exactly one subprocess call, in the form the census recognises"
    assert {c["file"] for c in census} <= {w["file"] for w in WRITE_CALLS}
    assert [w["effect"] for w in WRITE_CALLS if w["file"] == "nanosoc_rm/gitrun.py"] == ["git-runner"]


def test_a_planted_write_is_caught(sw, tmp_path):
    # Negative control: the same check over a copy of the package with a git push and an HTTP DELETE added.
    pkg = tmp_path / "nanosoc_rm"
    shutil.copytree(SRC_PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    (pkg / "planted.py").write_text(
        "import subprocess, httpx\n"
        "def sneak(url):\n"
        "    subprocess.run(['git', 'push', url, 'HEAD'])\n"
        "    httpx.Client().delete('repos/SoC-Labs/X/git/refs/tags/silicon/x')\n")
    census = sw.write_call_census(pkg)
    bad = [c for c in census if not sw.allowed(c, WRITE_CALLS)]
    assert {(c["file"], c["method"]) for c in bad} == {("nanosoc_rm/planted.py", "git push"),
                                                       ("nanosoc_rm/planted.py", "DELETE")}
    probs = sw.check_a6(census, WRITE_CALLS)
    assert any("planted.py" in p and "git push" in p for p in probs), probs


def test_the_executor_effect_is_confined(sw, tmp_path):
    # CR-WG-executor: effect `executor` is a POST to /executor/v1/ from executor/client.py, guarded; anything else is
    # an allowlist problem, and a POST planted in another executor module is not covered by the entry.
    ok = [w for w in WRITE_CALLS if w["effect"] == "executor"]
    assert [(w["file"], w["method"]) for w in ok] == [("nanosoc_rm/executor/client.py", "POST")]
    for bad in (dict(ok[0], file="nanosoc_rm/executor/agent.py"), dict(ok[0], method="PUT"),
                dict(ok[0], target="api/v1/anything"), {k: v for k, v in ok[0].items() if k != "guard"}):
        assert any("executor entry" in p for p in sw.allowlist_problems(
            [w for w in WRITE_CALLS if w["effect"] != "executor"] + [bad])), bad
    pkg = tmp_path / "nanosoc_rm"
    shutil.copytree(SRC_PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    with (pkg / "executor" / "agent.py").open("a") as f:
        f.write("\n\ndef _sneak(http):\n    http.post('/executor/v1/claim')\n")
    census = sw.write_call_census(pkg)
    assert [(c["file"], c["method"]) for c in census if not sw.allowed(c, WRITE_CALLS)] == \
        [("nanosoc_rm/executor/agent.py", "POST")]
