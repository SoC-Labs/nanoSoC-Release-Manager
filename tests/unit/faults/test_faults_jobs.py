"""Failure injection on the local job runner (CR-17; interfaces.md section 13a): a job process killed mid-run, a
child whose exit code and printed verdict disagree, a write cut short, a store that fails while the worker records
a result, a corrupt or unwritable job store, and a service restart. The rule under test: a job that did not answer
is INFRA or UNVERIFIED with a reason, never PASS, and never left `running`.

Robustness angle: faults. A test that exposes a defect is a strict xfail named ROB-faults-<n>."""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

import faults_fix  # sys.path for the other unit-test directories

assert faults_fix.UNIT.is_dir()
from nanosoc_rm import jobs
from nanosoc_rm.config import load_config

CHILD = [sys.executable, str(Path(__file__).with_name("faults_child.py"))]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def _run_one(cfg, monkeypatch, mode, kind="walk", params=None):
    monkeypatch.setenv("FAULT_MODE", mode)
    job = jobs.submit(cfg, kind, params or {"configuration": "eth-main"}, by="alice")
    assert jobs.Worker(cfg.path, argv_prefix=CHILD).step() == job["id"]
    return jobs.get(cfg, job["id"])


# ----- a job process killed mid-run --------------------------------------------------------------------------------
@pytest.mark.parametrize("kind, params", [("walk", {"configuration": "eth-main"}),
                                          ("compat", {"configuration": "eth-main"})])
def test_a_job_killed_by_sigkill_is_infra_with_the_signal(cfg, monkeypatch, kind, params):
    job = _run_one(cfg, monkeypatch, "sigkill", kind, params)
    assert job["state"] == "infra" and job["verdict"] == "INFRA", job
    assert "signal 9" in job["reason"] and job["exit_code"] == -9
    assert "working..." in jobs.log_text(cfg, job["id"])


def test_a_compat_job_whose_result_line_was_cut_short_is_not_pass(cfg, monkeypatch):
    """Disk full while the child printed its result: exit 0 but no whole compat_result line."""
    job = _run_one(cfg, monkeypatch, "half_line", "compat", {"configuration": "eth-main"})
    assert job["state"] == "done" and job["verdict"] == "UNVERIFIED", job


def test_the_control_a_whole_pass_line_with_exit_0_is_pass(cfg, monkeypatch):
    """The control for the two tests above and below: the same child, answering, does read PASS."""
    job = _run_one(cfg, monkeypatch, "ok", "compat", {"configuration": "eth-main"})
    assert job["state"] == "done" and job["verdict"] == "PASS", job


@pytest.mark.xfail(strict=True, reason="ROB-faults-1: a compat job whose child printed a PASS compat_result but exited "
                                       "2 (could not answer) is recorded verdict PASS; the exit code is ignored")
def test_a_compat_job_that_exits_2_after_printing_pass_is_not_pass(cfg, monkeypatch):
    job = _run_one(cfg, monkeypatch, "pass_then_2", "compat", {"configuration": "eth-main"})
    assert job["verdict"] != "PASS", job


@pytest.mark.xfail(strict=True, reason="ROB-faults-1: a compat job whose child printed a PASS compat_result but exited "
                                       "1 (FAIL) is recorded verdict PASS; the exit code is ignored")
def test_a_compat_job_that_exits_1_after_printing_pass_is_not_pass(cfg, monkeypatch):
    job = _run_one(cfg, monkeypatch, "pass_then_1", "compat", {"configuration": "eth-main"})
    assert job["verdict"] != "PASS", job


# ----- the worker cannot record the result -------------------------------------------------------------------------
def test_a_failed_final_write_never_leaves_the_job_running(cfg, monkeypatch):
    monkeypatch.setenv("FAULT_MODE", "ok")
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    real = jobs._update
    calls = {"n": 0}

    def flaky(config, job_id, **fields):
        if "finished_at" in fields and calls["n"] == 0:      # the one write that records the end: fails once
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real(config, job_id, **fields)
    monkeypatch.setattr(jobs, "_update", flaky)
    try:
        jobs.Worker(cfg.path, argv_prefix=CHILD).step()
    except sqlite3.OperationalError:
        pass                                                 # _loop would log this and carry on
    assert calls["n"] == 1
    assert jobs.get(cfg, job["id"])["state"] != "running"


