"""Load: the store (nanosoc_rm.db) as its history grows (interfaces.md section 12; README section 4).

The 15-minute poll stores a complete pin_state snapshot of every configuration and a mirror_status snapshot of
every repo each time it runs (about 96 a day), and section 12 keeps all history. So the cost of reading the
current facts must not grow faster than the history does. Measured with loadfix.OpCounter (SQLite VM steps).
"""
from __future__ import annotations

import sqlite3

import pytest

from nanosoc_rm import db

import loadfix as L


def _latest_ops(monkeypatch, cfg, kind, prefix=""):
    c = L.OpCounter()
    with c.installed(monkeypatch):
        n = len(db.latest(kind, prefix, config=cfg))
    return c.ops, n


def test_current_pins_cost_at_most_linear_in_the_walk_history(tmp_path, monkeypatch):
    """Doubling the number of stored walks of one configuration at most doubles the cost of reading its current
    pins. Today it quadruples: 1000 walks of eth-rc4's 44 pins take 33.7 s per db.latest call."""
    small, large = tmp_path / "small", tmp_path / "large"
    small.mkdir(), large.mkdir()
    c_small, c_large = L.store_config(small), L.store_config(large)
    L.add_pin_snapshots(c_small, "eth-rc4", 60)
    L.add_pin_snapshots(c_large, "eth-rc4", 120)
    ops_small, n_small = _latest_ops(monkeypatch, c_small, "pin_state", "pin_state:eth-rc4:")
    ops_large, n_large = _latest_ops(monkeypatch, c_large, "pin_state", "pin_state:eth-rc4:")
    assert n_small == n_large == len(L.examples("pin-state.example.jsonl", "pin_state"))
    assert ops_large <= 2.6 * ops_small, (ops_small, ops_large)


def test_current_pins_of_one_configuration_among_500_cost_little(tmp_path, monkeypatch):
    """500 registered configurations with one walk each (1500 pins): reading one configuration's pins touches
    each pin row about once, and every configuration's current pins come back. Budget: 20 VM steps per stored
    row (measured: 6 for one configuration, 48 for all of them)."""
    cfg = L.store_config(tmp_path)
    L.add_configurations(cfg, 500)
    rows = 500 * len(L.examples("pin-state.example.jsonl", "pin_state"))
    ops_one, n_one = _latest_ops(monkeypatch, cfg, "pin_state", "pin_state:cfg-007:")
    assert n_one == rows // 500
    assert ops_one <= 20 * rows, ops_one
    ops_all, n_all = _latest_ops(monkeypatch, cfg, "pin_state")
    assert n_all == rows
    assert ops_all <= 100 * rows, ops_all


def test_current_alerts_cost_linear_in_the_alert_history(tmp_path, monkeypatch):
    """An event kind (here `alert`) keeps every raise; reading the current ones grows linearly with them."""
    def grow(path, n):
        path.mkdir()
        cfg = L.store_config(path)
        with db.connect(cfg) as con, con:
            for i in range(n):
                at = "2026-10-%02dT%02d:%02d:00Z" % (1 + i // 1440, (i // 60) % 24, i % 60)
                d = {"schema": 1, "kind": "alert", "subject": "", "observed_at": at,
                     "sources": [{"kind": "derived", "command": "nanosoc-rm mirror sync"}],
                     "fact": {"alert_kind": "mirror_failed", "about": "repo-%d" % (i % 400), "raised_at": at,
                              "cleared_at": None, "cleared_by": None, "message": "fetch failed"}}
                d["subject"] = db.subject_of(d)
                if i == 0:
                    assert db.check_observation(d) == []
                db._insert(con, d)
        return cfg
    a, b = grow(tmp_path / "a", 1000), grow(tmp_path / "b", 2000)
    ops_a, n_a = _latest_ops(monkeypatch, a, "alert")
    ops_b, n_b = _latest_ops(monkeypatch, b, "alert")
    assert n_a == n_b == 400
    assert ops_b <= 2.6 * ops_a, (ops_a, ops_b)


def test_a_read_does_not_wait_for_a_writer(tmp_path, monkeypatch):
    """WAL mode lets readers run while a writer holds its lock (a walk or a quality --store mid-transaction).
    db.latest is a read; it must answer while another connection holds a write transaction open. Today it
    queues for the write lock and, after sqlite's timeout (30 s in db.connect), fails 'database is locked'."""
    cfg = L.store_config(tmp_path)
    L.add_pin_snapshots(cfg, "eth-rc4", 1)
    path = db.database_path(cfg)
    writer = sqlite3.connect(str(path), isolation_level=None)
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **dict(k, timeout=0.2)))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO actions (at, login, action, target, result) VALUES ('t', 'w', 'run_t0', 'x', 'ok')")
        assert len(db.latest("pin_state", config=cfg)) == 3
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_a_read_commits_nothing(tmp_path):
    """Another connection's PRAGMA data_version changes only when someone commits. Reading must not."""
    cfg = L.store_config(tmp_path)
    L.add_pin_snapshots(cfg, "eth-rc4", 1)
    watcher = sqlite3.connect(str(db.database_path(cfg)))
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(3):
            db.latest("pin_state", config=cfg)
        assert watcher.execute("PRAGMA data_version").fetchone()[0] == before
    finally:
        watcher.close()
