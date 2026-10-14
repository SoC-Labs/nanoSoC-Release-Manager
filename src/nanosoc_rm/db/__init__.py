"""The store (interfaces.md section 12; README section 4; A3, A2, H2).

SQLite in WAL mode at `<service.data_dir>/rm.sqlite`. Every function finds the database through the config,
read per call (`$NANOSOC_RM_CONFIG`, or the `config=` keyword: a Config, or a path to one).

Tables (section 12): `observations`, `snapshots`, `runs`, `actions`, `alerts`. `runs` and `actions` cannot be
rebuilt from anywhere else: back them up.

What `put` enforces beyond the schema (README section 4):

1. the document validates (observation.schema.json; a pin_state also pin-state.schema.json);
2. `subject` equals the key the README table derives from the fact;
3. the envelope's `observed_at` is the oldest time inside it (for pin_state: no host state is older);
4. a set kind carries a `snapshot` whose scope starts with its kind; an event kind carries none.

Snapshots (set kinds). The current value of a scope is its latest COMPLETE snapshot, so a fact that is no
longer produced disappears. "Latest" is the one that completed last, in commit order, never by the wall clock
(table `current_snapshots`, kept by triggers; CR-ROB-store). A producer writes a whole run with `put_snapshot(scope, docs)`: one transaction,
marked complete only at the end, so a run that fails midway never becomes current. `put(doc)` of a single
set-kind document adds it to the snapshot it names and marks that snapshot complete: it is for documents that
are complete on their own (fixtures, a one-row scope such as `runner_settings:<id>`). Producers of
multi-row scopes use put_snapshot.

Event kinds (rewrite_alert, t0_result, alert, compat_result, and the CR-14 quality kinds): the latest observation per subject (newest observed_at; on a
tie, the one stored last).

Alerts: putting a kind-`alert` observation also raises its row in `alerts`; `clear_alert` clears it there and
logs the action. `alerts()` and `latest("alert")` show each alert with the clearing from that table.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from nanosoc_rm import clock, contracts
from nanosoc_rm.config import Config, load_config

__all__ = ["SET_KINDS", "EVENT_KINDS", "ACTIONS", "ObservationRefused", "as_doc", "subject_of",
           "check_observation", "database_path", "connect", "ensure_schema", "put", "put_snapshot", "new_snapshot_id",
           "latest", "newest_per", "subjects", "history",
           "data_age", "put_run", "get_run", "runs", "log_action", "actions", "raise_alert", "alerts",
           "clear_alert"]

SET_KINDS = ("pin_state", "host_compare", "containment", "silicon_protection", "mirror_status", "repo_inventory",
             "runner_settings", "credential_status", "registry_record", "branch_state",
             "repo_seen",                                                             # section 23.2 (CR-WF-catalogue)
             "freshness")                                                            # section 24 (CR-WF-fresh)
EVENT_KINDS = ("rewrite_alert", "t0_result", "alert", "compat_result",
               "quality_density", "quality_finding", "readme_check", "doc_score",      # CR-14 (Q5)
               "matrix_cell", "matrix_quarantine",                                    # D3, D8 (section 18)
               "changelog_lint", "version_plan", "tag_check", "manifest_proposal",     # CR-VER (Wave B versioning)
               "tool_release_draft", "yank_proposal", "defect_trailers",
               "release_draft", "release_verify", "fork_status",                      # CR-R1
               "regen_class",                                                         # C3, C7 (section 20)
               "store_index",                                                         # B10, G8 (section 22)
               "release_publication")                                                 # G8 (CR-ART-publish)
# interfaces.md section 14 Logging: the v1 write actions, plus the person-confirmed publishes (CR-ART-publish)
ACTIONS = ("run_t0", "clear_alert", "publish_release", "register_tapeout")
RUN_STATES_FINAL = ("accepted", "rejected", "refused", "no-bundle")

SCHEMA_VERSION = 2
# The schema, statement by statement (run once per store by `ensure_schema`, never on every connect: ROB-load-3).
# Version 2 adds `current_snapshots`: the current (last completed) snapshot of each scope, kept by triggers so
# every writer, including a direct INSERT, keeps it right (ROB-load-1). "Last" means last completed in commit order,
# never the wall clock (ROB-faults-4).
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS observations (
    kind TEXT NOT NULL, subject TEXT NOT NULL, observed_at TEXT NOT NULL, snapshot_id TEXT, doc TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS observations_kind_subject ON observations (kind, subject)",
    "CREATE INDEX IF NOT EXISTS observations_snapshot ON observations (snapshot_id)",
    """CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, completed_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS snapshots_scope ON snapshots (scope)",
    """CREATE TABLE IF NOT EXISTS runs (
    nonce TEXT PRIMARY KEY, configuration TEXT NOT NULL, "commit" TEXT NOT NULL, request BLOB NOT NULL,
    "trigger" TEXT NOT NULL, triggered_by TEXT NOT NULL, triggered_at TEXT NOT NULL,
    pipeline_id INTEGER, job_id INTEGER, state TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS actions (
    at TEXT NOT NULL, login TEXT NOT NULL, action TEXT NOT NULL, target TEXT, result TEXT)""",
    """CREATE TABLE IF NOT EXISTS alerts (
    kind TEXT NOT NULL, subject TEXT PRIMARY KEY, raised_at TEXT NOT NULL, cleared_at TEXT, cleared_by TEXT)""",
    # version 2
    """CREATE TABLE IF NOT EXISTS current_snapshots (
    scope TEXT PRIMARY KEY, kind TEXT NOT NULL, snapshot_id TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS current_snapshots_kind ON current_snapshots (kind)",
    """CREATE TRIGGER IF NOT EXISTS snapshots_complete_on_insert AFTER INSERT ON snapshots
    WHEN NEW.completed_at IS NOT NULL BEGIN
    INSERT OR REPLACE INTO current_snapshots (scope, kind, snapshot_id) VALUES (NEW.scope, NEW.kind, NEW.id); END""",
    """CREATE TRIGGER IF NOT EXISTS snapshots_complete_on_update AFTER UPDATE OF completed_at ON snapshots
    WHEN NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL BEGIN
    INSERT OR REPLACE INTO current_snapshots (scope, kind, snapshot_id) VALUES (NEW.scope, NEW.kind, NEW.id); END""",
    # Deleting the current snapshot (pruning by hand) makes the newest remaining complete one current.
    """CREATE TRIGGER IF NOT EXISTS snapshots_current_on_delete AFTER DELETE ON snapshots
    WHEN OLD.id IN (SELECT snapshot_id FROM current_snapshots WHERE scope = OLD.scope) BEGIN
    DELETE FROM current_snapshots WHERE scope = OLD.scope;
    INSERT INTO current_snapshots (scope, kind, snapshot_id)
        SELECT scope, kind, id FROM snapshots WHERE scope = OLD.scope AND completed_at IS NOT NULL
        ORDER BY rowid DESC LIMIT 1; END""",
    # Backfill from a version-1 store: the last stored complete snapshot of each scope (rows go in rowid order, so
    # the last one of a scope replaces the earlier ones).
    """INSERT OR REPLACE INTO current_snapshots (scope, kind, snapshot_id)
    SELECT scope, kind, id FROM snapshots WHERE completed_at IS NOT NULL ORDER BY rowid""",
)


class ObservationRefused(ValueError):
    """put/put_snapshot refused a document. `problems` lists every breach; nothing was stored."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("observation refused: " + "; ".join(self.problems))


