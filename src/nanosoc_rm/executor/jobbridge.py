"""The executor API's second queue: tier JOBS of the local job queue (interfaces.md section 27.6).

The capabilities unit (Wave G, section 26) makes a tier job (`compat --tier`, `matrix cell --tier`, `test-bump`) an
executor kind: the service's local worker leaves it queued, and an executor claims it with
`jobs.claim_for_executor(config, executor, accepts)` and reports it with
`jobs.finish_for_executor(config, job_id, executor, rc, stdout, log)`. This module is the only caller of those two on
the service: it carries them over the executor API, adding what the job queue does not keep, a LEASE per claimed
job (`job_leases` in executor.sqlite), so only the agent holding the current lease may heartbeat or finish it, and a
job whose agent stops heartbeating ends INFRA (finished with no exit code) instead of running for ever.

    available() -> bool                                   the job queue has the executor seam (else: tasks only)
    claim(config, agent, labels, *, now=None) -> dict|None   {type: job, job_id, lease, lease_seconds, argv, kind}
    heartbeat(config, job_id, agent, lease, *, now=None) -> dict   {cancel, lease_seconds}
    finish(config, job_id, agent, lease, exit_code, stdout, log) -> dict   the job, finished
    expire(config, *, now=None) -> list[int]

An agent claims jobs only when its `[[executor.agents]]` entry grants the label JOB_LABEL: a job runs the whole
`nanosoc-rm` command on the agent host (with that host's own nanosoc-rm config), which is a wider grant than one
capability command.
"""
from __future__ import annotations

import secrets
import time

from nanosoc_rm import jobs
from nanosoc_rm.config import Config
from nanosoc_rm.executor import queue as Q

__all__ = ["available", "claim", "heartbeat", "finish", "expire", "JOB_LABEL", "MAX_STDOUT_BYTES"]

JOB_LABEL = "jobs"
MAX_STDOUT_BYTES = 1024 * 1024
_DDL = "CREATE TABLE IF NOT EXISTS job_leases (job_id INTEGER PRIMARY KEY, agent TEXT NOT NULL, lease TEXT NOT NULL, " \
       "deadline REAL NOT NULL)"


def available() -> bool:
    return callable(getattr(jobs, "claim_for_executor", None)) and callable(getattr(jobs, "finish_for_executor", None))


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _expired(config: Config, t: float) -> list[tuple[int, str]]:
    with Q._tx(config) as con:
        con.execute(_DDL)
        rows = con.execute("SELECT job_id, agent FROM job_leases WHERE deadline < ?", (t,)).fetchall()
        con.execute("DELETE FROM job_leases WHERE deadline < ?", (t,))
    return [(int(r["job_id"]), r["agent"]) for r in rows]


def expire(config: Config, *, now: float | None = None) -> list[int]:
    """Jobs whose agent stopped heartbeating: finished with no exit code (the job queue reads that as INFRA)."""
    out = []
    for job_id, agent in _expired(config, _now(now)):
        try:
            jobs.finish_for_executor(config, job_id, agent, None, "",
                                     "[executor] %s stopped reporting: the lease expired\n" % agent)
        except Exception:                                 # noqa: BLE001 - already finished or cancelled elsewhere
            pass
        out.append(job_id)
    return out


def claim(config: Config, agent: str, labels, *, now: float | None = None) -> dict | None:
    if JOB_LABEL not in set(labels or ()) or not available():
        return None
    s = Q.settings(config)
    t = _now(now)
    expire(config, now=t)
    job = jobs.claim_for_executor(config, agent)
    if job is None:
        return None
    lease = secrets.token_hex(16)
    with Q._tx(config) as con:
        con.execute(_DDL)
        con.execute("INSERT OR REPLACE INTO job_leases (job_id, agent, lease, deadline) VALUES (?, ?, ?, ?)",
                    (int(job["id"]), agent, lease, t + s["lease_seconds"]))
    return {"type": "job", "job_id": int(job["id"]), "lease": lease, "lease_seconds": s["lease_seconds"],
            "kind": job["kind"], "argv": list(jobs.cli_args(job["kind"], job["params"]))}


def _held(con, job_id: int, agent: str, lease: str) -> None:
    con.execute(_DDL)
    r = con.execute("SELECT agent, lease FROM job_leases WHERE job_id = ?", (int(job_id),)).fetchone()
    if r is None or r["agent"] != agent or not secrets.compare_digest(str(r["lease"]), str(lease or "")):
        raise Q.LeaseLost("job %d is not leased to this executor" % int(job_id))


def heartbeat(config: Config, job_id: int, agent: str, lease: str, *, now: float | None = None) -> dict:
    s = Q.settings(config)
    t = _now(now)
    expire(config, now=t)
    with Q._tx(config) as con:
        _held(con, job_id, agent, lease)
        con.execute("UPDATE job_leases SET deadline = ? WHERE job_id = ?", (t + s["lease_seconds"], int(job_id)))
    job = jobs.get(config, job_id) or {}
    return {"cancel": bool(job.get("cancel_by")), "lease_seconds": s["lease_seconds"]}


def finish(config: Config, job_id: int, agent: str, lease: str, exit_code, stdout: str, log: str) -> dict:
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise Q.TaskRefused(["/exit_code: an integer or null"])
    if not isinstance(stdout, str) or not isinstance(log, str):
        raise Q.TaskRefused(["/stdout and /log: strings"])
    with Q._tx(config) as con:
        _held(con, job_id, agent, lease)
        con.execute("DELETE FROM job_leases WHERE job_id = ?", (int(job_id),))
    job = jobs.finish_for_executor(config, job_id, agent, exit_code, stdout[:MAX_STDOUT_BYTES],
                                   log[-Q.MAX_LOG_BYTES:])
    return {"state": job.get("state"), "verdict": job.get("verdict")}
