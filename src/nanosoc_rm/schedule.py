"""The scheduler (interfaces.md section 12a; D2, H6, H3, D10).

    tick(now) -> [job names]   runs every job that is due at `now`, in a fixed order, and returns their names

`serve` calls `tick(utcnow())` every 30 s; tests call it with an injected time, so no schedule test waits on the
wall clock. Jobs, in the order they run within one tick:

    poll         every schedule.poll_minutes (15): sync every catalogue component's mirror, then walk every
                 configuration, compare hosts and refresh the silicon-protection list (sections 3 to 7)
    freshness_scan  every schedule.poll_minutes, right after poll: queue one `freshness-scan` local job (section
                 23; read-only git over the walks poll just stored), unless one is queued or running already. The
                 worker runs it as a child process, never this process.
    inventory    every schedule.poll_minutes: list the org and the group (A1)
    nightly_t0   once per UTC day, at or after schedule.nightly_utc: trigger T0 for every configuration and
                 @registry, kind `schedule` (D2)
    fetch        every schedule.fetch_minutes (2): fetch, verify and store pending T0 runs (A4, D5)
    credentials  once per UTC day, at or after schedule.nightly_utc: `credentials check` (H3 expiry warning)
    runner       every schedule.poll_minutes: `runner` (D10; the dashboard's runner status)
    store_index  once per UTC day, at or after schedule.nightly_utc, when [stores.artifactory] is configured: queue
                 one `store-index` local job (section 22; read-only: GET, HEAD and AQL), unless one is queued or
                 running already. The worker runs it as a child process, never this process.

With no `[ci] project` configured (a local deployment, CR-17), the CI jobs (nightly_t0, fetch, runner) are left
out: there is nothing to trigger or fetch, and Run executes locally instead (nanosoc_rm.jobs).

A job registered with `enabled=False` (CR-15: built, not enabled; `gate` names the phase) is never run.

When a job last ran is kept in `<data_dir>/schedule.json` (written atomically), so a restart neither repeats a
night nor forgets one. A job that raises is still marked as run (it is retried at its next due time, never in a
tight loop) and its error goes to stderr; the other jobs still run. A night missed while the service was down is
caught up later the same UTC day; a night that was missed entirely shows in `t0 coverage` (D2).

Job bodies import their modules when they run, so the scheduler never imports a module it does not need now,
and never imports nanosoc_rm.t0.runner (A4).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nanosoc_rm import clock
from nanosoc_rm.config import Config, load_config

__all__ = ["tick", "JOBS", "CI_JOBS", "Job", "due", "state_path", "load_state"]


@dataclass(frozen=True)
class Job:
    name: str
    every: str              # "poll" | "fetch" (a period from [schedule]) or "nightly" (once per UTC day)
    run: Callable[[Config], object]
    # CR-15: the phase whose capability the job automates, and whether it is switched on. A later phase's
    # automation may be registered BUILT but disabled (enabled=False, `gate` naming the phase whose entry gate
    # must be met first); tick() never runs a disabled job, and the phase-gate check (rule 5) judges only enabled
    # ones, by declared phase, by name and by what their code reaches.
    phase: str = "v1"
    enabled: bool = True
    gate: str | None = None


# ----------------------------------------------------------------------------------------------------------------
# Job bodies (front ends over the contracted functions, interfaces.md section 17.2)
# ----------------------------------------------------------------------------------------------------------------
def _catalogue(config: Config) -> tuple[list[str], list[str]]:
    """(component ids with a copy on another host, every component and external id): what poll mirrors."""
    from nanosoc_rm import contracts
    doc = contracts.load_yaml((Path(config.require("registry", "path")) / "catalogue.yaml").read_text())
    comps = doc.get("components") or []
    return ([c["id"] for c in comps if c.get("copies")],
            [c["id"] for c in comps] + [e["id"] for e in doc.get("external") or [] if "id" in e])


def _poll(config: Config) -> None:
    from nanosoc_rm.analysis.hosts import compare_hosts
    from nanosoc_rm.analysis.protect import silicon_protection
    from nanosoc_rm.ci import configs
    from nanosoc_rm.mirror import sync_component
    from nanosoc_rm.walker import walk
    errors = []
    with_copies, mirrored = _catalogue(config)
    for cid in mirrored:
        try:
            sync_component(cid)
        except Exception as e:                    # one repo's failure is its mirror_status; keep going
            errors.append("sync %s: %s" % (cid, e))
    for c in configs.configurations(config):
        try:
            walk(c["id"])
        except Exception as e:
            errors.append("walk %s: %s" % (c["id"], e))
    for cid in with_copies:
        try:
            compare_hosts(cid)
        except Exception as e:
            errors.append("hosts %s: %s" % (cid, e))
    try:
        silicon_protection(None)
    except Exception as e:
        errors.append("silicon-protection: %s" % e)
    if errors:
        raise RuntimeError("; ".join(errors))


def _freshness_scan(config: Config) -> None:
    from nanosoc_rm import jobs
    if jobs.jobs(config, kind="freshness-scan", states=("queued", "running")):
        return
    jobs.submit(config, "freshness-scan", {}, by="schedule")


def _inventory(config: Config) -> None:
    from nanosoc_rm.mirror import inventory
    inventory()


def _nightly_t0(config: Config) -> None:
    from nanosoc_rm.ci import configs
    from nanosoc_rm.ci.trigger import trigger
    errors = []
    for cfg in [c["id"] for c in configs.configurations(config)] + [configs.REGISTRY_JOB]:
        try:
            trigger(cfg, by="schedule", kind="schedule", config=config)
        except Exception as e:                    # an untriggerable configuration shows in `t0 coverage`
            errors.append("%s: %s" % (cfg, e))
    if errors:
        raise RuntimeError("; ".join(errors))


def _fetch(config: Config) -> None:
    from nanosoc_rm.ci.fetch import fetch_pending
    fetch_pending(config=config)


def _credentials(config: Config) -> None:
    from nanosoc_rm.credentials import check
    check(config)


def _runner(config: Config) -> None:
    from nanosoc_rm.ci.runners import runner_settings
    runner_settings(config=config)


def _store_index(config: Config) -> None:
    if not (config.get("stores") or {}).get("artifactory"):
        return                                    # no store configured: nothing to index
    from nanosoc_rm import jobs
    if any(j["kind"] == "store-index" for j in jobs.jobs(config, states=("queued", "running"))):
        return
    jobs.submit(config, "store-index", {}, by="schedule")


JOBS: tuple[Job, ...] = (
    Job("poll", "poll", _poll),
    Job("freshness_scan", "poll", _freshness_scan),
    Job("inventory", "poll", _inventory),
    Job("nightly_t0", "nightly", _nightly_t0),
    Job("fetch", "fetch", _fetch),
    Job("credentials", "nightly", _credentials),
    Job("runner", "poll", _runner),
    Job("store_index", "nightly", _store_index),
)
CI_JOBS = ("nightly_t0", "fetch", "runner")          # need a CI project; skipped without one (CR-17)


# ----------------------------------------------------------------------------------------------------------------
# When
# ----------------------------------------------------------------------------------------------------------------
def state_path(config: Config) -> Path:
    return config.data_dir / "schedule.json"


def load_state(config: Config) -> dict[str, str]:
    """{job name: UTC time it last ran}. A missing or unreadable file is an empty state."""
    try:
        doc = json.loads(state_path(config).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(doc, dict) else {}


def _save_state(config: Config, state: dict[str, str]) -> None:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".schedule.", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, sort_keys=True, indent=1)
    os.replace(tmp, path)


def due(job: Job, now: dt.datetime, last: str | None, config: Config) -> bool:
    """Is `job` due at `now`, given when it last ran (None: never)?"""
    sched = config["schedule"]
    if job.every == "nightly":
        hh, mm = (int(x) for x in sched["nightly_utc"].split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < start:
            return False
        return last is None or clock.parse(last) < start
    minutes = sched["fetch_minutes"] if job.every == "fetch" else sched["poll_minutes"]
    return last is None or now - clock.parse(last) >= dt.timedelta(minutes=minutes)


def tick(now: dt.datetime, *, config: Config | None = None, jobs: tuple[Job, ...] | None = None) -> list[str]:
    """Run every job due at `now` (module docstring). Returns the names of the jobs it ran, in order."""
    config = config if config is not None else load_config()
    now = (now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    state = load_state(config)
    ran = []
    if jobs is None:
        jobs = JOBS if (config.get("ci") or {}).get("project") else tuple(j for j in JOBS if j.name not in CI_JOBS)
    for job in jobs:
        if not job.enabled or not due(job, now, state.get(job.name), config):
            continue
        try:
            job.run(config)
        except Exception:
            print("nanosoc-rm schedule: job %s failed at %s:" % (job.name, clock.iso(now)), file=sys.stderr)
            traceback.print_exc()
        state[job.name] = clock.iso(now)
        _save_state(config, state)
        ran.append(job.name)
    return ran
