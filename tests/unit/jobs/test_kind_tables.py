"""One job-kind seam (Wave C, CR-WC-seams): every plug-in kind registers through jobs.register_kind, and a fresh
worker process knows them all."""
from __future__ import annotations

import subprocess
import sys

import pytest

from nanosoc_rm import jobs


def test_a_fresh_worker_process_knows_every_wave_b_kind():
    # In a fresh interpreter (the service's worker imports no CLI plug-in before its first job)
    code = "from nanosoc_rm import jobs; print(' '.join(jobs.all_kinds()))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    for kind in ("compat", "matrix-cell", "bumps", "quality", "release-draft", "release-verify", "forks",
                 "versions-refresh", "version-draft"):
        assert kind in out, (kind, out)
    assert len(out) == len(set(out)), out


def test_every_plugin_kind_sits_in_the_one_table():
    jobs.all_kinds()
    assert not hasattr(jobs, "MODULE_KINDS") and not hasattr(jobs, "EXTRA_KINDS") and not hasattr(jobs, "PLUGIN_KINDS")
    for name in ("matrix-cell", "bumps", "quality", "release-draft", "release-verify", "forks", "versions-refresh",
                 "version-draft"):
        k = jobs.registered(name)
        assert isinstance(k, jobs.JobKind) and k.name == name, name
    assert jobs.registered("matrix-cell").action == "run_t0" and jobs.registered("matrix-cell").outcome is not None
    assert jobs.registered("compat") is None and jobs.registered("no-such-kind") is None


def test_every_kind_gives_cli_args_ending_in_json_and_refuse_a_bad_request():
    assert jobs.cli_args("matrix-cell", jobs._params("matrix-cell", {"configuration": "x", "column": "pinned"}))[-1] == "--json"
    assert jobs.cli_args("bumps", jobs._params("bumps", {"configuration": "eth-main"})) == [
        "bumps", "scan", "eth-main", "--store", "--json"]
    assert jobs.cli_args("versions-refresh", jobs._params("versions-refresh", {"components": ["a"]})) == [
        "version", "refresh", "a", "--json"]
    for kind, bad in (("bumps", {"configuration": "../x"}), ("version-draft", {"component": "../x"}),
                      ("matrix-cell", {"configuration": "x", "column": "nope"})):
        with pytest.raises(jobs.JobUsage):
            jobs._params(kind, bad)
    with pytest.raises(ValueError):
        jobs.register_kind("bumps", lambda p: p, lambda p: [])          # another module's kind cannot be replaced
