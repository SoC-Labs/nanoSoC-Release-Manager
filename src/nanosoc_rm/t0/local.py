"""The T0 gates run LOCALLY (`nanosoc-rm compat`; interfaces.md section 9a).

The same gate table, checks and negative controls as the compat-ci job (`t0.gates`), in run mode `local`:

- allowed off CI; the Context comes from `Context.for_local` (anonymous, isolated git: empty HOME and TMPDIR,
  no global or system config, no ssh agent, so ~/.netrc and stored credentials are never read);
- every gate in `gates.LOCAL_NOT_RUN` (`t0.runner.hardened`) is recorded as one SKIP row with its reason and no
  control row; `gates.rollup(rows, scope, mode="local")` turns that into NOT-RUN, never PASS and never FAIL;
- `extra_not_run` adds may-skip gates the caller cannot judge in this run (compat with `--pin`: the upgrade-path
  clone checks the registered commit, not the what-if tree), recorded the same way;
- every other gate runs with its control in the same run (`run_with_control`); when `t0.infra.probe` is INFRA
  the host-dependent gates are recorded INFRA without running, as in the job.

Nothing is written but the work directory: no bundle, no store. This module never imports the CI runner.
"""
from __future__ import annotations

from typing import Mapping

from nanosoc_rm import clock
from nanosoc_rm.t0 import gates as G

__all__ = ["run_local"]


def _row(gate: str, status: str, detail: str, nonce: str) -> dict:
    return {"utc": clock.iso(), "status": status, "gate": gate, "detail": G.clean_detail(detail), "nonce": nonce}


def run_local(ctx: G.Context, scope: str = "configuration",
              extra_not_run: Mapping[str, str] | None = None) -> tuple[list[dict], dict[str, list[dict]]]:
    """(verdict rows, {gate id: items}) of one local run of `scope`'s gates on `ctx` (a `local`-mode Context)."""
    if ctx.mode != "local":
        raise ValueError("run_local needs a local-mode Context (Context.for_local)")
    extra = dict(extra_not_run or {})
    bad = [g for g in extra if g not in G.MAY_SKIP]
    if bad:
        raise ValueError("only a gate that may skip can be left NOT-RUN: %s" % ", ".join(bad))
    rows: list[dict] = []
    items: dict[str, list[dict]] = {}
    probe_infra = False
    for gid in G.gates_for(scope):
        if gid in G.LOCAL_NOT_RUN:
            rows.append(_row(gid, "SKIP", G.LOCAL_NOT_RUN[gid], ctx.nonce))
            continue
        if gid in extra:
            rows.append(_row(gid, "SKIP", extra[gid], ctx.nonce))
            continue
        if probe_infra and gid in G.HOST_DEPENDENT:
            rows.append(_row(gid, "INFRA", "not run: t0.infra.probe could not reach a host", ctx.nonce))
            continue
        pair = G.run_with_control(gid, ctx)
        items[gid] = ctx.items(gid)
        if gid == "t0.infra.probe" and pair[0]["status"] == "INFRA":
            probe_infra = True
        rows += list(pair)
    return rows, items
