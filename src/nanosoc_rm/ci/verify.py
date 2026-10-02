"""Verify a fetched T0 bundle against the CI API's own record (interfaces.md section 10 Verify; D5, D10, D11).

    verify(run, pipeline, job, variables, bundle) -> Verdict

The checks run in this order; the first that fails makes the run `rejected` (checks 1 to 6) or `refused`
(check 7), and `Verdict.check` names it. The service trusts the CI API's records, never the bundle's word about
itself (D5: a job cannot forge its own verdict).

1. The API says: the pipeline is in the configured CI project (`ci.project_id`), `source == "trigger"`, its ref
   is in `ci.allowed_refs`, the job is named `ci.job` (`t0`), the pipeline id is the run's, and the job ran on a
   runner in `ci.allowed_runner_ids` (D10).
2. The API's `RM_REQUEST` variable is byte-equal to the stored request.
3. `provenance.json` exists and validates against result-bundle v1.
4. `run_nonce` is the run's nonce; `request` is the stored request minus `schema` and `nonce`; `ci.pipeline_id`,
   `ci.job_id` and `ci.harness_sha` equal the API's values (and `ci.project`, `ci.job_name`, `ci.ref` agree).
5. `captured.superproject_commit` equals `request.commit` (the D5 done-when).
6. `verdicts.tsv`: at least one row; 5 fields per row, each valid; every nonce is the run's; every timestamp
   inside the job's API start and finish, +/- 60 s.
7. The service's own vendor scan (nanosoc_rm.scan.scan_dir): a finding, or a scan that could not run, refuses the
   bundle. The reason gives rule ids and locations only, never matched text.

`verify` reads the bundle and decides; it stores and deletes nothing (the caller, fetch.py, does).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nanosoc_rm import clock, contracts
from nanosoc_rm.config import Config, load_config

__all__ = ["RunRecord", "Verdict", "verify", "read_rows", "read_provenance", "ROW_SLACK_S"]

ROW_SLACK_S = 60


@dataclass
class RunRecord:
    """One row of the `runs` table (interfaces.md section 12): what the service asked for, kept byte for byte."""
    nonce: str
    configuration: str
    commit: str
    request: bytes          # RM_REQUEST exactly as built, stored and sent
    trigger: str            # schedule | manual
    triggered_by: str
    triggered_at: str
    pipeline_id: int | None
    job_id: int | None
    state: str              # pending | accepted | rejected | refused | no-bundle


@dataclass
class Verdict:
    acceptance: str         # accepted | rejected | refused
    check: int | None       # the verify step that failed; None when accepted
    reason: str | None


class _Fail(Exception):
    def __init__(self, check: int, reason: str):
        super().__init__(reason)
        self.check, self.reason = check, reason


def _reject(check: int, reason: str) -> _Fail:
    return _Fail(check, "check %d: %s" % (check, reason))


def _ids(values: Any) -> str:
    return "[%s]" % ", ".join(str(v) for v in values)


def _check_api(run: RunRecord, pipeline: dict, job: dict, ci: dict) -> None:
    """Check 1: the CI API's own record of the pipeline and the job."""
    project_id = ci["project_id"]
    if pipeline.get("project_id") != project_id:
        raise _reject(1, "the API's pipeline is in project %s, not the CI project %s (%s)"
                      % (pipeline.get("project_id"), project_id, ci["project"]))
    jp = job.get("pipeline") or {}
    if "project_id" in jp and jp["project_id"] != project_id:
        raise _reject(1, "the API's job belongs to project %s, not the CI project %s" % (jp["project_id"], project_id))
    if pipeline.get("source") != "trigger":
        raise _reject(1, "pipeline source is %r, not trigger" % pipeline.get("source"))
    ref = pipeline.get("ref")
    if ref not in ci["allowed_refs"]:
        raise _reject(1, "pipeline ref %r is not in ci.allowed_refs %s" % (ref, ci["allowed_refs"]))
    for where, value in (("job", job.get("ref")), ("job's pipeline", jp.get("ref"))):
        if value is not None and value != ref:
            raise _reject(1, "%s ref %r differs from the pipeline ref %r" % (where, value, ref))
    if job.get("name") != ci["job"]:
        raise _reject(1, "job name is %r, not %r" % (job.get("name"), ci["job"]))
    if pipeline.get("id") != run.pipeline_id:
        raise _reject(1, "pipeline id %s is not the run's pipeline %s" % (pipeline.get("id"), run.pipeline_id))
    if "id" in jp and jp["id"] != pipeline.get("id"):
        raise _reject(1, "the job belongs to pipeline %s, not %s" % (jp["id"], pipeline.get("id")))
    runner = job.get("runner") or {}
    if runner.get("id") not in ci["allowed_runner_ids"]:
        raise _reject(1, "the job ran on runner %s, which is not in ci.allowed_runner_ids %s (D10)"
                      % (runner.get("id", "none"), _ids(ci["allowed_runner_ids"])))


