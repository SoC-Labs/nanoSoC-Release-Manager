"""Mutation-killing tests for the local job runner's state machine (robustness angle: mutation).

Scripted AST mutation over jobs.submit, cancel, queue_position, recover, _outcome, _params, Worker._claim,
Worker.step, Worker._run, _limit_s, _tail, get, jobs, _check_id and _is_job_process (2026-10-14), against
test_jobs.py. These tests reach the decision functions directly (no child process), so they run in well under
a second each. Each names the mutant it kills."""
from __future__ import annotations

import pytest

from nanosoc_rm import jobs
from nanosoc_rm.config import load_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def _job(kind="walk", **params):
    return {"kind": kind, "params": params or {"configuration": "c"}}


# ---- _outcome: exit code -> state ------------------------------------------------------------------------------
def test_usage_exit_is_failed_with_the_last_log_line_or_a_default():
    assert jobs._outcome(_job(), 3, "", "x\nnanosoc-rm: bad id\n") == {"state": "failed", "verdict": None,
                                                                      "reason": "nanosoc-rm: bad id"}
    assert jobs._outcome(_job(), 3, "", "")["reason"] == "usage error (exit 3)"


@pytest.mark.parametrize("rc, words", [(-9, "signal 9"), (9, "exit 9"), (None, "exit None"), (4, "exit 4")])
def test_any_other_exit_is_infra(rc, words):
    """Kills `rc < 0 -> <= 0`, the `rc is not None` guard, and drops from the (0, 1, 2) tuple."""
    out = jobs._outcome(_job(), rc, "", "boom")
    assert (out["state"], out["verdict"]) == ("infra", "INFRA") and out["reason"] == \
        "the job process ended with %s: boom" % words


def test_infra_reason_without_a_log_line_has_no_colon():
    assert jobs._outcome(_job(), 9, "", "")["reason"] == "the job process ended with exit 9"


@pytest.mark.parametrize("rc, verdict, reason", [(0, "PASS", None), (1, "FAIL", "last"), (2, "UNVERIFIED", "last")])
def test_plain_kinds_map_0_1_2(rc, verdict, reason):
    """Kills the verdict table and `None if rc == 0` mutants."""
    out = jobs._outcome(_job(), rc, '{"kind": "pin_state"}\nnot json\n{bad\n', "last")
    assert (out["state"], out["verdict"], out["reason"]) == ("done", verdict, reason)
    assert out["result"] == {"documents": 1, "kinds": {"pin_state": 1}}


def test_a_compat_job_takes_the_last_compat_result():
    first = '{"kind": "compat_result", "fact": {"verdict": "PASS"}}'
    last = '{"kind": "compat_result", "fact": {"verdict": "PASS", "run_verdict": "FAIL", "reason": "tier"}}'
    out = jobs._outcome(_job("compat"), 1, "\n".join([first, '{"kind": "pin_state"}', last]), "")
    assert (out["verdict"], out["reason"]) == ("FAIL", "tier") and out["result"]["fact"]["run_verdict"] == "FAIL"
    out = jobs._outcome(_job("compat"), 0, first, "")
    assert out["verdict"] == "PASS"


def test_a_compat_job_that_printed_no_result_is_unverified():
    assert jobs._outcome(_job("compat"), 0, "", "") == {"state": "done", "verdict": "UNVERIFIED",
                                                       "reason": "compat printed no result"}
    assert jobs._outcome(_job("compat"), 2, "", "why")["reason"] == "why"


def test_the_reason_line_is_capped_at_500():
    assert len(jobs._outcome(_job(), 3, "", "x" * 900)["reason"]) == 500


# ---- requests --------------------------------------------------------------------------------------------------
def test_unknown_kind_lists_the_known_ones(cfg):
    """Kills `Raise -> pass` on the unknown-kind error (cli_args would raise a shorter message instead)."""
    with pytest.raises(jobs.JobUsage, match=r"unknown job kind 'push' \(known: compat, mirror-sync, walk"):
        jobs.submit(cfg, "push", {}, "alice")


