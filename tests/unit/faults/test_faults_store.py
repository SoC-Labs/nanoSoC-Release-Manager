"""Failure injection on the store (rm.sqlite), the scheduler state and the clock: a locked or corrupt database, a
read-only data directory, a wall clock that steps backwards (NTP correcting a fast clock, a VM restored from a
snapshot) or a fact stamped in the future. The rules under test: a command that cannot read its facts exits 2
(could not answer), never 0; the newest producer run is the current one whatever the wall clock said; skew is
visible, never silently "fresh".

Robustness angle: faults. A test that exposes a defect is a strict xfail named ROB-faults-<n>."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from click.testing import CliRunner

import faults_fix  # sys.path for the other unit-test directories

assert faults_fix.UNIT.is_dir()
from nanosoc_rm import cli, db, schedule
from nanosoc_rm.config import load_config
from nanosoc_rm.web.page import human_age

SRC = [{"kind": "derived", "command": "faults fixture"}]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def status_doc(scope_id: str, ok: bool, at: str) -> dict:
    repo = {"host": "github", "path": "SoC-Labs/x"}
    return {"schema": 1, "kind": "mirror_status", "subject": "mirror_status:github:SoC-Labs/x", "observed_at": at,
            "sources": SRC, "snapshot": {"id": scope_id, "scope": "mirror_status:x"},
            "fact": {"repo": repo, "component": "x", "ok": ok, "last_success_at": at if ok else None,
                     "error": None if ok else "fatal: unable to access: Could not resolve host"}}


def _invoke(*args):
    return CliRunner().invoke(cli.main, list(args))


# ----- a store that cannot be read ---------------------------------------------------------------------------------
def test_a_corrupt_store_is_could_not_answer_never_an_answer(cfg):
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "it failed", SRC, config=cfg)
    assert _invoke("alerts").exit_code == 1                       # control: the store reads, one open alert
    cfg.db_path.write_bytes(b"not a database\n" * 100)
    for ext in ("-wal", "-shm"):
        cfg.db_path.with_name(cfg.db_path.name + ext).unlink(missing_ok=True)
    for args in (("alerts",), ("mirror", "status"), ("mirror", "status", "--summary")):
        r = _invoke(*args)
        assert r.exit_code == 2, (args, r.exit_code, r.output)


def test_a_locked_store_is_could_not_answer_never_an_answer(cfg, monkeypatch):
    """Another process holds a write lock past the busy timeout (a stuck job, a backup tool)."""
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "it failed", SRC, config=cfg)
    holder = sqlite3.connect(str(cfg.db_path), isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")                  # no WAL: a writer blocks readers too
    holder.execute("BEGIN EXCLUSIVE")
    real = sqlite3.connect
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: real(*a, **dict(k, timeout=0.2)))
    try:
        r = _invoke("alerts")
        assert r.exit_code == 2, (r.exit_code, r.output)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _invoke("alerts").exit_code == 1                       # control: lock released, the same command answers


def test_a_snapshot_cut_short_never_becomes_current(cfg, monkeypatch):
    """Disk full (or a crash) half-way through a producer run: the previous complete snapshot stays current."""
    db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@1", True, "2026-10-14T08:00:00Z")], config=cfg)
    real_insert = db._insert

    def full(con, doc):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(db, "_insert", full)
    with pytest.raises(sqlite3.OperationalError):
        db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@2", False, "2026-10-14T08:15:00Z")], config=cfg)
    monkeypatch.setattr(db, "_insert", real_insert)
    cur = db.latest("mirror_status", config=cfg)
    assert [d["snapshot"]["id"] for d in cur] == ["mirror_status:x@1"]
    with db.connect(cfg) as con:                                  # and the cut-short run left no row behind
        assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


# ----- the wall clock steps backwards -------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ROB-faults-4: the current snapshot is chosen by wall-clock completed_at "
                                       "(db._latest ORDER BY completed_at); after the clock steps back, a newer run "
                                       "(here a FAILED mirror fetch) stays hidden behind the older ok:true one")
def test_the_newest_snapshot_is_current_even_after_the_clock_steps_back(cfg, monkeypatch):
    monkeypatch.setattr(db, "_now_exact", lambda: "2026-10-14T09:00:00.000000+00:00")
    db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@1", True, "2026-10-14T09:00:00Z")], config=cfg)
    monkeypatch.setattr(db, "_now_exact", lambda: "2026-10-14T08:00:00.000000+00:00")     # clock stepped back 1 h+
    db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@2", False, "2026-10-14T08:00:00Z")], config=cfg)
    cur = db.latest("mirror_status", config=cfg)
    assert [d["fact"]["ok"] for d in cur] == [False], cur


def test_the_newest_snapshot_is_current_when_the_clock_moves_forward(cfg, monkeypatch):
    """The control for the test above: the same two runs with a sane clock."""
    monkeypatch.setattr(db, "_now_exact", lambda: "2026-10-14T09:00:00.000000+00:00")
    db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@1", True, "2026-10-14T09:00:00Z")], config=cfg)
    monkeypatch.setattr(db, "_now_exact", lambda: "2026-10-14T09:15:00.000000+00:00")
    db.put_snapshot("mirror_status:x", [status_doc("mirror_status:x@2", False, "2026-10-14T09:15:00Z")], config=cfg)
    assert [d["fact"]["ok"] for d in db.latest("mirror_status", config=cfg)] == [False]


@pytest.mark.xfail(strict=True, reason="ROB-faults-5: an alert raised again after the clock stepped back (raised_at "
                                       "older than the stored raise) is ignored by db._raise_row, so a cleared "
                                       "mirror_failed alert stays cleared while the mirror fails again")
def test_an_alert_raised_again_after_the_clock_stepped_back_is_open(cfg):
    subj = "alert:mirror_failed:github:SoC-Labs/x"
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "first failure", SRC, at="2026-10-14T09:00:00Z", config=cfg)
    db.clear_alert(subj, "alice", at="2026-10-14T09:05:00Z", config=cfg)
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "fails again", SRC, at="2026-10-14T08:30:00Z", config=cfg)
    assert subj in {a["subject"] for a in db.alerts(config=cfg)}


def test_an_alert_raised_again_later_is_open(cfg):
    """The control for the test above."""
    subj = "alert:mirror_failed:github:SoC-Labs/x"
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "first failure", SRC, at="2026-10-14T09:00:00Z", config=cfg)
    db.clear_alert(subj, "alice", at="2026-10-14T09:05:00Z", config=cfg)
    db.raise_alert("mirror_failed", "github:SoC-Labs/x", "fails again", SRC, at="2026-10-14T09:30:00Z", config=cfg)
    assert subj in {a["subject"] for a in db.alerts(config=cfg)}


# ----- the scheduler under a skewed clock or a broken state file ----------------------------------------------------
def _ran(cfg, now, last=None, state_text=None):
    seen = []
    jobs_ = (schedule.Job("poll", "poll", lambda c: seen.append("poll")),
             schedule.Job("credentials", "nightly", lambda c: seen.append("credentials")))
    path = schedule.state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state_text is not None:
        path.write_text(state_text)
    elif last is not None:
        path.write_text(json.dumps({"poll": last, "credentials": last}))
    schedule.tick(now, config=cfg, jobs=jobs_)
    return seen


NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '{"poll": 5}', "\x00\x01"])
def test_an_unreadable_schedule_state_runs_the_jobs_rather_than_stalling(cfg, text):
    assert _ran(cfg, NOW, state_text=text) == ["poll", "credentials"]


def test_a_schedule_state_from_the_past_runs_the_jobs(cfg):
    """The control for the skew test below: last ran a day ago, so both jobs are due."""
    assert _ran(cfg, NOW, last="2026-10-13T08:00:00Z") == ["poll", "credentials"]


@pytest.mark.xfail(strict=True, reason="ROB-faults-6: schedule.due() trusts a last-run time in the future; after the "
                                       "clock steps back (or ran fast once) poll and the nightly jobs stop until the "
                                       "clock catches up, with no alert or log line")
def test_a_last_run_in_the_future_does_not_stall_the_schedule(cfg):
    assert _ran(cfg, NOW, last="2026-10-21T09:00:00Z") == ["poll", "credentials"]


@pytest.mark.xfail(strict=True, reason="ROB-faults-8: one malformed last-run time in schedule.json makes every tick "
                                       "raise ValueError (load_state checks the JSON, not the times), so the schedule "
                                       "stops for good; serve logs 'tick failed' every 30 s")
@pytest.mark.parametrize("bad", ["2026-13-45T99:00:00Z", "yesterday"])
def test_a_garbled_last_run_time_does_not_stop_every_later_job(cfg, bad):
    """One malformed time in schedule.json (a hand edit, a torn write): tick must not raise before any job runs."""
    try:
        seen = _ran(cfg, NOW, state_text=json.dumps({"poll": bad, "credentials": "2026-10-13T08:00:00Z"}))
    except ValueError as e:
        pytest.fail("tick raised on a garbled last-run time: %s" % e)
    assert "credentials" in seen


# ----- facts stamped in the future ----------------------------------------------------------------------------------
def test_human_age_of_a_normal_fact():
    assert human_age(90) == "90 s" and human_age(3 * 3600) == "3 h"


@pytest.mark.xfail(strict=True, reason="ROB-faults-7: a fact stamped in the future (clock skew between hosts or a "
                                       "stepped clock) prints a negative age such as '-3600 s' and reads as fresh; "
                                       "the CLI clamps the same case to 0")
def test_a_fact_from_the_future_is_shown_as_clock_skew_not_as_fresh():
    text = human_age(-3600)
    assert not text.startswith("-") and ("skew" in text.lower() or "future" in text.lower()), text
