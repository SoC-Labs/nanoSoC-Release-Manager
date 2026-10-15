"""Store retention, keep-on-change (CR-1015-retention; interfaces.md section 12; A3, ROB-faults-4).

A producer run whose content equals the scope's current snapshot stores no rows: the current snapshot records the
confirmation, and `latest` returns exactly what the run produced (check times, snapshot id). `nanosoc-rm
store-maint compact` collapses runs of identical snapshots stored before the rule.

Mutations each test here is meant to catch (PR6; measured in the commit message):
  M1  put_snapshot never confirms (every run stored, the old behaviour): the 100-polls test.
  M2  put_snapshot confirms whenever the scope has a current snapshot, content or not: the change test.
  M3  latest returns the stored rows without the confirmation: the data-age test.
  M4  the run is compared with the newest snapshot by wall-clock completed_at, not the current one: the clock test.
  M5  compaction drops the run's last check (no confirmation on the kept snapshot): the compaction test.
  M6  --dry-run commits: the dry-run test.
  M7  latest sorts by stored rowid only (a confirmed scope reads first, out of the old order): the order test.
"""
from __future__ import annotations

import copy
import datetime as dt
import sqlite3

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, clock, contracts, db

from conftest import EXAMPLES

T0 = dt.datetime(2026, 10, 15, 0, 0, tzinfo=dt.timezone.utc)
SCOPE = "pin_state:eth-rc4"
PINS = [d for _, d in contracts.docs_in(EXAMPLES / "pin-state.example.jsonl")]
_runs = iter(range(1, 10 ** 6))


def poll(t: dt.datetime, tag: str | None = None, cfg: str = "eth-rc4") -> list[dict]:
    """One walk of `cfg` at `t`, as the walker builds it: every document stamped with this run's check times
    (envelope at t, host states 30 s later) and snapshot id. `tag` changes the content (a new tag on one host)."""
    scope = "pin_state:%s" % cfg
    sid = "%s@%s#%08x" % (scope, clock.iso(t), next(_runs))
    docs = copy.deepcopy(PINS)
    for d in docs:
        d["fact"]["configuration"] = cfg
        d["subject"] = db.subject_of(d)
        d["snapshot"]["scope"] = scope
        d["observed_at"] = clock.iso(t)
        d["snapshot"]["id"] = sid
        for h in d["fact"]["hosts"].values():
            h["observed_at"] = clock.iso(t + dt.timedelta(seconds=30))
    if tag:
        docs[0]["fact"]["hosts"]["gitlab"]["tags"].append(tag)
    return docs


def put(t: dt.datetime, tag: str | None = None, cfg: str = "eth-rc4") -> list[dict]:
    docs = poll(t, tag, cfg)
    db.put_snapshot("pin_state:%s" % cfg, docs)
    return docs


def q(rm_config, sql):
    con = sqlite3.connect(str(rm_config.parent / "data" / "rm.sqlite"))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def stored_bytes(rm_config) -> int:
    return q(rm_config, "SELECT coalesce(sum(length(doc)), 0) FROM observations")[0][0]


def every(i: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=15 * i)


# ----- keep-on-change ---------------------------------------------------------------------------------------------
def test_a_hundred_identical_polls_store_one_copy_and_read_as_the_last_poll(rm_config):
    put(every(0))
    one_copy = stored_bytes(rm_config)
    for i in range(1, 99):
        put(every(i))
    last = put(every(99))
    assert q(rm_config, "SELECT count(*) FROM observations")[0][0] == len(PINS)
    assert stored_bytes(rm_config) == one_copy
    assert q(rm_config, "SELECT count(*), sum(confirmations) FROM snapshots") == [(1, 99)]
    # exactly what the old code returned: the last run's documents, its snapshot id and check times
    assert db.latest("pin_state") == last
    assert db.latest("pin_state", "pin_state:eth-rc4:") == last


def test_a_change_stores_a_new_snapshot_and_history_keeps_each_state_first_and_last(rm_config):
    first = put(every(0))
    put(every(1))
    last_a = put(every(2))
    b = put(every(3), tag="keep/eth-rc4/new")
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 2
    assert db.latest("pin_state") == b
    a_again = put(every(4))                                   # back to A: a change again, so a third snapshot
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 3
    assert db.latest("pin_state") == a_again
    subject = PINS[0]["subject"]
    assert db.history(subject) == [first[0], last_a[0], b[0], a_again[0]]


