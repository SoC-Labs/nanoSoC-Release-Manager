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
    assert sw.wildcard_overflow(census, WRITE_CALLS) == []
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


# The evaluation's planted probes (2026-10-16, tests.md 3.3 item 4): each form slipped past the census before it
# resolved imports. Each probe is its own file, so a census that misses one names it.
EVASIONS = {
    "from_import.py": ("from subprocess import Popen\n"
                       "def sneak(u):\n    Popen(['git', 'push', u, 'HEAD'])\n", "git push"),
    "alias.py": ("import subprocess as proc\n"
                 "def sneak(u):\n    proc.run(['git', 'push', u])\n", "git push"),
    "from_alias.py": ("from subprocess import check_call as cc\n"
                      "def sneak(u):\n    cc(['git', 'push', u])\n", "git push"),
    "getattr_http.py": ("import httpx\n"
                        "def sneak(u):\n    getattr(httpx.Client(), 'delete')(u)\n", "DELETE"),
    "getattr_dynamic.py": ("import httpx\n"
                           "def sneak(u, verb):\n    getattr(httpx.Client(), verb)(u)\n", "?"),
    "os_execvp.py": ("import os\n"
                     "def sneak(u):\n    os.execvp('git', ['git', 'push', u])\n", "git push"),
    "os_from_execl.py": ("from os import execlp\n"
                         "def sneak(u):\n    execlp('git', 'git', 'push', u)\n", "git push"),
    "os_alias_spawn.py": ("import os as o\n"
                          "def sneak(u):\n    o.spawnvp(o.P_WAIT, 'git', ['git', 'push', u])\n", "git push"),
    "from_httpx.py": ("from httpx import post\n"
                      "def sneak(u):\n    post('repos/SoC-Labs/X/git/refs', json={})\n", "POST"),
}


def _planted(tmp_path, files):
    pkg = tmp_path / "nanosoc_rm"
    shutil.copytree(SRC_PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in files.items():
        (pkg / rel).write_text(text)
    return pkg


def test_every_evasion_form_is_caught(sw, tmp_path):
    census = sw.write_call_census(_planted(tmp_path, {k: v[0] for k, v in EVASIONS.items()}))
    bad = {(c["file"].rsplit("/", 1)[1], c["method"]) for c in census if not sw.allowed(c, WRITE_CALLS)}
    want = {(name, method) for name, (_src, method) in EVASIONS.items()}
    assert want <= bad, "missed: %s" % sorted(want - bad)
    probs = sw.check_a6(census, WRITE_CALLS)
    for name in ("os_execvp.py", "alias.py", "from_import.py"):
        assert any(name in p for p in probs), (name, probs)


def test_the_census_ignores_lookalikes(sw, tmp_path):
    # the control: names that only look like a process start or a write are not censused
    pkg = _planted(tmp_path, {"lookalike.py": (
        "import os\nfrom pathlib import Path\n"
        "def run(x):\n    return x\n"
        "def fine(d, u):\n    run(['git', 'push', u])\n    d.run(['git', 'push', u])\n"
        "    os.path.join('a', 'b')\n    getattr(d, 'name')\n")})
    census = sw.write_call_census(pkg)
    assert [c for c in census if c["file"].endswith("lookalike.py")] == []


def test_a_second_unresolved_call_cannot_hide_behind_an_entry(sw, tmp_path):
    client = SRC_PKG / "ci" / "client.py"
    text = client.read_text()
    assert "def _second(" not in text
    pkg = _planted(tmp_path, {"ci/client.py": text + "\n\ndef _second(http, url):\n    return http.post(url)\n"})
    census = sw.write_call_census(pkg)
    assert all(sw.allowed(c, WRITE_CALLS) for c in census)          # each one alone looks covered...
    over = sw.wildcard_overflow(census, WRITE_CALLS)
    assert len(over) == 1 and "nanosoc_rm/ci/client.py POST trigger/pipeline covers 1" in over[0], over
    assert sw.wildcard_overflow(sw.write_call_census(SRC_PKG), WRITE_CALLS) == []   # the real package: none
