"""The executor's agent half (interfaces.md section 27.4), end to end against an in-process server: a hermetic fake
tier in a throwaway mirror, claimed over the real executor API (create_app behind its Guard), laid out, run in
bubblewrap (and in the fallback), judged, and reported. Plus the sandbox's own limits and the judge's rules."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from execfix import Rig, needs_bwrap
from nanosoc_rm.executor import agent as A
from nanosoc_rm.executor import queue as Q
from nanosoc_rm.executor import sandbox as S
from nanosoc_rm.executor.agentconfig import load_agent_config, read_token
from nanosoc_rm.config import ConfigError


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _run(rig, mode="bwrap", **run):
    """Queue one task, let the agent run it once against the in-process server; (stored task, posted result)."""
    tid = rig.enqueue(**run)
    res = A.run_once(rig.agent, rig.client(), mode=mode)
    assert res is not None and res["task_id"] == tid
    return Q.get(rig.config, tid), res


# --- end to end -----------------------------------------------------------------------------------------
@needs_bwrap
def test_e2e_pass_in_bwrap(rig):
    t, res = _run(rig, env={"MODE": "pass"})
    assert t["state"] == "done" and t["verdict"] == "PASS" and t["agent"] == "eda-1"
    assert res["sandbox"] == "bwrap" and res["exit_code"] == 0 and res["log"]["uploaded"]
    assert Q.log_path(rig.config, t["id"]).read_text().count("PASS 3") == 1
    assert not (rig.root / "scratch" / ("task-%d" % t["id"])).exists()      # work dir removed


@needs_bwrap
def test_e2e_exit_zero_with_a_forbidden_marker_is_fail(rig):
    t, res = _run(rig, env={"MODE": "skipped"}, verdict={"exit": 0, "forbid": ["SKIPPED"]})
    assert res["exit_code"] == 0 and t["verdict"] == "FAIL"
    assert [r["status"] for r in res["rows"]] == ["PASS", "FAIL"]
    assert "SKIPPED" in t["result"]["reason"]


@needs_bwrap
def test_e2e_nonzero_exit_is_fail(rig):
    t, _ = _run(rig, env={"MODE": "fail"})
    assert t["verdict"] == "FAIL" and t["result"]["exit_code"] == 1


@needs_bwrap
def test_e2e_control_that_fires_keeps_pass_and_one_that_does_not_is_unverified(rig):
    t, res = _run(rig, env={"MODE": "pass"}, control="exit 1")
    assert t["verdict"] == "PASS" and res["rows"][-1]["gate"] == "tier.t1.negctl" and res["rows"][-1]["status"] == "PASS"
    t, res = _run(rig, env={"MODE": "pass"}, control="exit 0")
    assert t["verdict"] == "UNVERIFIED" and res["rows"][-1]["status"] == "FAIL"


@needs_bwrap
def test_e2e_declared_verdicts_file(rig):
    t, res = _run(rig, env={"MODE": "verdicts"}, verdict={"verdicts": "out/verdicts.tsv"})
    assert t["verdict"] == "UNVERIFIED"                   # out/ does not exist: the file could not be written
    t, res = _run(rig, command="mkdir -p out && ./tier.sh", env={"MODE": "verdicts"},
                  verdict={"verdicts": "out/verdicts.tsv"})
    assert t["verdict"] == "PASS" and "tier.t1.lint-wrapper" in [r["gate"] for r in res["rows"]]


@needs_bwrap
def test_e2e_pins_are_laid_out_from_the_mirror(rig):
    task = rig.task(env={"MODE": "pin"})
    task["pins"] = [{"path": "sub", "mirror": "fixture-pin", "commit": rig.pin_commit}]
    tid = Q.enqueue(rig.config, task)
    res = A.run_once(rig.agent, rig.client(), mode="bwrap")
    assert res["verdict"] == "PASS" and res["checkout"]["laid_out"] == ["", "sub"]
    assert "pinned content" in Q.log_path(rig.config, tid).read_text()


def test_e2e_a_commit_not_in_the_mirror_is_infra_not_fail(rig):
    task = rig.task()
    task["commit"] = "0" * 40
    tid = Q.enqueue(rig.config, task)
    res = A.run_once(rig.agent, rig.client(), mode="fallback")
    t = Q.get(rig.config, tid)
    assert t["verdict"] == "INFRA" and "not in mirror" in res["reason"] and res["rows"][0]["status"] == "INFRA"


def test_e2e_a_budget_over_the_agents_limit_is_not_run(rig):
    res = _run(rig, mode="fallback", budget_s=172800)[1]
    assert res["verdict"] == "INFRA" and "max_budget_s" in res["reason"]


def test_e2e_a_missing_tool_tree_is_infra_not_fail(rig):
    rig.agent_path.write_text(rig.agent_path.read_text() + 'ro_binds = ["%s"]\n' % (rig.root / "no-such-eda"))
    res = _run(rig, mode="fallback")[1]
    assert res["verdict"] == "INFRA" and "tool tree(s) not on this host" in res["reason"]


def test_e2e_nothing_queued(rig):
    assert A.run_once(rig.agent, rig.client(), mode="fallback") is None


@needs_bwrap
def test_e2e_bwrap_hides_the_token_and_files_outside_the_binds(rig):
    secret = rig.root / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("do-not-read\n")
    t, res = _run(rig, env={"MODE": "probe", "PROBE": str(secret)})
    log = Q.log_path(rig.config, t["id"]).read_text()
    assert "do-not-read" not in log and "READ-OK" not in log
    t, res = _run(rig, env={"MODE": "probe", "PROBE": str(rig.token_file)})
    log = Q.log_path(rig.config, t["id"]).read_text()
    assert rig.token not in log and "READ-OK" not in log
    home = S.real_home()
    t, res = _run(rig, command="ls -a %s; echo DONE" % home)
    log = Q.log_path(rig.config, t["id"]).read_text()
    assert ".ssh" not in log and ".config" not in log and "DONE" in log


def test_e2e_fallback_reads_what_the_user_reads(rig):
    """The documented residual risk of the fallback: no filesystem isolation. This test pins the claim."""
    secret = rig.root / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("readable-in-fallback\n")
    t, res = _run(rig, mode="fallback", env={"MODE": "probe", "PROBE": str(secret)})
    assert res["sandbox"] == "fallback"
    assert "readable-in-fallback" in Q.log_path(rig.config, t["id"]).read_text()


@pytest.mark.parametrize("mode", ["fallback", pytest.param("bwrap", marks=needs_bwrap)])
def test_e2e_the_environment_is_built_from_nothing(rig, monkeypatch, mode):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("NANOSOC_RM_EXEC_TEST_SECRET", "env-leak")
    monkeypatch.setenv("ARM_IP_LIBRARY_PATH", "/vendor/arm-ip")
    t, res = _run(rig, mode=mode, env={"MODE": "env"})
    log = Q.log_path(rig.config, t["id"]).read_text()
    assert "env-leak" not in log and "SSH_AUTH_SOCK" not in log and "ARM_IP_LIBRARY_PATH" not in log
    assert rig.token not in log
    work = rig.root / "scratch" / ("task-%d" % t["id"])
    assert "HOME=%s/home\n" % work in log and "PATH=/usr/bin:/bin\n" in log
    assert "IdentityFile=/dev/null" in log and "NANOSOC_RM_NONCE=%s\n" % t["nonce"] in log


@needs_bwrap
def test_e2e_a_cancel_stops_the_run(rig):
    tid = rig.enqueue(env={"MODE": "sleep"}, budget_s=120)
    client = rig.client()
    hb = client.heartbeat

    calls = []

    def heartbeat(task_id, lease):
        calls.append(task_id)
        if len(calls) == 2:                             # the first beat is before the run, after the checkout
            Q.cancel(rig.config, task_id, "david")     # the operator cancels while it runs
        return hb(task_id, lease)
    client.heartbeat = heartbeat
    res = A.run_once(rig.agent, client, mode="bwrap")
    assert res["killed"] and res["verdict"] == "FAIL" and "cancelled by the service" in res["reason"]
    assert res["seconds"] < 30 and Q.get(rig.config, tid)["state"] == "done"


@needs_bwrap
def test_e2e_a_cancel_before_the_run_starts_is_not_run(rig):
    tid = rig.enqueue(env={"MODE": "pass"})
    client = rig.client()
    hb = client.heartbeat

    def heartbeat(task_id, lease):
        Q.cancel(rig.config, task_id, "david")
        return hb(task_id, lease)
    client.heartbeat = heartbeat
    res = A.run_once(rig.agent, client, mode="fallback")
    assert res["verdict"] == "INFRA" and "not run: cancelled by the service" in res["reason"]
    assert res["exit_code"] is None and not res["killed"] and Q.get(rig.config, tid)["state"] == "done"


def test_e2e_a_lost_lease_drops_the_result(rig):
    tid = rig.enqueue(env={"MODE": "sleep"}, budget_s=120)
    client = rig.client()

    def heartbeat(task_id, lease):
        Q.expire(rig.config, now=10 ** 12)              # the lease ran out (and attempts are not exhausted)
        Q.claim(rig.config, "eda-2", ["verilator"])      # another agent holds it now
        from nanosoc_rm.executor.client import ExecutorClient
        return ExecutorClient.heartbeat(client, task_id, lease)
    client.heartbeat = heartbeat
    assert A.run_once(rig.agent, client, mode="bwrap") is None
    t = Q.get(rig.config, tid)
    assert t["state"] == "claimed" and t["agent"] == "eda-2" and t["result"] is None


def test_run_loop_stops_on_a_revoked_token(rig):
    from nanosoc_rm.executor.client import ApiError
    rig.config_path.write_text(rig.config_path.read_text().replace('name = "eda-1"\n',
                                                                   'name = "eda-1"\nrevoked = true\n'))
    with pytest.raises(ApiError) as e:
        A.run_loop(rig.agent, rig.client(), mode="fallback", sleep=lambda s: None)
    assert e.value.status == 401


def test_run_loop_runs_until_stopped(rig):
    rig.enqueue(env={"MODE": "pass"})
    rig.enqueue(env={"MODE": "fail"})
    polls = []
    n = A.run_loop(rig.agent, rig.client(), mode="fallback", stop=lambda: len(polls) >= 1,
                   sleep=lambda s: polls.append(s))
    assert n == 2 and [t["verdict"] for t in Q.tasks(rig.config)] == ["FAIL", "PASS"]


# --- the sandbox ----------------------------------------------------------------------------------------
def test_bwrap_argv_mounts_no_home_and_binds_tools_read_only(rig):
    cfg = rig.agent
    cfg.ro_binds = [Path("/opt/eda")]
    work = rig.root / "scratch" / "task-1"
    argv = S.argv_for("bwrap", cfg, "make lint", work / "tree", work, work / "tree")
    assert argv[0] == S.BWRAP and "--unshare-all" in argv and "--die-with-parent" in argv
    assert "--new-session" in argv and "--share-net" in argv
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--ro-bind", "/opt/eda", "/opt/eda") in pairs
    assert ("--ro-bind", str(cfg.mirror_root), str(cfg.mirror_root)) in pairs
    assert ("--bind", str(work), str(work)) in pairs
    rw = [b for flag, b, _ in pairs if flag == "--bind"]
    assert rw == [str(work)]                                   # the only writable mount
    assert str(S.real_home()) not in argv and "/" not in [b for flag, b, _ in pairs if flag.endswith("bind")]
    cfg.network = False
    assert "--share-net" not in S.argv_for("bwrap", cfg, "x", work, work, work)


def test_exposure_refuses_a_bind_that_holds_a_protected_path(rig, tmp_path):
    cfg = rig.agent
    assert S.exposure(cfg, rig.root / "scratch" / "t") == []
    cfg.ro_binds = [rig.token_file.parent]
    assert any("token" in p for p in S.exposure(cfg, rig.root / "scratch" / "t"))
    cfg.ro_binds = [Path("/users")]
    assert S.exposure(cfg, rig.root / "scratch" / "t", home=Path("/users/someone"))
    cfg.ro_binds = []
    assert S.exposure(cfg, Path("/users/someone/scratch/t"), home=Path("/users/someone")) == []


def test_exposure_stops_the_task_before_it_runs(rig):
    cfg = rig.agent
    cfg.ro_binds = [rig.root / "agent"]
    tid = rig.enqueue()
    claim = Q.claim(rig.config, "eda-1", ["verilator"])
    res, log = A.execute(cfg, claim, "bwrap")
    assert res["verdict"] == "INFRA" and "visible inside the sandbox" in res["reason"] and log is None
    assert tid == claim["task_id"]


def test_mode_for(rig):
    cfg = rig.agent
    cfg.mode = "auto"
    assert S.mode_for(cfg, probe=lambda: True) == "bwrap"
    assert S.mode_for(cfg, probe=lambda: False) == "fallback"
    cfg.mode = "bwrap"
    with pytest.raises(ConfigError):
        S.mode_for(cfg, probe=lambda: False)
    cfg.mode = "fallback"
    assert S.mode_for(cfg, probe=lambda: True) == "fallback"


def _limits(**kw):
    base = dict(kill_after_s=60, max_log_bytes=1_000_000, max_file_bytes=10 ** 9)
    base.update(kw)
    return S.Limits(**base)


def test_the_time_limit_kills_the_whole_group(tmp_path):
    marker = tmp_path / "child-survived"
    out = S.run(["/bin/bash", "-c", "(sleep 3; touch %s) & sleep 30" % marker], {"PATH": "/usr/bin:/bin"}, tmp_path,
                tmp_path / "log", _limits(kill_after_s=0.5))
    assert out.killed and out.returncode is None and "past the 0.5 s limit" in out.stop_reason
    import time
    time.sleep(3.5)
    assert not marker.exists()                       # the background child died with the group


def test_the_output_limit_kills_the_run_and_keeps_the_cap(tmp_path):
    out = S.run(["/bin/bash", "-c", "yes x | head -c 5000000; sleep 5"], {"PATH": "/usr/bin:/bin"}, tmp_path,
                tmp_path / "log", _limits(max_log_bytes=10_000))
    assert out.killed and out.truncated and "10000-byte limit" in out.stop_reason
    assert (tmp_path / "log").stat().st_size == 10_000 and out.seconds < 5


def test_a_tick_answer_stops_the_run(tmp_path):
    out = S.run(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, tmp_path, tmp_path / "log", _limits(),
                tick=lambda: "stop now", tick_every_s=0.1)
    assert out.killed and out.stop_reason == "stop now" and out.seconds < 15


def test_the_command_runs_with_umask_077_and_no_core(tmp_path):
    out = S.run(["/bin/bash", "-c", "umask; ulimit -c"], {"PATH": "/usr/bin:/bin"}, tmp_path, tmp_path / "log",
                _limits())
    assert out.returncode == 0 and (tmp_path / "log").read_text().split() == ["0077", "0"]


def test_a_command_that_cannot_start(tmp_path):
    out = S.run(["/nonexistent/tool"], {"PATH": "/usr/bin:/bin"}, tmp_path, tmp_path / "log", _limits())
    assert not out.started
    v, reason, rows = A.judge({}, "t1", "a" * 32, out, tmp_path / "log", tmp_path)
    assert v == "FAIL" and "did not start" in reason


# --- the judge ------------------------------------------------------------------------------------------
def _out(rc=0, **kw):
    return S.Outcome(returncode=rc, seconds=1.0, **kw)


def test_judge_rules(tmp_path):
    log = tmp_path / "log"
    log.write_text("PASS 3\nWRAPPER: SKIPPED\n")
    n = "b" * 32
    assert A.judge({}, "t1", n, _out(0), log, tmp_path)[0] == "PASS"
    assert A.judge({"verdict": {"exit": 2}}, "t1", n, _out(0), log, tmp_path)[0] == "FAIL"
    assert A.judge({"verdict": {"exit": 2}}, "t1", n, _out(2), log, tmp_path)[0] == "PASS"
    v, reason, rows = A.judge({"verdict": {"forbid": ["SKIPPED"]}}, "t1", n, _out(0), log, tmp_path)
    assert v == "FAIL" and rows[1]["gate"] == "tier.t1.forbid" and all(r["nonce"] == n for r in rows)
    assert A.judge({"verdict": {"forbid": ["NOPE"]}}, "t1", n, _out(0), log, tmp_path)[0] == "PASS"
    assert A.judge({}, "t1", n, _out(None, killed=True, stop_reason="x"), log, tmp_path)[0] == "FAIL"
    assert A.judge({}, "t1", n, _out(0), log, tmp_path, control=_out(0))[0] == "UNVERIFIED"
    assert A.judge({}, "t1", n, _out(0), log, tmp_path, control=_out(1))[0] == "PASS"
    assert A.judge({}, "t1", n, _out(1), log, tmp_path, control=_out(0))[0] == "FAIL"


def test_upload_keeps_head_and_tail(tmp_path):
    log = tmp_path / "log"
    log.write_bytes(b"H" * 1000 + b"M" * 10000 + b"T" * 1000)
    data = A.upload_bytes(log, 2000)
    assert len(data) <= 2000 and data.startswith(b"H" * 100) and data.endswith(b"T" * 100) and b"cut" in data
    log.write_bytes(b"small")
    assert A.upload_bytes(log, 2000) == b"small"


# --- the agent config -----------------------------------------------------------------------------------
def test_the_token_file_must_be_private(rig):
    cfg = rig.agent
    assert read_token(cfg) == rig.token
    os.chmod(rig.token_file, 0o640)
    with pytest.raises(ConfigError, match="0600"):
        read_token(cfg)


@pytest.mark.parametrize("line,want", [
    ('server = "http://rm.example.invalid"', "https://"),
    ('server = "https://user:pw@rm.example.invalid"', "no user or token"),
    ('name = "EDA 1"', "name must be"),
])
def test_agent_config_is_checked(rig, line, want):
    text = rig.agent_path.read_text()
    key = line.split(" = ")[0]
    text = "\n".join(line if ln.startswith(key + " = ") else ln for ln in text.splitlines())
    rig.agent_path.write_text(text)
    with pytest.raises(ConfigError, match=want):
        load_agent_config(rig.agent_path)


def test_check_command(rig, monkeypatch):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, ["executor", "check", "--agent-config", str(rig.agent_path)], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert ("sandbox bwrap" if S.bwrap_usable() else "sandbox fallback") in r.output
    assert rig.token not in r.output


# --- tier jobs of the local job queue (section 27.6), against a fake of the capabilities unit's seam -------
class FakeJobs:
    """jobs.claim_for_executor / finish_for_executor / get, with the capabilities unit's signatures."""

    def __init__(self, argv):
        self.queued = [{"id": 5, "kind": "fake-kind", "params": {}}]
        self.running, self.finished, self.cancel_by = {}, [], None
        self.argv = argv

    def claim_for_executor(self, config, executor, accepts=None):
        if not self.queued:
            return None
        job = self.queued.pop(0)
        self.running[job["id"]] = executor
        return dict(job, state="running", executor=executor)

    def finish_for_executor(self, config, job_id, executor, rc, stdout, log=""):
        assert self.running.get(job_id) == executor
        del self.running[job_id]
        self.finished.append({"id": job_id, "executor": executor, "rc": rc, "stdout": stdout, "log": log})
        return {"id": job_id, "state": "done" if rc is not None else "infra", "verdict": "PASS" if rc == 0 else None}

    def get(self, config, job_id):
        return {"id": job_id, "cancel_by": self.cancel_by}

    def cli_args(self, kind, params, work=None):
        return list(self.argv)


