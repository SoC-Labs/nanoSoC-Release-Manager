"""Fetch T0 results from the CI API, verify them, roll them up and store them (interfaces.md section 10 Fetch;
A4, D2, D5, D7, D10, D11).

    fetch_pending(client=None) -> [t0_result]   every run that became final in this call
    coverage(night)            -> [row]         configurations (and @registry) with no accepted scheduled run
    results(configuration)     -> [t0_result]   the stored runs, oldest trigger first
    items(nonce, gate)         -> [gate_item]   one gate's items from the stored bundle

For each pending run (every 2 minutes in `serve`):

- `GET pipelines/:pid`, `GET pipelines/:pid/jobs`, `GET pipelines/:pid/variables`; when the `t0` job has ended,
  `GET jobs/:job_id/artifacts`, unzipped (rm-bundle/ only, no path outside it) into `<paths.quarantine>/<nonce>/`.
- verify.verify(). Accepted: the roll-up (`gates.rollup`, `gates.apply_waivers` with the configuration's
  t0_waivers, `gates.run_verdict`), the times and budget from the verified provenance (`tiers.check_budget`), the
  runner from the job API; the bundle moves to `<data_dir>/bundles/<nonce>/` (the local copy of the `ci` store,
  read by `t0 items`). Each waiver that covered nothing raises one `waiver_unused` alert per run, naming every
  such waiver's gate and tree path. Rejected or refused: the quarantined copy is deleted (D11: only the finding
  locations survive, in the reason).
- No bundle: the job's `failure_reason` decides INFRA (runner_system_failure, stuck_or_timeout_failure,
  job_execution_timeout, scheduler_failure, api_failure, or exit 137) or UNVERIFIED. A run still unfinished after
  `ci.fetch_timeout_minutes` (120) is `no-bundle` / INFRA.

A CI API call that fails leaves the run pending (and so it is retried), until the timeout.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.ci import configs
from nanosoc_rm.ci.client import CIClient, CIError, make_client
from nanosoc_rm.ci.verify import RunRecord, ci_settings, read_provenance, read_rows, verify
from nanosoc_rm.config import Config, load_config

__all__ = ["fetch_pending", "coverage", "results", "items", "bundle_dir", "default_night", "INFRA_FAILURES",
           "MAX_BUNDLE_BYTES", "FetchErrors"]

INFRA_FAILURES = ("runner_system_failure", "stuck_or_timeout_failure", "job_execution_timeout",
                  "scheduler_failure", "api_failure")
_UNFINISHED = ("created", "pending", "running", "waiting_for_resource", "preparing", "scheduled", "manual")
_ENDED_WITH_BUNDLE = ("success", "failed")
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_GATE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$")


class FetchErrors(RuntimeError):
    """Some runs could not be processed (a bug, not a CI failure); the others were. They stay pending."""


def _cfg(config: Config | None) -> Config:
    return config if config is not None else load_config()


def bundle_dir(nonce: str, config: Config) -> Path:
    """Where an accepted bundle is kept: <data_dir>/bundles/<nonce>/rm-bundle."""
    return config.data_dir / "bundles" / nonce / "rm-bundle"


def _t0():
    """The shared roll-up and tier table (interfaces.md section 9), imported only when a bundle is accepted."""
    from nanosoc_rm.t0 import gates, tiers
    return gates, tiers


# ---------------------------------------------------------------------------------------------------------------
# Bundle handling
# ---------------------------------------------------------------------------------------------------------------
class _BadBundle(ValueError):
    pass


def _unzip(data: bytes, dest: Path) -> Path:
    """Extract rm-bundle/ from an artifact zip into dest/rm-bundle. Refuses absolute paths, `..`, links and more
    than MAX_BUNDLE_BYTES; ignores entries outside rm-bundle/."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _BadBundle("the artifact is not a zip archive") from None
    total = 0
    with z:
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise _BadBundle("the artifact holds an unsafe path %r" % info.filename)
            if not name.parts or name.parts[0] != "rm-bundle":
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise _BadBundle("the artifact holds a symbolic link %r" % info.filename)
            total += info.file_size
            if total > MAX_BUNDLE_BYTES:
                raise _BadBundle("the artifact is larger than %d bytes" % MAX_BUNDLE_BYTES)
            target = dest.joinpath(*name.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    out = dest / "rm-bundle"
    if not out.is_dir():
        raise _BadBundle("the artifact has no rm-bundle/ directory")
    return out


def _read_items(bundle: Path) -> tuple[dict[str, list[dict]], set[str]]:
    """({gate: items}, gates whose items file is malformed). A malformed file is never used to waive."""
    out: dict[str, list[dict]] = {}
    bad: set[str] = set()
    d = bundle / "items"
    for f in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        gate = f.name[: -len(".jsonl")]
        lines = []
        try:
            for line in f.read_text().splitlines():
                if line.strip():
                    item = json.loads(line)
                    if contracts.validate_gate_item(item):
                        raise ValueError("invalid item")
                    lines.append(item)
        except (OSError, ValueError):
            bad.add(gate)
            continue
        out[gate] = lines
    return out, bad


# ---------------------------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------------------------
def _run_record(row: dict) -> RunRecord:
    return RunRecord(**row)


def _runner(job: dict | None) -> dict | None:
    r = (job or {}).get("runner")
    if not isinstance(r, dict) or not isinstance(r.get("id"), int) or r["id"] < 1:
        return None
    tags = job.get("tag_list") if isinstance(job.get("tag_list"), list) else r.get("tags") or []
    return {"id": r["id"], "description": r.get("description"), "tags": [str(t) for t in tags]}


def _doc(run: RunRecord, now: str, sources: list[dict], **fact: Any) -> dict:
    f = {"configuration": run.configuration, "commit": run.commit,
         "run": {"nonce": run.nonce, "trigger": run.trigger, "triggered_by": run.triggered_by,
                 "triggered_at": run.triggered_at, "pipeline_id": run.pipeline_id, "job_id": run.job_id},
         "acceptance": None, "reason": None, "verdict": None, "gates": [], "bundle": None, "started_at": None,
         "finished_at": None, "over_budget": None, "runner": None}
    f.update(fact)
    return {"schema": 1, "kind": "t0_result", "subject": "t0_result:%s:%s" % (run.configuration, run.nonce),
            "observed_at": now, "sources": sources, "fact": f}


def _no_bundle(job: dict | None, why: str) -> tuple[str, str]:
    reason = (job or {}).get("failure_reason")
    infra = reason in INFRA_FAILURES or (job or {}).get("exit_code") == 137
    detail = " (failure_reason %s)" % reason if reason else ""
    return ("INFRA" if infra else "UNVERIFIED"), why + detail


def _rollup(run: RunRecord, bundle: Path, config: Config) -> tuple[list[dict], str, list[dict]]:
    """(gates, run verdict, unused waivers): README section 6 over the verified rows and items."""
    gates, _tiers = _t0()
    scope = "registry" if run.configuration == configs.REGISTRY_JOB else "configuration"
    rows = read_rows(bundle)
    rolled = gates.rollup(rows, scope=scope)
    item_map, malformed = _read_items(bundle)
    waivers = [w for w in configs.waivers(run.configuration, config) if w["gate"] not in malformed]
    waived, unused = gates.apply_waivers(rolled, item_map, waivers)
    verdict, _not_run = gates.run_verdict(waived)
    return [db.as_doc(g) if not isinstance(g, dict) else g for g in waived], verdict, list(unused)


def _budget(prov: dict) -> bool:
    _gates, tiers = _t0()
    return not tiers.check_budget("t0", prov["started_at"], prov["finished_at"]).ok


def _waiver_alert(run: RunRecord, unused: list[dict], now: str, config: Config) -> None:
    about = "t0_result:%s:%s" % (run.configuration, run.nonce)
    names = "; ".join("%s at %s (%s)" % (w["gate"], w["tree_path"], w["result"]) for w in unused)
    db.raise_alert("waiver_unused", about,
                   "%d t0_waivers of %s covered nothing in run %s: %s. Remove them from eligibility.yaml once "
                   "their cause is fixed (README section 6)." % (len(unused), run.configuration, run.nonce, names),
                   [{"kind": "derived", "command": "nanosoc-rm t0 runs %s --json" % run.configuration}],
                   at=now, config=config)


def _process(run: RunRecord, client: CIClient, config: Config, now: dt.datetime) -> dict | None:
    """The run's final t0_result, stored; None while it is still pending."""
    stamp = clock.iso(now)
    sources = [{"kind": "ci_api", "command": client.command("GET", "pipeline", run.pipeline_id)}]
    timeout = dt.timedelta(minutes=int(config.get("ci", {}).get("fetch_timeout_minutes", 120)))
    timed_out = now - clock.parse(run.triggered_at) > timeout

    def final(acceptance: str, verdict: str, reason: str | None, job: dict | None = None, **extra: Any) -> dict:
        run.state = acceptance
        run.job_id = (job or {}).get("id") if isinstance((job or {}).get("id"), int) else run.job_id
        db.put_run(run, config=config)
        doc = _doc(run, stamp, sources, acceptance=acceptance, verdict=verdict, reason=reason,
                   runner=_runner(job), **extra)
        db.put(doc, config=config)
        return doc

    if run.pipeline_id is None:                          # a trigger that failed is final at once (trigger.py)
        return None
    try:
        pipeline = client.pipeline(run.pipeline_id)
        sources.append({"kind": "ci_api", "command": client.command("GET", "jobs", run.pipeline_id)})
        jobs = client.jobs(run.pipeline_id)
    except CIError as e:
        if timed_out:
            return final("no-bundle", "INFRA", "the CI API could not be read for %d minutes: %s"
                         % (timeout.total_seconds() // 60, e))
        return None
    name = config.get("ci", {}).get("job", "t0")
    mine = [j for j in jobs if isinstance(j, dict) and j.get("name") == name]
    if len(mine) > 1:
        return final("rejected", "UNVERIFIED", "check 1: pipeline %s has %d jobs named %s, want exactly 1"
                     % (run.pipeline_id, len(mine), name))
    job = mine[0] if mine else None
    status = (job or {}).get("status") or pipeline.get("status")
    if job is None:
        if pipeline.get("status") in _UNFINISHED and not timed_out:
            return None
        return final("no-bundle", *_no_bundle(None, "pipeline %s has no job named %s" % (run.pipeline_id, name)))
    if status in _UNFINISHED:
        if timed_out:
            return final("no-bundle", "INFRA", "job %s still %s after %d minutes"
                         % (job.get("id"), status, timeout.total_seconds() // 60), job)
        return None
    if status not in _ENDED_WITH_BUNDLE:
        return final("no-bundle", *_no_bundle(job, "job %s ended %s without a bundle" % (job.get("id"), status)), job)

    try:
        sources.append({"kind": "ci_api", "command": client.command("GET", "variables", run.pipeline_id)})
        variables = client.variables(run.pipeline_id)
        sources.append({"kind": "ci_api", "command": client.command("GET", "artifacts", job["id"])})
        data = client.artifacts(job["id"])
    except CIError as e:
        if e.status == 404 and "artifacts" in e.call:
            return final("no-bundle", *_no_bundle(job, "job %s ended %s with no rm-bundle artifact"
                                                  % (job["id"], status)), job)
        if timed_out:
            return final("no-bundle", "INFRA", "the CI API could not be read: %s" % e, job)
        return None

    quarantine = config.quarantine / run.nonce
    shutil.rmtree(quarantine, ignore_errors=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    try:
        try:
            bundle = _unzip(data, quarantine)
        except _BadBundle as e:
            return final("rejected", "UNVERIFIED", "check 3: %s" % e, job)
        v = verify(run, pipeline, job, variables, bundle, config=config)
        if v.acceptance != "accepted":
            return final(v.acceptance, "UNVERIFIED", v.reason, job)
        prov = read_provenance(bundle)
        gate_docs, verdict, unused = _rollup(run, bundle, config)
        over = _budget(prov)
        keep = bundle_dir(run.nonce, config)
        shutil.rmtree(keep.parent, ignore_errors=True)
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(bundle), str(keep))
        doc = final("accepted", verdict, None, job, gates=gate_docs,
                    bundle={"store": "ci", "path": "jobs/%d/artifacts" % job["id"]},
                    started_at=prov["started_at"], finished_at=prov["finished_at"], over_budget=over)
        if unused:
            _waiver_alert(run, unused, stamp, config)
        return doc
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)


def fetch_pending(client: CIClient | None = None, *, config: Config | None = None,
                  now: dt.datetime | None = None) -> list[dict]:
    """interfaces.md section 10 Fetch: every pending run, once. Returns the t0_results that became final."""
    config = _cfg(config)
    pending = db.runs(state="pending", config=config)
    if pending:
        ci_settings(config)                              # a config that cannot verify is exit 3, not a run error
    client = client if client is not None else make_client(config)
    now = now or clock.now()
    out, errors = [], []
    for row in pending:
        run = _run_record(row)
        try:
            doc = _process(run, client, config, now)
        except Exception as e:                           # one broken run never blocks the others
            errors.append("%s: %s: %s" % (run.nonce, type(e).__name__, e))
            continue
        if doc is not None:
            out.append(doc)
    if errors:
        for e in errors:
            print("nanosoc-rm t0 fetch: %s" % e, file=sys.stderr)
        raise FetchErrors("%d pending runs could not be processed: %s" % (len(errors), "; ".join(errors)))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------------------------------------------
def results(configuration: str | None = None, *, config: Config | None = None) -> list[dict]:
    """The stored t0_results (latest per run), oldest trigger first."""
    prefix = "t0_result:%s:" % configuration if configuration else "t0_result:"
    docs = db.latest("t0_result", prefix, config=_cfg(config))
    return sorted(docs, key=lambda d: (d["fact"]["run"]["triggered_at"], d["subject"]))


def default_night(now: dt.datetime | None = None, config: Config | None = None) -> dt.date:
    """The most recent night whose nightly trigger time has passed (UTC)."""
    now = now or clock.now()
    hh, mm = (int(x) for x in _cfg(config)["schedule"]["nightly_utc"].split(":"))
    today = now.date()
    return today if (now.hour, now.minute) >= (hh, mm) else today - dt.timedelta(days=1)


def coverage(night: dt.date, *, config: Config | None = None) -> list[dict]:
    """interfaces.md section 10 (D2): one row per configuration (and @registry) with no ACCEPTED scheduled run
    triggered on `night` (UTC date). [] means the night is covered."""
    config = _cfg(config)
    expected = [c["id"] for c in configs.configurations(config)] + [configs.REGISTRY_JOB]
    by_cfg: dict[str, list[dict]] = {c: [] for c in expected}
    for d in db.latest("t0_result", "t0_result:", config=config):
        f = d["fact"]
        if f["run"]["trigger"] == "schedule" and clock.parse(f["run"]["triggered_at"]).date() == night \
                and f["configuration"] in by_cfg:
            by_cfg[f["configuration"]].append(f)
    out = []
    for cfg in expected:
        runs = by_cfg[cfg]
        if any(f["acceptance"] == "accepted" for f in runs):
            continue
        if not runs:
            why = "no scheduled run was triggered"
        else:
            why = "; ".join("run %s is %s%s" % (f["run"]["nonce"], f["acceptance"],
                                                 (": " + f["reason"]) if f["reason"] else "") for f in runs)
        out.append({"configuration": cfg, "night": night.isoformat(), "reason": why,
                    "runs": [{"nonce": f["run"]["nonce"], "acceptance": f["acceptance"], "verdict": f["verdict"]}
                             for f in runs]})
    return out


def items(nonce: str, gate: str, *, config: Config | None = None) -> list[dict]:
    """One gate's items (rm-bundle/items/<gate>.jsonl) from the stored bundle of an accepted run.
    KeyError: no such run. FileNotFoundError: the run has no stored bundle, or the gate wrote no items."""
    config = _cfg(config)
    if not re.fullmatch(r"[0-9a-f]{32}", nonce) or db.get_run(nonce, config=config) is None:
        raise KeyError("no run with nonce %s" % nonce)
    if not _GATE_ID.match(gate):
        raise KeyError("%r is not a gate id" % gate)
    path = bundle_dir(nonce, config) / "items" / ("%s.jsonl" % gate)
    if not path.is_file():
        raise FileNotFoundError("run %s has no stored items for %s" % (nonce, gate))
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