def as_doc(obj: Any) -> dict:
    """A contract document from a dict or a pydantic model (interfaces.md section 17.2)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    raise TypeError("not a contract document: %r" % (obj,))


# ---------------------------------------------------------------------------------------------------------
# README section 4 rules, pure
# ---------------------------------------------------------------------------------------------------------
def _repo_key(repo: dict) -> str:
    return "%s:%s" % (repo["host"], repo["path"])


_SUBJECT = {
    "pin_state": lambda f: "%s:%s" % (f["configuration"], f["tree_paths"][0]),
    "host_compare": lambda f: "%s:%s" % (f["component"], _repo_key(f["copy"]["repo"])),
    "containment": lambda f: "%s:%s:%s:%s" % (f["query"]["component"], f["query"]["commit"], f["configuration"],
                                              f["host"]),
    "silicon_protection": lambda f: "%s:%s:%s:%s" % (f["configuration"], f["component"], f["commit"],
                                                     f["repo"]["host"]),
    "mirror_status": lambda f: _repo_key(f["repo"]),
    "repo_inventory": lambda f: _repo_key(f["repo"]),
    "repo_seen": lambda f: _repo_key(f["repo"]),                     # section 23.2: one per uncatalogued repo
    "runner_settings": lambda f: "%s" % f["runner_id"],
    "credential_status": lambda f: f["name"],
    "registry_record": lambda f: "%s#%s" % (f["file"], f["pointer"]),
    "rewrite_alert": lambda f: "%s:%s:%s" % (_repo_key(f["repo"]), f["branch"], f["old_head"]),
    "t0_result": lambda f: "%s:%s" % (f["configuration"], f["run"]["nonce"]),
    "alert": lambda f: "%s:%s" % (f["alert_kind"], f["about"]),
    "branch_state": lambda f: "%s:%s" % (_repo_key(f["repo"]), f["branch"]),
    # section 23: one tree document per configuration (`freshness:<configuration>`) and one per pin
    "freshness": lambda f: f["configuration"] + (":" + f["tree_path"] if f["level"] == "pin" else ""),
    "compat_result": lambda f: "%s:%s" % (f["configuration"], f["nonce"]),
    "matrix_cell": lambda f: "%s:%s:%s:%s" % (f["configuration"], f["component"] or "-", f["column"], f["nonce"]),
    "matrix_quarantine": lambda f: "%s:%s" % (f["configuration"], f["test"]),
    # CR-R1 (Wave B releases): event kinds, one per draft and per fork
    "release_draft": lambda f: "%s:%s" % (f["configuration"], f["version"]),
    "release_verify": lambda f: "%s:%s" % (f["configuration"], f["version"]),
    "fork_status": lambda f: "%s:%s:%s" % (f["component"], f["fork"]["host"], f["fork"]["path"]),
    # Wave D regenerator (section 20): one move of one component in one consumer configuration
    # CR-ART-publish (G8): one published manifest or tapeout record
    "release_publication": lambda f: "%s:%s:%s" % (f["project"], f["version"], f["name"]),
    "regen_class": lambda f: "%s:%s:%s:%s" % (f["component"], f["from"]["commit"], f["to"]["commit"], f["configuration"]),
    # Wave E store index (section 22): one per store
    "store_index": lambda f: f["store"],
    **{k: (lambda f: "%s:%s%s" % (f["target"], f["commit"] or "worktree",
                                  "+worktree" if f["worktree"] and f["commit"] else ""))
       for k in ("quality_density", "quality_finding", "readme_check", "doc_score")},
    # CR-VER (Wave B versioning): the quality key for per-commit kinds; per component, or per component and version
    **{k: (lambda f: "%s:%s%s" % (f["target"], f["commit"] or "worktree",
                                  "+worktree" if f["worktree"] and f["commit"] else ""))
       for k in ("changelog_lint", "version_plan", "manifest_proposal", "defect_trailers")},
    "tag_check": lambda f: f["target"],
    "tool_release_draft": lambda f: "%s:%s" % (f["target"], f["version"]),   # renamed at integration (CR-R1's release_draft)
    "yank_proposal": lambda f: "%s:%s" % (f["target"], f["version"]),
}


def subject_of(doc: dict) -> str:
    """The subject README section 4 derives from an observation's kind and fact."""
    return "%s:%s" % (doc["kind"], _SUBJECT[doc["kind"]](doc["fact"]))