@pytest.fixture
def fake_jobs(monkeypatch):
    from nanosoc_rm import jobs
    fake = FakeJobs(["--version"])
    for name in ("claim_for_executor", "finish_for_executor", "get", "cli_args"):
        monkeypatch.setattr(jobs, name, getattr(fake, name), raising=False)
    return fake


def _job_rig(rig, labels=('verilator', 'jobs')):
    rmdir = rig.root / "rmcfg"
    rmdir.mkdir()
    (rmdir / "rm.toml").write_text('[service]\ndata_dir = "%s"\n' % (rig.root / "agentdata"))
    (rig.root / "agentdata").mkdir()
    rig.config_path.write_text(rig.config_path.read_text().replace(
        'labels = ["verilator", "vcs"]', 'labels = %s' % json.dumps(list(labels)), 1))
    rig.agent_path.write_text(rig.agent_path.read_text().replace(
        'labels = ["verilator", "vcs"]', 'labels = ["verilator", "jobs"]\nrm_config = "%s"' % (rmdir / "rm.toml")))
    return rig


@pytest.mark.parametrize("mode", ["fallback", pytest.param("bwrap", marks=needs_bwrap)])
def test_e2e_a_tier_job_runs_nanosoc_rm_and_reports_to_the_job_queue(rig, fake_jobs, mode):
    _job_rig(rig)
    res = A.run_once(rig.agent, rig.client(), mode=mode)
    assert res == {"job_id": 5, "exit_code": 0, "state": "done", "verdict": "PASS"}
    (done,) = fake_jobs.finished
    assert done["executor"] == "eda-1" and done["rc"] == 0 and "nanosoc-rm, version" in done["stdout"]
    assert rig.token not in done["stdout"] + done["log"]


