#!/usr/bin/env python3
"""Measure rm.sqlite growth over 24 h of polls on a synthetic store, with and without keep-on-change
(CR-1015-retention). Hermetic: a temporary store, no network, no service.

    .venv/bin/python scripts/measure_store_growth.py [--configs 6] [--pins 30] [--components 30]
                                                     [--changes-per-day 24] [--keep DIR]

Cadence (nanosoc_rm.schedule, [schedule] defaults): every poll_minutes (15), so 96 runs a day, of
  poll            one mirror_status snapshot per component; one pin_state snapshot (walk) per configuration;
                  one host_compare snapshot per component with a copy; silicon_protection per silicon configuration
  freshness_scan  one freshness snapshot per configuration (tree document + one per pin)
  inventory       one repo_inventory snapshot per host
  runner          one runner_settings snapshot
Nightly jobs (credentials, org_scan, store_index) and event kinds are left out: they are not the growth.

Every document is built from the contract examples (schemas/examples) and stamped with the run's check times and
snapshot id, as the producers do. `--changes-per-day` spreads that many content changes over the day (one
configuration gains a tag: its walk and its freshness snapshot change); freshness's `judged_on` date also rolls
over once (the day starts at 12:00 UTC). Prints MB/day for the old rule (every run stored) and keep-on-change.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from nanosoc_rm import clock, contracts, db  # noqa: E402

EX = REPO / "schemas" / "examples"


def _examples(name: str, kind: str) -> list[dict]:
    return [d for _, d in contracts.docs_in(EX / name) if d["kind"] == kind]


class World:
    """The scopes one poll writes, as {scope: [documents]} with placeholder times."""

    def __init__(self, configs: int, pins: int, components: int):
        pin = _examples("pin-state.example.jsonl", "pin_state")[0]
        fresh_tree, fresh_pin = _examples("observation.freshness.example.jsonl", "freshness")[:2]
        mirror = _examples("observation.example.jsonl", "mirror_status")[0]
        hosts = _examples("observation.example.jsonl", "host_compare")[0]
        sil = _examples("observation.example.jsonl", "silicon_protection")
        inv = _examples("observation.example.jsonl", "repo_inventory")[0]
        runner = _examples("observation.example.jsonl", "runner_settings")[0]
        self.scopes: dict[str, list[dict]] = {}
        self.configs = ["cfg%d" % c for c in range(configs)]
        for cfg in self.configs:
            docs, fdocs = [], [self._fresh_tree(fresh_tree, cfg)]
            for p in range(pins):
                d = copy.deepcopy(pin)
                d["fact"]["configuration"] = cfg
                d["fact"]["tree_paths"] = ["sub%02d/lib" % p]
                docs.append(d)
                f = copy.deepcopy(fresh_pin)
                f["fact"].update(configuration=cfg, tree_path="sub%02d/lib" % p, tree_paths=["sub%02d/lib" % p])
                fdocs.append(f)
            self.scopes["pin_state:%s" % cfg] = docs
            self.scopes["freshness:%s" % cfg] = fdocs
        for c in range(components):
            d = copy.deepcopy(mirror)
            d["fact"].update(component="comp%d" % c, repo={"host": "gitlab", "path": "soclabs/comp%d" % c})
            e = copy.deepcopy(d)
            e["fact"]["repo"] = {"host": "github", "path": "SoC-Labs/comp%d" % c}
            self.scopes["mirror_status:comp%d" % c] = [d, e]
            if c % 3 == 0:
                h = copy.deepcopy(hosts)
                h["fact"]["component"] = "comp%d" % c
                self.scopes["host_compare:comp%d" % c] = [h]
        for cfg in self.configs[:2]:
            rows = []
            for d in sil:
                d = copy.deepcopy(d)
                d["fact"]["configuration"] = cfg
                rows.append(d)
            self.scopes["silicon_protection:%s" % cfg] = rows
        for host in ("gitlab", "github"):
            rows = []
            for c in range(components + 10):
                d = copy.deepcopy(inv)
                d["fact"]["repo"] = {"host": host, "path": "soclabs/repo%d" % c}
                rows.append(d)
            self.scopes["repo_inventory:%s" % host] = rows
        self.scopes["runner_settings:9001"] = [copy.deepcopy(runner)]
        for scope, docs in self.scopes.items():
            for d in docs:
                d["snapshot"] = {"id": "", "scope": scope}
                d["subject"] = db.subject_of(d)

    @staticmethod
    def _fresh_tree(tree: dict, cfg: str) -> dict:
        d = copy.deepcopy(tree)
        d["fact"]["configuration"] = cfg
        return d

    def change(self, cfg: str, n: int) -> None:
        """A content change in one configuration: a new tag on its first pin (walk and freshness differ)."""
        self.scopes["pin_state:%s" % cfg][0]["fact"]["hosts"]["gitlab"]["tags"].append("keep/%s/r%d" % (cfg, n))
        self.scopes["freshness:%s" % cfg][1]["fact"]["branches"].append("change-%d" % n)

    def run(self, t: dt.datetime) -> None:
        """Every scope's producer run at `t`: check times, snapshot ids and the judging date stamped."""
        stamp = clock.iso(t)
        for scope, docs in self.scopes.items():
            sid = "%s@%s#%08x" % (scope, stamp, abs(hash((scope, stamp))) & 0xFFFFFFFF)
            out = []
            for d in docs:
                d = db._walk(d, lambda _tok: stamp)              # every check time is this run's
                d["snapshot"]["id"] = sid
                if d["kind"] == "freshness":
                    d["fact"]["judged_on"] = t.date().isoformat()
                out.append(d)
            db.put_snapshot(scope, out, config=self.cfg)


def size(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
    finally:
        con.close()


def measure(args, keep_on_change: bool, root: Path) -> dict:
    d = root / ("keep" if keep_on_change else "old")
    d.mkdir(parents=True)
    cfg = d / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n' % (d / "data"))
    w = World(args.configs, args.pins, args.components)
    w.cfg = cfg
    real = db._confirm
    if not keep_on_change:
        db._confirm = lambda *a, **k: False
    try:
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        w.run(start)
        base = size(d / "data" / "rm.sqlite")
        rows0 = sum(len(v) for v in w.scopes.values())
        every = max(1, 96 // args.changes_per_day) if args.changes_per_day else 0
        for i in range(1, 97):
            if every and i % every == 0:
                w.change(w.configs[i % len(w.configs)], i)
            w.run(start + dt.timedelta(minutes=15 * i))
        grown = size(d / "data" / "rm.sqlite") - base
    finally:
        db._confirm = real
    return {"rows_per_poll": rows0, "scopes": len(w.scopes), "mb_per_day": grown / 1e6}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--configs", type=int, default=6)
    ap.add_argument("--pins", type=int, default=30)
    ap.add_argument("--components", type=int, default=30)
    ap.add_argument("--changes-per-day", type=int, default=24)
    ap.add_argument("--keep", help="keep the two stores under this directory (default: a removed temp dir)")
    args = ap.parse_args()
    os.environ.pop("NANOSOC_RM_CONFIG", None)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(args.keep) if args.keep else Path(tmp)
        old = measure(args, False, root)
        new = measure(args, True, root)
    print("synthetic store: %d scopes, %d set-kind rows per poll, 96 polls (24 h at 15 min), %d content changes/day"
          % (old["scopes"], old["rows_per_poll"], args.changes_per_day))
    print("every run stored (before): %8.2f MB/day" % old["mb_per_day"])
    print("keep-on-change (after):    %8.2f MB/day" % new["mb_per_day"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