def test_the_data_age_is_the_last_check_not_the_first_store(rm_config):
    put(every(0))
    for i in range(1, 8):
        put(every(i))
    now = every(8)
    assert db.data_age(db.latest("pin_state"), now) == dt.timedelta(minutes=15)
    assert {d["observed_at"] for d in db.latest("pin_state")} == {clock.iso(every(7))}


def test_the_current_snapshot_is_the_last_committed_when_the_clock_steps_back(rm_config, monkeypatch):
    """ROB-faults-4 with keep-on-change: a run is compared with the CURRENT snapshot (last committed), never with
    the one whose wall-clock completed_at is newest; reads show the last committed run."""
    late, early = T0 + dt.timedelta(hours=2), T0
    monkeypatch.setattr(db, "_now_exact", lambda: late.isoformat())
    put(late)                                                 # A at 02:00
    monkeypatch.setattr(db, "_now_exact", lambda: early.isoformat())      # the clock steps back two hours
    a_back = put(early)                                       # A again: a confirmation, read as committed
    assert db.latest("pin_state") == a_back
    b = put(early + dt.timedelta(minutes=15), tag="keep/eth-rc4/b")       # B: new snapshot, current
    assert db.latest("pin_state") == b
    a = put(early + dt.timedelta(minutes=30))                 # A again: compared with B (current), so stored
    assert db.latest("pin_state") == a
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 3


def test_reads_keep_the_order_the_old_rule_stored_rows_in(rm_config, monkeypatch):
    """`latest` across scopes is in stored order. A confirmed run sits where its rows would have been stored, so
    a scope confirmed after another scope changed reads after it, as before; compaction keeps that order too."""
    def sequence():
        put(every(0), cfg="x")
        put(every(0), cfg="y")
        put(every(1), cfg="x")
        y = put(every(1), tag="b", cfg="y")
        x = put(every(2), cfg="x")
        return y + x
    expected = sequence()
    assert db.latest("pin_state") == expected
    with db.connect() as con, con:
        con.execute("DELETE FROM observations")
        con.execute("DELETE FROM snapshots")
    with monkeypatch.context() as m:                          # the old rule, then compacted
        m.setattr(db, "_confirm", lambda *a, **k: False)
        expected = sequence()
    db.compact(apply=True)
    assert db.latest("pin_state") == expected


def test_an_empty_run_and_a_run_with_other_ids_are_compared_by_content(rm_config):
    db.put_snapshot("pin_state:x", [])
    db.put_snapshot("pin_state:x", [])
    assert q(rm_config, "SELECT count(*), sum(confirmations) FROM snapshots WHERE scope = 'pin_state:x'") == [(1, 1)]
    assert db.latest("pin_state", "pin_state:x:") == []


def test_a_put_naming_a_confirmed_run_joins_it_as_before(rm_config):
    """put(doc) adds to the snapshot it names; when that run was kept only as a confirmation it is stored in
    full first, so the result is what the old code gave."""
    put(every(0))
    last = put(every(1))
    extra = copy.deepcopy(last[2])
    extra["fact"]["tree_paths"] = ["zz-extra"]
    extra["subject"] = db.subject_of(extra)
    db.put(extra)
    assert db.latest("pin_state") == last + [extra]
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 2


def test_a_reused_snapshot_id_is_refused_even_when_it_was_only_confirmed(rm_config):
    put(every(0))
    last = put(every(1))
    with pytest.raises(db.ObservationRefused, match="already exists"):
        db.put_snapshot(SCOPE, last)


# ----- compaction ------------------------------------------------------------------------------------------------
def _old_store(rm_config, monkeypatch, plan):
    """Store `plan` ([(time, tag)]) as the code before keep-on-change did: every run in full."""
    with monkeypatch.context() as m:
        m.setattr(db, "_confirm", lambda *a, **k: False)
        return [put(t, tag) for t, tag in plan]


PLAN = [(every(0), None), (every(1), None), (every(2), None), (every(3), "b"), (every(4), "b"), (every(5), None),
        (every(6), None)]