@pytest.mark.parametrize("mode", ["fallback", "bwrap"])
def test_a_bwrap_job_asks_for_a_pid_namespace_per_tier_command(rig, fake_jobs, monkeypatch, mode):
    """Integration review 2026-10-16: in a bwrap job each tier command runs in its own pid namespace
    (tierrun.pidns_wrap), so it cannot forge the job's stdout through /proc; the agent asks for it."""
    from nanosoc_rm.analysis.tierrun import PIDNS_ENV
    _job_rig(rig)
    seen = {}

    def fake_run(argv, env, cwd, log, limits, **kw):
        seen.update(env)
        return S.Outcome(0, 0.1)
    monkeypatch.setattr(A.sandbox, "run", fake_run)
    monkeypatch.setattr(A.sandbox, "exposure", lambda *a, **k: [])
    A.run_once(rig.agent, rig.client(), mode=mode)
    assert seen, "the job did not reach the sandbox"
    assert seen.get(PIDNS_ENV) == (S.BWRAP if mode == "bwrap" else None)


def test_a_job_needs_the_jobs_label_granted(rig, fake_jobs):
    _job_rig(rig, labels=("verilator",))                              # the agent asks for jobs; eda-1 is not granted it
    assert "jobs" in A.asked_labels(rig.agent)
    assert A.run_once(rig.agent, rig.client(), mode="fallback") is None
    assert fake_jobs.queued and not fake_jobs.finished


