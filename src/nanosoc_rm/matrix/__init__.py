"""The compatibility matrix in local mode (D3, D4, D7, D8, D9, B7; interfaces.md section 18).

    matrix(configuration, *, columns, components, config) -> matrix_view observation (not stored)
    run_cell(configuration, column, component, ...) -> [matrix_cell, matrix_quarantine...]   (cells.py)
    queue(configuration, *, columns, components, by, config) -> [job]     the cells as local jobs, pinned first
    nightly_plan(configuration, now, ...) -> [(component, column)]        what the nightly schedule WOULD queue
    NIGHTLY                                                                that schedule: shipped DISABLED
    bump_cell(configuration, overrides, *, config) -> {verdict, ...} | None  the stored cell of exactly one pin
                                                                           move, for the bump engine (section 19.6)

A row per component of the configuration's stored tree; columns pinned | in-range | latest-tag | head
(`columns`). Each cell is a local compat T0 run with that component moved (`cells`), queued through the local
job runner (`nanosoc_rm.jobs`, kind `matrix-cell`), never run inside a web request. The view shows each cell's
latest stored result, its age, the job queued or running for it, whether the result is for the column's current
commit (`stale` when the mirror moved since), and its gate-by-gate comparison with the same night's pinned run (D9).

A view may add tier t1 (`tiers=["t1"]`): every shown cell gets a t1 cell beside it, a separate
stored cell of the same (component, column) whose run adds the registry's t1 command (`cells`, `tiers`). The
view runs the tier's preflight for this host: no command is NOT-RUN, a missing tool INFRA "tool not on this host",
and neither is queued by Run.

Building and using this locally is allowed now. ENABLING the nightly matrix is not: it is automation and
waits for the Phase 2 entry gate (docs/phase-gates.yaml). `NIGHTLY.enabled` is False and the schedule module does
not list it; `nightly_plan` only says what it would queue.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from nanosoc_rm import clock, contracts
from nanosoc_rm.matrix import cells as K
from nanosoc_rm.matrix import columns as C
from nanosoc_rm.matrix import tiers as TI
from nanosoc_rm.matrix.cells import run_cell, set_owner          # noqa: F401 - the package's public functions

__all__ = ["matrix", "run_cell", "set_owner", "queue", "nightly_plan", "NIGHTLY", "COLUMNS", "parse_columns",
           "MatrixUsage", "JOB_KIND", "bump_cell", "parse_tiers", "TIERS"]

COLUMNS = C.COLUMNS
TIERS = TI.TIERS
JOB_KIND = "matrix-cell"


class MatrixUsage(ValueError):
    """Exit 3: an unknown column or tier."""


def parse_tiers(text: str | Sequence[str] | None) -> list[str]:
    try:
        return TI.parse_tiers(text)
    except ValueError as e:
        raise MatrixUsage(str(e)) from None


@dataclass(frozen=True)
class Nightly:
    name: str
    enabled: bool
    gate: str
    covers: str


NIGHTLY = Nightly(
    name="nightly_matrix", enabled=False,
    gate="Phase 2 entry gate (docs/phase-gates.yaml: deterministic generator, ephemeral runner for 7 nights, "
         "CPU-budgeted host); status not-met",
    covers="every night: the pinned column, plus every component whose column commits changed since the last "
           "night; the head column once a week (D3)")


def parse_columns(text: str | Sequence[str] | None) -> list[str]:
    if not text:
        return list(COLUMNS)
    parts = [p.strip() for p in (text.split(",") if isinstance(text, str) else text) if p.strip()]
    bad = [p for p in parts if p not in COLUMNS]
    if bad:
        raise MatrixUsage("unknown column(s) %s (columns: %s)" % (", ".join(bad), ", ".join(COLUMNS)))
    return [c for c in COLUMNS if c in parts]


def _jobs_by_cell(configuration: str, config) -> dict[tuple[str | None, str, str | None], dict]:
    """The newest job for each cell (queued, running, or finished without a stored cell), by (component,
    column, tier); tier None is the T0-only cell."""
    from nanosoc_rm import jobs
    out: dict[tuple[str | None, str, str | None], dict] = {}
    for j in reversed(jobs.jobs(config, limit=2000, kind=JOB_KIND, configuration=configuration)):
        out[(j["params"].get("component"), j["params"]["column"], j["params"].get("tier") or None)] = j
    return out


def _cell_view(doc: dict | None, job: dict | None, target: C.Target, row: C.Row | None, now: dt.datetime,
               stored: list[dict], quarantined: set[str]) -> dict:
    """One cell of the view."""
    v: dict = {"target": target.as_dict(), "status": "not-run", "verdict": None, "subject": None,
               "finished_at": None, "age_s": None, "stale": False, "job": None, "job_state": None,
               "same_as_pinned": bool(row is not None and C.same_as_pinned(row, target.commit)),
               "comparison": None, "flaky_gates": [], "quarantined_gates": []}
    if row is not None and target.commit is None:
        v["status"] = "no-target"
    if doc is not None:
        f = doc["fact"]
        v.update(status="result", verdict=f["verdict"], subject=doc["subject"], finished_at=f["finished_at"],
                 age_s=int((now - clock.parse(f["finished_at"])).total_seconds()), flaky_gates=f["flaky_gates"])
        ran_at = (f.get("target") or {}).get("commit")
        v["stale"] = row is not None and ran_at != target.commit
        fails = {g["id"] for a in f["attempts"][-1:] for g in a["gates"] if g["verdict"] in ("FAIL", "UNVERIFIED")}
        v["quarantined_gates"] = sorted(fails & quarantined)
        if f["column"] != C.PINNED:
            cmp_ = K.compare(f, K.baseline_for(f, stored), quarantined)
            v["comparison"] = {"baseline": cmp_["baseline"],
                               "changes": [r for r in cmp_["rows"] if r["change"] != "same"], "note": cmp_["note"]}
    if job is not None and (doc is None or job["state"] in ("queued", "running")
                            or clock.parse(job["created_at"]) > clock.parse(doc["fact"]["finished_at"])):
        v["job"], v["job_state"] = job["id"], job["state"]
        if job["state"] in ("queued", "running"):
            v["status"] = job["state"]
        elif doc is None or job["state"] in ("infra", "failed", "cancelled"):
            v["status"] = "job-" + job["state"]
            if job["state"] == "infra" and doc is None:
                v["verdict"] = "INFRA"                             # D7: a lost or killed job is never FAIL
    return v


def _preflight_view(cv: dict, ready: "TI.Ready") -> dict:
    """A tier cell this host cannot run, with no stored result and no live job: say so (no-tool INFRA, or
    no-command NOT-RUN) instead of "not run"."""
    if not ready.ready and cv["status"] in ("not-run",):
        cv.update(status="no-tool" if ready.verdict == "INFRA" else "no-command", verdict=ready.verdict,
                  reason=ready.reason)
    return cv


_PINNED_TARGET = {"column": C.PINNED, "commit": None, "ref": None, "why": None, "range": None}


class _Viewer:
    """The cells of one row: T0 (no tier, no preflight), or one tier's (its stored cells and its preflight)."""

    def __init__(self, latest: dict, by_job: dict, now, stored, active, ready=None):
        self.latest, self.by_job, self.now, self.stored, self.active, self.ready = (
            latest, by_job, now, stored, active, ready)

    def row_cells(self, r, cols: Sequence[str], pinned: dict, tier: str | None, sources: list | None) -> dict:
        out = {}
        for c in cols:
            if c == C.PINNED:
                out[c] = dict(pinned, target=dict(_PINNED_TARGET))
                continue
            doc = self.latest.get((r.component, c))
            cv = _cell_view(doc, self.by_job.get((r.component, c, tier)), r.targets[c], r, self.now, self.stored,
                            set(self.active))
            if tier is not None:
                cv["reason"] = (doc or {}).get("fact", {}).get("reason")
            if cv["same_as_pinned"] and cv["status"] in ("not-run", "no-target"):
                cv.update(status="same-as-pinned", verdict=pinned["verdict"], subject=pinned["subject"],
                          finished_at=pinned["finished_at"], age_s=pinned["age_s"])
            out[c] = _preflight_view(cv, self.ready) if self.ready is not None else cv
            if sources is not None:
                sources += r.targets[c].sources
        return out


