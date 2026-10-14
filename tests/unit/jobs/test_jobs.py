"""The local job runner (CR-17; interfaces.md section 13a): queue, one worker, results, cancel, restart, ceiling.

The worker runs `nanosoc-rm` as a child process; here `argv_prefix` points it at fake_cli.py (the test seam),
except in the one test that runs the real CLI."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nanosoc_rm import db, jobs
from nanosoc_rm.config import load_config

FAKE = [sys.executable, str(Path(__file__).with_name("fake_cli.py"))]
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                            # a zombie is dead for our purposes
        return Path("/proc/%d/stat" % pid).read_text().split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def _wait(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


# ----- requests ------------------------------------------------------------------------------------------------
def test_submit_checks_parameters_as_the_cli_would(cfg):
    with pytest.raises(jobs.JobUsage, match="40-hex"):
        jobs.submit(cfg, "compat", {"configuration": "eth-main", "pins": ["a/b=123"]}, "alice")
    with pytest.raises(jobs.JobUsage, match="needs a tier"):
        jobs.submit(cfg, "compat", {"configuration": "eth-main", "force_tier": True}, "alice")
    with pytest.raises(jobs.JobUsage, match="not a configuration id"):
        jobs.submit(cfg, "compat", {"configuration": "../etc"}, "alice")
    with pytest.raises(jobs.JobUsage, match="unknown job kind"):
        jobs.submit(cfg, "push", {}, "alice")
    assert jobs.jobs(cfg) == []


def test_a_real_run_is_stored_and_a_what_if_never_is():
    real = jobs.cli_args("compat", {"configuration": "eth-main", "pins": [], "tier": "t1", "force_tier": True})
    assert real == ["compat", "eth-main", "--store", "--tier", "t1", "--force-tier", "--json"]
    what_if = jobs.cli_args("compat", {"configuration": "eth-main", "pins": ["x=%s" % SHA], "tier": None,
                                       "force_tier": False}, Path("/w"))
    assert "--store" not in what_if and what_if[-3:] == ["--keep-work", "/w", "--json"]
    assert jobs.command_text("mirror-sync", {"component": None}) == "nanosoc-rm mirror sync"


def test_a_compat_job_logs_run_t0_with_the_login(cfg):
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main"}, "alice")
    assert job["state"] == "queued" and job["command"] == "nanosoc-rm compat eth-main --store"
    assert [(a["login"], a["action"], a["target"]) for a in db.actions(config=cfg)] == [("alice", "run_t0", "eth-main")]


# ----- running -------------------------------------------------------------------------------------------------
def test_step_runs_the_oldest_job_and_records_the_result(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    a = jobs.submit(cfg, "compat", {"configuration": "eth-rc4"}, "alice")
    b = jobs.submit(cfg, "compat", {"configuration": "eth-main"}, "bob")
    w = jobs.Worker(cfg.path, argv_prefix=FAKE)
    assert jobs.queue_position(cfg, b["id"]) == 2
    assert w.step() == a["id"]
    done = jobs.get(cfg, a["id"])
    assert done["state"] == "done" and done["exit_code"] == 0
    assert done["verdict"] == "FAIL"                             # the example what-if's verdict
    assert done["result"]["kind"] == "compat_result"
    log = jobs.log_text(cfg, a["id"])
    assert "$ nanosoc-rm compat eth-rc4 --store" in log and "fake nanosoc-rm compat eth-rc4 --store" in log
    assert "--keep-work" not in log                              # T0 only: the CLI's own temporary work dir
    assert jobs.get(cfg, b["id"])["state"] == "queued" and jobs.queue_position(cfg, b["id"]) == 1
    assert w.step() == b["id"] and w.step() is None


def test_a_tier_log_streams_from_the_work_dir_and_is_kept_when_it_is_removed(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main", "tier": "t1"}, "alice")
    jobs.Worker(cfg.path, argv_prefix=FAKE).step()
    d = jobs.job_dir(cfg, job["id"])
    assert "--keep-work %s" % (d / "work") in jobs.log_text(cfg, job["id"])
    assert not (d / "work").exists()                              # a tier checkout is not kept
    assert "--- tier-t1.log (tier command output) ---\ntier output line" in jobs.log_text(cfg, job["id"])


@pytest.mark.parametrize("mode, state, verdict", [("usage", "failed", None), ("crash", "infra", "INFRA"),
                                                  ("docs", "done", "UNVERIFIED")])
def test_exit_codes_map_to_states(cfg, monkeypatch, mode, state, verdict):
    monkeypatch.setenv("FAKE_MODE", mode)
    kind, params = ("mirror-sync", {}) if mode == "docs" else ("walk", {"configuration": "nope"})
    job = jobs.submit(cfg, kind, params, "alice")
    jobs.Worker(cfg.path, argv_prefix=FAKE).step()
    got = jobs.get(cfg, job["id"])
    assert (got["state"], got["verdict"]) == (state, verdict), got
    if mode == "usage":
        assert "no registered configuration" in got["reason"]
    if mode == "crash":
        assert "exit 9" in got["reason"]
    if mode == "docs":
        assert got["result"] == {"documents": 2, "kinds": {"mirror_status": 2}, "failed": ["mirror_status:github:x/False"]}


def test_the_real_cli_runs_as_the_child(cfg):
    # No registry configured: the real `nanosoc-rm walk` exits 3 (config error); the job says why.
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    jobs.Worker(cfg.path).step()
    got = jobs.get(cfg, job["id"])
    assert got["state"] == "failed" and got["exit_code"] == 3, got
    assert "registry" in got["reason"]


def test_cancel_of_a_queued_job_never_starts_it(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    assert jobs.cancel(cfg, job["id"], "bob")["state"] == "cancelled"
    assert jobs.Worker(cfg.path, argv_prefix=FAKE).step() is None
    assert jobs.get(cfg, job["id"])["reason"] == "cancelled by bob before it started"


def test_cancel_of_a_running_job_stops_its_whole_process_tree(cfg, monkeypatch, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main", "tier": "t2"}, "alice")
    w = jobs.Worker(cfg.path, argv_prefix=FAKE, poll_s=0.1).start()
    try:
        assert _wait(lambda: pidfile.exists() and pidfile.read_text().strip()), "the fake job never started"
        grandchild = int(pidfile.read_text())
        child = jobs.get(cfg, job["id"])["pid"]
        assert _alive(grandchild) and os.getpgid(grandchild) != os.getpgid(child)   # its own group, like a tier
        assert _wait(lambda: "working..." in jobs.log_text(cfg, job["id"]))         # the log streams while it runs
        jobs.cancel(cfg, job["id"], "alice")
        assert _wait(lambda: jobs.get(cfg, job["id"])["state"] == "cancelled"), jobs.get(cfg, job["id"])
        assert _wait(lambda: not _alive(grandchild) and not _alive(child), 30), "a job process outlived the cancel"
        assert jobs.get(cfg, job["id"])["reason"].startswith("cancelled by alice after")
    finally:
        w.stop()
        if pidfile.exists() and _alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_the_ceiling_stops_a_job_as_infra(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(tmp_path / "gc.pid"))
    monkeypatch.setattr(jobs, "JOB_LIMIT_S", 1)
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    jobs.Worker(cfg.path, argv_prefix=FAKE).step()
    got = jobs.get(cfg, job["id"])
    assert got["state"] == "infra" and "ceiling of 1 s" in got["reason"]
    gc = int((tmp_path / "gc.pid").read_text())
    assert _wait(lambda: not _alive(gc), 10)


# ----- restart -------------------------------------------------------------------------------------------------
def test_a_restart_marks_a_running_job_infra_and_stops_its_process(cfg, monkeypatch):
    dead = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    live = jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, "alice")
    queued = jobs.submit(cfg, "walk", {"configuration": "multicore-master"}, "alice")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True,
                            env=dict(os.environ, NANOSOC_RM_JOB=str(live["id"])))
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        jobs._update(cfg, dead["id"], state="running", pid=gone.pid, started_at="2026-10-12T00:00:00Z")
        jobs._update(cfg, live["id"], state="running", pid=proc.pid, started_at="2026-10-12T00:00:00Z")
        w = jobs.Worker(cfg.path, argv_prefix=FAKE, poll_s=60)
        monkeypatch.setattr(w, "_loop", lambda: None)            # recover only; run nothing
        w.start()
        w.stop()
        for job, phrase in ((dead, "had already ended"), (live, "pid %d) was stopped" % proc.pid)):
            got = jobs.get(cfg, job["id"])
            assert (got["state"], got["verdict"]) == ("infra", "INFRA"), got
            assert "the service restarted while this job ran" in got["reason"] and phrase in got["reason"], got
            assert "[worker] the service restarted" in jobs.log_text(cfg, job["id"])
        assert proc.wait(timeout=10) is not None
        assert jobs.get(cfg, queued["id"])["state"] == "queued"       # a queued job runs after the restart
        # a pid that is not the job's (no NANOSOC_RM_JOB marker) is never signalled
        jobs._update(cfg, queued["id"], state="running", pid=stranger.pid)
        jobs.recover(cfg)
        assert stranger.poll() is None
    finally:
        for p in (proc, stranger):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_one_worker_per_data_dir(cfg):
    a, b = jobs.Worker(cfg.path), jobs.Worker(cfg.path)
    try:
        assert a.acquire(cfg) and not b.acquire(cfg)
    finally:
        a.release()
        b.release()


def test_the_scheduler_leaves_out_the_ci_jobs_without_a_ci_project(cfg, monkeypatch, tmp_path):
    import datetime as dt
    from nanosoc_rm import schedule
    ran = []
    fake = tuple(schedule.Job(j.name, j.every, lambda c, n=j.name: ran.append(n)) for j in schedule.JOBS)
    monkeypatch.setattr(schedule, "JOBS", fake)
    now = dt.datetime(2026, 10, 12, 3, 0, tzinfo=dt.timezone.utc)
    # CR-WF-fresh: freshness_scan is not a CI job, so it runs without a CI project too
    assert schedule.tick(now, config=cfg) == ["poll", "freshness_scan", "inventory", "credentials"]
    with_ci = tmp_path / "ci.toml"
    with_ci.write_text('[service]\ndata_dir = "%s"\n[ci]\nproject = "soclabs/nanosoc-compat-ci"\n' % (tmp_path / "d2"))
    assert schedule.tick(now, config=load_config(with_ci)) == [j.name for j in schedule.JOBS]


def test_stopping_the_worker_stops_its_job_as_infra(cfg, monkeypatch, tmp_path):
    pidfile = tmp_path / "gc.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    job = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    w = jobs.Worker(cfg.path, argv_prefix=FAKE, poll_s=0.1).start()
    try:
        assert _wait(lambda: pidfile.exists() and pidfile.read_text().strip())
        w.stop()
        got = jobs.get(cfg, job["id"])
        assert (got["state"], got["reason"]) == ("infra", "the service stopped while this job ran. Run it again.")
        assert _wait(lambda: not _alive(int(pidfile.read_text())), 10)
    finally:
        w.stop()


# ----- the job's verdict (the pill and the job lists read job.verdict) -------------------------------------------
@pytest.mark.parametrize("mode, kind, params, verdict, reason", [
    ("tierfail", "compat", {"configuration": "eth-rc4", "tier": "t1"}, "FAIL", None),   # T0 PASS, tier FAIL
    ("silent", "compat", {"configuration": "eth-rc4"}, "UNVERIFIED", "fake nanosoc-rm compat"),
    ("fail", "walk", {"configuration": "eth-main"}, "FAIL", "nanosoc-rm: 2 steps failed"),
    ("fail", "mirror-sync", {}, "FAIL", "nanosoc-rm: 2 steps failed"),
    ("ok", "walk", {"configuration": "eth-main"}, "PASS", None),
])
def test_the_job_verdict_follows_the_run_not_t0_alone(cfg, monkeypatch, mode, kind, params, verdict, reason):
    monkeypatch.setenv("FAKE_MODE", mode)
    job = jobs.submit(cfg, kind, params, "alice")
    jobs.Worker(cfg.path, argv_prefix=FAKE).step()
    got = jobs.get(cfg, job["id"])
    assert (got["state"], got["verdict"]) == ("done", verdict), got
    if reason:
        assert reason in got["reason"], got
    if mode == "tierfail":
        assert got["result"]["fact"]["verdict"] == "PASS" and got["exit_code"] == 1


def test_a_tier_job_ceiling_includes_the_tier_budget(cfg, monkeypatch):
    from nanosoc_rm.ci import configs
    monkeypatch.setattr(configs, "tiers", lambda cid, config: {"t2": {"budget_s": 900}})
    t0 = {"kind": "compat", "params": {"configuration": "eth-main", "tier": None}}
    t2 = {"kind": "compat", "params": {"configuration": "eth-main", "tier": "t2"}}
    assert jobs._limit_s(cfg, t0) == jobs.JOB_LIMIT_S
    assert jobs._limit_s(cfg, t2) == jobs.JOB_LIMIT_S + 1350           # killed by tierrun at budget x 1.5 first

    def unreadable(cid, config):
        raise ValueError("no tiers.yaml")
    monkeypatch.setattr(configs, "tiers", unreadable)
    assert jobs._limit_s(cfg, t2) == jobs.JOB_LIMIT_S + 4 * 3600