def test_a_kind_scoped_job_label_claims_only_that_kind(rig):
    """Integration review 2026-10-16: an agent granted `jobs` claimed every tier job; `jobs:<kind>` grants one."""
    from nanosoc_rm import jobs
    from nanosoc_rm.executor import jobbridge as JB
    jobs._load_kinds()
    tb = jobs.submit(rig.config, "test-bump", {"configuration": "eth-main", "path": "tidelink", "to": "v1.0"}, "ann")
    cell = jobs.submit(rig.config, "compat", {"configuration": "eth-main", "tier": "t1"}, "ann")
    assert jobs.needs_executor("compat", cell["params"])
    assert JB.kinds_granted(["jobs"]) is None and JB.kinds_granted(["jobs:compat", "vcs"]) == {"compat"}
    assert JB.kinds_granted(["vcs", "jobs:"]) == set()
    assert JB.claim(rig.config, "eda-1", ["vcs"]) is None
    got = JB.claim(rig.config, "eda-1", ["jobs:compat"])
    assert got["job_id"] == cell["id"]                         # the older test-bump is not its kind: left queued
    assert jobs.get(rig.config, tb["id"])["state"] == "queued"
    assert JB.claim(rig.config, "eda-1", ["jobs"])["job_id"] == tb["id"]