def _inner_times(doc: dict) -> list[str]:
    """observed_at values inside the fact that the envelope must not be newer than (README section 4 rule 3).
    Only pin_state carries them (one per host state)."""
    if doc.get("kind") == "pin_state":
        return [h["observed_at"] for h in doc["fact"]["hosts"].values()]
    return []


def check_observation(doc: Any) -> list[str]:
    """Every reason `put` would refuse this document; [] means it would be stored."""
    try:
        doc = as_doc(doc)
    except TypeError as e:
        return [str(e)]
    problems = contracts.validate_observation(doc)
    if problems:
        return problems                                   # the rules below read fields the schema guarantees
    kind = doc["kind"]
    try:
        want = subject_of(doc)
    except (KeyError, IndexError, TypeError) as e:
        return ["the subject key cannot be derived from the fact (missing %s)" % e]
    if doc["subject"] != want:
        problems.append("subject %r is not the key README section 4 derives from the fact: %r"
                        % (doc["subject"], want))
    env = clock.parse(doc["observed_at"])
    older = [t for t in _inner_times(doc) if clock.parse(t) < env]
    if older:
        problems.append("observed_at %s is newer than the oldest observation inside it (%s): a page's data age "
                        "would flatter it (README section 4 rule 3)" % (doc["observed_at"], min(older)))
    snap = doc.get("snapshot")
    if kind in SET_KINDS:
        if not snap:
            problems.append("set kind %s needs a snapshot (README section 4)" % kind)
        elif not snap["scope"].startswith(kind + ":"):
            problems.append("snapshot scope %r does not start with %s:" % (snap["scope"], kind))
    elif snap is not None:
        problems.append("event kind %s carries no snapshot (README section 4)" % kind)
    return problems


