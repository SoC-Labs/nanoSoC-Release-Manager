"""Failure injection on `nanosoc-rm compat` (CR-11; interfaces.md section 9a): the network goes down between the
walk and the gates, or one host stops answering. The rule under test: a T0 run whose hosts did not answer is INFRA or
UNVERIFIED (exit 2), never PASS and never a FAIL that blames the tree.

Reuses the compat unit tests' hermetic Scene (tests/unit/analysis/test_compat.py); the gates' network git calls
(ls-remote, fetch, clone) are made to fail with git's own DNS-failure text.

Robustness angle: faults. A test that exposes a defect is a strict xfail named ROB-faults-<n>."""
from __future__ import annotations

import pytest

import faults_fix  # sys.path for the other unit-test directories

assert faults_fix.UNIT.is_dir()
from analysis_fixtures import load_at
from nanosoc_rm.analysis import compat as C
from nanosoc_rm.gitrun import GitResult
from test_compat import Scene


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    at = load_at("test_pins")
    h = at.LocalHosts(tmp_path_factory.mktemp("faults-compat"))
    yield Scene(at, h)
    h.api.close()


@pytest.fixture(autouse=True)
def _developer_env(monkeypatch):
    for k in ("CI_JOB_ID", "CI_SERVER_URL", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT", "RM_PROBE_URLS"):
        monkeypatch.delenv(k, raising=False)


DNS_DOWN = "fatal: unable to access '%s': Could not resolve host: %s"
NETWORK_SUBCOMMANDS = ("ls-remote", "fetch", "clone")


def _subcommand(argv) -> str:
    i = 0
    while i < len(argv) and str(argv[i]).startswith("-"):
        i += 2 if argv[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return str(argv[i]) if i < len(argv) else ""


def network_down(monkeypatch, only=None):
    """Every network git call of the gates fails as it does with DNS down (the url_map routes stay in place, so
    the failure is the network's, not a missing fixture). `only`: fail only the calls that name a URL containing
    this text."""
    from nanosoc_rm.t0 import gitwork
    real = gitwork.run_git

    def run_git(*args, **kw):
        argv = [str(a) for a in args]
        if kw.get("cwd") is not None:
            argv = ["-C", str(kw["cwd"])] + argv
        if _subcommand(argv) in NETWORK_SUBCOMMANDS:
            url = next((a for a in argv if "://" in a), "")
            if only is None or only in url:
                return GitResult(("git", *argv), 128, "", DNS_DOWN % (url, url.split("/")[2] if "//" in url else url))
        return real(*args, **kw)
    monkeypatch.setattr(gitwork, "run_git", run_git)


def gates_of(doc):
    return {g["id"]: g["verdict"] for g in doc["fact"]["gates"]}


def test_the_healthy_scene_passes(scene):
    """The control: the same scene with its hosts up is PASS."""
    doc = scene.run("top-main")
    assert doc["fact"]["verdict"] == "PASS", gates_of(doc)


def test_network_down_for_every_host_is_infra_never_pass(scene, monkeypatch):
    network_down(monkeypatch)
    doc = scene.run("top-main")
    f = doc["fact"]
    # README section 6 ladder: every host-dependent gate INFRA and no gate PASS reads UNVERIFIED; both are exit 2
    assert f["verdict"] in ("INFRA", "UNVERIFIED"), (f["verdict"], gates_of(doc))
    assert C.exit_for(f.get("run_verdict") or f["verdict"]) == 2
    g = gates_of(doc)
    assert g["t0.infra.probe"] == "INFRA" and "PASS" not in {g[k] for k in ("t0.pins.fetchable", "t0.pins.reachable")}


def test_one_pinned_repo_unreachable_is_never_pass(scene, monkeypatch):
    """Only beta's repo stops answering after the walk: its pin cannot be fetched, so the run cannot be PASS."""
    network_down(monkeypatch, only="/beta")
    doc = scene.run("top-main")
    f = doc["fact"]
    assert f["verdict"] in ("INFRA", "UNVERIFIED"), (f["verdict"], gates_of(doc))      # never PASS, never FAIL
    g = gates_of(doc)
    assert g["t0.infra.probe"] == "PASS" and g["t0.pins.fetchable"] == "INFRA", g