def test_an_agent_without_rm_config_does_not_ask_for_jobs(rig):
    cfg = rig.agent
    cfg.labels = ["verilator", "jobs", "jobs:compat"]
    assert A.asked_labels(cfg) == ["verilator"]
    cfg.rm_config = Path("/x/rm.toml")
    assert A.asked_labels(cfg) == ["verilator", "jobs", "jobs:compat"]


def test_job_leases_are_enforced_and_expire_to_infra(rig, fake_jobs):
    from nanosoc_rm.executor import jobbridge as JB
    _job_rig(rig)
    c = JB.claim(rig.config, "eda-1", ["jobs"], now=1000)
    assert c["type"] == "job" and c["argv"] == ["--version"]
    with pytest.raises(Q.LeaseLost):
        JB.heartbeat(rig.config, 5, "eda-2", c["lease"], now=1001)
    with pytest.raises(Q.LeaseLost):
        JB.finish(rig.config, 5, "eda-1", "e" * 32, 0, "", "")
    fake_jobs.cancel_by = "david"
    assert JB.heartbeat(rig.config, 5, "eda-1", c["lease"], now=1001)["cancel"] is True
    assert JB.expire(rig.config, now=5000) == [5]
    assert fake_jobs.finished[0]["rc"] is None and "lease expired" in fake_jobs.finished[0]["log"]
    with pytest.raises(Q.LeaseLost):
        JB.finish(rig.config, 5, "eda-1", c["lease"], 0, "", "")