def test_a_final_write_that_fails_every_retry_is_written_before_the_next_claim(cfg, monkeypatch):
    """Every attempt to record the end fails: the worker keeps the fields and writes them on its next step, before
    it claims another job (ROB-faults-2; review: the kept-fields path had no test)."""
    monkeypatch.setenv("FAULT_MODE", "ok")
    monkeypatch.setattr(jobs.Worker, "FINISH_RETRY_S", (0, 0))
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    real = jobs._update
    broken = {"on": True}

    def flaky(config, job_id, **fields):
        if "finished_at" in fields and broken["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real(config, job_id, **fields)
    monkeypatch.setattr(jobs, "_update", flaky)
    worker = jobs.Worker(cfg.path, argv_prefix=CHILD)
    with pytest.raises(sqlite3.OperationalError):
        worker.step()
    assert jobs.get(cfg, job["id"])["state"] == "running"     # the store refused every attempt
    broken["on"] = False
    assert worker.step() is None                              # nothing else queued; the kept end is written first
    got = jobs.get(cfg, job["id"])
    assert got["state"] != "running" and got["finished_at"], got


def test_a_job_row_that_cannot_be_updated_does_not_stop_recovery_of_the_others(cfg, monkeypatch):
    """recover(): one job whose row update fails is logged and skipped; the next job still becomes INFRA
    (ROB-faults-3; review: only the log-write half had a test)."""
    a = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    b = jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, by="alice")
    for j in (a, b):
        jobs._update(cfg, j["id"], state="running", started_at="2026-10-14T08:00:00Z")
    real = jobs._update

    def refuse_a(config, job_id, **fields):
        if job_id == a["id"]:
            raise sqlite3.OperationalError("disk I/O error")
        return real(config, job_id, **fields)
    monkeypatch.setattr(jobs, "_update", refuse_a)
    said = []
    assert jobs.recover(cfg, said.append) == [b["id"]]
    assert jobs.get(cfg, b["id"])["state"] == "infra"
    assert any("job %d could not be recovered" % a["id"] in m for m in said), said


def test_a_job_dir_that_cannot_be_made_is_infra_with_the_reason(cfg, monkeypatch):
    """Read-only data dir (or disk full) for the job's own directory: the worker records INFRA, it does not crash."""
    monkeypatch.setenv("FAULT_MODE", "ok")
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    jobs_root = cfg.data_dir / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)

    real_mkdir = Path.mkdir

    def refuse(self, *a, **k):
        if self.parent == jobs_root:                           # only the job's own directory
            raise OSError(28, "No space left on device")
        return real_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", refuse)
    try:
        jobs.Worker(cfg.path, argv_prefix=CHILD).step()
    except OSError:
        pass                     # the closing log line cannot be written either; _loop logs it and carries on
    monkeypatch.undo()
    got = jobs.get(cfg, job["id"])
    assert got["state"] == "infra" and got["verdict"] == "INFRA", got
    assert "No space left on device" in got["reason"]


# ----- the job store itself is broken ------------------------------------------------------------------------------
def _corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not an sqlite database\n" * 64)
    for ext in ("-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)


def test_a_corrupt_jobs_store_refuses_a_submit_loudly(cfg):
    """Nothing is queued and nothing pretends to be: the caller gets the database error."""
    _corrupt(cfg.data_dir / "jobs.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")


def test_a_corrupt_jobs_store_does_not_stop_the_worker_from_starting(cfg):
    _corrupt(cfg.data_dir / "jobs.sqlite")
    w = jobs.Worker(cfg.path, argv_prefix=CHILD, poll_s=60)
    try:
        w.start()
    finally:
        w.stop(timeout=5)


# ----- a service restart during a job -------------------------------------------------------------------------------
def test_a_restart_with_a_recycled_pid_marks_infra_and_kills_nothing(cfg):
    """The job's recorded pid now belongs to an unrelated process (this test process): recover() must not signal
    it, and the job still becomes INFRA with the reason."""
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    jobs._update(cfg, job["id"], state="running", started_at="2026-10-14T08:00:00Z", pid=os.getpid())
    assert jobs.recover(cfg) == [job["id"]]                  # this process survives: we are still running
    got = jobs.get(cfg, job["id"])
    assert got["state"] == "infra" and got["verdict"] == "INFRA"
    assert "had already ended" in got["reason"]


def test_a_restart_marks_every_running_job_infra_even_with_no_pid(cfg):
    """The service died between claiming a job and starting its process (pid never recorded)."""
    a = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    b = jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, by="alice")
    for j in (a, b):
        jobs._update(cfg, j["id"], state="running", started_at="2026-10-14T08:00:00Z")
    assert sorted(jobs.recover(cfg)) == sorted([a["id"], b["id"]])
    assert {jobs.get(cfg, j["id"])["state"] for j in (a, b)} == {"infra"}
    assert jobs.recover(cfg) == []                            # idempotent: a second start finds nothing running


def test_an_unwritable_job_log_does_not_stop_recovery(cfg):
    a = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, by="alice")
    b = jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, by="alice")
    for j in (a, b):
        jobs._update(cfg, j["id"], state="running", started_at="2026-10-14T08:00:00Z")
    d = jobs.job_dir(cfg, a["id"])
    d.mkdir(parents=True)
    (d / "log.txt").write_text("")
    os.chmod(d / "log.txt", stat.S_IRUSR)
    try:
        if os.access(d / "log.txt", os.W_OK):
            pytest.skip("running as a user that ignores file modes")
        try:
            jobs.recover(cfg)
        finally:
            states = {jobs.get(cfg, j["id"])["state"] for j in (a, b)}
        assert states == {"infra"}, states
    finally:
        os.chmod(d / "log.txt", stat.S_IRUSR | stat.S_IWUSR)