def matrix(configuration: str, *, columns: Sequence[str] | None = None, components: Sequence[str] | None = None,
           config=None, now: dt.datetime | None = None, tiers: Sequence[str] | None = None) -> dict:
    """One matrix_view observation (module docstring). Reads stored data only: the latest walk, the mirrors,
    stored cells, the job records. Raises analysis UsageProblem (exit 3) or CannotAnswer (exit 2)."""
    from nanosoc_rm.analysis import _inputs as I
    cfg = I.config_of(config)
    cols = parse_columns(columns)
    tier_list = parse_tiers(tiers)
    now = now or clock.now()
    rws, tree = C.rows(configuration, cfg, components)
    croot = I.registry(cfg).configuration(configuration)
    stored = K.cells(configuration, cfg)
    latest = K.latest_cells(configuration, cfg)
    by_job = _jobs_by_cell(configuration, cfg)
    q_entries = K.quarantine(configuration, cfg)
    active = K.active_quarantine(q_entries, now)
    pinned = _cell_view(latest.get((None, C.PINNED)), by_job.get((None, C.PINNED, None)), C.Target(C.PINNED, None),
                        None, now, stored, set(active))
    readies = {t: TI.preflight(configuration, t, config=cfg) for t in tier_list}
    latest_t = {t: K.latest_cells(configuration, cfg, tier=t) for t in tier_list}
    pinned_t = {}
    for t in tier_list:
        pv = _cell_view(latest_t[t].get((None, C.PINNED)), by_job.get((None, C.PINNED, t)), C.Target(C.PINNED, None),
                        None, now, stored, set(active))
        pv["reason"] = (latest_t[t].get((None, C.PINNED)) or {}).get("fact", {}).get("reason")
        pinned_t[t] = _preflight_view(pv, readies[t])
    view = _Viewer(latest, by_job, now, stored, active)
    rows_out, sources = [], []
    for r in rws:
        row_out = {"component": r.component, "split": r.split, "catalogued": r.catalogued,
                   "pins": C.pinned_commits(r), "mirror_synced_at": r.mirror_at,
                   "cells": view.row_cells(r, cols, pinned, None, sources)}
        if tier_list:
            row_out["tier_cells"] = {}
            for t in tier_list:
                tv = _Viewer(latest_t[t], by_job, now, stored, active, readies[t])
                row_out["tier_cells"][t] = tv.row_cells(r, cols, pinned_t[t], t, None)
        rows_out.append(row_out)
    shown_subjects = {cv["subject"] for row in rows_out for cv in row["cells"].values()} | {
        cv["subject"] for row in rows_out for tc in (row.get("tier_cells") or {}).values() for cv in tc.values()}
    shown = [d for d in list(latest.values()) + [d for lt in latest_t.values() for d in lt.values()]
             if d["subject"] in shown_subjects]
    times = [d["observed_at"] for d in tree] + [d["observed_at"] for d in shown] or [clock.iso(now)]
    srcs = [{"kind": "derived", "command": "nanosoc-rm matrix %s%s%s%s" % (
        configuration, " --columns %s" % ",".join(cols) if list(cols) != list(COLUMNS) else "",
        "".join(" --component %s" % c for c in components or []),
        " --tiers %s" % ",".join(tier_list) if tier_list else "")}]
    seen = set()
    for s in sources:
        if s["command"] not in seen and len(srcs) < 60:
            seen.add(s["command"])
            srcs.append(s)
    doc = {"schema": 1, "kind": "matrix_view", "subject": "matrix_view:%s" % configuration,
           "observed_at": min(times, key=clock.parse), "sources": srcs,
           "fact": {"configuration": configuration, "commit": C.root_commit(tree, croot), "columns": cols,
                    "generated_at": clock.iso(now), "rows": rows_out, "pinned": pinned,
                    # a split lists the pins it is split across; a stored split flag the stored tree shows only one
                    # pin of (a partial tree) is no split here, rather than a view that fails its schema (503)
                    "splits": [{"component": r["component"], "pins": r["pins"]} for r in rows_out
                               if r["split"] and len(r["pins"]) > 1],
                    "quarantine": [dict(e["fact"], subject=e["subject"],
                                        active=e["fact"]["test"] in active) for e in q_entries],
                    "nightly": {"name": NIGHTLY.name, "enabled": NIGHTLY.enabled, "gate": NIGHTLY.gate,
                                "covers": NIGHTLY.covers}}}
    if tier_list:
        doc["fact"]["tiers"] = [readies[t].as_dict() for t in tier_list]
        doc["fact"]["pinned_tiers"] = pinned_t
    problems = contracts.validate_observation(doc)
    if problems:
        raise contracts.SchemaError("observation", problems)
    return doc