def test_a_claimed_job_whose_lease_was_never_written_ends_infra(rig):
    """Integration review 2026-10-16: the claim marks the job running (jobs.sqlite), then writes its lease
    (executor.sqlite) in a second transaction. A service that dies between the two left the job running on an
    executor with no lease: recover() skips it and no heartbeat can end it. expire() now ends it INFRA once it is
    older than a lease, and leaves a job that holds a lease, or a younger one, alone."""
    from nanosoc_rm import clock, jobs
    from nanosoc_rm.executor import jobbridge as JB
    jobs._load_kinds()
    a = jobs.submit(rig.config, "test-bump", {"configuration": "eth-main", "path": "tidelink", "to": "v1.0"}, "ann")
    b = jobs.submit(rig.config, "test-bump", {"configuration": "eth-main", "path": "tidechart", "to": "v1.0"}, "ann")
    assert jobs.claim_for_executor(rig.config, "eda-1")["id"] == a["id"]        # the crash: no lease row written
    t0 = clock.parse(jobs.get(rig.config, a["id"])["started_at"]).timestamp()
    lease_s = Q.settings(rig.config)["lease_seconds"]
    held = JB.claim(rig.config, "eda-1", ["jobs"], now=t0 + lease_s)            # b: claimed later, with its lease
    assert held["job_id"] == b["id"]
    assert JB.expire(rig.config, now=t0 + lease_s - 5) == []                    # younger than a lease: left alone
    assert JB.expire(rig.config, now=t0 + lease_s + 5) == [a["id"]]
    done = jobs.get(rig.config, a["id"])
    assert done["state"] == "infra" and done["executor"] == "eda-1"
    assert jobs.get(rig.config, b["id"])["state"] == "running"                  # its lease is current


