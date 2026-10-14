"""Coverage angle (rob/coverage): nanosoc_rm.jobs branches the worker tests do not reach.

The outcome table for every exit code, the log tail, queue positions, cancel on each state, recovery of a job
whose process is gone, the kind registry's refusals and the ceiling for a tier whose tiers.yaml cannot be read.
No child process is started except where a test says so."""
from __future__ import annotations

import json

import pytest

from nanosoc_rm import jobs
from nanosoc_rm.config import load_config

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def _doc(kind, **fact):
    return json.dumps({"kind": kind, "subject": "s", "fact": fact})


# ----- outcome table -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("rc,state,verdict,words", [
    (3, "failed", None, "unknown configuration"),
    (None, "infra", "INFRA", "exit None"),
    (-9, "infra", "INFRA", "signal 9"),
    (137, "infra", "INFRA", "exit 137"),
])
def test_outcome_of_a_usage_error_or_a_crash(rc, state, verdict, words):
    out = jobs._outcome({"kind": "walk", "params": {}}, rc, "", "noise\nunknown configuration x\n")
    assert (out["state"], out["verdict"]) == (state, verdict) and words in out["reason"]
    assert "result" not in out


def test_outcome_of_exit_3_without_a_log():
    assert jobs._outcome({"kind": "walk", "params": {}}, 3, "", "")["reason"] == "usage error (exit 3)"


def test_outcome_compat_reads_the_last_compat_result_and_prefers_run_verdict():
    job = {"kind": "compat", "params": {}}
    out = "\n".join(["not json", "{broken", _doc("compat_result", verdict="FAIL"),
                     _doc("pin_state"), _doc("compat_result", verdict="PASS", run_verdict="WARN", reason="r")])
    o = jobs._outcome(job, 0, out, "")
    assert (o["state"], o["verdict"], o["reason"]) == ("done", "WARN", "r") and o["result"]["fact"]["run_verdict"]
    o = jobs._outcome(job, 1, _doc("compat_result", verdict="FAIL"), "")
    assert o["verdict"] == "FAIL" and o["reason"] is None


def test_outcome_compat_without_a_result_is_unverified_not_pass():
    o = jobs._outcome({"kind": "compat", "params": {}}, 0, _doc("pin_state"), "last words\n")
    assert (o["state"], o["verdict"], o["reason"]) == ("done", "UNVERIFIED", "last words")
    o = jobs._outcome({"kind": "compat", "params": {}}, 0, "", "")
    assert o["reason"] == "compat printed no result"


@pytest.mark.parametrize("rc,verdict", [(0, "PASS"), (1, "FAIL"), (2, "UNVERIFIED")])
def test_outcome_of_a_builtin_kind_by_exit_code(rc, verdict):
    stdout = "\n".join([_doc("mirror_status", ok=True), json.dumps({"kind": "mirror_status", "subject": "b",
                                                                     "fact": {"ok": False}}), _doc("alert")])
    o = jobs._outcome({"kind": "mirror-sync", "params": {}}, rc, stdout, "tail line\n")
    assert o["verdict"] == verdict and o["result"]["documents"] == 3
    assert o["result"]["failed"] == ["b"] and o["result"]["kinds"] == {"mirror_status": 2, "alert": 1}
    assert o["reason"] == (None if rc == 0 else "tail line")
    w = jobs._outcome({"kind": "walk", "params": {}}, rc, "{}\n", "")
    assert w["result"] == {"documents": 1, "kinds": {"?": 1}} and "failed" not in w["result"]


def test_outcome_reason_is_the_last_log_line_capped():
    o = jobs._outcome({"kind": "walk", "params": {}}, 1, "", "x" * 2000 + "\n\n")
    assert o["reason"] == "x" * 500


# ----- logs --------------------------------------------------------------------------------------------------
def test_tail_drops_the_cut_line_and_says_how_much_was_cut(tmp_path):
    p = tmp_path / "log.txt"
    assert jobs._tail(p) == ""                                    # missing: empty, not an error
    p.write_bytes(b"line-one\nline-two\nline-three\n")
    assert jobs._tail(p, 1000) == "line-one\nline-two\nline-three\n"
    t = jobs._tail(p, 15)                                         # cuts inside "line-two"
    assert t.startswith("[... 14 bytes not shown]\n") and t.endswith("line-three\n")
    assert t.count("\n") == 2                                    # the header and line-three: no part of line-two
    p.write_bytes(b"\xff\xfeok\n")
    assert "ok" in jobs._tail(p)                                  # undecodable bytes are replaced, not fatal