def _check_variable(run: RunRecord, variables: list[dict]) -> None:
    """Check 2: the pipeline's RM_REQUEST, as the API reports it, is the stored request byte for byte."""
    values = [v.get("value") for v in variables or () if v.get("key") == "RM_REQUEST"]
    if not values:
        raise _reject(2, "RM_REQUEST not delivered to the pipeline (the CI API lists no such variable)")
    if len(values) > 1:
        raise _reject(2, "the pipeline carries %d RM_REQUEST variables" % len(values))
    got = values[0].encode() if isinstance(values[0], str) else None
    if got != bytes(run.request):
        raise _reject(2, "the pipeline's RM_REQUEST differs from the stored request of run %s" % run.nonce)


def read_provenance(bundle: Path) -> dict:
    """rm-bundle/provenance.json as JSON. _Fail(3) when missing, unreadable or not JSON."""
    path = Path(bundle) / "provenance.json"
    try:
        doc = json.loads(path.read_text())
    except OSError:
        raise _reject(3, "the bundle has no readable provenance.json") from None
    except ValueError as e:
        raise _reject(3, "provenance.json is not JSON (%s)" % e) from None
    if not isinstance(doc, dict):
        raise _reject(3, "provenance.json is not a JSON object")
    return doc


def _check_provenance(prov: dict) -> None:
    """Check 3: provenance.json validates against result-bundle v1."""
    problems = contracts.validate_provenance(prov)
    if problems:
        raise _reject(3, "provenance.json does not validate against result-bundle v1: %s" % "; ".join(problems[:3]))


def _check_identity(run: RunRecord, pipeline: dict, job: dict, prov: dict, ci: dict) -> dict:
    """Check 4: the bundle is this run's, as the API knows it. Returns the stored request."""
    stored = json.loads(bytes(run.request))
    if prov["run_nonce"] != run.nonce:
        raise _reject(4, "run_nonce %s is not this run's nonce %s" % (prov["run_nonce"], run.nonce))
    want = {k: v for k, v in stored.items() if k not in ("schema", "nonce")}
    if prov["request"] != want:
        diff = sorted(k for k in set(want) | set(prov["request"]) if want.get(k) != prov["request"].get(k))
        raise _reject(4, "provenance request differs from the stored request in %s" % ", ".join(diff))
    pc = prov["ci"]
    pairs = (("ci.pipeline_id", pc["pipeline_id"], pipeline.get("id")), ("ci.job_id", pc["job_id"], job.get("id")),
             ("ci.harness_sha", pc["harness_sha"], pipeline.get("sha")), ("ci.project", pc["project"], ci["project"]),
             ("ci.job_name", pc["job_name"], job.get("name")), ("ci.ref", pc["ref"], pipeline.get("ref")))
    for field, theirs, api in pairs:
        if theirs != api:
            raise _reject(4, "provenance %s is %s, the CI API says %s" % (field, theirs, api))
    if prov["tier"] != "t0":
        raise _reject(4, "provenance tier is %s; v1 runs t0 only" % prov["tier"])
    return stored


def _check_commit(prov: dict, stored: dict) -> None:
    """Check 5: the job fetched the commit it was asked for (D5 done-when)."""
    got, want = prov["captured"]["superproject_commit"], stored["commit"]
    if got != want:
        raise _reject(5, "captured.superproject_commit %s != requested %s (D5)" % (got, want))