def data_age(obs: Iterable[Any], now: dt.datetime | None = None) -> dt.timedelta:
    """now minus the OLDEST observed_at among `obs` (A3): what a page showing these facts prints as its age.
    ValueError for no facts: a page with nothing on it proves nothing."""
    times = [clock.parse(as_doc(o)["observed_at"]) for o in obs]
    if not times:
        raise ValueError("no facts, so no data age")
    return (now or clock.now()) - min(times)


# ---------------------------------------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------------------------------------
def _config(config: Config | str | Path | None) -> Config:
    return config if isinstance(config, Config) else load_config(config)


def database_path(config: Config | str | Path | None = None) -> Path:
    return _config(config).db_path


def ensure_schema(con: sqlite3.Connection, statements: Iterable[str], version: int) -> None:
    """Put the store on WAL and `statements` (idempotent DDL) at `version`, only when it is not there yet.

    The common case is two reads (`PRAGMA journal_mode`, `PRAGMA user_version`) and no write, so a reader never
    queues for the write lock behind a poll or a quality run (ROB-load-3). Otherwise, under BEGIN IMMEDIATE: check
    again, run the statements, set user_version. A store at a newer version is left as it is."""
    if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    if con.execute("PRAGMA user_version").fetchone()[0] >= version:
        return
    level = con.isolation_level
    con.isolation_level = None                            # explicit transaction control for the migration
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("PRAGMA user_version").fetchone()[0] < version:
                for stmt in statements:
                    con.execute(stmt)
                con.execute("PRAGMA user_version = %d" % int(version))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.isolation_level = level