def test_log_text_reads_live_tier_logs_then_the_kept_copies(cfg):
    d = jobs.job_dir(cfg, 7)
    (d / "work").mkdir(parents=True)
    (d / "log.txt").write_text("main\n")
    (d / "work" / "tier-t1.log").write_text("live\n")
    (d / "tier-t1.log").write_text("kept\n")
    text = jobs.log_text(cfg, 7)
    assert text.startswith("main\n") and "--- tier-t1.log" in text and "live" in text and "kept" not in text
    (d / "work" / "tier-t1.log").unlink()
    (d / "work").rmdir()
    assert "kept" in jobs.log_text(cfg, 7)
    assert jobs.log_text(cfg, 99) == ""


# ----- the queue ---------------------------------------------------------------------------------------------
def test_queue_position_cancel_and_listing(cfg):
    a = jobs.submit(cfg, "walk", {"configuration": "c-a"}, "ann")
    b = jobs.submit(cfg, "mirror-sync", {}, "bob")
    c = jobs.submit(cfg, "walk", {"configuration": "c-b"}, "ann")
    assert [jobs.queue_position(cfg, j["id"]) for j in (a, b, c)] == [1, 2, 3]
    assert b["command"] == "nanosoc-rm mirror sync" and b["params"] == {"component": None}
    done = jobs.cancel(cfg, b["id"], "carol")
    assert done["state"] == "cancelled" and done["cancel_by"] == "carol" and "before it started" in done["reason"]
    assert jobs.queue_position(cfg, b["id"]) is None and jobs.queue_position(cfg, c["id"]) == 2
    assert jobs.cancel(cfg, b["id"], "dave")["cancel_by"] == "carol"           # a finished job is unchanged
    jobs._update(cfg, a["id"], state="running")
    run = jobs.cancel(cfg, a["id"], "erin")
    assert run["state"] == "running" and run["cancel_by"] == "erin"          # the worker stops it
    assert jobs.cancel(cfg, 12345, "x") is None
    assert [j["id"] for j in jobs.jobs(cfg, kind="walk")] == [c["id"], a["id"]]
    assert [j["id"] for j in jobs.jobs(cfg, configuration="c-b")] == [c["id"]]
    assert [j["id"] for j in jobs.jobs(cfg, states=("cancelled", "running"), limit=1)] == [b["id"]]
    assert jobs.get(cfg, -1) is None and jobs.get(cfg, 2 ** 63) is None


def test_submit_needs_a_login_and_checks_ids(cfg):
    with pytest.raises(jobs.JobUsage, match="login"):
        jobs.submit(cfg, "walk", {"configuration": "x"}, "")
    for bad in (None, 5, "", "a" * 129, "-lead", "x y", "x/y"):
        with pytest.raises(jobs.JobUsage):
            jobs.submit(cfg, "walk", {"configuration": bad}, "ann")
    with pytest.raises(jobs.JobUsage, match="tier must be"):
        jobs.submit(cfg, "compat", {"configuration": "x", "tier": "t9"}, "ann")
    assert jobs.jobs(cfg) == []                                   # nothing was queued by a refused request


def test_compat_what_if_args_never_store():
    p = jobs._params("compat", {"configuration": "eth", "pins": [" a/b=%s " % SHA, "  "], "tier": "t1",
                                "force_tier": 1})
    assert p == {"configuration": "eth", "pins": ["a/b=%s" % SHA], "tier": "t1", "force_tier": True}
    args = jobs.cli_args("compat", p)
    assert "--store" not in args and args[-1] == "--json" and "--force-tier" in args
    real = jobs.cli_args("compat", dict(p, pins=[]), work=jobs.Path("/w"))
    assert "--store" in real and real[real.index("--keep-work") + 1] == "/w"
    with pytest.raises(jobs.JobUsage):
        jobs.cli_args("nope", {})


