"""One matrix cell: a local compat T0 run with one component moved, judged and stored (D3, D4, D7, D8, D9).

    run_cell(configuration, column, component=None, *, config, store, by, ...) -> [matrix_cell, matrix_quarantine...]

1. **Target** (`columns`): the column's commit of the component's canonical repo. The pinned column (component
   None) moves nothing. No commit (no range, no version tag) is NOT-RUN with the reason; a commit equal to every
   pinned commit of the component is NOT-RUN "same as pinned" (the pinned cell is that result).
2. **Run** `analysis.compat(configuration, overrides)` with its work directory kept, then capture what the run
   read (`provenance.capture`) and delete the directory. Host load before and after, tool versions.
3. **Judge.** The compat verdict, then, in order:
   - D7: an exception that is the host's, not the tree's (out of memory, no space, a timeout, a lost process) is
     INFRA; so is a FAIL whose every failing gate's detail is an infrastructure signature (`INFRA_SIGNATURES`:
     timed out, out of memory, killed, licence denied, ...). INFRA never counts as FAIL.
     An overloaded host (1-minute load above `provenance.OVERLOAD_PER_CPU` per CPU at an attempt's start or
     end) is recorded per attempt; a FAIL on an overloaded host whose re-run is green is INFRA, not FLAKY (and so
     never counts toward quarantine). Red twice stays FAIL whatever the load: the tree failed both times.
   - D4: a pin captured at a commit other than the requested one makes the cell UNVERIFIED (`moved`).
   - D8: a FAIL runs again once, alone (the job worker runs one job at a time and this is the same job). Red again
     is FAIL; green (PASS, WARN, WAIVED) is FLAKY, which blocks auto-merge; anything else is UNVERIFIED. The gates
     that failed first and not on the re-run are the cell's `flaky_gates`.
4. **Quarantine** (D8): a test is a T0 gate of one configuration. When a FLAKY cell makes it the second FLAKY of
   that gate within 14 days, the gate is quarantined for 30 days with an owner (the login that ran the cell; a
   person changes it with `matrix quarantine --owner`). An entry stops counting at its expiry without anyone acting.
5. **Baseline** (D9): the cell is compared gate by gate with the pinned cell of the same configuration commit
   run within 12 hours of it (the same night), never with older history; the comparison names that run.

Stored (event kind `matrix_cell`) when `store`; the job always stores. A what-if is not involved: every cell's
inputs are registered refs, so its result is a fact about them.

**Tier cells.** `tier="t1"` makes a separate cell of the same (component, column): the run is
`compat(..., tier="t1")`, T0 then the registry's t1 command in a sandbox checkout of the same tree, and the
attempt's verdict is compat's `run_verdict` (T0 and the tier). Before anything runs, `tiers.preflight`: no t1
command (or a refused one) is NOT-RUN; a tool the spec names that this host lacks is INFRA "tool not on this host"
with one attempt that ran nothing (no T0 either). After the command, D4 re-reads the tier's working checkout
(`provenance.capture_checkout`): a pin checked out, or recorded in its parent's index, at another commit than
requested is MOVED and the attempt UNVERIFIED. The D4 IP hash is the hash of the file list the tier declares
(`ip_files`) and wrote, else null with the reason. A tier cell's D9 baseline is the pinned cell of the same tier.
`force_tier` (a person's `--force-tier`) runs the tier past a T0 FAIL; the cell stays FAIL.
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from nanosoc_rm import clock, contracts
from nanosoc_rm.matrix import columns as C
from nanosoc_rm.matrix import provenance as P

__all__ = ["run_cell", "compare", "baseline_for", "classify_infra", "cells", "latest_cells", "quarantine",
           "set_owner", "active_quarantine", "INFRA_SIGNATURES", "FLAKY_WINDOW", "QUARANTINE_FOR", "NIGHT_WINDOW",
           "CELL_VERDICTS", "GREEN", "subject_of_cell", "exit_for", "CellUsage", "why_not_comparable", "tier_of"]

GREEN = ("PASS", "WARN", "WAIVED")
CELL_VERDICTS = ("PASS", "WARN", "WAIVED", "FAIL", "FLAKY", "UNVERIFIED", "INFRA", "NOT-RUN")
FLAKY_WINDOW = dt.timedelta(days=14)
QUARANTINE_FOR = dt.timedelta(days=30)
NIGHT_WINDOW = dt.timedelta(hours=12)
# Whole phrases a host prints, never a bare word: `timeout`, `oom` or `killed` alone also occur inside file names
# (rtl/watchdog_timeout.v, bloom_filter.v), and a real finding naming such a file must stay a FAIL (D7).
INFRA_SIGNATURES = re.compile(
    r"(?<![\w./-])(?:"
    r"timed out"
    r"|time ?out (?:after|of|expired|exceeded|reached)"
    r"|out of memory|cannot allocate memory|memoryerror"
    r"|oom[- ]kill(?:ed|er)?"
    r"|killed by (?:signal|the kernel|the oom)|killed \(signal|sigkill"
    r"|no space left on device|disk quota exceeded"
    r"|resource temporarily unavailable|too many open files"
    r"|licen[cs]e (?:checkout )?(?:denied|failed|unavailable|refused)"
    r")(?![\w/-])", re.I)
_INFRA_EXC = (MemoryError, TimeoutError)


class CellUsage(ValueError):
    """A cell that cannot be asked for: unknown column, a component with the pinned column. Exit 3."""


def subject_of_cell(fact: Mapping) -> str:
    return "matrix_cell:%s:%s:%s:%s" % (fact["configuration"], fact["component"] or "-", fact["column"],
                                        fact["nonce"])


def tier_of(fact: Mapping) -> str | None:
    """The tier a stored cell added to its T0 run (None for a T0-only cell)."""
    return fact.get("tier") or None


def exit_for(verdict: str) -> int:
    return 0 if verdict in GREEN else 1 if verdict in ("FAIL", "FLAKY") else 2


# ---------------------------------------------------------------------------------------------------------
# D7
# ---------------------------------------------------------------------------------------------------------
def classify_infra(result: Mapping) -> str | None:
    """The reason a FAIL compat_result is infrastructure, not the tree (D7), else None: every gate that is FAIL
    says so in its detail (a timeout, out of memory, a kill, a licence refusal)."""
    f = result["fact"]
    if f["verdict"] != "FAIL":
        return None
    failing = [g for g in f["gates"] if g["verdict"] == "FAIL"]
    if failing and all(INFRA_SIGNATURES.search(g.get("detail") or "") for g in failing):
        return "INFRA, not FAIL (D7): every failing gate failed for an infrastructure reason: %s" % "; ".join(
            "%s: %s" % (g["id"], (g.get("detail") or "")[:160]) for g in failing)
    return None


# ---------------------------------------------------------------------------------------------------------
# Store reads
# ---------------------------------------------------------------------------------------------------------
def cells(configuration: str, config) -> list[dict]:
    """Every stored matrix_cell of a configuration (each subject holds one run), oldest first."""
    from nanosoc_rm import db
    docs = db.latest("matrix_cell", "matrix_cell:%s:" % configuration, config=config)
    return sorted(docs, key=lambda d: clock.parse(d["fact"]["finished_at"]))


def latest_cells(configuration: str, config, tier: str | None = None) -> dict[tuple[str | None, str], dict]:
    """{(component, column): newest stored cell} of one tier (None: the T0-only cells, as before tier cells)."""
    out: dict[tuple[str | None, str], dict] = {}
    for d in cells(configuration, config):
        if tier_of(d["fact"]) != tier:
            continue
        out[(d["fact"]["component"], d["fact"]["column"])] = d
    return out


def baseline_for(cell_fact: Mapping, stored: list[dict]) -> dict | None:
    """D9: the pinned cell of the same configuration commit nearest in time within NIGHT_WINDOW of the cell's
    start (the same night's pinned run), or None. Never an older run outside the window. A tier cell's baseline
    is the pinned cell of the same tier (its gates include the tier's rows)."""
    if cell_fact["column"] == C.PINNED:
        return None
    t = clock.parse(cell_fact["started_at"])
    tier = tier_of(cell_fact)
    cands = [d for d in stored if d["fact"]["column"] == C.PINNED and d["fact"]["verdict"] != "NOT-RUN"
             and tier_of(d["fact"]) == tier
             and d["fact"]["configuration_commit"] == cell_fact["configuration_commit"]
             and abs(clock.parse(d["fact"]["started_at"]) - t) <= NIGHT_WINDOW]
    if not cands:
        return None
    # prefer a run with usable gate results; an unusable one is still named (compare says why it is not compared)
    usable = [d for d in cands if why_not_comparable(d["fact"]) is None] or cands
    return min(usable, key=lambda d: abs(clock.parse(d["fact"]["started_at"]) - t))


def why_not_comparable(fact: Mapping) -> str | None:
    """Why a pinned run cannot stand as a D9 baseline, or None when it can: no gate results (an INFRA or
    unanswered run), or a run that read moved pins (its gates are about another tree)."""
    if not _gates(fact):
        return "it has no gate results (verdict %s)" % fact.get("verdict")
    if (fact.get("provenance") or {}).get("moved"):
        return "it read moved pins (verdict %s), so its gates are not about the pinned tree" % fact.get("verdict")
    return None


def _gates(fact: Mapping) -> dict[str, str]:
    att = (fact.get("attempts") or [])
    return {g["id"]: g["verdict"] for g in (att[-1]["gates"] if att else [])}


def compare(cell_fact: Mapping, baseline: Mapping | None, quarantined: set[str] = frozenset()) -> dict:
    """D9: the cell's gates against the baseline's, gate by gate. Names the baseline run, or says why none."""
    if baseline is None:
        return {"baseline": None, "rows": [], "note": "no pinned run of this configuration commit within %d h of this "
                "cell: not compared (D9 compares with the same night's pinned run, never with history)"
                % (NIGHT_WINDOW.total_seconds() // 3600)}
    bf = baseline["fact"]
    named = {"subject": baseline["subject"], "nonce": bf["nonce"], "verdict": bf["verdict"],
             "finished_at": bf["finished_at"]}
    why = why_not_comparable(bf)
    if why:
        return {"baseline": named, "rows": [], "note": "the same night's pinned run %s cannot be compared: %s. "
                "Not compared (D9 never falls back to older history); run the pinned cell again"
                % (bf["nonce"][:12], why)}
    mine, theirs = _gates(cell_fact), _gates(bf)
    bad = ("FAIL", "UNVERIFIED")
    answered_good = ("PASS", "WARN", "WAIVED")          # the cell checked the gate and it held
    rows = []
    for gid in sorted(set(mine) | set(theirs)):
        b, c = theirs.get(gid), mine.get(gid)
        # A baseline failure is "fixed" only by an answer that the gate held; a cell that gave no answer (INFRA,
        # NOT-RUN, or no row) is "unanswered": missing evidence is never shown as an improvement.
        change = ("same" if b == c else "new-failure" if c in bad and b not in bad else
                  "fixed" if b in bad and c in answered_good else
                  "unanswered" if b in bad and c not in bad else "changed")
        rows.append({"gate": gid, "baseline": b, "cell": c, "change": change, "quarantined": gid in quarantined})
    return {"baseline": named, "rows": rows, "note": None}


# ---------------------------------------------------------------------------------------------------------
# Quarantine (D8)
# ---------------------------------------------------------------------------------------------------------
def quarantine(configuration: str, config) -> list[dict]:
    """Every stored matrix_quarantine entry of a configuration (latest per gate), active and expired."""
    from nanosoc_rm import db
    return db.latest("matrix_quarantine", "matrix_quarantine:%s:" % configuration, config=config)


def active_quarantine(entries: list[dict], now: dt.datetime | None = None) -> dict[str, dict]:
    now = now or clock.now()
    return {e["fact"]["test"]: e for e in entries if clock.parse(e["fact"]["expires_at"]) > now}


def _quarantine_doc(configuration: str, test: str, since: str, owner: str, flaky_cells: list[str], set_by: str,
                    command: str) -> dict:
    exp = clock.iso(clock.parse(since) + QUARANTINE_FOR)
    return {"schema": 1, "kind": "matrix_quarantine",
            "subject": "matrix_quarantine:%s:%s" % (configuration, test), "observed_at": since,
            "sources": [{"kind": "derived", "command": command}],
            "fact": {"configuration": configuration, "test": test, "since": since, "expires_at": exp,
                     "owner": owner, "set_by": set_by, "flaky_cells": flaky_cells,
                     "reason": "two FLAKY results within %d days (D8)" % FLAKY_WINDOW.days}}


def _update_quarantine(cell: dict, stored: list[dict], config, store: bool) -> list[dict]:
    """New quarantine entries this FLAKY cell causes (two FLAKY of a gate within FLAKY_WINDOW)."""
    f = cell["fact"]
    if f["verdict"] != "FLAKY":
        return []
    now = clock.parse(f["finished_at"])
    active = active_quarantine(quarantine(f["configuration"], config), now)
    recent = [d for d in stored if d["fact"]["verdict"] == "FLAKY" and d["subject"] != cell["subject"]
              and now - clock.parse(d["fact"]["finished_at"]) <= FLAKY_WINDOW] + [cell]
    out = []
    for test in f["flaky_gates"]:
        hits = [d["subject"] for d in recent if test in d["fact"]["flaky_gates"]]
        if len(hits) >= 2 and test not in active:
            doc = _quarantine_doc(f["configuration"], test, f["finished_at"], f["by"], hits, "matrix (automatic)",
                                  "nanosoc-rm matrix cell %s --column %s%s%s" % (
                                      f["configuration"], f["column"],
                                      " --component %s" % f["component"] if f["component"] else "",
                                      " --tier %s" % tier_of(f) if tier_of(f) else ""))
            _validate(doc)
            if store:
                from nanosoc_rm import db
                db.put(doc, config=config)
            out.append(doc)
    return out


def set_owner(configuration: str, test: str, owner: str, by: str, *, config) -> dict:
    """A person names the owner of an active quarantine entry (stored as a new observation of the same subject)."""
    from nanosoc_rm import db
    cur = active_quarantine(quarantine(configuration, config)).get(test)
    if cur is None:
        raise CellUsage("no active quarantine for %s on %s" % (test, configuration))
    f = cur["fact"]
    doc = _quarantine_doc(configuration, test, f["since"], owner, f["flaky_cells"], by,
                          "nanosoc-rm matrix quarantine %s --owner %s=%s" % (configuration, test, owner))
    doc["fact"]["expires_at"] = f["expires_at"]
    _validate(doc)
    db.put(doc, config=config)
    return doc


# ---------------------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------------------
def _validate(doc: dict) -> None:
    problems = contracts.validate_observation(doc)
    if problems:
        raise contracts.SchemaError("observation", problems)


_TIER_STATUS = {"SKIP": "NOT-RUN"}            # verdicts.tsv statuses that are not gate verdicts


def _tier_part(td: Mapping) -> dict:
    return {"tier": td["tier"], "verdict": td["verdict"], "reason": (td.get("reason") or None) and td["reason"][:500],
            "forced": bool(td.get("forced")), "seconds": td.get("seconds"), "exit_code": td.get("exit_code"),
            "killed": bool(td.get("killed")), "over_budget": bool(td.get("over_budget")),
            "missing_paths": list(td.get("missing_paths") or []), "log_tail": list(td.get("log_tail") or [])[-20:]}


def _checkout_unread(co: Mapping, tier: str, ran: bool) -> str | None:
    """D4 for a tier's checkout beyond moved commits: a laid-out repo the command removed (LOST), a tracked file
    it changed (DIRTY), or no layout record from a command that ran (removed and not-laid-out cannot be told apart).
    The reason, or None."""
    if not ran:
        return None
    if co.get("laid_out") is None:
        return ("D4: the %s run left no layout record (%s), so a repo its command removed cannot be told from one "
                "not laid out: the run's inputs could not be captured" % (tier, "analysis.tierrun.LAYOUT_RECORD"))
    parts = []
    if co.get("lost"):
        parts.append("laid-out repo(s) %s could not be re-read after the %s command (removed or broken by it): "
                     "inputs not captured" % (", ".join(x or "(superproject)" for x in co["lost"][:5]), tier))
    if co.get("dirty"):
        files = ["%s/%s" % (d["tree_path"], x) if d["tree_path"] else x for d in co["dirty"] for x in d["files"]]
        parts.append("the %s command changed %d tracked file(s) of the requested commits: %s" % (
            tier, len(files), ", ".join(files[:5]) + (" ..." if len(files) > 5 else "")))
    return ("D4: " + "; ".join(parts))[:600] if parts else None


def _merge_moved(*lists: list[dict]) -> list[dict]:
    out, seen = [], set()
    for lst in lists:
        for m in lst:
            k = (m["tree_path"], m["captured"])
            if k not in seen:
                seen.add(k)
                out.append(m)
    return out


def _no_tool_attempt(reason: str) -> dict:
    """D7: the tier's tool is not on this host. Nothing ran (no T0 either): one attempt that records the host and
    the reason, so the cell is INFRA (schema: every verdict but NOT-RUN has an attempt)."""
    now = clock.iso()
    h = P.host_load()
    host = {"cpus": h["cpus"], "load_start": h["load"], "load_end": h["load"]}
    host["overloaded"] = P.overloaded(host)
    return {"nonce": secrets.token_hex(16), "started_at": now, "finished_at": now, "verdict": "INFRA",
            "reason": "INFRA (D7): %s" % reason, "gates": [], "compat_subject": None, "not_run": 0,
            "over_budget": False, "unjudged_overrides": [], "requested": None, "captured": None, "moved": [],
            "host": host, "tier": None, "checkout": None,
            "ip": {"hash": None, "note": "nothing ran: no IP hash", "files": 0}}


def _attempt(configuration: str, overrides: dict, cfg, compat_fn: Callable, capture_fn: Callable,
             url_map, tier: str | None = None, force_tier: bool = False, spec: Mapping | None = None) -> dict:
    """One compat run with its D4 capture. Never raises for the host's failures: they become INFRA (D7).
    With `tier`, the run adds that tier and the capture also re-reads the tier's checkout (module docstring)."""
    nonce = secrets.token_hex(16)
    started = clock.iso()
    load_start = P.host_load()
    work = Path(tempfile.mkdtemp(prefix="nanosoc-rm-matrix-"))
    keep = work / "compat"
    att: dict[str, Any] = {"nonce": nonce, "started_at": started, "finished_at": started, "verdict": "UNVERIFIED",
                           "reason": None, "gates": [], "compat_subject": None, "not_run": 0, "over_budget": False,
                           "unjudged_overrides": [], "requested": None, "captured": None, "moved": [],
                           "host": {"cpus": load_start["cpus"], "load_start": load_start["load"], "load_end": None}}
    tier_kw: dict[str, Any] = {}
    if tier:
        tier_kw = {"tier": tier, "force_tier": bool(force_tier)}
        att.update(tier=None, checkout=None, ip={"hash": None, "note": "the %s command did not run: no IP hash" % tier,
                                                 "files": 0})
    try:
        res = _run_compat(att, compat_fn, configuration, overrides, cfg, keep, nonce, url_map, tier_kw)
        if res is None:
            return att
        f = res["fact"]
        requested = _record_compat(att, res, tier, keep, spec)
        td = f.get("tier") if tier else None
        try:
            cap = capture_fn(keep / "run", f["commit"], requested, overrides, work)
        except Exception as e:                                    # noqa: BLE001 - an unreadable capture
            cap = None
            att["verdict"] = "UNVERIFIED" if att["verdict"] != "INFRA" else "INFRA"
            att["reason"] = "D4: the run's inputs could not be captured: %s" % e
        att["captured"] = cap
        co_moved: list[dict] = []
        if tier and (keep / "tree").is_dir():
            co_moved = _tier_checkout(att, keep, f["commit"], requested, tier, td, spec)
        if cap is not None or co_moved:
            _moved(att, cap, co_moved, f["commit"], requested, tier)
        # D7 over the T0 gates as compat reported them; a tier cell also over its tier rows
        judged = {"fact": {"verdict": att["verdict"], "gates": list(f["gates"]) + att["gates"][len(f["gates"]):]}} \
            if tier else res
        infra = classify_infra(judged) if att["verdict"] == "FAIL" else None
        if infra:
            att.update(verdict="INFRA", reason=infra)
        return att
    finally:
        att["finished_at"] = clock.iso()
        att["host"]["load_end"] = P.host_load()["load"]
        att["host"]["overloaded"] = P.overloaded(att["host"])
        shutil.rmtree(work, ignore_errors=True)


def _run_compat(att: dict, compat_fn: Callable, configuration: str, overrides: dict, cfg, keep: Path, nonce: str,
                url_map, tier_kw: dict) -> dict | None:
    """The compat document, or None with the attempt's verdict and reason set: a request that is not a tree is
    UNVERIFIED, the host's failures are INFRA (D7)."""
    from nanosoc_rm.analysis.compat import CompatUsage
    from nanosoc_rm.walker import OverrideError, UnknownConfiguration
    try:
        return compat_fn(configuration, overrides or None, config=cfg, keep_work=keep, nonce=nonce,
                         url_map=url_map, **tier_kw)
    except (UnknownConfiguration, OverrideError, CompatUsage, _tier_usage()) as e:
        att["reason"] = "the cell's request is not a tree: %s" % e
    except _INFRA_EXC as e:
        att.update(verdict="INFRA", reason="INFRA (D7): %s: %s" % (type(e).__name__, e))
    except OSError as e:
        att.update(verdict="INFRA", reason="INFRA (D7): the host failed the run: %s" % e)
    except Exception as e:                                        # noqa: BLE001 - could not answer, said why
        att["reason"] = "the run could not answer: %s: %s" % (type(e).__name__, str(e)[:300])
    return None


def _record_compat(att: dict, res: dict, tier: str | None, keep: Path, spec: Mapping | None) -> list[dict]:
    """Copy the compat result (and its tier part) into the attempt. Returns the requested pins."""
    f = res["fact"]
    att.update(verdict=f["verdict"], reason=f.get("reason"), compat_subject=res["subject"],
               gates=[{"id": g["id"], "verdict": g["verdict"], "detail": (g.get("detail") or "")[:300]}
                      for g in f["gates"]],
               not_run=f["not_run"], over_budget=f["over_budget"], unjudged_overrides=f["unjudged_overrides"])
    requested = [{"tree_path": tp, "commit": p["commit"]} for p in f["pins"] for tp in p["tree_paths"]]
    att["requested"] = {"superproject_commit": f["commit"], "pins": sorted(requested, key=lambda r: r["tree_path"])}
    td = f.get("tier") if tier else None
    if td is not None:
        att["tier"] = _tier_part(td)
        att["gates"] += [{"id": r["gate"], "verdict": _TIER_STATUS.get(r["status"], r["status"]),
                          "detail": (r.get("detail") or "")[:300]} for r in td.get("rows") or []]
        att["verdict"] = f.get("run_verdict") or f["verdict"]
        if td["verdict"] != "PASS":
            att["reason"] = "; ".join(x for x in (f.get("reason"), "tier %s %s: %s" % (
                tier, td["verdict"], td.get("reason") or "")) if x)[:600]
        if td.get("seconds") is not None:
            h, note, n = P.ip_hash(keep / "tree", (spec or {}).get("ip_files"))
            att["ip"] = {"hash": h, "note": note, "files": n}
    return requested


def _tier_checkout(att: dict, keep: Path, commit: str, requested: list[dict], tier: str, td: dict | None,
                   spec: Mapping | None) -> list[dict]:
    """Re-read the tier's checkout after its command (D4). Returns the pins it holds at other commits."""
    from nanosoc_rm.analysis.tierrun import read_layout_record
    record = read_layout_record(keep)
    declared = [x for x in ((spec or {}).get("verdicts"), (spec or {}).get("ip_files")) if x]
    try:
        co = P.capture_checkout(keep / "tree", commit, requested, record=record, exclude=declared)
    except Exception as e:                                        # noqa: BLE001 - an unreadable checkout
        co = None
        if att["verdict"] != "INFRA":
            att["verdict"] = "UNVERIFIED"
        att["reason"] = "D4: the tier's checkout could not be re-read after the run: %s" % e
    att["checkout"] = co
    if co is None:
        return []
    co_moved = P.moved(requested, co)
    if co["superproject_commit"] not in (None, commit):
        co_moved.insert(0, {"tree_path": "", "requested": commit, "captured": co["superproject_commit"]})
    ran = td is not None and td.get("seconds") is not None
    why = _checkout_unread(co, tier, ran)
    if why and att["verdict"] != "INFRA":
        att["verdict"] = "UNVERIFIED"
        att["reason"] = why
    return co_moved


def _moved(att: dict, cap: dict | None, co_moved: list[dict], commit: str, requested: list[dict],
           tier: str | None) -> None:
    """D4: a run that read any pin at a commit other than requested is UNVERIFIED (INFRA stays INFRA)."""
    mv = P.moved(requested, cap) if cap is not None else []
    if cap is not None and cap["superproject_commit"] not in (None, commit):
        mv.insert(0, {"tree_path": "", "requested": commit, "captured": cap["superproject_commit"]})
    mv = _merge_moved(mv, co_moved)
    att["moved"] = mv
    if mv and att["verdict"] != "INFRA":
        att["verdict"] = "UNVERIFIED"
        att["reason"] = "D4: the run read %d pin(s) at commits other than requested%s: %s" % (
            len(mv), " (the %s checkout, re-read after its command)" % tier if co_moved else "",
            ", ".join("%s %s != %s" % (m["tree_path"] or "(superproject)", m["captured"][:7],
                                       m["requested"][:7]) for m in mv[:5]))


def _cell_request(column: str, component: str | None, tier: str | None, force_tier: bool, tiers) -> None:
    if column not in C.COLUMNS:
        raise CellUsage("unknown column %r (columns: %s)" % (column, ", ".join(C.COLUMNS)))
    if (column == C.PINNED) != (component is None):
        raise CellUsage("the pinned column takes no --component; every other column needs one")
    if tier is not None and tier not in tiers:
        raise CellUsage("unknown tier %r (a matrix cell may add: %s)" % (tier, ", ".join(tiers)))
    if force_tier and not tier:
        raise CellUsage("--force-tier needs --tier")


def _target_verdict(row, target) -> tuple[str | None, str | None, dict[str, str]]:
    """(NOT-RUN and why, or None and None, the overrides) of a cell's target: nothing to run when the column has
    no commit or names the commit the tree already pins."""
    if row is None:
        return None, None, {}
    if target.commit is None:
        return "NOT-RUN", target.why, {}
    if C.same_as_pinned(row, target.commit):
        return "NOT-RUN", "same as pinned: %s is the commit the tree already pins" % target.commit[:12], {}
    return None, None, C.overrides_for(row, target.commit)


def _tier_ready(TI, configuration: str, tier: str, cfg, environ, attempts: list[dict]) -> tuple:
    """(verdict, reason, spec) after the tier's preflight: NOT-RUN, INFRA with a no-tool attempt (D7), or ready."""
    ready = TI.preflight(configuration, tier, config=cfg, environ=environ)
    if ready.verdict == "NOT-RUN":
        return "NOT-RUN", ready.reason, None
    if ready.verdict == "INFRA":
        attempts.append(_no_tool_attempt(ready.reason))
        return "INFRA", attempts[0]["reason"], None
    return None, None, TI.spec_of(configuration, tier, cfg)


def _provenance(tier: str | None, last: dict | None) -> dict:
    return {"tools": P.tools(), "harness": P.HARNESS, "ip_hash": None,
            "ip_hash_note": "T0 reads no IP file list, so there is no IP hash to record",
            **_tier_provenance(tier, last),
            "requested": last["requested"] if last else None,
            "captured": last["captured"] if last else None,
            "moved": last["moved"] if last else [],
            "host": last["host"] if last else None}


def _rerun_verdict(attempts: list[dict]) -> tuple[str, str]:
    """D8 and D7: the cell's verdict after a red first attempt and its re-run alone."""
    first, second = attempts
    hot = [i + 1 for i, a in enumerate(attempts) if a["host"].get("overloaded")]
    if second["verdict"] == "FAIL":
        reason = "red twice (the re-run, alone, failed again)"
        if hot:                                                   # red twice is the tree's, whatever the load
            reason += "; attempt %s ran on an overloaded host (D7), noted" % " and ".join(map(str, hot))
        return "FAIL", reason
    if second["verdict"] in GREEN and first["host"].get("overloaded"):
        # D7: overload, not the tree; not FLAKY
        return "INFRA", ("INFRA, not FLAKY (D7): red on an overloaded host (load %s/%s on %s CPUs, above %.1f per "
                         "CPU), then %s on the re-run alone" % (
                             first["host"]["load_start"], first["host"]["load_end"], first["host"]["cpus"],
                             P.OVERLOAD_PER_CPU, second["verdict"]))
    if second["verdict"] in GREEN:
        return "FLAKY", "red, then %s on the re-run alone (D8): blocks auto-merge" % second["verdict"]
    return "UNVERIFIED", "red, then %s on the re-run: %s" % (second["verdict"], second["reason"] or "")


def run_cell(configuration: str, column: str, component: str | None = None, *, config=None, store: bool = False,
             by: str = "cli", job: int | None = None, compat_fn: Callable | None = None,
             capture_fn: Callable | None = None, url_map: Mapping[str, str] | None = None,
             policy=None, tier: str | None = None, force_tier: bool = False,
             environ: Mapping[str, str] | None = None) -> list[dict]:
    """[the matrix_cell observation, then any matrix_quarantine entry it caused] (module docstring).
    `compat_fn`, `capture_fn` and `url_map` are test seams
    (url_map is compat's own `Context.for_local` seam, refused in a CI job). `tier` adds a tier run (t1);
    `force_tier` runs it past a T0 FAIL (a person's override); `environ` is the preflight's environment."""
    from nanosoc_rm.matrix import tiers as TI
    from nanosoc_rm.analysis import _inputs as I
    from nanosoc_rm.analysis.compat import compat as default_compat
    from nanosoc_rm.config import load_config
    cfg = config if config is not None else load_config()
    _cell_request(column, component, tier, force_tier, TI.TIERS)
    compat_fn = compat_fn or default_compat
    capture_fn = capture_fn or P.capture
    reg = I.registry(cfg)
    croot = reg.configuration(configuration)
    rws, tree = C.rows(configuration, cfg, [component] if component else [], **({"policy": policy} if policy else {}))
    row = rws[0] if component else None
    target = row.targets[column] if row else C.Target(C.PINNED, None)
    started = clock.iso()
    verdict, reason, overrides = _target_verdict(row, target)
    attempts: list[dict] = []
    spec = None
    if verdict is None and tier:
        verdict, reason, spec = _tier_ready(TI, configuration, tier, cfg, environ, attempts)
    run_kw = {"tier": tier, "force_tier": force_tier, "spec": spec} if tier else {}
    if verdict is None:
        first = _attempt(configuration, overrides, cfg, compat_fn, capture_fn, url_map, **run_kw)
        attempts.append(first)
        verdict, reason = first["verdict"], first["reason"]
        if verdict == "FAIL":                                     # D8: once more, alone
            attempts.append(_attempt(configuration, overrides, cfg, compat_fn, capture_fn, url_map, **run_kw))
            verdict, reason = _rerun_verdict(attempts)
    flaky = []
    if verdict == "FLAKY":
        g1 = {g["id"] for g in attempts[0]["gates"] if g["verdict"] == "FAIL"}
        g2 = {g["id"] for g in attempts[1]["gates"] if g["verdict"] == "FAIL"}
        flaky = sorted(g1 - g2) or ["cell"]
    nonce = attempts[-1]["nonce"] if attempts else secrets.token_hex(16)
    last = attempts[-1] if attempts else None
    root = ((last or {}).get("requested") or {}).get("superproject_commit") or C.root_commit(tree, croot)
    if root is None:
        raise I.CannotAnswer("the root commit of %s is unknown: walk it first" % configuration)
    fact = {
        "configuration": configuration,
        "configuration_commit": root,
        "component": component,
        "column": column, "nonce": nonce,
        "target": target.as_dict() if row else {"column": C.PINNED, "commit": None, "ref": None, "why": None,
                                                 "range": None},
        "pinned": C.pinned_commits(row) if row else [],
        "split": bool(row.split) if row else False,
        "overrides": [{"tree_path": t, "commit": s} for t, s in sorted(overrides.items())],
        "attempts": attempts, "verdict": verdict, "reason": reason, "flaky_gates": flaky,
        "blocks_auto_merge": verdict in ("FLAKY", "FAIL", "UNVERIFIED", "INFRA"),
        "provenance": _provenance(tier, last),
        "by": by, "job": job, "started_at": started, "finished_at": clock.iso(),
        "baseline": None, "comparison": None,
    }
    if tier:
        fact["tier"], fact["force_tier"] = tier, bool(force_tier)
    stored = cells(configuration, cfg)
    if column != C.PINNED:
        base = baseline_for(fact, stored)
        q = set(active_quarantine(quarantine(configuration, cfg)))
        cmp_ = compare(fact, base, q)
        fact["baseline"], fact["comparison"] = cmp_["baseline"], {"rows": cmp_["rows"], "note": cmp_["note"]}
    times = [started] + [d["observed_at"] for d in tree]
    srcs = [{"kind": "derived", "command": "nanosoc-rm matrix cell %s --column %s%s%s" % (
        configuration, column, " --component %s" % component if component else "",
        (" --tier %s%s" % (tier, " --force-tier" if force_tier else "")) if tier else "")}]
    srcs += [s for s in (target.sources if row else [])][:20]
    doc = {"schema": 1, "kind": "matrix_cell", "subject": subject_of_cell(fact),
           "observed_at": min(times, key=clock.parse), "sources": srcs, "fact": fact}
    _validate(doc)
    if store:
        from nanosoc_rm import db
        db.put(doc, config=cfg)
    return [doc] + _update_quarantine(doc, stored, cfg, store)


def _tier_usage():
    from nanosoc_rm.analysis.tierrun import TierUsage
    return TierUsage


def _tier_provenance(tier: str | None, last: Mapping | None) -> dict:
    """The provenance fields a tier cell overrides: its harness, its IP hash and the tier checkout's capture."""
    if not tier:
        return {}
    ip = (last or {}).get("ip") or {"hash": None, "note": "nothing ran: no IP hash", "files": 0}
    return {"harness": "nanosoc-rm %s compat (T0 + %s, run mode local)" % (P.__version__, tier),
            "ip_hash": ip["hash"], "ip_hash_note": ip["note"], "checkout": (last or {}).get("checkout")}
