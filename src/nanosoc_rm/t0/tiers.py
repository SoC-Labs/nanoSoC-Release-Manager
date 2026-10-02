"""The test tiers and their budgets (D1; interfaces.md section 9 "Tiers and budget").

One table shared by the CLI (`nanosoc-rm tiers`), the dashboard and the roll-up. Budgets are D1's numbers,
taking the upper end of a range: T0 "under 2 min" = 120 s; T1 "~10 min" = 600 s; T2 "5-15 min per SoC" = 900 s;
T3 "1-3 h" = 10800 s; T6 "~6.5 h" = 23400 s. D1 gives T4 and T5 no number, so each carries a written reason.

The T0 budget is measured on the D5-verified bundle's `finished_at - started_at`, never on the job's exit. Over
budget is a warning (`over_budget: true` on the t0_result); it never changes a verdict. The job's
`timeout: 15m` is only the kill limit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from nanosoc_rm import clock

__all__ = ["Tier", "TIERS", "TIER_IDS", "Budget", "validate_tiers", "tier_commands", "tier_rows", "check_budget",
           "T0_COMMAND"]

TIER_IDS = ("t0", "t1", "t2", "t3", "t4", "t5", "t6")       # result-bundle.schema.json `tier` enum
T0_COMMAND = "nanosoc-rm t0 trigger %s"                   # -> compat-ci job t0 -> nanosoc-rm t0 run --out rm-bundle


@dataclass(frozen=True)
class Tier:
    id: str
    budget_s: int | None
    budget_reason: str | None
    manual: bool
    phase: str               # when a per-SoC command arrives
    name: str = ""


TIERS: dict[str, Tier] = {
    "t0": Tier("t0", 120, None, False, "v1", "resolve (no licence)"),
    "t1": Tier("t1", 600, None, False, "Phase 2", "static and unit"),
    "t2": Tier("t2", 900, None, False, "Phase 2", "smoke simulation, per SoC (1 VCS seat)"),
    "t3": Tier("t3", 10800, None, False, "Phase 2", "regression"),
    "t4": Tier("t4", None, "D1 gives no runtime for the FPGA build; the budget is set in Phase 6", False,
               "Phase 3", "FPGA build"),
    "t5": Tier("t5", None, "D1 gives no runtime for on-hardware tests, which are manual", True, "Phase 3",
               "on hardware"),
    "t6": Tier("t6", 23400, None, True, "Phase 3", "ASIC flow"),
}


def validate_tiers(tiers: Mapping[str, Tier]) -> list[str]:
    """Problems with a tier table: exactly t0..t6, each with a positive budget in seconds or a written reason."""
    problems = []
    if sorted(tiers) != sorted(TIER_IDS):
        problems.append("tiers must be exactly %s, got %s" % (", ".join(TIER_IDS), ", ".join(sorted(tiers)) or "none"))
    for tid in sorted(tiers):
        t = tiers[tid]
        if t.id != tid:
            problems.append("%s: row id is %r" % (tid, t.id))
        if t.budget_s is None:
            if not (isinstance(t.budget_reason, str) and t.budget_reason.strip()):
                problems.append("%s: no budget and no written reason" % tid)
        elif not (isinstance(t.budget_s, int) and not isinstance(t.budget_s, bool) and t.budget_s > 0):
            problems.append("%s: budget %r is not a positive number of seconds" % (tid, t.budget_s))
    return problems


def tier_commands(configuration: str) -> dict[str, str | None]:
    """tier -> the documented command for `configuration`, or None where none exists yet (v1: T0 only)."""
    return {tid: (T0_COMMAND % configuration if tid == "t0" else None) for tid in TIER_IDS}


def tier_rows(configuration: str | None = None, tiers: Mapping[str, Tier] | None = None) -> list[dict]:
    """One row per tier: {tier, budget_s, budget_reason, manual, phase, command} (the `tiers` CLI)."""
    tiers = TIERS if tiers is None else tiers
    cmds = tier_commands(configuration if configuration else "CONFIG")
    return [{"tier": tid, "budget_s": t.budget_s, "budget_reason": t.budget_reason, "manual": t.manual,
             "phase": t.phase, "command": cmds.get(tid)} for tid, t in sorted(tiers.items())]


@dataclass
class Budget:
    ok: bool
    seconds: int
    budget_s: int | None


def check_budget(tier: str, started_at: str, finished_at: str) -> Budget:
    """Whether a run of `tier` stayed inside its budget, from the verified bundle's own times. A tier with no
    budget is never over it. A finish before the start is a malformed bundle: ValueError."""
    t = TIERS[tier]
    seconds = int((clock.parse(finished_at) - clock.parse(started_at)).total_seconds())
    if seconds < 0:
        raise ValueError("finished_at %s is before started_at %s" % (finished_at, started_at))
    return Budget(ok=t.budget_s is None or seconds <= t.budget_s, seconds=seconds, budget_s=t.budget_s)

