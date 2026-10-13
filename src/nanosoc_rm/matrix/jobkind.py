"""The `matrix-cell` job kind (nanosoc_rm.jobs EXTRA_KINDS seam): one matrix cell as a local job.

The worker runs `nanosoc-rm matrix cell CONFIG --column COL [--component C] --store --json` as a child process
(the cell reads its job id from NANOSOC_RM_JOB, which the worker sets, and the login from that job's record), so
the cell and its D8 re-run (in the same job: alone, since the worker runs one job at a time) never run inside the
service. The job's verdict is the cell's.
"""
from __future__ import annotations

import re

from nanosoc_rm.matrix.columns import COLUMNS, PINNED

__all__ = ["params", "cli_args", "outcome", "limit_s", "LIMIT_S", "ACTION"]

ACTION = "run_t0"                 # a matrix cell is a T0 run: submitting one is logged like Run (H2)

LIMIT_S = 3600                    # two compat T0 runs (the D8 re-run) at the worker's 30-minute ceiling each
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}$")


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
    return {"configuration": cfg, "component": comp, "column": col}


def cli_args(p: dict) -> list[str]:
    args = ["matrix", "cell", p["configuration"], "--column", p["column"]]
    if p.get("component"):
        args += ["--component", p["component"]]
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


def limit_s(config, job: dict) -> float:
    return float(LIMIT_S)
