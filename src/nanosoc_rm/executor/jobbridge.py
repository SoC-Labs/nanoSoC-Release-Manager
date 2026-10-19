"""The executor API's second queue: tier JOBS of the local job queue (interfaces.md section 27.6).

The capabilities unit (section 26) makes a tier job (`compat --tier`, `matrix cell --tier`, `test-bump`) an
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

An agent claims jobs only when its `[[executor.agents]]` entry grants the label JOB_LABEL (every tier job kind) or
`jobs:<kind>` (that kind only: `jobs:test-bump`, `jobs:compat`, `jobs:matrix-cell`): a job runs the whole
`nanosoc-rm` command on the agent host (with that host's own nanosoc-rm config), which is a wider grant than one
capability command. Which TOOLS a job needs is not known when it is claimed (the plan is read on the agent host);
the job checks them there (`host_check`: a capability whose tool the host lacks is INFRA, so the job reads
UNVERIFIED, never PASS), so grant a kind only to an agent whose host can run it.
"""
from __future__ import annotations

import secrets
import time

from nanosoc_rm import clock, jobs
from nanosoc_rm.config import Config
from nanosoc_rm.executor import queue as Q

__all__ = ["available", "claim", "heartbeat", "finish", "expire", "kinds_granted", "JOB_LABEL", "MAX_STDOUT_BYTES"]

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


def _orphans(config: Config, t: float) -> list[tuple[int, str]]:
    """Running executor jobs with NO lease row, started more than a lease ago.
    A claim marks the job running in jobs.sqlite, then writes its lease to executor.sqlite in a second
    transaction; a service that dies between the two (or a finish that dropped the lease, then failed) leaves a
    job `running` on an executor that holds no lease, which recover() rightly skips and no heartbeat can end."""
    try:
        lease_s = Q.settings(config)["lease_seconds"]
    except Exception:                                     # noqa: BLE001 - no [executor]: nothing was claimed
        return []
    with Q._tx(config) as con:
        con.execute(_DDL)
        leased = {int(r["job_id"]) for r in con.execute("SELECT job_id FROM job_leases")}
    out = []
    for j in jobs.jobs(config, limit=10 ** 6, states=("running",)):
        ex = j.get("executor")
        if not ex or ex == jobs.LOCAL_EXECUTOR or int(j["id"]) in leased:
            continue
        try:
            started = clock.parse(j.get("started_at") or "").timestamp()
        except Exception:                                 # noqa: BLE001 - an unreadable start is old
            started = 0.0
        if t - started > lease_s:
            out.append((int(j["id"]), ex))
    return out


def expire(config: Config, *, now: float | None = None) -> list[int]:
    """Jobs whose agent stopped heartbeating, and running executor jobs that never got a lease (`_orphans`):
    finished with no exit code (the job queue reads that as INFRA)."""
    out = []
    t = _now(now)
    gone = _expired(config, t)
    ended = {j for j, _ in gone}
    for job_id, agent in gone + [o for o in _orphans(config, t) if o[0] not in ended]:
        try:
            jobs.finish_for_executor(config, job_id, agent, None, "",
                                     "[executor] %s stopped reporting: the lease expired\n" % agent)
        except Exception:                                 # noqa: BLE001 - already finished or cancelled elsewhere
            pass
        out.append(job_id)
    return out


def kinds_granted(labels) -> set[str] | None:
    """The job kinds a label set grants: None for every kind (`jobs`), else the kinds named by `jobs:<kind>`."""
    have = set(labels or ())
    if JOB_LABEL in have:
        return None
    return {x.split(":", 1)[1] for x in have if x.startswith(JOB_LABEL + ":") and x.split(":", 1)[1]}


def claim(config: Config, agent: str, labels, *, now: float | None = None) -> dict | None:
    kinds = kinds_granted(labels)
    if kinds == set() or not available():
        return None
    s = Q.settings(config)
    t = _now(now)
    expire(config, now=t)
    job = jobs.claim_for_executor(config, agent, None if kinds is None else (lambda j: j["kind"] in kinds))
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