@pytest.mark.parametrize("params, words", [
    ({"configuration": "c", "tier": "t3"}, "tier must be t1 or t2"),
    ({"configuration": 7}, "not a configuration id"),
    ({"configuration": "c", "pins": ["bad"]}, ""),
])
def test_compat_params_are_checked(cfg, params, words):
    with pytest.raises(jobs.JobUsage, match=words):
        jobs.submit(cfg, "compat", params, "alice")
    assert jobs.jobs(cfg) == []


def test_compat_params_are_normalised(cfg):
    p = jobs._params("compat", {"configuration": "c", "pins": [" a=%s " % ("0" * 40), "  "], "tier": "",
                                "force_tier": 0})
    assert p == {"configuration": "c", "pins": ["a=%s" % ("0" * 40)], "tier": None, "force_tier": False}
    assert jobs._params("mirror-sync", {"component": ""}) == {"component": None}
    assert jobs._params("mirror-sync", {"component": "x"}) == {"component": "x"}
    with pytest.raises(jobs.JobUsage):
        jobs._params("mirror-sync", {"component": "../x"})


def test_a_job_needs_a_login(cfg):
    with pytest.raises(jobs.JobUsage, match="needs the login"):
        jobs.submit(cfg, "walk", {"configuration": "c"}, "")


@pytest.mark.parametrize("value", ["a" * 128, "a@b", "A_b.c-d"])
def test_ids_that_are_accepted(value):
    assert jobs._check_id(value, "x") == value


@pytest.mark.parametrize("value", ["a" * 129, "-a", ".a", "", None, "a/b", "a b"])
def test_ids_that_are_refused(value):
    with pytest.raises(jobs.JobUsage):
        jobs._check_id(value, "x")


# ---- the store -------------------------------------------------------------------------------------------------
def test_get_out_of_range_is_none_not_an_error(cfg):
    assert jobs.get(cfg, -1) is None and jobs.get(cfg, 2 ** 63) is None
    j = jobs.submit(cfg, "walk", {"configuration": "c"}, "alice")
    assert jobs.get(cfg, j["id"])["id"] == j["id"]


def test_jobs_filters_and_limit(cfg):
    """Kills the `kind` / `states` / `configuration` filter guards and the default limit (50 -> 51)."""
    ids = [jobs.submit(cfg, "walk", {"configuration": "c%d" % (i % 2)}, "alice")["id"] for i in range(52)]
    s = jobs.submit(cfg, "mirror-sync", {}, "bob")["id"]
    assert len(jobs.jobs(cfg)) == 50 and jobs.jobs(cfg)[0]["id"] == s           # newest first
    assert [j["id"] for j in jobs.jobs(cfg, kind="mirror-sync")] == [s]
    assert {j["params"]["configuration"] for j in jobs.jobs(cfg, configuration="c1", limit=100)} == {"c1"}
    assert len(jobs.jobs(cfg, configuration="c1", limit=100)) == 26
    jobs.cancel(cfg, ids[0], "alice")
    assert [j["id"] for j in jobs.jobs(cfg, states=("cancelled",))] == [ids[0]]
    assert len(jobs.jobs(cfg, states=("queued", "cancelled"), limit=1000)) == 53


def test_cancel_and_queue_position_by_state(cfg):
    """A queued job is cancelled at once; a running one only gets cancel_by; a finished one is unchanged."""
    a = jobs.submit(cfg, "walk", {"configuration": "a"}, "alice")["id"]
    b = jobs.submit(cfg, "walk", {"configuration": "b"}, "alice")["id"]
    c = jobs.submit(cfg, "walk", {"configuration": "c"}, "alice")["id"]
    assert [jobs.queue_position(cfg, x) for x in (a, b, c)] == [1, 2, 3]
    got = jobs.cancel(cfg, a, "bob")
    assert (got["state"], got["cancel_by"], got["reason"]) == ("cancelled", "bob", "cancelled by bob before it started")
    assert got["finished_at"] and jobs.queue_position(cfg, a) is None and jobs.queue_position(cfg, b) == 1
    jobs._update(cfg, b, state="running")
    got = jobs.cancel(cfg, b, "carol")
    assert (got["state"], got["cancel_by"], got["finished_at"]) == ("running", "carol", None)
    jobs._update(cfg, c, state="done", verdict="PASS")
    got = jobs.cancel(cfg, c, "dave")
    assert (got["state"], got["cancel_by"]) == ("done", None)
    assert jobs.cancel(cfg, 999, "x") is None


