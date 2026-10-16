"""The executor queue: `<data_dir>/executor.sqlite` (interfaces.md section 27.3).

    enqueue(config, task, *, now=None) -> int          a checked task, queued; its id
    claim(config, agent, labels, *, now=None) -> dict|None   the oldest queued task this agent can run, leased to it
    heartbeat(config, task_id, agent, lease, *, now=None) -> dict   {cancel}; extends the lease
    store_log(config, task_id, agent, lease, data) -> Path   the run's log, kept beside the store (0600)
    complete(config, task_id, agent, lease, result) -> dict  the stored row; the task is done
    cancel(config, task_id, by) -> dict                 a queued task is cancelled; a claimed one is asked to stop
    expire(config, *, now=None) -> list[int]            leases past their deadline: re-queued, or lost
    get(config, task_id) / tasks(config, state=None, limit=50)   rows, for the CLI and the consumers

States: queued -> claimed -> done; claimed -> queued again when the lease runs out (up to `max_attempts` claims),
then lost (verdict INFRA). queued -> cancelled. A claimed task asked to stop stays claimed until its agent posts
the result (or the lease runs out). Only the agent holding the current lease may heartbeat, upload a log or post
the result; any other caller gets LeaseLost (HTTP 409). Every write is one BEGIN IMMEDIATE transaction, so two
agents can never hold the same task.

Expiry runs inside claim() and heartbeat() (and `nanosoc-rm executor list`), so no background thread is needed.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.executor import task as T

__all__ = ["enqueue", "claim", "heartbeat", "store_log", "complete", "cancel", "expire", "get", "tasks", "path",
           "log_path", "settings", "QueueError", "LeaseLost", "TaskRefused", "NotFound", "STATES",
           "DEFAULT_LEASE_S", "DEFAULT_MAX_ATTEMPTS", "MAX_LOG_BYTES"]

STATES = ("queued", "claimed", "done", "lost", "cancelled")
DEFAULT_LEASE_S = 300
DEFAULT_MAX_ATTEMPTS = 2
MAX_LOG_BYTES = 4 * 1024 * 1024          # what the service keeps of one run's log
_SCHEMA_VERSION = 1
_DDL = ("""CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, requested_by TEXT NOT NULL, labels TEXT NOT NULL, doc TEXT NOT NULL,
    nonce TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    agent TEXT, lease TEXT, claimed_at TEXT, deadline REAL, finished_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0, verdict TEXT, result TEXT, log TEXT, note TEXT)""",
        "CREATE INDEX IF NOT EXISTS tasks_state ON tasks (state, id)")


class QueueError(Exception):
    status = 400


class TaskRefused(QueueError):
    """The task or result breaks the contract (HTTP 422). `problems` lists each breach."""
    status = 422

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems)[:1000])


class LeaseLost(QueueError):
    """The caller does not hold the task's current lease (HTTP 409): it expired, or another agent holds it."""
    status = 409


class NotFound(QueueError):
    status = 404


