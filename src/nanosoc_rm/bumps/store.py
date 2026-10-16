"""The bump engine's own records: `<data_dir>/bumps.sqlite` (interfaces.md section 19.4).

Not rebuildable, not facts, so they are kept apart from `rm.sqlite` (as jobs.sqlite is):

- `proposals`: every proposal the engine computed in shadow mode, by id: first and last time it was computed, the
  configurations it was computed for, and its latest bump_proposal observation. The shadow ledger compares these
  with what people later did; a proposal computed once stays here even when a later scan no longer makes it.
- `marks`: a person's word on a proposal ("done": I applied it; "declined": I will not), with the login, the time
  and a note. Every mark is a write action and is logged here with its login (H2).
- `proposal_docs`: the latest bump_proposal observation of each proposal per configuration. One move (same consumer
  branch, path, from and to) can be proposed for two configurations; each keeps its own document (its tree paths,
  order, waits_for and matrix pins are that configuration's), so the last scan never overwrites the other.
- `scans`: the latest bump_scan observation per configuration (what the queue page shows).
- `ledger`: the latest bump_ledger observation, computed by the scan job or `bumps ledger --store` (never inside a
  web request: it reads the mirrors).

Writes go only to this file on the service's own disk (interfaces.md section 0.4).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from nanosoc_rm import clock
from nanosoc_rm.config import Config

__all__ = ["path", "connect", "record", "record_scan", "record_ledger", "ledger", "proposals", "proposal", "scans",
           "scan", "mark", "marks", "DECISIONS"]

DECISIONS = ("done", "declined")
_DDL = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, configurations TEXT NOT NULL,
    doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS marks (
    n INTEGER PRIMARY KEY AUTOINCREMENT, proposal TEXT NOT NULL, at TEXT NOT NULL, login TEXT NOT NULL,
    decision TEXT NOT NULL, note TEXT);
CREATE INDEX IF NOT EXISTS marks_proposal ON marks (proposal);
CREATE TABLE IF NOT EXISTS scans (configuration TEXT PRIMARY KEY, at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposal_docs (
    id TEXT NOT NULL, configuration TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (id, configuration));
CREATE TABLE IF NOT EXISTS ledger (one INTEGER PRIMARY KEY CHECK (one = 1), at TEXT NOT NULL, doc TEXT NOT NULL);
"""


def path(config: Config) -> Path:
    return config.data_dir / "bumps.sqlite"


@contextlib.contextmanager
def connect(config: Config) -> Iterator[sqlite3.Connection]:
    from nanosoc_rm.db import private_store
    p = private_store(path(config))
    con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_DDL)
        yield con
    finally:
        con.close()


def record(config: Config, docs: list[dict], configuration: str, at: str | None = None) -> None:
    """Keep each bump_proposal observation, merging the configuration into its list."""
    at = at or clock.iso()
    with connect(config) as con:
        con.execute("BEGIN IMMEDIATE")
        for d in docs:
            pid = d["fact"]["id"]
            r = con.execute("SELECT configurations FROM proposals WHERE id = ?", (pid,)).fetchone()
            if r is None:
                con.execute("INSERT INTO proposals (id, first_seen, last_seen, configurations, doc) VALUES (?, ?, ?, ?, ?)",
                            (pid, at, at, json.dumps([configuration]), json.dumps(d, separators=(",", ":"))))
            else:
                confs = sorted(set(json.loads(r["configurations"])) | {configuration})
                con.execute("UPDATE proposals SET last_seen = ?, configurations = ?, doc = ? WHERE id = ?",
                            (at, json.dumps(confs), json.dumps(d, separators=(",", ":")), pid))
            con.execute("INSERT OR REPLACE INTO proposal_docs (id, configuration, doc) VALUES (?, ?, ?)",
                        (pid, configuration, json.dumps(d, separators=(",", ":"))))
        con.execute("COMMIT")