_NOT_QUEUED = ("queued", "running", "same-as-pinned", "no-target", "no-tool", "no-command")


def cell_requests(view: dict, *, rerun_results: bool = True) -> list[tuple]:
    """(component, column) of every cell in a view that a Run would queue: the pinned run first, then each cell
    with a target that is not the pinned tree and has no job queued or running. A view with tiers adds
    (component, column, tier) for each tier cell that this host can run (preflight ready), after the T0 cells."""
    out: list[tuple] = []
    f = view["fact"]
    if C.PINNED in f["columns"] and f["pinned"]["status"] not in ("queued", "running"):
        out.append((None, C.PINNED))
    for r in f["rows"]:
        for c, cv in r["cells"].items():
            if c == C.PINNED or cv["status"] in _NOT_QUEUED:
                continue
            if cv["status"] == "result" and not rerun_results and not cv["stale"]:
                continue
            out.append((r["component"], c))
    for t, pv in (f.get("pinned_tiers") or {}).items():
        if C.PINNED in f["columns"] and pv["status"] not in _NOT_QUEUED:
            out.append((None, C.PINNED, t))
    for r in f["rows"]:
        for t, tc in (r.get("tier_cells") or {}).items():
            for c, cv in tc.items():
                if c == C.PINNED or cv["status"] in _NOT_QUEUED:
                    continue
                if cv["status"] == "result" and not rerun_results and not cv["stale"]:
                    continue
                out.append((r["component"], c, t))
    return out


