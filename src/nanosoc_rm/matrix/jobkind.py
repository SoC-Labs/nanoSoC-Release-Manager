"""The `matrix-cell` job kind (registered with nanosoc_rm.jobs.register_kind): one matrix cell as a local job.

The worker runs `nanosoc-rm matrix cell CONFIG --column COL [--component C] [--tier t1 [--force-tier]] --store
--json` as a child process
(the cell reads its job id from NANOSOC_RM_JOB, which the worker sets, and the login from that job's record), so
the cell and its D8 re-run (in the same job: alone, since the worker runs one job at a time) never run inside the
service. The job's verdict is the cell's. A tier cell (param `tier`, CR-WD-matrix-t1) gets a longer ceiling: two
T0 runs plus two tier runs at the tier's kill limit (budget x 1.5).
"""
from __future__ import annotations

import re

from nanosoc_rm.jobs import register_kind
from nanosoc_rm.matrix.columns import COLUMNS, PINNED
from nanosoc_rm.matrix.tiers import TIERS

__all__ = ["params", "cli_args", "outcome", "limit_s", "LIMIT_S", "ACTION"]

ACTION = "run_t0"                 # a matrix cell is a T0 run: submitting one is logged like Run (H2)

LIMIT_S = 3600                    # two compat T0 runs (the D8 re-run) at the worker's 30-minute ceiling each
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")


def params(p: dict) -> dict:
    """Checked parameters; ValueError when the CLI would exit 3 (the worker turns it into JobUsage)."""
    cfg, comp, col = p.get("configuration"), p.get("component") or None, p.get("column")
    if not isinstance(cfg, str) or not _ID.match(cfg):
        raise ValueError("%r is not a configuration id" % str(cfg)[:60])
    if col not in COLUMNS:
        raise ValueError("column must be one of %s" % ", ".join(COLUMNS))
    if (col == PINNED) != (comp is None):
        raise ValueError("the pinned column takes no component; every other column needs one")
    if comp is not None and (not isinstance(comp, str) or not _ID.match(comp)):
        raise ValueError("%r is not a component id" % str(comp)[:60])
    out = {"configuration": cfg, "component": comp, "column": col}
    tier, force = p.get("tier") or None, p.get("force_tier")
    if tier is not None:
        if tier not in TIERS:
            raise ValueError("tier must be one of %s" % ", ".join(TIERS))
        out["tier"] = tier
    if force not in (None, False, True):
        raise ValueError("force_tier must be true or false")
    if force:
        if tier is None:
            raise ValueError("force_tier needs a tier")
        out["force_tier"] = True
    return out


def cli_args(p: dict) -> list[str]:
    args = ["matrix", "cell", p["configuration"], "--column", p["column"]]
    if p.get("component"):
        args += ["--component", p["component"]]
    if p.get("tier"):
        args += ["--tier", p["tier"]] + (["--force-tier"] if p.get("force_tier") else [])
    return args + ["--store", "--json"]


def outcome(docs: list[dict], last_line: str) -> dict:
    """The job's verdict, reason and result from the documents the child printed (exit 0, 1 or 2)."""
    doc = next((d for d in reversed(docs) if d.get("kind") == "matrix_cell"), None)
    if doc is None:
        return {"state": "done", "verdict": "UNVERIFIED", "reason": last_line or "matrix cell printed no result"}
    f = doc["fact"]
    return {"state": "done", "verdict": f["verdict"], "reason": f.get("reason"),
            "result": {"subject": doc["subject"], "verdict": f["verdict"], "column": f["column"],
                       "component": f["component"], "attempts": len(f["attempts"]),
                       "flaky_gates": f["flaky_gates"]}}


TIER_KILL_DEFAULT_S = 900         # a tier whose spec cannot be read: the t1 lint budget (600 s) x 1.5


def limit_s(config, job: dict) -> float:
    tier = (job.get("params") or {}).get("tier")
    if not tier:
        return float(LIMIT_S)
    kill = TIER_KILL_DEFAULT_S
    try:
        from nanosoc_rm.analysis.tierrun import KILL_FACTOR
        from nanosoc_rm.matrix.tiers import spec_of
        spec = spec_of(job["params"]["configuration"], tier, config) or {}
        kill = int(spec.get("budget_s") or 0) * KILL_FACTOR or kill
    except Exception:                                             # noqa: BLE001 - the default ceiling
        pass
    return float(LIMIT_S + 2 * kill)


register_kind("matrix-cell", params, cli_args, outcome=outcome, limit_s=limit_s, action=ACTION)
