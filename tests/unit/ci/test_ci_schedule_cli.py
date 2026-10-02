"""schedule.tick (interfaces.md section 12a), the `t0` commands, and AT-A2's census on the real package."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, db, schedule
from nanosoc_rm.ci.client import FakeCI
from nanosoc_rm.contracts import WRITE_CALLS
from nanosoc_rm.schedule import Job, tick

import ci_helpers

# fixtures (ci_helpers docstring)
ce, sw, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.sw, ci_helpers.ci_config, ci_helpers.scanner_clean

REPO = Path(__file__).resolve().parents[3]


def utc(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture
def recorder():
    calls = []
    jobs = tuple(Job(j.name, j.every, (lambda cfg, n=j.name: calls.append(n))) for j in schedule.JOBS)
    return calls, jobs


def test_job_names_are_the_contracted_ones():
    assert [j.name for j in schedule.JOBS] == ["poll", "inventory", "nightly_t0", "fetch", "credentials", "runner"]


def test_first_tick_after_the_nightly_time_runs_everything_in_order(ci_config, recorder):
    calls, jobs = recorder
    assert tick(utc("2026-10-01T02:00:10Z"), jobs=jobs) == [j.name for j in jobs]
    assert calls == [j.name for j in jobs]


def test_cadence_poll_fetch_and_nightly(ci_config, recorder):
    calls, jobs = recorder
    assert tick(utc("2026-10-01T01:00:00Z"), jobs=jobs) == ["poll", "inventory", "fetch", "runner"]  # before 02:00
    assert tick(utc("2026-10-01T01:00:30Z"), jobs=jobs) == []
    assert tick(utc("2026-10-01T01:02:00Z"), jobs=jobs) == ["fetch"]
    assert tick(utc("2026-10-01T01:14:59Z"), jobs=jobs) == ["fetch"]
    assert tick(utc("2026-10-01T01:15:00Z"), jobs=jobs) == ["poll", "inventory", "runner"]
    assert tick(utc("2026-10-01T01:29:00Z"), jobs=jobs) == ["fetch"]
    assert tick(utc("2026-10-01T02:00:00Z"), jobs=jobs) == [j.name for j in jobs]     # poll is due too
    assert tick(utc("2026-10-01T02:00:30Z"), jobs=jobs) == []
    assert "nightly_t0" not in tick(utc("2026-10-01T23:59:00Z"), jobs=jobs)          # once per night
    assert "nightly_t0" in tick(utc("2026-10-02T02:00:00Z"), jobs=jobs)


def test_state_survives_a_restart_and_a_missed_night_is_caught_up_that_day(ci_config, recorder):
    calls, jobs = recorder
    tick(utc("2026-10-01T02:00:00Z"), jobs=jobs)
    state = json.loads(schedule.state_path(ci_config).read_text())
    assert state["nightly_t0"] == "2026-10-01T02:00:00Z"
    assert "nightly_t0" not in tick(utc("2026-10-01T09:00:00Z"), jobs=jobs)          # restart: no repeat
    assert "nightly_t0" in tick(utc("2026-10-02T09:00:00Z"), jobs=jobs)              # down at 02:00: caught up


def test_a_failing_job_is_marked_run_and_the_others_still_run(ci_config, capsys):
    ran = []

    def boom(cfg):
        raise RuntimeError("mirror host down")
    jobs = (Job("poll", "poll", boom), Job("fetch", "fetch", lambda cfg: ran.append("fetch")))
    assert tick(utc("2026-10-01T01:00:00Z"), jobs=jobs) == ["poll", "fetch"]
    assert ran == ["fetch"] and "mirror host down" in capsys.readouterr().err
    assert tick(utc("2026-10-01T01:01:00Z"), jobs=jobs) == []                         # not retried in a tight loop


def test_nightly_job_triggers_every_configuration_and_the_registry(ci_config, monkeypatch):
    from nanosoc_rm.ci import trigger as trig
    seen = []
    monkeypatch.setattr(trig, "trigger", lambda cfg, by, kind, config=None: seen.append((cfg, by, kind)))
    nightly = next(j for j in schedule.JOBS if j.name == "nightly_t0")
    nightly.run(ci_config)
    assert seen == [(c, "schedule", "schedule") for c in ("eth-rc4", "eth-main", "multicore-master", "@registry")]


# ---- the t0 commands -------------------------------------------------------------------------------------------
def invoke(*args):
    return CliRunner().invoke(cli.main, list(args))


def test_t0_trigger_usage(ci_config):
    assert invoke("t0", "trigger", "eth-rc4").exit_code == 3                         # a shell run needs --by
    assert invoke("t0", "trigger", "eth-rc4", "--by", "schedule").exit_code == 3
    assert invoke("t0", "trigger", "--by", "alice").exit_code == 3                   # neither CONFIG nor --all
    assert invoke("t0", "trigger", "no-such", "--by", "alice").exit_code == 3
    assert db.runs() == []


def test_t0_trigger_runs_items_and_coverage(ce, ci_config, monkeypatch):
    from nanosoc_rm.ci import trigger as trig
    monkeypatch.setattr(trig, "make_client", lambda config: FakeCI(fail_trigger_for={"eth-rc4": 503}))
    r = invoke("t0", "trigger", "eth-rc4", "--by", "alice", "--json")
    assert r.exit_code == 2, r.output                                                 # the trigger failed
    (doc,) = [json.loads(line) for line in r.output.splitlines()]
    assert doc["fact"]["acceptance"] == "no-bundle" and doc["fact"]["verdict"] == "INFRA"
    assert [a["login"] for a in db.actions()] == ["alice"]
    r = invoke("t0", "runs", "eth-rc4", "--json")
    assert r.exit_code == 1 and json.loads(r.output.splitlines()[0])["subject"] == doc["subject"]
    assert invoke("t0", "runs", "eth-main", "--json").exit_code == 0
    nonce = doc["fact"]["run"]["nonce"]
    assert invoke("t0", "items", nonce, "t0.pins.reachable").exit_code == 2          # no stored bundle
    assert invoke("t0", "items", "0" * 32, "t0.pins.reachable").exit_code == 3       # no such run
    assert invoke("t0", "items", nonce, "../../etc").exit_code == 3
    r = invoke("t0", "coverage", "--night", doc["fact"]["run"]["triggered_at"][:10], "--json")
    assert r.exit_code == 1
    assert {json.loads(line)["configuration"] for line in r.output.splitlines()} == {
        "eth-rc4", "eth-main", "multicore-master", "@registry"}
    assert invoke("t0", "coverage", "--night", "yesterday").exit_code == 3


def test_t0_items_prints_the_stored_bundle(ce, ci_config):
    from nanosoc_rm.ci.fetch import bundle_dir
    from nanosoc_rm.ci.verify import RunRecord
    case = ce.genuine_case()
    db.put_run(RunRecord(**case.run))
    d = bundle_dir(case.run["nonce"], ci_config) / "items"
    d.mkdir(parents=True)
    lines = ce.example_items()["t0.pins.fetchable"]
    (d / "t0.pins.fetchable.jsonl").write_text("".join(json.dumps(i) + "\n" for i in lines))
    r = invoke("t0", "items", case.run["nonce"], "t0.pins.fetchable", "--json")
    assert r.exit_code == 1 and [json.loads(x) for x in r.output.splitlines()] == lines   # 1: non-pass items


# ---- A2: the one write, on the real package ----------------------------------------------------------------------
def test_the_census_finds_exactly_one_write_and_it_is_the_trigger(sw):
    census = sw.write_call_census(REPO / "src" / "nanosoc_rm")
    writes = [c for c in census if c["kind"] == "http"]
    assert [(c["file"], c["method"]) for c in writes] == [("nanosoc_rm/ci/client.py", "POST")]
    assert sw.allowed(writes[0], [w for w in WRITE_CALLS if w["effect"] == "write"])
    creds = {"github_read": {"scope": ["contents:read", "metadata:read", "members:read"]},
             "gitlab_read": {"scope": ["read_api", "read_repository"]},
             "ci_trigger": {"scope": ["trigger"], "project": sw.CI_PROJECT}, "github_oauth": {"scope": ["read:org"]}}
    runs = [{"nonce": "1" * 32, "configuration": "eth-rc4", "trigger": "manual", "triggered_by": "alice",
             "triggered_at": "2026-10-01T02:03:10Z"}]
    actions = [{"at": "2026-10-01T02:03:10Z", "login": "alice", "action": "run_t0", "target": "eth-rc4", "result": "pending"}]
    assert sw.check_a2(census, list(WRITE_CALLS), creds, ci_project=sw.CI_PROJECT, runs=runs, actions=actions) == []


def test_the_service_side_never_imports_the_t0_runner():
    import subprocess
    import sys
    code = ("import sys, nanosoc_rm.cli, nanosoc_rm.ci.fetch, nanosoc_rm.ci.trigger, nanosoc_rm.schedule, "
            "nanosoc_rm.credentials; print('nanosoc_rm.t0.runner' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "False", out.stderr