def test_claim_takes_the_oldest_queued_and_marks_it_running(cfg):
    a = jobs.submit(cfg, "walk", {"configuration": "a"}, "alice")["id"]
    b = jobs.submit(cfg, "walk", {"configuration": "b"}, "alice")["id"]
    jobs.cancel(cfg, a, "x")
    w = jobs.Worker(cfg.path)
    got = w._claim(cfg)
    assert got["id"] == b and got["state"] == "running" and got["started_at"]
    assert w._claim(cfg) is None


def test_recover_marks_running_jobs_infra_and_leaves_others(cfg):
    """Kills `bool(pid) and ...` mutants and the alive/ended wording."""
    a = jobs.submit(cfg, "walk", {"configuration": "a"}, "alice")["id"]
    b = jobs.submit(cfg, "walk", {"configuration": "b"}, "alice")["id"]
    jobs._update(cfg, a, state="running", pid=None)
    assert jobs.recover(cfg) == [a]
    got = jobs.get(cfg, a)
    assert (got["state"], got["verdict"]) == ("infra", "INFRA")
    assert got["reason"].startswith("the service restarted while this job ran; its process had already ended")
    assert "[worker] the service restarted" in jobs.log_text(cfg, a)
    assert jobs.get(cfg, b)["state"] == "queued" and jobs.recover(cfg) == []


def test_a_recycled_pid_is_not_the_jobs_process():
    import os
    assert jobs._is_job_process(os.getpid(), 12345) is False
    assert jobs._is_job_process(2 ** 22 + 7, 1) is False


def test_tail_keeps_whole_lines_and_says_what_it_dropped(tmp_path):
    """_tail: `size > limit` and the dropped-bytes header. Kills `> -> >=`, `- -> +`, `max(0, ...)`."""
    p = tmp_path / "log.txt"
    p.write_text("aaaa\nbbbb\ncccc\n")                  # 15 bytes
    assert jobs._tail(p, 15) == "aaaa\nbbbb\ncccc\n"
    assert jobs._tail(p, 12) == "[... 3 bytes not shown]\nbbbb\ncccc\n"
    assert jobs._tail(tmp_path / "missing") == ""


def test_the_ceiling_is_the_default_unless_a_tier_runs(cfg):
    """_limit_s: plain jobs JOB_LIMIT_S; a tier with an unreadable budget adds 4 h."""
    assert jobs._limit_s(cfg, {"kind": "walk", "params": {"configuration": "c"}}) == jobs.JOB_LIMIT_S
    assert jobs._limit_s(cfg, {"kind": "compat", "params": {"configuration": "c", "tier": None}}) == jobs.JOB_LIMIT_S
    assert jobs._limit_s(cfg, {"kind": "compat", "params": {"configuration": "nope", "tier": "t1"}}) == \
        jobs.JOB_LIMIT_S + 4 * 3600


# ---- a registered (plug-in) kind ---------------------------------------------------------------------------------
@pytest.fixture
def plugin(monkeypatch):
    """A registered kind with every optional hook, removed again after the test."""
    jobs.all_kinds()                                                  # import KIND_MODULES first
    monkeypatch.setattr(jobs, "_REGISTERED", dict(jobs._REGISTERED))

    def params(p):
        if p.get("configuration") == "bad":
            raise ValueError("bad configuration")
        return {"configuration": p["configuration"], "n": int(p.get("n", 1))}
    jobs.register_kind("rob-plugin", params, lambda p: ["plug", p["configuration"]],
                       outcome=lambda docs, last: {"state": "done", "verdict": "WARN", "reason": last},
                       limit_s=lambda config, job: 42, action="run_t0")
    return "rob-plugin"


