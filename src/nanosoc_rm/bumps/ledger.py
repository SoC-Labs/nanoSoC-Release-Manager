"""The shadow ledger (E2; interfaces.md section 19.5): what the bot would have done against what people did.

    ledger(*, config=None, now=None) -> bump_ledger observation

Every proposal the engine stored (bumps.sqlite) is compared with its consumer branch's HISTORY in the mirror, so
the evidence is the repos themselves, not what anyone typed. Proposals for the same gitlink opportunity (consumer
repo, branch, path and the pin they move from) are judged together, by the first commit on the branch since the
proposal's base that moved the gitlink (`git log --first-parent --reverse base..tip -- path`). That first move is
one person decision, and it stays decided when the branch moves on later (review findings BUMPS-R1, BUMPS-R2):

- **agreed**: the first move pinned exactly this proposal's commit.
- **disagreed**: the first move pinned some other commit that is not a newer release containing the bot's latest
  proposal, or (no move yet) a person marked the latest proposal "declined".
- **pending**: no move yet (a "done" mark the mirror does not show yet stays pending, with a note: sync the mirror).
- **superseded**: a newer proposal for the same gitlink replaced this one, the move is decided by another proposal
  of the group, or the first move went to a newer release tag that contains the proposed commit. Neither counts
  for or against the bot.
- **held**: the bot would hold this proposal (decision.would == hold, for example a tag not on the repo the
  .gitmodules names), so whatever a person did is not scored (BUMPS-R3).
- **unknown**: the mirror cannot answer (branch, history or commit missing).

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


def _first_move(mirrors: _Mirrors, comp: str, tip: str, base: str, path: str,
                frm: str) -> tuple[str, dict | None, str | None]:
    """The person's decision on one gitlink: the FIRST commit on the branch (first-parent history, oldest first)
    since `base` whose gitlink at `path` is not `frm`. Later moves never change it (BUMPS-R1: an agreement lasts
    when the branch moves on). ("none", None, None) when the branch still pins `frm` in every commit since `base`,
    ("move", {pin, commit, at}, None) for a move, ("unknown", None, why) when the mirror cannot answer."""
    r = mirrors.git.run(mirrors.mirror(comp), "log", "--first-parent", "--reverse", "--format=%H%x1f%cI",
                        "%s..%s" % (base, tip), "--", path)
    if not r.ok:
        return "unknown", None, "the history of %s since %s is not in the mirror" % (path, base[:12])
    for line in r.stdout.splitlines():
        sha, _, at = line.strip().partition("\x1f")
        if not sha:
            continue
        pin = mirrors.gitlink(comp, sha, path)
        if pin == frm:
            continue
        if pin is None:
            return "unknown", None, "%s removed the gitlink at %s" % (sha[:12], path)
        try:
            at = clock.iso(clock.parse(at))
        except ValueError:
            pass
        return "move", {"pin": pin, "commit": sha, "at": at}, None
    return "none", None, None


def _judge_group(group: list[dict], marks: dict[str, list[dict]], mirrors: _Mirrors) -> dict[str, tuple]:
    """Every kept proposal for one gitlink opportunity (consumer repo, branch, path, from), oldest first, judged
    together: the person's first move after `from` is ONE decision (BUMPS-R2). It agrees with the proposal whose
    `to` it pins exactly (the others are superseded); otherwise it is judged against the bot's latest word. A
    proposal the bot would hold (decision.would == hold) is never scored: `held` (BUMPS-R3)."""
    out: dict[str, tuple] = {}
    latest = group[-1]["doc"]["fact"]
    c, dep = latest["consumer"], latest["dependency"]["component"]
    held = {r["id"] for r in group if (r["doc"]["fact"].get("decision") or {}).get("would") == "hold"}

    def put(rid, outcome, person, note):
        if rid in held and outcome != "unknown":
            note = "the bot would hold this proposal (%s), so it is not scored; %s" % (
                next(r for r in group if r["id"] == rid)["doc"]["fact"].get("reason") or "blocked", note)
            outcome = "held"
        out[rid] = (outcome, person, note)

    tip = mirrors.tip(c["component"], c["repo"], c["branch"]) if mirrors.has(c["component"]) else None
    if tip is None:
        for r in group:
            put(r["id"], "unknown", None, "branch %s of %s:%s is not in the mirror"
                % (c["branch"], c["repo"]["host"], c["repo"]["path"]))
        return out
    base = group[0]["doc"]["fact"]["consumer"]["base"]
    what, person, why = _first_move(mirrors, c["component"], tip, base, latest["path"], latest["from"]["commit"])
    if what == "unknown":
        for r in group:
            put(r["id"], "unknown", None, why)
        return out
    if what == "none":
        for r in group[:-1]:
            put(r["id"], "superseded", None, "a newer proposal for the same gitlink replaced this one")
        ms = marks.get(group[-1]["id"], [])
        declined = [m for m in ms if m["decision"] == "declined"]
        done = [m for m in ms if m["decision"] == "done"]
        if declined:
            m = declined[-1]
            put(group[-1]["id"], "disagreed", None, "declined by %s at %s%s"
                % (m["login"], m["at"], (": " + m["note"]) if m.get("note") else ""))
        elif done:
            put(group[-1]["id"], "pending", None, "marked done by %s at %s, but the mirror still shows the old pin: "
                "sync the mirror" % (done[-1]["login"], done[-1]["at"]))
        else:
            put(group[-1]["id"], "pending", None, "the branch still pins %s" % latest["from"]["commit"][:12])
        return out
    pin = person["pin"]
    exact = [r for r in group if r["doc"]["fact"]["to"]["commit"] == pin]
    if exact:
        hit = exact[-1]
        for r in group:
            if r is hit:
                put(r["id"], "agreed", person, "%s pinned the proposed commit" % person["commit"][:12])
            else:
                put(r["id"], "superseded", person, "the same move (%s) is decided by %s" % (pin[:12], hit["id"]))
        return out
    for r in group[:-1]:
        put(r["id"], "superseded", person, "a newer proposal for the same gitlink replaced this one")
    to = latest["to"]
    contains = mirrors.is_ancestor(dep, to["commit"], pin)
    newer_tag = next((t for t in mirrors.tags(dep) if t.commit == pin), None)
    to_v = parse_tag(to["tag"] or "")
    if contains is None:
        put(group[-1]["id"], "unknown", person, "the new pin %s is not in the %s mirror" % (pin[:12], dep))
    elif contains and newer_tag is not None and to_v is not None and newer_tag.version.key() > to_v.key():
        put(group[-1]["id"], "superseded", person,
            "a person moved to %s, a newer release that contains the proposal" % newer_tag.name)
    else:
        put(group[-1]["id"], "disagreed", person, "a person pinned %s%s instead of %s" % (
            pin[:12], (" (%s)" % newer_tag.name) if newer_tag else " (no release tag)", to["tag"] or to["commit"][:12]))
    return out


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
    # one group per gitlink opportunity (consumer repo, branch, path, from); rows are oldest first
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        f = r["doc"]["fact"]
        key = (f["consumer"]["repo"]["host"], f["consumer"]["repo"]["path"], f["consumer"]["branch"], f["path"],
               f["from"]["commit"])
        groups.setdefault(key, []).append(r)
    judged: dict[str, tuple] = {}
    for group in groups.values():
        judged.update(_judge_group(group, all_marks, mirrors))
    entries, by_class = [], {}
    counts = dict.fromkeys(("agreed", "disagreed", "pending", "superseded", "unknown", "held"), 0)
    for r in rows:
        f = r["doc"]["fact"]
        marks = all_marks.get(r["id"], [])
        outcome, person, note = judged[r["id"]]
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