def test_job_api_over_http(rig, fake_jobs):
    _job_rig(rig)
    h = rig.http()
    auth = {"Authorization": "Bearer " + rig.token}
    c = h.post("/executor/v1/claim", json={"labels": ["jobs"]}, headers=auth).json()
    assert c["type"] == "job"
    assert h.post("/executor/v1/jobs/5/log", json={"lease": c["lease"]}, headers=auth).status_code == 404
    r = h.post("/executor/v1/jobs/5/result", json={"lease": c["lease"], "exit_code": "0"}, headers=auth)
    assert r.status_code == 422
    r = h.post("/executor/v1/jobs/5/result", json={"lease": c["lease"], "exit_code": 0, "stdout": "{}\n", "log": "ok"},
               headers=auth)
    assert r.status_code == 200 and r.json() == {"state": "done", "verdict": "PASS"}


def test_a_job_whose_config_sits_beside_the_token_is_not_run(rig, fake_jobs):
    _job_rig(rig)
    (rig.root / "agent" / "rm.toml").write_text('[service]\ndata_dir = "%s"\n' % (rig.root / "agentdata"))
    rig.agent_path.write_text(rig.agent_path.read_text().replace(str(rig.root / "rmcfg" / "rm.toml"),
                                                                 str(rig.root / "agent" / "rm.toml")))
    res = A.run_once(rig.agent, rig.client(), mode="bwrap")
    assert res["exit_code"] is None and "visible inside the sandbox" in fake_jobs.finished[0]["log"]