def settings(config: Config) -> dict:
    """[executor] with defaults; ConfigError when the section is absent (the executor is off)."""
    ex = config.get("executor")
    if not isinstance(ex, dict):
        raise ConfigError("%s: [executor] is required for this command (the remote executor is not configured)"
                          % config.path)
    return {"lease_seconds": int(ex.get("lease_seconds", DEFAULT_LEASE_S)),
            "max_attempts": int(ex.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            "agents": list(ex.get("agents") or [])}


def path(config: Config) -> Path:
    return config.data_dir / "executor.sqlite"


def log_path(config: Config, task_id: int) -> Path:
    return config.data_dir / "executor-logs" / ("%d.log" % int(task_id))


@contextlib.contextmanager
def _connect(config: Config) -> Iterator[sqlite3.Connection]:
    p = path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists()
    con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    try:
        if fresh:
            with contextlib.suppress(OSError):
                os.chmod(p, 0o600)
        con.row_factory = sqlite3.Row
        db.ensure_schema(con, _DDL, _SCHEMA_VERSION)
        yield con
    finally:
        con.close()


@contextlib.contextmanager
def _tx(config: Config) -> Iterator[sqlite3.Connection]:
    with _connect(config) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")


def _out(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["labels"] = json.loads(d["labels"])
    d["task"] = json.loads(d.pop("doc"))
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d.pop("lease", None)                  # a lease is a bearer secret of its agent: never shown
    d["cancel_requested"] = bool(d["cancel_requested"])
    return d


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def enqueue(config: Config, task: dict, *, now: float | None = None) -> int:
    """Queue a task after check_task. TaskRefused for a task that breaks the contract."""
    settings(config)
    probs = T.check_task(task)
    if probs:
        raise TaskRefused(probs)
    with _tx(config) as con:
        cur = con.execute("INSERT INTO tasks (created_at, requested_by, labels, doc, nonce, state) "
                          "VALUES (?, ?, ?, ?, ?, 'queued')",
                          (clock.iso(datetime.datetime.fromtimestamp(_now(now), datetime.timezone.utc)),
                           task["requested_by"], json.dumps(sorted(set(task["labels"]))),
                           json.dumps(task, sort_keys=True), secrets.token_hex(16)))
        return int(cur.lastrowid)


def _expire(con: sqlite3.Connection, now: float, max_attempts: int) -> list[int]:
    out = []
    for r in con.execute("SELECT id, attempts, cancel_requested FROM tasks WHERE state = 'claimed' AND deadline < ?",
                         (now,)).fetchall():
        if r["attempts"] >= max_attempts or r["cancel_requested"]:
            con.execute("UPDATE tasks SET state = 'lost', verdict = 'INFRA', lease = NULL, deadline = NULL, "
                        "finished_at = ?, note = ? WHERE id = ?",
                        (clock.iso(), "the executor stopped reporting (lease expired after %d claim(s))"
                         % r["attempts"], r["id"]))
        else:
            con.execute("UPDATE tasks SET state = 'queued', lease = NULL, deadline = NULL, agent = NULL, "
                        "note = 'lease expired; queued again' WHERE id = ?", (r["id"],))
        out.append(int(r["id"]))
    return out


def expire(config: Config, *, now: float | None = None) -> list[int]:
    s = settings(config)
    with _tx(config) as con:
        return _expire(con, _now(now), s["max_attempts"])


def claim(config: Config, agent: str, labels, *, now: float | None = None) -> dict | None:
    """The oldest queued task whose labels the agent carries, leased to it: {task_id, lease, nonce,
    lease_seconds, task} (executor.schema.json#/$defs/claim). None: nothing it can run."""
    s = settings(config)
    have = set(labels or ())
    t = _now(now)
    with _tx(config) as con:
        _expire(con, t, s["max_attempts"])
        for r in con.execute("SELECT id, labels, nonce, doc FROM tasks WHERE state = 'queued' ORDER BY id"):
            if not set(json.loads(r["labels"])) <= have:
                continue
            lease = secrets.token_hex(16)
            con.execute("UPDATE tasks SET state = 'claimed', agent = ?, lease = ?, claimed_at = ?, deadline = ?, "
                        "attempts = attempts + 1 WHERE id = ?",
                        (agent, lease, clock.iso(), t + s["lease_seconds"], r["id"]))
            return {"task_id": int(r["id"]), "lease": lease, "nonce": r["nonce"],
                    "lease_seconds": s["lease_seconds"], "task": json.loads(r["doc"])}
    return None


def _held(con: sqlite3.Connection, task_id: int, agent: str, lease: str) -> sqlite3.Row:
    r = con.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    if r is None:
        raise NotFound("no executor task %d" % int(task_id))
    if r["state"] != "claimed" or r["agent"] != agent or not r["lease"] or \
            not secrets.compare_digest(str(r["lease"]), str(lease or "")):
        raise LeaseLost("task %d is not leased to this executor (its lease expired or was never issued)"
                        % int(task_id))
    return r


def heartbeat(config: Config, task_id: int, agent: str, lease: str, *, now: float | None = None) -> dict:
    s = settings(config)
    t = _now(now)
    with _tx(config) as con:
        _expire(con, t, s["max_attempts"])
        r = _held(con, task_id, agent, lease)
        con.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (t + s["lease_seconds"], r["id"]))
        return {"cancel": bool(r["cancel_requested"]), "lease_seconds": s["lease_seconds"]}


def store_log(config: Config, task_id: int, agent: str, lease: str, data: bytes) -> Path:
    """Keep at most MAX_LOG_BYTES of the run's log (the agent already capped it). 0600, beside the store."""
    settings(config)
    with _tx(config) as con:
        _held(con, task_id, agent, lease)
        p = log_path(config, task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(p.parent, 0o700)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(data[:MAX_LOG_BYTES]))
        con.execute("UPDATE tasks SET log = ? WHERE id = ?", (str(p), int(task_id)))
        return p


def complete(config: Config, task_id: int, agent: str, lease: str, result: dict) -> dict:
    """Accept the agent's result: the lease must be current and the result must meet the contract, with every row
    carrying the task's nonce. TaskRefused / LeaseLost otherwise; the task stays claimed, so the agent may retry."""
    settings(config)
    with _tx(config) as con:
        r = _held(con, task_id, agent, lease)
        probs = T.check_result(result, r["nonce"], int(task_id))
        if not probs and result.get("executor") != agent:
            probs = ["/executor: not the agent holding the lease"]
        if probs:
            raise TaskRefused(probs)
        con.execute("UPDATE tasks SET state = 'done', verdict = ?, result = ?, finished_at = ?, lease = NULL, "
                    "deadline = NULL WHERE id = ?",
                    (result["verdict"], json.dumps(result, sort_keys=True), clock.iso(), int(task_id)))
        return _out(con.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone())


def cancel(config: Config, task_id: int, by: str) -> dict:
    settings(config)
    with _tx(config) as con:
        r = con.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        if r is None:
            raise NotFound("no executor task %d" % int(task_id))
        if r["state"] == "queued":
            con.execute("UPDATE tasks SET state = 'cancelled', finished_at = ?, note = ? WHERE id = ?",
                        (clock.iso(), "cancelled by %s" % by, r["id"]))
        elif r["state"] == "claimed":
            con.execute("UPDATE tasks SET cancel_requested = 1, note = ? WHERE id = ?",
                        ("stop asked by %s" % by, r["id"]))
        return _out(con.execute("SELECT * FROM tasks WHERE id = ?", (r["id"],)).fetchone())


def get(config: Config, task_id: int) -> dict | None:
    with _connect(config) as con:
        return _out(con.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone())


def tasks(config: Config, state: str | None = None, limit: int = 50) -> list[dict]:
    with _connect(config) as con:
        if state:
            rows = con.execute("SELECT * FROM tasks WHERE state = ? ORDER BY id DESC LIMIT ?", (state, int(limit)))
        else:
            rows = con.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),))
        return [_out(r) for r in rows.fetchall()]