def test_compaction_keeps_every_distinct_state_with_its_first_and_last_times(rm_config, monkeypatch):
    runs = _old_store(rm_config, monkeypatch, PLAN)
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 7
    current = db.latest("pin_state")
    report = db.compact(apply=True)
    assert [(r["kind"], r["snapshots_removed"], r["rows_removed"]) for r in report] == [("pin_state", 4, 4 * 3)]
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 3
    assert db.latest("pin_state") == current == runs[-1]
    for n in range(len(PINS)):
        assert db.history(PINS[n]["subject"]) == [runs[0][n], runs[2][n], runs[3][n], runs[4][n], runs[5][n],
                                                  runs[6][n]]
    assert q(rm_config, "SELECT confirmations FROM snapshots ORDER BY rowid") == [(2,), (1,), (1,)]
    # safe to rerun: nothing left to collapse, nothing changes
    before = q(rm_config, "SELECT * FROM snapshots ORDER BY rowid")
    assert sum(r["snapshots_removed"] for r in db.compact(apply=True)) == 0
    assert q(rm_config, "SELECT * FROM snapshots ORDER BY rowid") == before
    # and keep-on-change carries on from the compacted store
    nxt = put(every(7))
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 3 and db.latest("pin_state") == nxt


def test_compaction_folds_a_run_that_already_ends_in_a_confirmation(rm_config, monkeypatch):
    runs = _old_store(rm_config, monkeypatch, PLAN[:2])
    last = put(every(2))                                      # new code: a confirmation of the second old row
    report = db.compact(apply=True)
    assert sum(r["snapshots_removed"] for r in report) == 1
    assert db.latest("pin_state") == last
    assert db.history(PINS[0]["subject"]) == [runs[0][0], last[0]]
    assert q(rm_config, "SELECT confirmations FROM snapshots") == [(2,)]


def test_a_dry_run_reports_the_apply_and_changes_nothing(rm_config, monkeypatch):
    _old_store(rm_config, monkeypatch, PLAN)
    before = (q(rm_config, "SELECT * FROM snapshots ORDER BY rowid"), q(rm_config, "SELECT count(*) FROM observations"))
    dry = db.compact(apply=False)
    assert q(rm_config, "SELECT * FROM snapshots ORDER BY rowid") == before[0]
    assert q(rm_config, "SELECT count(*) FROM observations") == before[1]
    assert dry == db.compact(apply=True)
    assert dry[0]["bytes_removed"] > 0


def test_compaction_is_one_transaction(rm_config, monkeypatch):
    _old_store(rm_config, monkeypatch, PLAN)
    _old_store(rm_config, monkeypatch, [(t, None) for t, _ in PLAN[:3]])
    db.put_snapshot("mirror_status:x", [])
    calls = []
    real = db._compact_scope

    def second_scope_fails(con, scope, kind, row):
        calls.append(scope)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        real(con, scope, kind, row)

    before = q(rm_config, "SELECT count(*) FROM observations")
    monkeypatch.setattr(db, "_compact_scope", second_scope_fails)
    with pytest.raises(RuntimeError):
        db.compact(apply=True)
    assert q(rm_config, "SELECT count(*) FROM observations") == before      # the first scope's work rolled back


def test_a_scope_whose_current_is_not_its_last_stored_is_skipped(rm_config, monkeypatch):
    _old_store(rm_config, monkeypatch, PLAN[:3])
    first = q(rm_config, "SELECT id FROM snapshots ORDER BY rowid LIMIT 1")[0][0]
    with db.connect() as con, con:
        con.execute("UPDATE current_snapshots SET snapshot_id = ? WHERE scope = ?", (first, SCOPE))
    report = db.compact(apply=True)
    assert report[0]["skipped"] == [SCOPE] and report[0]["snapshots_removed"] == 0


def test_the_cli_needs_one_of_dry_run_and_apply(rm_config, monkeypatch):
    _old_store(rm_config, monkeypatch, PLAN)
    run = lambda *a: CliRunner().invoke(cli.main, ["store-maint", "compact", *a])  # noqa: E731
    assert run().exit_code == 3 and run("--dry-run", "--apply").exit_code == 3
    r = run("--dry-run")
    assert r.exit_code == 0 and "dry run, nothing changed: 4 snapshot(s), 12 row(s)" in r.output
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 7
    r = run("--apply", "--json")
    assert r.exit_code == 0 and '"snapshots_removed":4' in r.output
    assert q(rm_config, "SELECT count(*) FROM snapshots")[0][0] == 3