# --- the shipped templates ------------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[3]


def test_the_example_agent_config_loads_and_names_no_lab_host():
    cfg = load_agent_config(REPO / "scripts" / "executor" / "agent.example.toml")
    assert cfg.mode == "bwrap" and cfg.server.startswith("https://") and cfg.rm_config is None  # never a silent fallback
    text = (REPO / "scripts" / "executor" / "agent.example.toml").read_text()
    assert "rmx_" not in text and "<host>" in text


def test_the_unit_template_runs_the_loop_with_the_agent_config_only():
    text = (REPO / "scripts" / "executor" / "nanosoc-rm-executor.service").read_text()
    assert "ExecStart=%h/nanosoc-rm/venv/bin/nanosoc-rm executor run --loop" in text
    envs = [ln for ln in text.splitlines() if ln.startswith("Environment=")]
    assert envs == ["Environment=NANOSOC_RM_AGENT_CONFIG=%h/.config/nanosoc-rm-executor/agent.toml"]
    assert "NoNewPrivileges=yes" in text and "UMask=0077" in text


# --- the agent reads run output unsandboxed: a command may not point it outside the sandbox (review g1) -------
@needs_bwrap
def test_a_verdicts_file_linked_out_of_the_tree_is_not_read(rig):
    tid = rig.enqueue(command='ln -s %s "$NANOSOC_RM_VERDICTS"' % rig.token_file, verdict={"verdicts": "v.tsv"})
    res = A.run_once(rig.agent, rig.client(), mode="bwrap")
    assert rig.token not in json.dumps(Q.get(rig.config, tid))
    assert res["verdict"] == "UNVERIFIED" and "link out of the tree" in res["reason"]


@needs_bwrap
def test_a_command_cannot_swap_its_log_for_a_link_or_hide_a_forbidden_marker(rig):
    swap = 'for f in "$TREE"/../*.log; do rm -f "$f"; ln -s %s "$f"; done; ' % rig.token_file
    tid = rig.enqueue(command=swap + "echo WRAPPER: SKIPPED", verdict={"exit": 0, "forbid": ["SKIPPED"]})
    res = A.run_once(rig.agent, rig.client(), mode="bwrap")
    assert rig.token not in json.dumps(Q.get(rig.config, tid)) + Q.log_path(rig.config, tid).read_text()
    assert res["verdict"] == "FAIL"
    assert not (rig.root / "scratch" / ("task-%d.logs" % tid)).exists()