def read_rows(bundle: Path) -> list[dict]:
    """verdicts.tsv as row dicts {utc, status, gate, detail, nonce}. _Fail(6) for a missing file or a row that is
    not 5 tab-separated fields."""
    path = Path(bundle) / "verdicts.tsv"
    try:
        text = path.read_text()
    except OSError:
        raise _reject(6, "the bundle has no readable verdicts.tsv") from None
    rows, lines = [], text.split("\n")
    for n, line in enumerate(lines, 1):
        if line == "" and n == len(lines):
            break                                        # the newline that ends the last row
        try:
            rows.append(contracts.parse_verdict_row(line))
        except ValueError as e:
            raise _reject(6, "verdicts.tsv line %d: %s" % (n, e)) from None
    return rows


def _check_rows(run: RunRecord, job: dict, bundle: Path) -> None:
    """Check 6: the rows are well formed, this run's, and written while the job ran."""
    rows = read_rows(bundle)
    if not rows:
        raise _reject(6, "verdicts.tsv holds no rows: nothing was checked")
    try:
        lo = clock.parse(job["started_at"]) - dt.timedelta(seconds=ROW_SLACK_S)
        hi = clock.parse(job["finished_at"]) + dt.timedelta(seconds=ROW_SLACK_S)
    except (KeyError, TypeError, ValueError):
        raise _reject(6, "the CI API gives the job no start and finish time to check the rows against") from None
    for n, row in enumerate(rows, 1):
        problems = contracts.validate_verdict_row(row)
        if problems:
            raise _reject(6, "verdicts.tsv line %d is not a valid row: %s" % (n, "; ".join(problems[:2])))
        if row["nonce"] != run.nonce:
            raise _reject(6, "verdicts.tsv line %d carries nonce %s, not this run's %s" % (n, row["nonce"], run.nonce))
        t = clock.parse(row["utc"])
        if not lo <= t <= hi:
            raise _reject(6, "verdicts.tsv line %d is stamped %s, outside the job's run %s to %s (+/- %d s)"
                          % (n, row["utc"], job["started_at"], job["finished_at"], ROW_SLACK_S))


def _scan_dir() -> Callable[[Path], Any]:
    from nanosoc_rm.scan import scan_dir           # section 11; imported only when a bundle reaches check 7
    return scan_dir


def _check_scan(bundle: Path, scanner: Callable[[Path], Any]) -> None:
    """Check 7: the service's own vendor scan (D11). Locations and rule ids only, never the matched text."""
    result = scanner(Path(bundle))
    verdict = getattr(result, "verdict", None)
    if verdict == "clean":
        return
    if verdict == "finding":
        where = ", ".join("%s at %s:%s" % (f["rule"], f["file"], f["line"]) for f in result.findings)
        raise _Fail(7, "check 7: the vendor scan found %s (D11); the bundle is refused and deleted" % where)
    unarmed = sorted(r for r, ok in (getattr(result, "armed", None) or {}).items() if not ok)
    why = ("%d files scanned" % getattr(result, "files_scanned", 0)) if not unarmed else \
        "rules not armed on their specimens: %s" % ", ".join(unarmed)
    raise _Fail(7, "check 7: the vendor scan could not run (%s); the bundle is refused and deleted" % why)


def _ci_settings(config: Config) -> dict:
    return {"project": config.require("ci", "project"), "project_id": config.require("ci", "project_id"),
            "allowed_refs": config.require("ci", "allowed_refs"), "job": config["ci"].get("job", "t0"),
            "allowed_runner_ids": config.require("ci", "allowed_runner_ids")}


def verify(run: RunRecord, pipeline: dict, job: dict, variables: list[dict], bundle: Path, *,
           config: Config | None = None, scanner: Callable[[Path], Any] | None = None) -> Verdict:
    """interfaces.md section 10 Verify. `scanner` replaces nanosoc_rm.scan.scan_dir (unit tests only)."""
    config = config if config is not None else load_config()
    ci = _ci_settings(config)
    try:
        _check_api(run, pipeline, job, ci)
        _check_variable(run, variables)
        prov = read_provenance(bundle)
        _check_provenance(prov)
        stored = _check_identity(run, pipeline, job, prov, ci)
        _check_commit(prov, stored)
        _check_rows(run, job, bundle)
        _check_scan(bundle, scanner or _scan_dir())
    except _Fail as f:
        return Verdict("refused" if f.check == 7 else "rejected", f.check, f.reason)
    return Verdict("accepted", None, None)
