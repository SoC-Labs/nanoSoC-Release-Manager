"""A tier command run for an executor job gets its own pid namespace (integration review 2026-10-16).

A job's verdict documents are the stdout of `nanosoc-rm` running in the agent's sandbox; the repo commands it
launches ran in the same pid namespace, so one could write /proc/$PPID/fd/1 and print a fake PASS. With
$NANOSOC_RM_TIER_PIDNS (set by the agent for a bwrap job) each tier command runs under a nested bwrap with
--unshare-pid: the launching process is not in its /proc at all."""
from __future__ import annotations

import os

import pytest

from nanosoc_rm.analysis import tierrun as T
from nanosoc_rm.executor import sandbox

pytestmark = [pytest.mark.requirement("D1"), pytest.mark.requirement("PR6")]
needs_bwrap = pytest.mark.skipif(not sandbox.bwrap_usable(), reason="bubblewrap cannot create a sandbox here")
NONCE = "0123456789abcdef0123456789abcdef"


def _run(tmp_path, command):
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    return T.run_tier({"command": command, "cwd": ".", "budget_s": 60}, "t1", tree, tmp_path, NONCE)


def test_no_request_no_wrap(monkeypatch):
    monkeypatch.delenv(T.PIDNS_ENV, raising=False)
    assert T.pidns_wrap(["make", "lint"]) == (["make", "lint"], None)


def test_a_requested_namespace_that_cannot_be_made_is_infra_never_a_bare_run(tmp_path, monkeypatch):
    mark = tmp_path / "ran"
    monkeypatch.setenv(T.PIDNS_ENV, str(tmp_path / "no-bwrap"))
    d = _run(tmp_path, "touch %s" % mark)
    assert d["verdict"] == "INFRA" and "pid namespace was not made" in d["reason"]
    assert not mark.exists(), "the command ran without the namespace it was asked to have"
    monkeypatch.setenv(T.PIDNS_ENV, "bwrap")                       # not an absolute path: refused the same way
    assert _run(tmp_path, "true")["verdict"] == "INFRA"


@needs_bwrap
def test_the_launching_process_is_invisible_to_the_tier_command(tmp_path, monkeypatch):
    me = os.getpid()                                               # stands for the job's nanosoc-rm process
    probe = "test ! -e /proc/%d/fd/1 && test ! -e /proc/$PPID/fd/1 -o $PPID -le 1" % me
    monkeypatch.delenv(T.PIDNS_ENV, raising=False)
    assert _run(tmp_path, "test -e /proc/%d/fd/1" % me)["verdict"] == "PASS"     # the control: visible without it
    monkeypatch.setenv(T.PIDNS_ENV, sandbox.BWRAP)
    d = _run(tmp_path, probe)
    assert d["verdict"] == "PASS", d
    assert _run(tmp_path, "echo FORGED > /proc/%d/fd/1" % me)["verdict"] == "FAIL"

