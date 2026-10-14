"""Helpers for the load tests (robustness angle: load; imported by the test_load_*.py modules).

The load tests grow a scratch store to a realistic volume and check how the cost of a read grows with it. Wall
clock is noisy on a shared login server, so the scaling tests count SQLite virtual-machine steps instead
(`OpCounter`): deterministic, and a quadratic query shows as a ratio of about 4 when its input doubles, a linear
one as about 2. Wall-clock budgets are kept only where a person waits on the result, and are generous.

Measured on <host> (2026-10-14) with the phase-1 demo data, for context of the budgets:
    1000 walks of one 44-pin configuration     db.latest("pin_state") 33.7 s; every shell page over 30 s
    100 walks (about one day of the 15-minute poll)   / 9.9 s, /alerts 3.6 s, /mirrors 3.3 s
    one quality_finding document of 50k findings  /alerts 10 s, /quality/ 43 s
"""
from __future__ import annotations

import copy
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from nanosoc_rm import contracts, db

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"


def examples(name: str, kind: str | None = None) -> list[dict]:
    return [d for _, d in contracts.docs_in(EXAMPLES / name) if kind is None or d["kind"] == kind]


def store_config(tmp_path: Path) -> Path:
    path = tmp_path / "rm.toml"
    path.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    return path


class OpCounter:
    """Counts SQLite VM steps on every connection opened through sqlite3.connect while installed."""

    STEP = 100

    def __init__(self) -> None:
        self.ops = 0
        self.connections = 0

    def _tick(self) -> int:
        self.ops += self.STEP
        return 0

    @contextmanager
    def installed(self, monkeypatch) -> Iterator["OpCounter"]:
        real = sqlite3.connect

        def connect(*a, **k):
            con = real(*a, **k)
            self.connections += 1
            con.set_progress_handler(self._tick, self.STEP)
            return con
        monkeypatch.setattr(sqlite3, "connect", connect)
        try:
            yield self
        finally:
            monkeypatch.setattr(sqlite3, "connect", real)


def add_pin_snapshots(cfg: Path, configuration: str, walks: int, pins: list[dict] | None = None) -> None:
    """`walks` complete pin_state snapshots of `configuration`, as `walk --store` leaves them after that many polls.
    The first goes through db.put_snapshot (so the documents are known to validate); the rest are the same
    rows written in one transaction, to keep the test fast."""
    pins = pins or examples("pin-state.example.jsonl", "pin_state")
    scope = "pin_state:%s" % configuration

    def docs(sid: str) -> list[dict]:
        out = []
        for p in pins:
            d = copy.deepcopy(p)
            d["fact"]["configuration"] = configuration
            d["snapshot"] = {"id": sid, "scope": scope}
            d["subject"] = db.subject_of(d)
            out.append(d)
        return out
    db.put_snapshot(scope, docs(db.new_snapshot_id(scope)), config=cfg)
    with db.connect(cfg) as con, con:
        for i in range(1, walks):
            sid = "%s@2026-10-%02dT%02d:%02d:00Z#%s" % (scope, 1 + i // 1440, (i // 60) % 24, i % 60,
                                                        secrets.token_hex(4))
            con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES (?, ?, 'pin_state', ?)",
                        (sid, scope, "2026-10-14T00:00:00.%06d+00:00" % i))
            for d in docs(sid):
                db._insert(con, d)


def add_configurations(cfg: Path, n: int, pins: list[dict] | None = None) -> list[str]:
    """One walk each of `n` configurations cfg-000.. (the first through db.put_snapshot, the rest as the same rows in
    one transaction). Returns their ids."""
    pins = pins or examples("pin-state.example.jsonl", "pin_state")
    ids = ["cfg-%03d" % i for i in range(n)]
    add_pin_snapshots(cfg, ids[0], 1, pins)
    with db.connect(cfg) as con, con:
        for c in ids[1:]:
            scope = "pin_state:%s" % c
            sid = db.new_snapshot_id(scope)
            con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES (?, ?, 'pin_state', ?)",
                        (sid, scope, "2026-10-14T00:00:00.000000+00:00"))
            for p in pins:
                d = copy.deepcopy(p)
                d["fact"]["configuration"] = c
                d["snapshot"] = {"id": sid, "scope": scope}
                d["subject"] = db.subject_of(d)
                db._insert(con, d)
    return ids


def quality_doc(target: str, commit: str, findings: int, observed_at: str) -> dict:
    """A quality_finding observation for `target` at `commit` with `findings` findings (built from the example)."""
    q = copy.deepcopy(examples("observation.quality.example.jsonl", "quality_finding")[0])
    f = q["fact"]
    f.update(target=target, commit=commit, worktree=False)
    tmpl, out = f["findings"], []
    for j in range(findings):
        x = copy.deepcopy(tmpl[j % len(tmpl)])
        x["path"] = "src/rtl/f%04d.v" % (j // 20)
        x["line"] = 10 + (j % 20) * 7
        x["end_line"] = x["line"] + 1
        x["edit"].update(start=x["line"], end=x["end_line"])
        x["context"]["start"] = x["line"] - 1
        out.append(x)
    f["findings"] = out
    f["counts"] = {k: sum(1 for x in out if x["kind"] == k) for k in f["counts"]}
    f["total"] = findings
    rule = f["bulk_rules"][0]
    rule["count"] = rule["edits"] = f["counts"]["banner"] + f["counts"]["boilerplate"]
    rule["by_kind"] = {"banner": f["counts"]["banner"], "boilerplate": f["counts"]["boilerplate"]}
    rule["files"] = len({x["path"] for x in out if x["kind"] in ("banner", "boilerplate")})
    q["observed_at"] = observed_at
    q["subject"] = db.subject_of(q)
    return q


class DocCounter:
    """Counts the stored documents db decodes (json.loads of a `doc` column), by kind."""

    def __init__(self) -> None:
        self.by_kind: dict[str, int] = {}
        self.bytes = 0

    def install(self, monkeypatch) -> "DocCounter":
        real = json.loads
        outer = self

        class Proxy:
            def __getattr__(self, name):
                return getattr(json, name)

            @staticmethod
            def loads(s, *a, **k):
                d = real(s, *a, **k)
                if isinstance(d, dict) and "kind" in d:
                    outer.by_kind[d["kind"]] = outer.by_kind.get(d["kind"], 0) + 1
                    outer.bytes += len(s)
                return d
        monkeypatch.setattr(db, "json", Proxy())
        return self