def test_a_registered_kind_checks_runs_and_logs_through_its_hooks(cfg, plugin):
    """Kills `In -> NotIn` on `kind in KINDS`, `return k.params(...) -> None`, `Raise -> pass` on its ValueError,
    the registered-kind action branch (`kind == "compat"` -> `!=`) and its hooks' `return ... -> None`."""
    from nanosoc_rm import db
    with pytest.raises(jobs.JobUsage, match="bad configuration"):
        jobs.submit(cfg, plugin, {"configuration": "bad"}, "alice")
    j = jobs.submit(cfg, plugin, {"configuration": "c", "n": "3"}, "alice")
    assert j["params"] == {"configuration": "c", "n": 3} and j["command"] == "nanosoc-rm plug c"
    assert jobs.cli_args(plugin, j["params"]) == ["plug", "c", "--json"]
    acts = [(a["login"], a["action"], a["target"], a["result"]) for a in db.actions(config=cfg)]
    assert acts == [("alice", "run_t0", "c", "local job %d (nanosoc-rm plug c)" % j["id"])]
    assert jobs._limit_s(cfg, j) == 42.0
    assert jobs._outcome(j, 1, "", "why") == {"state": "done", "verdict": "WARN", "reason": "why"}


def test_a_registered_kind_without_hooks_uses_the_defaults(cfg, monkeypatch):
    jobs.all_kinds()
    monkeypatch.setattr(jobs, "_REGISTERED", dict(jobs._REGISTERED))
    jobs.register_kind("rob-bare", lambda p: dict(p), lambda p: ["bare", "--json"])
    j = jobs.submit(cfg, "rob-bare", {"configuration": "c"}, "alice")
    assert jobs._limit_s(cfg, j) == jobs.JOB_LIMIT_S
    assert jobs._outcome(j, 1, '{"kind": "x"}', "l")["result"] == {"documents": 1, "kinds": {"x": 1}}


def test_a_tier_ceiling_adds_one_and_a_half_budgets(cfg, monkeypatch):
    """_limit_s: `budget_s or 0`. Kills `0 -> 1` and the 1.5 factor."""
    from nanosoc_rm.ci import configs
    monkeypatch.setattr(configs, "tiers", lambda cid, config: {"t1": {"budget_s": 100}, "t2": {}})
    job = {"kind": "compat", "params": {"configuration": "c", "tier": "t1"}}
    assert jobs._limit_s(cfg, job) == jobs.JOB_LIMIT_S + 150
    job["params"]["tier"] = "t2"
    assert jobs._limit_s(cfg, job) == jobs.JOB_LIMIT_S


def test_a_worker_crash_is_recorded_on_the_job(cfg, monkeypatch):
    """Worker.step's except branch: reason capped at 300, and the log line for a verdict of None reads
    "[worker] failed  at" (kills `verdict or ""` -> `and`, and `300 -> 301`)."""
    j = jobs.submit(cfg, "walk", {"configuration": "c"}, "alice")
    w = jobs.Worker(cfg.path)

    def boom(config, job):
        raise RuntimeError("x" * 400)
    monkeypatch.setattr(w, "_run", boom)
    assert w.step() == j["id"]
    got = jobs.get(cfg, j["id"])
    assert (got["state"], got["verdict"]) == ("infra", "INFRA") and got["finished_at"]
    assert got["reason"] == "the worker could not run the job: RuntimeError: " + "x" * 300
    k = jobs.submit(cfg, "walk", {"configuration": "c"}, "alice")
    monkeypatch.setattr(w, "_run", lambda config, job: {"state": "failed", "verdict": None, "reason": "usage"})
    w.step()
    assert "[worker] failed  at " in jobs.log_text(cfg, k["id"])


def test_a_long_bad_id_is_quoted_at_sixty_characters():
    with pytest.raises(jobs.JobUsage) as e:
        jobs._check_id("/" + "a" * 100, "configuration")
    assert str(e.value) == "%r is not a configuration id" % ("/" + "a" * 59)