def _key(cell: Sequence) -> tuple:
    return (cell[0], cell[1], cell[2] if len(cell) > 2 else None)


def queue(configuration: str, *, columns: Sequence[str] | None = None, components: Sequence[str] | None = None,
          cells: Sequence[tuple] | None = None, by: str, config=None, tiers: Sequence[str] | None = None) -> list[dict]:
    """Queue cells as local jobs (pinned first) and return the job records. `cells` picks exact (component,
    column) pairs, or (component, column, tier) triples, from the view; otherwise every runnable cell of the
    filtered view. `tiers` adds the tier cells (only those this host can run)."""
    from nanosoc_rm import jobs
    from nanosoc_rm.analysis import _inputs as I
    cfg = I.config_of(config)
    view = matrix(configuration, columns=columns, components=components, config=cfg, tiers=tiers)
    runnable = cell_requests(view)
    want = runnable if cells is None else [c for c in runnable if _key(c) in {_key(x) for x in cells}]
    out = []
    for cell in want:
        comp, col, tier = _key(cell)
        params = {"configuration": configuration, "component": comp, "column": col}
        if tier:
            params["tier"] = tier
        out.append(jobs.submit(cfg, JOB_KIND, params, by))
    return out


def nightly_plan(configuration: str, now: dt.datetime, *, config=None, weekly_day: int = 6) -> list[tuple]:
    """What the (disabled) nightly matrix would queue at `now` (D3): the pinned column; every non-head cell whose
    column commit differs from its last stored run (the component changed); the head column only on
    `weekly_day` (0 = Monday)."""
    view = matrix(configuration, config=config, now=now)
    out: list[tuple] = [(None, C.PINNED)]
    for r in view["fact"]["rows"]:
        for c, cv in r["cells"].items():
            if c == C.PINNED or cv["target"]["commit"] is None or cv["same_as_pinned"]:
                continue
            if c == C.HEAD and now.weekday() != weekly_day:
                continue
            if cv["status"] != "result" or cv["stale"] or c == C.HEAD:
                out.append((r["component"], c))
    return out


def bump_cell(configuration: str, overrides: dict[str, str], *, config=None) -> dict | None:
    """The newest stored matrix cell of `configuration` whose pin overrides are exactly `overrides`
    ({tree_path: commit}), as the bump engine reads it (interfaces.md section 19.6): {verdict, observed_at, href,
    detail}, or None when no such cell is stored. A cell of the pinned column, or one NOT-RUN, is never a move's
    result. FLAKY (red, then green alone, D8) reads as UNVERIFIED: a bump never rests on a flaky green."""
    from nanosoc_rm.analysis import _inputs as I
    from urllib.parse import urlencode
    cfg = I.config_of(config)
    want = {str(k): str(v) for k, v in (overrides or {}).items()}
    if not want:
        return None
    for d in reversed(K.cells(configuration, cfg)):                 # newest first
        f = d["fact"]
        if f["column"] == C.PINNED or f["verdict"] == "NOT-RUN" or K.tier_of(f):
            continue                                                  # a tier cell is not the T0 cell of a move
        if {o["tree_path"]: o["commit"] for o in f["overrides"]} != want:
            continue
        verdict = "UNVERIFIED" if f["verdict"] == "FLAKY" else f["verdict"]
        detail = "matrix cell %s (column %s, configuration commit %s)%s" % (
            d["subject"], f["column"], f["configuration_commit"][:12],
            ": FLAKY (D8), read as UNVERIFIED" if f["verdict"] == "FLAKY" else
            (": %s" % f["reason"] if f.get("reason") else ""))
        return {"verdict": verdict, "observed_at": d["observed_at"],
                "href": "/matrix/%s/cell?%s" % (configuration, urlencode({"component": f["component"],
                                                                          "column": f["column"]})),
                "detail": detail[:500]}
    return None