@contextmanager
def connect(config: Config | str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection to the store (created on first use, WAL mode), closed on exit. For this package's
    modules; prefer the functions below, which keep the README section 4 rules. Opening it writes nothing once
    the store is at SCHEMA_VERSION (ensure_schema)."""
    path = database_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=30)) as con:
        con.row_factory = sqlite3.Row
        ensure_schema(con, _SCHEMA, SCHEMA_VERSION)
        yield con


def _now_exact() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _insert(con: sqlite3.Connection, doc: dict) -> None:
    snap = doc.get("snapshot") or {}
    con.execute("INSERT INTO observations (kind, subject, observed_at, snapshot_id, doc) VALUES (?, ?, ?, ?, ?)",
                (doc["kind"], doc["subject"], doc["observed_at"], snap.get("id"),
                 json.dumps(doc, separators=(",", ":"), sort_keys=True)))
    if doc["kind"] == "alert":
        _raise_row(con, doc)


def _raise_row(con: sqlite3.Connection, doc: dict) -> None:
    f = doc["fact"]
    row = con.execute("SELECT raised_at, cleared_at FROM alerts WHERE subject = ?", (doc["subject"],)).fetchone()
    if row is None:
        con.execute("INSERT INTO alerts (kind, subject, raised_at, cleared_at, cleared_by) VALUES (?, ?, ?, ?, ?)",
                    (f["alert_kind"], doc["subject"], f["raised_at"], f["cleared_at"], f["cleared_by"]))
    elif (clock.parse(f["raised_at"]) > clock.parse(row["raised_at"])
          or (row["cleared_at"] and not f["cleared_at"] and f["raised_at"] != row["raised_at"])):
        # Raised again: open again (with whatever clearing the new document carries). A new uncleared raise of a
        # cleared alert reopens it whatever its time, so a raise after the wall clock stepped back is not lost
        # (ROB-faults-5); only the very raise that was cleared (same raised_at) leaves it cleared.
        con.execute("UPDATE alerts SET raised_at = ?, cleared_at = ?, cleared_by = ? WHERE subject = ?",
                    (f["raised_at"], f["cleared_at"], f["cleared_by"], doc["subject"]))
    elif f["cleared_at"] and not row["cleared_at"]:
        con.execute("UPDATE alerts SET cleared_at = ?, cleared_by = ? WHERE subject = ?",
                    (f["cleared_at"], f["cleared_by"], doc["subject"]))


# ---------------------------------------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------------------------------------
def put(obs: Any, *, config: Config | str | Path | None = None) -> None:
    """Store one observation after checking it (module docstring rules 1 to 4); ObservationRefused otherwise.
    A set-kind document joins the snapshot it names, which is then complete (see the module docstring)."""
    doc = as_doc(obs)
    problems = check_observation(doc)
    if problems:
        raise ObservationRefused(problems)
    with connect(config) as con, con:
        snap = doc.get("snapshot")
        if snap:
            row = con.execute("SELECT scope, kind FROM snapshots WHERE id = ?", (snap["id"],)).fetchone()
            if row is None:
                con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES (?, ?, ?, ?)",
                            (snap["id"], snap["scope"], doc["kind"], _now_exact()))
            elif (row["scope"], row["kind"]) != (snap["scope"], doc["kind"]):
                raise ObservationRefused(["snapshot %s is already a %s snapshot of %s, not %s"
                                          % (snap["id"], row["kind"], row["scope"], snap["scope"])])
        _insert(con, doc)


def new_snapshot_id(scope: str) -> str:
    """A new snapshot id for one producer run of `scope`: `<scope>@<now>#<8 hex>`. The random suffix makes two
    runs of one scope in the same clock second (or under a frozen test clock) distinct; every producer that
    names its own snapshot uses this."""
    return "%s@%s#%s" % (scope, clock.iso(), secrets.token_hex(4))


def put_snapshot(scope: str, docs: Iterable[Any], *, config: Config | str | Path | None = None) -> str:
    """Store one producer run of a set kind for `scope` atomically, mark it complete, return its id.
    Every document must be of the scope's kind, name this scope, and share one snapshot id, which must be
    new. An empty run is a complete, empty snapshot (the scope now has no current facts); its id is a fresh
    `new_snapshot_id(scope)`, so two empty runs of one scope in the same second are both stored."""
    docs = [as_doc(d) for d in docs]
    kind = scope.split(":", 1)[0]
    problems = []
    if kind not in SET_KINDS or ":" not in scope:
        problems.append("scope %r is not <set kind>:<key> (set kinds: %s)" % (scope, ", ".join(SET_KINDS)))
    for i, d in enumerate(docs):
        problems += ["doc %d: %s" % (i, p) for p in check_observation(d)]
        if d.get("kind") != kind:
            problems.append("doc %d: kind %s in a %s snapshot" % (i, d.get("kind"), kind))
        if (d.get("snapshot") or {}).get("scope") != scope:
            problems.append("doc %d: snapshot scope %r, not %r" % (i, (d.get("snapshot") or {}).get("scope"), scope))
    ids = {(d.get("snapshot") or {}).get("id") for d in docs}
    if len(ids) > 1:
        problems.append("one snapshot, several ids: %s" % sorted(map(str, ids)))
    if problems:
        raise ObservationRefused(problems)
    snap_id = ids.pop() if docs else new_snapshot_id(scope)
    with connect(config) as con, con:
        if con.execute("SELECT 1 FROM snapshots WHERE id = ?", (snap_id,)).fetchone():
            raise ObservationRefused(["snapshot id %s already exists: each producer run has its own" % snap_id])
        con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES (?, ?, ?, NULL)",
                    (snap_id, scope, kind))
        for d in docs:
            _insert(con, d)
        con.execute("UPDATE snapshots SET completed_at = ? WHERE id = ?", (_now_exact(), snap_id))
    return snap_id


def _overlay_alerts(con: sqlite3.Connection, docs: list[dict]) -> list[dict]:
    rows = {r["subject"]: r for r in con.execute("SELECT subject, cleared_at, cleared_by FROM alerts")}
    out = []
    for d in docs:
        r = rows.get(d["subject"])
        if r is not None and (r["cleared_at"], r["cleared_by"]) != (d["fact"]["cleared_at"], d["fact"]["cleared_by"]):
            d = dict(d, fact=dict(d["fact"], cleared_at=r["cleared_at"], cleared_by=r["cleared_by"]))
        out.append(d)
    return out


def _prefix_range(subject_prefix: str) -> tuple[str, str]:
    """SQL that keeps subjects starting with `subject_prefix`, as a range an index can seek (plus the exact test,
    so the range only has to be wide enough), and its arguments."""
    sql, args = "substr(o.subject, 1, ?) = ?", [len(subject_prefix), subject_prefix]
    if subject_prefix:
        last = ord(subject_prefix[-1])
        sql += " AND o.subject >= ?"
        args.append(subject_prefix)
        if last < 0x10FFFF and not 0xD7FF <= last < 0xE000:     # the next code point, if it has a UTF-8 form
            sql += " AND o.subject < ?"
            args.append(subject_prefix[:-1] + chr(last + 1))
    return sql, args


def _newest_rows(con: sqlite3.Connection, kind: str, subject_prefix: str) -> list[sqlite3.Row]:
    """An event kind's newest row per subject (newest observed_at; on a tie, the one stored last), in stored
    order, read without the documents: only the rows chosen are decoded (ROB-load-2)."""
    where, args = _prefix_range(subject_prefix)
    best: dict[str, Any] = {}
    for r in con.execute("SELECT o.rowid AS rid, o.subject, o.observed_at FROM observations o "
                         "WHERE o.kind = ? AND %s ORDER BY o.rowid" % where, [kind] + args):
        cur = best.get(r["subject"])
        if cur is None or clock.parse(r["observed_at"]) >= clock.parse(cur["observed_at"]):
            best[r["subject"]] = r
    return sorted(best.values(), key=lambda r: r["rid"])


def _docs(con: sqlite3.Connection, rids: list[int]) -> list[dict]:
    """The stored documents of these observation rows, in the order given."""
    docs: dict[int, dict] = {}
    for i in range(0, len(rids), 500):
        chunk = rids[i:i + 500]
        for r in con.execute("SELECT rowid AS rid, doc FROM observations WHERE rowid IN (%s)"
                             % ",".join("?" * len(chunk)), chunk):
            docs[r["rid"]] = json.loads(r["doc"])
    return [docs[i] for i in rids]


def _latest(con: sqlite3.Connection, kind: str, subject_prefix: str) -> list[dict]:
    if kind in SET_KINDS:
        # The current snapshot of each scope comes from current_snapshots (kept by triggers), so the cost is the
        # current rows, whatever the history (ROB-load-1), and "current" is the last completed run, whatever the
        # wall clock said when it completed (ROB-faults-4).
        where, args = _prefix_range(subject_prefix)
        rows = con.execute(
            # CROSS JOIN and INDEXED BY pin the plan: the current snapshots first, then their rows by snapshot id.
            # Left to itself the planner walks the (kind, subject) index over every stored row of the prefix, so the
            # cost grows with the history again.
            """SELECT o.rowid AS rid, o.subject, o.doc FROM current_snapshots c
               CROSS JOIN observations o INDEXED BY observations_snapshot ON o.snapshot_id = c.snapshot_id
               WHERE c.kind = ? AND o.kind = ? AND %s
               ORDER BY o.rowid""" % where, [kind, kind] + args).fetchall()
        best = {r["subject"]: r for r in rows}                    # a subject put twice into one snapshot: last
        return [json.loads(r["doc"]) for r in sorted(best.values(), key=lambda r: r["rid"])]
    docs = _docs(con, [r["rid"] for r in _newest_rows(con, kind, subject_prefix)])
    return _overlay_alerts(con, docs) if kind == "alert" else docs


def latest(kind: str, subject_prefix: str = "", *, config: Config | str | Path | None = None) -> list[dict]:
    """The current observations of `kind` whose subject starts with `subject_prefix`: set kinds from the latest
    complete snapshot of each scope, event kinds the latest per subject."""
    if kind not in SET_KINDS and kind not in EVENT_KINDS:
        raise ValueError("unknown observation kind %r" % kind)
    with connect(config) as con:
        return _latest(con, kind, subject_prefix)


def newest_per(kind: str, group: Callable[[str], str], subject_prefix: str = "", *,
               config: Config | str | Path | None = None) -> list[dict]:
    """For an event kind: the newest observation (newest observed_at; on a tie, the one stored last) in each group
    of subjects, where `group(subject)` names the group; in stored order. Only the chosen documents are read, so a
    caller that wants one per target does not decode every commit's document (ROB-load-2)."""
    if kind not in EVENT_KINDS:
        raise ValueError("newest_per reads event kinds, not %r" % kind)
    with connect(config) as con:
        best: dict[str, Any] = {}
        for r in _newest_rows(con, kind, subject_prefix):
            g = group(r["subject"])
            cur = best.get(g)
            if cur is None or clock.parse(r["observed_at"]) >= clock.parse(cur["observed_at"]):
                best[g] = r
        docs = _docs(con, sorted(r["rid"] for r in best.values()))
        return _overlay_alerts(con, docs) if kind == "alert" else docs


def subjects(kind: str, subject_prefix: str = "", *, config: Config | str | Path | None = None) -> list[str]:
    """For an event kind: the subjects that `latest(kind, subject_prefix)` would answer, in the same order, without
    decoding a document (the search box matches commit prefixes on quality_finding subjects only)."""
    if kind not in EVENT_KINDS:
        raise ValueError("subjects reads event kinds, not %r" % kind)
    with connect(config) as con:
        return [r["subject"] for r in _newest_rows(con, kind, subject_prefix)]


def history(subject: str, *, config: Config | str | Path | None = None) -> list[dict]:
    """Every stored observation of one subject, oldest stored first (section 12: all history is kept)."""
    with connect(config) as con:
        return [json.loads(r["doc"]) for r in
                con.execute("SELECT doc FROM observations WHERE subject = ? ORDER BY rowid", (subject,))]


# ---------------------------------------------------------------------------------------------------------
# Runs (not rebuildable)
# ---------------------------------------------------------------------------------------------------------
_RUN_FIELDS = ("nonce", "configuration", "commit", "request", "trigger", "triggered_by", "triggered_at",
               "pipeline_id", "job_id", "state")
_RUN_FIXED = ("configuration", "commit", "request", "trigger", "triggered_by", "triggered_at")


def _run_dict(run: Any) -> dict:
    if isinstance(run, dict):
        d = dict(run)
    elif dataclasses.is_dataclass(run):
        d = dataclasses.asdict(run)
    else:
        d = {k: getattr(run, k) for k in _RUN_FIELDS if hasattr(run, k)}
    missing = [k for k in _RUN_FIELDS if k not in d]
    if missing:
        raise ValueError("run record lacks %s" % ", ".join(missing))
    d = {k: d[k] for k in _RUN_FIELDS}
    if isinstance(d["request"], str):
        d["request"] = d["request"].encode()
    if not isinstance(d["request"], (bytes, bytearray)):
        raise ValueError("run request must be the exact RM_REQUEST bytes")
    d["request"] = bytes(d["request"])
    problems = contracts.validate("result-bundle", d["nonce"], "/$defs/nonce")
    if d["trigger"] not in ("schedule", "manual"):
        problems.append("trigger %r is not schedule or manual" % d["trigger"])
    if not d["triggered_by"]:
        problems.append("triggered_by is empty (A2, H2: every run names who started it)")
    if problems:
        raise ValueError("run record refused: " + "; ".join(problems))
    return d


def put_run(run: Any, *, config: Config | str | Path | None = None) -> None:
    """Store a RunRecord (nanosoc_rm.ci.verify), a dataclass or a dict with its fields. A second put of the same
    nonce updates pipeline_id, job_id and state only: the request bytes and who triggered it never change
    (verify check 2 compares the request byte for byte)."""
    d = _run_dict(run)
    with connect(config) as con, con:
        row = con.execute('SELECT configuration, "commit", request, "trigger", triggered_by, triggered_at '
                          "FROM runs WHERE nonce = ?", (d["nonce"],)).fetchone()
        if row is None:
            con.execute('INSERT INTO runs (nonce, configuration, "commit", request, "trigger", triggered_by, '
                        "triggered_at, pipeline_id, job_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(d[k] for k in _RUN_FIELDS))
            return
        changed = [k for k in _RUN_FIXED if (bytes(row[k]) if k == "request" else row[k]) != d[k]]
        if changed:
            raise ValueError("run %s: %s cannot change after the trigger" % (d["nonce"], ", ".join(changed)))
        con.execute("UPDATE runs SET pipeline_id = ?, job_id = ?, state = ? WHERE nonce = ?",
                    (d["pipeline_id"], d["job_id"], d["state"], d["nonce"]))


def _run_row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in _RUN_FIELDS}
    d["request"] = bytes(d["request"])
    return d


def get_run(nonce: str, *, config: Config | str | Path | None = None) -> dict | None:
    with connect(config) as con:
        r = con.execute("SELECT * FROM runs WHERE nonce = ?", (nonce,)).fetchone()
        return _run_row(r) if r else None


def runs(configuration: str | None = None, state: str | None = None, *,
         config: Config | str | Path | None = None) -> list[dict]:
    """Stored runs, oldest trigger first, optionally of one configuration and/or in one state."""
    sql, args = "SELECT * FROM runs WHERE 1 = 1", []
    if configuration is not None:
        sql += " AND configuration = ?"
        args.append(configuration)
    if state is not None:
        sql += " AND state = ?"
        args.append(state)
    with connect(config) as con:
        return [_run_row(r) for r in con.execute(sql + " ORDER BY triggered_at, rowid", args)]


# ---------------------------------------------------------------------------------------------------------
# Actions (not rebuildable; A2, H2)
# ---------------------------------------------------------------------------------------------------------
def _log_action(con: sqlite3.Connection, login: str, action: str, target: str | None, result: str | None,
                at: str | None) -> None:
    if action not in ACTIONS:
        raise ValueError("action %r is not one of %s (interfaces.md section 14)" % (action, ", ".join(ACTIONS)))
    if not login:
        raise ValueError("an action needs the login that made it (H2)")
    con.execute("INSERT INTO actions (at, login, action, target, result) VALUES (?, ?, ?, ?, ?)",
                (at or clock.iso(), login, action, target, result))


def log_action(login: str, action: str, target: str | None, result: str | None = "ok", *, at: str | None = None,
               config: Config | str | Path | None = None) -> None:
    """Log one write action (Run T0, clearing an alert) with the login that made it."""
    with connect(config) as con, con:
        _log_action(con, login, action, target, result, at)


def actions(*, config: Config | str | Path | None = None) -> list[dict]:
    with connect(config) as con:
        return [dict(r) for r in con.execute("SELECT at, login, action, target, result FROM actions ORDER BY rowid")]


# ---------------------------------------------------------------------------------------------------------
# Alerts (section 8)
# ---------------------------------------------------------------------------------------------------------
def raise_alert(alert_kind: str, about: str, message: str, sources: list[dict], *, at: str | None = None,
                config: Config | str | Path | None = None) -> dict:
    """Build, store and return a kind-`alert` observation (subject `alert:<alert_kind>:<about>`)."""
    at = at or clock.iso()
    doc = {"schema": 1, "kind": "alert", "subject": "alert:%s:%s" % (alert_kind, about), "observed_at": at,
           "sources": sources, "fact": {"alert_kind": alert_kind, "about": about, "raised_at": at,
                                        "cleared_at": None, "cleared_by": None, "message": message}}
    put(doc, config=config)
    return doc


def alerts(open_only: bool = True, *, config: Config | str | Path | None = None) -> list[dict]:
    """Kind-`alert` observations, newest raise first, with their clearing; only uncleared ones by default."""
    with connect(config) as con:
        docs = _latest(con, "alert", "")
    if open_only:
        docs = [d for d in docs if not d["fact"]["cleared_at"]]
    return sorted(docs, key=lambda d: clock.parse(d["fact"]["raised_at"]), reverse=True)


def clear_alert(subject: str, by: str, *, at: str | None = None, config: Config | str | Path | None = None) -> None:
    """Clear an open alert and log `clear_alert` in `actions` (a write to this database only). KeyError for an
    unknown subject; clearing a cleared alert changes nothing."""
    if not by:
        raise ValueError("clearing an alert needs the login that clears it (H2)")
    at = at or clock.iso()
    with connect(config) as con, con:
        row = con.execute("SELECT cleared_at FROM alerts WHERE subject = ?", (subject,)).fetchone()
        if row is None:
            raise KeyError(subject)
        if row["cleared_at"]:
            return
        con.execute("UPDATE alerts SET cleared_at = ?, cleared_by = ? WHERE subject = ?", (at, by, subject))
        _log_action(con, by, "clear_alert", subject, "ok", at)