def record_ledger(config: Config, doc: dict, at: str | None = None) -> None:
    with connect(config) as con:
        con.execute("INSERT OR REPLACE INTO ledger (one, at, doc) VALUES (1, ?, ?)",
                    (at or clock.iso(), json.dumps(doc, separators=(",", ":"))))


def ledger(config: Config) -> tuple[dict | None, str | None]:
    """(the stored bump_ledger observation, when it was computed), or (None, None)."""
    if not path(config).is_file():
        return None, None
    with connect(config) as con:
        r = con.execute("SELECT at, doc FROM ledger WHERE one = 1").fetchone()
    return (json.loads(r["doc"]), r["at"]) if r is not None else (None, None)


def record_scan(config: Config, doc: dict) -> None:
    with connect(config) as con:
        con.execute("INSERT OR REPLACE INTO scans (configuration, at, doc) VALUES (?, ?, ?)",
                    (doc["fact"]["configuration"], doc["observed_at"], json.dumps(doc, separators=(",", ":"))))


def _proposal_row(r: sqlite3.Row, docs: dict[str, dict]) -> dict:
    doc = json.loads(r["doc"])
    return {"id": r["id"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "configurations": json.loads(r["configurations"]), "doc": doc,
            "docs": docs or {doc["fact"]["configuration"]: doc}}


def _docs(con: sqlite3.Connection, pid: str | None = None) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    rows = con.execute("SELECT id, configuration, doc FROM proposal_docs %s ORDER BY configuration"
                       % ("WHERE id = ?" if pid else ""), (pid,) if pid else ())
    for r in rows:
        out.setdefault(r["id"], {})[r["configuration"]] = json.loads(r["doc"])
    return out


def proposals(config: Config) -> list[dict]:
    """Every stored proposal, oldest first: `doc` is the latest computed, `docs` each configuration's."""
    if not path(config).is_file():
        return []
    with connect(config) as con:
        docs = _docs(con)
        return [_proposal_row(r, docs.get(r["id"], {}))
                for r in con.execute("SELECT * FROM proposals ORDER BY first_seen, id")]


def proposal(config: Config, pid: str) -> dict | None:
    if not path(config).is_file():
        return None
    with connect(config) as con:
        r = con.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return _proposal_row(r, _docs(con, pid).get(pid, {})) if r is not None else None


def scans(config: Config) -> list[dict]:
    if not path(config).is_file():
        return []
    with connect(config) as con:
        return [json.loads(r["doc"]) for r in con.execute("SELECT doc FROM scans ORDER BY configuration")]


def scan(config: Config, configuration: str) -> dict | None:
    if not path(config).is_file():
        return None
    with connect(config) as con:
        r = con.execute("SELECT doc FROM scans WHERE configuration = ?", (configuration,)).fetchone()
    return json.loads(r["doc"]) if r is not None else None


def mark(config: Config, pid: str, login: str, decision: str, note: str | None = None, at: str | None = None) -> dict:
    """Record a person's word on a proposal. ValueError for an unknown decision, an empty login or proposal."""
    if decision not in DECISIONS:
        raise ValueError("decision must be one of %s" % ", ".join(DECISIONS))
    if not login:
        raise ValueError("a mark needs the login that made it (H2)")
    at = at or clock.iso()
    with connect(config) as con:
        if con.execute("SELECT 1 FROM proposals WHERE id = ?", (pid,)).fetchone() is None:
            raise KeyError(pid)
        con.execute("INSERT INTO marks (proposal, at, login, decision, note) VALUES (?, ?, ?, ?, ?)",
                    (pid, at, login, decision, (note or "").strip()[:500] or None))
    return {"proposal": pid, "at": at, "login": login, "decision": decision, "note": (note or "").strip()[:500] or None}


def marks(config: Config, pid: str | None = None) -> list[dict]:
    """Marks, oldest first (one proposal's, or all)."""
    if not path(config).is_file():
        return []
    with connect(config) as con:
        rows = con.execute("SELECT proposal, at, login, decision, note FROM marks %s ORDER BY n"
                           % ("WHERE proposal = ?" if pid else ""), (pid,) if pid else ())
        return [dict(r) for r in rows]
