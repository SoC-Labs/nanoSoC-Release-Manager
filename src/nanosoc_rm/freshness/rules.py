"""The freshness rule, as pure functions over stored facts (interfaces.md section 23.2; David 2026-10-14).

Nothing here reads git, the store or the clock: every function takes the measured facts and `today` (a UTC
date), so a page can re-judge stored facts on the day it is drawn and a test can pin the day.

Per pin, first match wins (the reviewer's rule, board R):

    frozen        the configuration is frozen or silicon: the pin reports what it WOULD be (`would_be`, the
                  ladder below) and is never counted as outdated
    unpushed      no branch or tag of any host holds the pinned commit
    not_measured  the commits behind could not be counted (no mirror, unreadable repo, commit not in the mirror,
                  no default branch, uncatalogued URL, no gitlink)
    current       0 commits behind the canonical default branch
    outdated      10 or more behind, or behind and the pin not bumped for 90 days or more
    behind        1 to 9 behind and bumped within 90 days (or the bump date is unknown)

Per tree: the verdict is the worst DIRECT pin (depth 1) by SEVERITY; "Updated" is the days since the newest bump
anywhere in the tree, worded Fresh under 30, Aging 30 to 89, Stale 90 or more.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

__all__ = ["STATES", "LIVE_STATES", "WORDS", "SEVERITY", "LAGGING", "OUTDATED_BEHIND", "OUTDATED_DAYS",
           "FRESH_DAYS", "STALE_DAYS", "AGE_WORDS", "day_of", "days_since", "age_word", "live_state", "judge_pin",
           "worst", "judge_tree"]

STATES = ("frozen", "unpushed", "not_measured", "current", "outdated", "behind")
LIVE_STATES = STATES[1:]
WORDS = {"frozen": "Frozen", "unpushed": "Unpushed", "not_measured": "Not measured", "current": "Current",
         "outdated": "Outdated", "behind": "Behind"}
# Worst first when rolling up: a pin nobody could measure ranks above Current, so a tree is Current only when
# every direct pin was measured at its upstream head (a zero that measured nothing is not green).
SEVERITY = {"unpushed": 4, "outdated": 3, "behind": 2, "not_measured": 1, "current": 0}
LAGGING = ("unpushed", "outdated", "behind")
OUTDATED_BEHIND = 10
OUTDATED_DAYS = 90
FRESH_DAYS = 30
STALE_DAYS = 90
AGE_WORDS = {"fresh": "Fresh", "aging": "Aging", "stale": "Stale"}


def day_of(ts: str | None) -> dt.date | None:
    """The UTC calendar date of a contract timestamp (or of any ISO 8601 time with an offset)."""
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).date()


def days_since(ts: str | None, today: dt.date) -> int | None:
    """Whole calendar days from the UTC date of `ts` to `today` (None when `ts` is None)."""
    d = day_of(ts)
    return None if d is None else (today - d).days


def age_word(days: int | None) -> str | None:
    """fresh (< 30 d), aging (30 to 89 d), stale (>= 90 d), or None when the age is unknown."""
    if days is None:
        return None
    return "fresh" if days < FRESH_DAYS else "aging" if days < STALE_DAYS else "stale"


def live_state(pin: dict, today: dt.date) -> str:
    """The ladder below `frozen` for one pin fact (scan.py's `level: pin` fact)."""
    if pin.get("unpushed") is True:
        return "unpushed"
    behind = pin.get("behind")
    if behind is None:
        return "not_measured"
    if behind == 0:
        return "current"
    bumped = days_since((pin.get("bump") or {}).get("date"), today)
    if behind >= OUTDATED_BEHIND or (bumped is not None and bumped >= OUTDATED_DAYS):
        return "outdated"
    return "behind"


def judge_pin(pin: dict, today: dt.date) -> dict:
    """{state, would_be, live, lagging, days_since_bump, pin_age_days} for one pin fact on `today`.
    `live` is the ladder's answer whatever the configuration; `state` is `frozen` for a frozen configuration's
    pin, which then carries the live answer as `would_be`. `lagging` counts toward a tree's totals: never for a
    frozen pin."""
    live = live_state(pin, today)
    frozen = bool(pin.get("frozen"))
    return {"state": "frozen" if frozen else live, "would_be": live if frozen else None, "live": live,
            "lagging": (not frozen) and live in LAGGING,
            "days_since_bump": days_since((pin.get("bump") or {}).get("date"), today),
            "pin_age_days": days_since(pin.get("pin_date"), today)}


def worst(states: Iterable[str]) -> str | None:
    """The most severe live state (SEVERITY), or None for none."""
    states = [s for s in states if s in SEVERITY]
    return max(states, key=lambda s: SEVERITY[s]) if states else None


def _pick(p: dict, j: dict, *keys: str) -> dict:
    out = {"tree_path": p["tree_path"], "component": p.get("component")}
    for k in keys:
        out[k] = j[k] if k in j else p.get(k)
    return out


def judge_tree(frozen: bool, pins: list[dict], today: dt.date) -> dict:
    """The tree-level judgement of one configuration's pin facts on `today`.

    state      `frozen` for a frozen configuration, else the worst direct pin (`not_measured` with no pins)
    would_be   a frozen configuration's live verdict (never counted), else None
    counts     pins per live state over the whole tree; `lagging`/`direct_lagging` count unpushed, outdated and
               behind pins, and are 0 for a frozen configuration (`would_be_lagging` keeps its numbers)
    updated_days / age   days since the newest bump in the whole tree, and its Fresh/Aging/Stale word
    """
    judged = [(p, judge_pin(p, today)) for p in pins]
    direct = [(p, j) for p, j in judged if p.get("depth") == 1]
    live_verdict = worst(j["live"] for _, j in direct) or "not_measured"
    counts = {s: 0 for s in LIVE_STATES}
    for _, j in judged:
        counts[j["live"]] += 1
    lag_all = [(p, j) for p, j in judged if j["live"] in LAGGING]
    lag_direct = [(p, j) for p, j in direct if j["live"] in LAGGING]
    bumped = [(p, j) for p, j in judged if j["days_since_bump"] is not None]
    bumped_direct = [(p, j) for p, j in bumped if p.get("depth") == 1]

    def newest(rows):
        if not rows:
            return None
        p, j = min(rows, key=lambda r: (r[1]["days_since_bump"], r[0]["tree_path"]))
        return dict(_pick(p, j), date=p["bump"]["date"], days=j["days_since_bump"])

    stalest = max(((p, j) for p, j in lag_all if j["days_since_bump"] is not None),
                  key=lambda r: (r[1]["days_since_bump"], r[0].get("behind") or 0), default=None)
    worst_lag = max(lag_all, key=lambda r: (SEVERITY[r[1]["live"]], r[0].get("behind") or 0), default=None)
    nb = newest(bumped)
    return {
        "state": "frozen" if frozen else live_verdict,
        "would_be": live_verdict if frozen else None,
        "counted": not frozen,
        "pins": len(pins),
        "direct": len(direct),
        "counts": counts,
        "lagging": 0 if frozen else len(lag_all),
        "direct_lagging": 0 if frozen else len(lag_direct),
        "would_be_lagging": len(lag_all) if frozen else None,
        "would_be_direct_lagging": len(lag_direct) if frozen else None,
        "total_behind": sum(p.get("behind") or 0 for p, _ in judged),
        "any_lower_bound": any(p.get("lower_bound") for p in pins),
        "newest_bump": nb,
        "newest_direct_bump": newest(bumped_direct),
        "updated_days": nb["days"] if nb else None,
        "age": age_word(nb["days"] if nb else None),
        "stalest_lagging": dict(_pick(*stalest, "behind"), days=stalest[1]["days_since_bump"],
                                state=stalest[1]["live"]) if stalest else None,
        "worst_lagging": dict(_pick(*worst_lag, "behind", "lower_bound"), state=worst_lag[1]["live"])
        if worst_lag else None,
    }