# ----- recovery and the ceiling ------------------------------------------------------------------------------
def test_recover_marks_a_job_whose_process_is_gone_infra(cfg):
    j = jobs.submit(cfg, "walk", {"configuration": "c"}, "ann")
    k = jobs.submit(cfg, "walk", {"configuration": "c"}, "ann")
    jobs._update(cfg, j["id"], state="running", pid=None)
    # pid 1 is alive but is not this job's process (no NANOSOC_RM_JOB in its environment): never signalled
    jobs._update(cfg, k["id"], state="running", pid=1)
    assert sorted(jobs.recover(cfg)) == sorted([j["id"], k["id"]])
    for jid in (j["id"], k["id"]):
        r = jobs.get(cfg, jid)
        assert (r["state"], r["verdict"]) == ("infra", "INFRA") and "already ended" in r["reason"]
        assert "service restarted" in jobs.log_text(cfg, jid)
    assert jobs.recover(cfg) == []


def test_is_job_process_needs_the_job_marker():
    import os
    assert not jobs._is_job_process(os.getpid(), 424242)
    assert not jobs._is_job_process(2 ** 22 + 12345, 1)          # no such pid


def test_ceiling_of_a_tier_whose_tiers_yaml_is_unreadable(cfg, monkeypatch):
    from nanosoc_rm.ci import configs

    def broken(*a, **k):
        raise RuntimeError("no registry")
    monkeypatch.setattr(configs, "tiers", broken)
    job = {"kind": "compat", "params": {"configuration": "c", "tier": "t1"}}
    assert jobs._limit_s(cfg, job) == jobs.JOB_LIMIT_S + 4 * 3600
    monkeypatch.setattr(configs, "tiers", lambda *a, **k: {"t1": {"budget_s": 100}})
    assert jobs._limit_s(cfg, job) == jobs.JOB_LIMIT_S + 150
    assert jobs._limit_s(cfg, {"kind": "walk", "params": {}}) == jobs.JOB_LIMIT_S


def test_step_records_a_worker_crash_on_the_job(cfg, monkeypatch):
    j = jobs.submit(cfg, "walk", {"configuration": "c"}, "ann")
    w = jobs.Worker(cfg.path, log=lambda m: None)

    def crash(config, job):
        raise OSError("disk full " + "x" * 400)
    monkeypatch.setattr(w, "_run", crash)
    assert w.step() == j["id"] and w.step() is None
    r = jobs.get(cfg, j["id"])
    assert r["state"] == "infra" and r["reason"].startswith("the worker could not run the job: OSError: disk full")
    assert len(r["reason"]) < 400 and r["finished_at"]
    assert "[worker] infra INFRA" in jobs.log_text(cfg, j["id"])


def test_one_worker_per_data_dir(cfg):
    a, b = jobs.Worker(cfg.path, log=lambda m: None), jobs.Worker(cfg.path, log=lambda m: None)
    try:
        assert a.acquire(cfg) and a.acquire(cfg)                  # re-entrant for the holder
        assert not b.acquire(cfg)
        a.release()
        assert b.acquire(cfg)
    finally:
        a.release()
        b.release()


# ----- the kind registry -------------------------------------------------------------------------------------
def test_register_kind_refuses_builtin_bad_and_foreign_names():
    def params(p):
        return p
    with pytest.raises(ValueError, match="built in"):
        jobs.register_kind("compat", params, lambda p: [])
    for bad in ("", "a b", "../x", 5):
        with pytest.raises(ValueError, match="not a job kind name"):
            jobs.register_kind(bad, params, lambda p: [])
    jobs.all_kinds()
    name = "matrix-cell"
    assert jobs.registered(name) is not None
    with pytest.raises(ValueError, match="already registered"):
        jobs.register_kind(name, params, lambda p: [])           # this module is not the one that owns it
    assert jobs.registered("compat") is None and jobs.registered("no-such-kind") is None
    assert set(jobs.KINDS) <= set(jobs.all_kinds())


def test_a_registered_kind_params_error_is_a_usage_error(cfg, monkeypatch):
    def params(p):
        raise ValueError("bad cell")
    k = jobs.JobKind("cov-kind", params, lambda p: ["x"])
    monkeypatch.setitem(jobs._REGISTERED, "cov-kind", k)
    with pytest.raises(jobs.JobUsage, match="bad cell"):
        jobs.submit(cfg, "cov-kind", {}, "ann")
    assert jobs.cli_args("cov-kind", {}) == ["x", "--json"]
