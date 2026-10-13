"""The shadow ledger (E2; interfaces.md section 18.5): what the bot would have done against what people did.

    ledger(*, config=None, now=None) -> bump_ledger observation

Every proposal the engine stored (bumps.sqlite) is compared with its consumer branch as the mirror holds it now
(`git ls-tree <branch tip> -- <path>`), so the evidence is the repos themselves, not what anyone typed:

- **agreed**: the branch pins exactly the proposed commit.
- **disagreed**: the branch pins some other commit that is not a newer release containing the proposal, or a
  person marked the proposal "declined".
- **pending**: the branch still pins the old commit (a "done" mark the mirror does not show yet stays pending,
  with a note: sync the mirror).
- **superseded**: a newer proposal for the same gitlink replaced this one, or a person moved to a newer release
  tag that contains the proposed commit. Neither counts for or against the bot.
- **unknown**: the mirror cannot answer (branch or commit missing).

E2's threshold: at least 4 weeks since the first proposal AND at least 20 decided bumps (agreed + disagreed),
whichever is later. Auto-merge may then be proposed per change class only for a class with zero disagreements;
switching it on is a registry change (eligibility auto_merge) a person makes after the Phase 4 gate. This module
never changes it, and reports anything already in it as a problem.
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm import clock, contracts
from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.bumps import store
from nanosoc_rm.bumps.engine import _Git, _load, _Mirrors, schedule_status
from nanosoc_rm.bumps.versions import parse_tag
from nanosoc_rm.config import Config

__all__ = ["ledger", "threshold_met", "THRESHOLD_WEEKS", "THRESHOLD_BUMPS"]

THRESHOLD_WEEKS = 4
THRESHOLD_BUMPS = 20


def threshold_met(weeks: float, decided: int) -> bool:
    """E2: "at least 4 weeks or 20 bumps, whichever is later" = both must hold."""
    return weeks >= THRESHOLD_WEEKS and decided >= THRESHOLD_BUMPS


def _person_commit(mirrors: _Mirrors, comp: str, tip: str, base: str, path: str) -> tuple[str | None, str | None]:
    """The newest commit on the branch since `base` that changed the gitlink at `path`, and its committer time."""
    r = mirrors.git.run(mirrors.mirror(comp), "log", "-1", "--format=%H%x1f%cI", "%s..%s" % (base, tip), "--", path)
    if not r.ok or not r.stdout.strip():
        return None, None
    sha, _, at = r.stdout.strip().partition("\x1f")
    try:
        at = clock.iso(clock.parse(at))
    except ValueError:
        pass
    return sha, at


def _judge(f: dict, latest: bool, marks: list[dict], mirrors: _Mirrors) -> tuple[str, dict | None, str]:
    c, dep = f["consumer"], f["dependency"]["component"]
    declined = [m for m in marks if m["decision"] == "declined"]
    done = [m for m in marks if m["decision"] == "done"]
    tip = mirrors.tip(c["component"], c["repo"], c["branch"]) if mirrors.has(c["component"]) else None
    if tip is None:
        return "unknown", None, "branch %s of %s:%s is not in the mirror" % (c["branch"], c["repo"]["host"], c["repo"]["path"])
    pin = mirrors.gitlink(c["component"], tip, f["path"])
    if pin is None:
        return "unknown", None, "the branch tip %s has no gitlink at %s any more" % (tip[:12], f["path"])
    person = None
    if pin != f["from"]["commit"]:
        sha, at = _person_commit(mirrors, c["component"], tip, c["base"], f["path"])
        person = {"pin": pin, "commit": sha, "at": at}
    if pin == f["to"]["commit"]:
        return "agreed", person, "the branch pins the proposed commit"
    if not latest:
        return "superseded", person, "a newer proposal for the same gitlink replaced this one"
    if pin == f["from"]["commit"]:
        if declined:
            m = declined[-1]
            return "disagreed", None, "declined by %s at %s%s" % (m["login"], m["at"], (": " + m["note"]) if m.get("note") else "")
        if done:
            return "pending", None, ("marked done by %s at %s, but the mirror still shows the old pin: sync the "
                                     "mirror" % (done[-1]["login"], done[-1]["at"]))
        return "pending", None, "the branch still pins %s" % f["from"]["commit"][:12]
    contains = mirrors.is_ancestor(dep, f["to"]["commit"], pin)
    newer_tag = next((t for t in mirrors.tags(dep) if t.commit == pin), None)
    to_v = parse_tag(f["to"]["tag"] or "")
    if contains and newer_tag is not None and to_v is not None and newer_tag.version.key() > to_v.key():
        return "superseded", person, "a person moved to %s, a newer release that contains the proposal" % newer_tag.name
    if contains is None:
        return "unknown", person, "the new pin %s is not in the %s mirror" % (pin[:12], dep)
    return "disagreed", person, "a person pinned %s%s instead of %s" % (
        pin[:12], (" (%s)" % newer_tag.name) if newer_tag else " (no release tag)", f["to"]["tag"] or f["to"]["commit"][:12])


def ledger(*, config: Config | str | Path | None = None, now: str | None = None) -> dict:
    """The bump_ledger observation."""
    cfg = inp.config_of(config)
    reg = _load(cfg)
    auto_merge = (reg.files().get("eligibility.yaml") or {}).get("auto_merge") or {}
    rows = store.proposals(cfg)
    git = _Git(cfg)
    mirrors = _Mirrors(cfg, reg, git)
    all_marks: dict[str, list[dict]] = {}
    for m in store.marks(cfg):
        all_marks.setdefault(m["proposal"], []).append(m)
    # the bot's latest word per gitlink opportunity (consumer repo, branch, path, from); rows are oldest first
    latest_of: dict[tuple, str] = {}
    for r in rows:
        f = r["doc"]["fact"]
        key = (f["consumer"]["repo"]["host"], f["consumer"]["repo"]["path"], f["consumer"]["branch"], f["path"],
               f["from"]["commit"])
        latest_of[key] = r["id"]
    entries, counts, by_class = [], dict.fromkeys(("agreed", "disagreed", "pending", "superseded", "unknown"), 0), {}
    for r in rows:
        f = r["doc"]["fact"]
        key = (f["consumer"]["repo"]["host"], f["consumer"]["repo"]["path"], f["consumer"]["branch"], f["path"],
               f["from"]["commit"])
        marks = all_marks.get(r["id"], [])
        outcome, person, note = _judge(f, latest_of[key] == r["id"], marks, mirrors)
        counts[outcome] += 1
        cls = f["change"]["class"]
        if outcome in ("agreed", "disagreed"):
            by_class.setdefault(cls, {"agreed": 0, "disagreed": 0})[outcome] += 1
        entries.append({"id": r["id"], "configurations": r["configurations"], "consumer": f["consumer"]["repo"],
                        "branch": f["consumer"]["branch"], "path": f["path"], "dependency": f["dependency"]["component"],
                        "from": f["from"]["commit"], "to": f["to"]["commit"], "tag": f["to"]["tag"], "class": cls,
                        "first_proposed": r["first_seen"], "outcome": outcome, "person": person,
                        "marks": [{k: m[k] for k in ("at", "login", "decision", "note")} for m in marks], "note": note})
    counts["decided"] = counts["agreed"] + counts["disagreed"]
    started = min((r["first_seen"] for r in rows), default=None)
    at = now or clock.iso()
    weeks = round(max(0.0, (clock.parse(at) - clock.parse(started)).total_seconds() / (7 * 86400)), 2) if started else 0.0
    met = threshold_met(weeks, counts["decided"])
    clean = sorted(c for c in ("fix", "feature") if c in by_class and by_class[c]["disagreed"] == 0
                   and by_class[c]["agreed"] > 0) if met else []
    reason = ("%.1f of %d weeks, %d of %d decided bumps, %d disagreement%s"
              % (weeks, THRESHOLD_WEEKS, counts["decided"], THRESHOLD_BUMPS, counts["disagreed"],
                 "" if counts["disagreed"] == 1 else "s"))
    reason += ("; met: auto-merge may be PROPOSED for %s (a registry change a person makes after the Phase 4 gate)"
               % (", ".join(clean) or "no class (each needs zero disagreements)")) if met else "; not met"
    problems = []
    if auto_merge:
        problems.append("eligibility.yaml auto_merge is not empty (%s): shadow mode ignores it (E2)"
                        % ", ".join(sorted(auto_merge)))
    sched = schedule_status()
    fact = {"started_at": started, "weeks": weeks, "counts": counts,
            "agreement_rate": round(counts["agreed"] / counts["decided"], 4) if counts["decided"] else None,
            "by_class": by_class,
            "threshold": {"weeks": THRESHOLD_WEEKS, "bumps": THRESHOLD_BUMPS, "met": met,
                          "classes_without_disagreement": clean, "reason": reason},
            "auto_merge": {"configured": auto_merge, "enabled": False,
                           "gate": "Phase 4 entry gate: %s" % sched["gate_status"]},
            "entries": entries, "problems": problems}
    sources = [{"kind": "derived", "command": "bumps.sqlite proposals and marks (%d proposals)" % len(rows)}]
    sources += git.take()
    # A3: the ledger is as fresh as the oldest consumer mirror it read (its last successful sync), never fresher
    synced = []
    for e in entries:
        st = inp.mirror_status(cfg, e["consumer"])
        if st and st["fact"].get("last_success_at"):
            synced.append(st["fact"]["last_success_at"])
    observed = min(synced + [at], key=clock.parse)
    obs = {"schema": 1, "kind": "bump_ledger", "subject": "bump_ledger:all", "observed_at": observed, "sources": sources,
           "fact": fact}
    contracts.require_valid("observation", obs)
    return obs
