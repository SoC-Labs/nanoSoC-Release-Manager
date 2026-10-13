"""The change classifier (C3, C5, C6, C7, C8): a change's class is the most severe of its signals.

    classify(signals) -> {class, status, signals}       status "complete" only when every C3 signal was measured

Signals (each {signal, class, status, detail}; status measured | unmeasured | undeclared):

- `declared`  (C3 1/3): the most severe class among the Unreleased changelog entries (C2). `undeclared` when the
  repo has no changelog or no classed entry.
- `api`       (C3 2/3): the diff of API.md (C12) between the baseline release and the head. A removed or changed
  line is breaking; only added lines are feature; no change is none. `unmeasured` without API.md at both ends.
- `consumers` (C3 3/3): regenerate every registered consumer's output with the component at the baseline and at
  the head, and diff ports, instance names, memory and IRQ maps and the module behind each address window. This
  needs the generator, so it is a pluggable `Regenerator`. The default, `NoGenerator`, measures nothing and says
  so: the class is then `partial`, never complete.
- `reexports` (C6): for each component the manifest passes through whose pin moved, the class of the change it
  passes on (from that component's own changelog). The repo is classed at least as severely.
- `build`     (C7): build-affecting, from file-list diffs per variant; also through the Regenerator (it runs the
  manifest's `variants.*.flist` commands). Never set by hand; `unmeasured` without a generator.
- `host_move` (C8): fix while the old host still serves the same commits; breaking once it is frozen or removed.

The final class is the most severe MEASURED or DECLARED signal. With no signal at all it is None ("class
unmeasured"): the tool never guesses a class from commit messages (C3: messages alone classed 2 of 9 correctly);
a commit-message hint is shown beside it, labelled as a hint, and never used.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nanosoc_rm.versioning import semver

__all__ = ["Regenerator", "NoGenerator", "RegenerationResult", "signal", "declared_signal", "api_signal",
           "consumer_signal", "reexport_signal", "build_signal", "host_move_class", "host_move_signal",
           "classify", "commit_hint", "diff_outputs", "C3_SIGNALS"]

C3_SIGNALS = ("declared", "api", "consumers")
NO_GENERATOR = ("class unmeasured: regenerating every registered consumer's output needs the generator, and no "
                "Regenerator is plugged in (C3 measured mode is partial)")


def signal(name: str, cls: str | None, status: str, detail: str, **extra) -> dict:
    return {"signal": name, "class": cls, "status": status, "detail": detail, **extra}


# ---------------------------------------------------------------------------------------------------------
# The pluggable regeneration step
# ---------------------------------------------------------------------------------------------------------
@dataclass
class RegenerationResult:
    """One consumer's generated output at one component commit: {path: text}, and its file list per variant."""
    consumer: str
    outputs: dict[str, str]
    flists: dict[str, list[str]]
    sources: list[dict]


@runtime_checkable
class Regenerator(Protocol):
    """What the measured C3/C7 mode needs. `name` is shown on every result it produced."""
    name: str

    def available(self) -> tuple[bool, str]:
        """(can it run here, why not)."""

    def consumers(self, component: str) -> list[str]:
        """The registered configurations that build with this component."""

    def regenerate(self, consumer: str, component: str, commit: str) -> RegenerationResult:
        """Regenerate `consumer`'s output with `component` pinned at `commit` (in a sandbox; never in a repo)."""


class NoGenerator:
    """The default: no generator on this host. Every measured signal is reported unmeasured, honestly."""
    name = "none"

    def __init__(self, reason: str = NO_GENERATOR):
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def consumers(self, component: str) -> list[str]:
        return []

    def regenerate(self, consumer: str, component: str, commit: str) -> RegenerationResult:   # pragma: no cover
        raise RuntimeError(self.reason)


# Lines of generated RTL and maps that carry the public shape of a SoC (C3: ports, instance names, memory and
# IRQ maps, the module behind each address window). A removed or changed one is breaking; an added one feature.
_SHAPE = [
    ("port", re.compile(r"^\s*(input|output|inout)\b")),
    ("instance", re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(#\s*\(.*\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(")),
    ("address", re.compile(r"(0x[0-9A-Fa-f_]{4,}|'h[0-9A-Fa-f_]{4,})")),
    ("irq", re.compile(r"\birq\b|\bIRQ\b|interrupt", re.I)),
]


def _shape_lines(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {k: set() for k, _ in _SHAPE}
    for line in text.splitlines():
        s = " ".join(line.split())
        if not s or s.startswith(("//", "#", "*", "/*")):
            continue
        for kind, rx in _SHAPE:
            if rx.search(s):
                out[kind].add(s)
                break
    return out


def diff_outputs(before: dict[str, str], after: dict[str, str]) -> tuple[str, list[str]]:
    """(class, reasons) of one consumer's regenerated output: removed files or removed/changed shape lines are
    breaking; added files or shape lines are feature; any other text change is fix; identical output is none."""
    reasons: list[str] = []
    cls = "none"
    for p in sorted(set(before) - set(after)):
        reasons.append("removed %s" % p)
        cls = "breaking"
    for p in sorted(set(before) & set(after)):
        if before[p] == after[p]:
            continue
        b, a = _shape_lines(before[p]), _shape_lines(after[p])
        for kind in b:
            gone = sorted(b[kind] - a[kind])
            new = sorted(a[kind] - b[kind])
            if gone:
                cls = "breaking"
                reasons.append("%s: %s %s removed or changed (e.g. %r)" % (p, len(gone), kind, gone[0][:80]))
            if new:
                cls = semver.max_class([cls, "feature"])
                reasons.append("%s: %s %s added" % (p, len(new), kind))
        cls = semver.max_class([cls, "fix"])
    for p in sorted(set(after) - set(before)):
        reasons.append("added %s" % p)
        cls = semver.max_class([cls, "feature"])
    return cls, reasons


def consumer_signal(component: str, base: str | None, head: str, regen: Regenerator | None) -> dict:
    """C3 3/3: regenerate each registered consumer at `base` and `head` and diff (the measured mode)."""
    regen = regen or NoGenerator()
    ok, why = regen.available()
    if not ok:
        return signal("consumers", None, "unmeasured", why, generator=regen.name)
    if base is None:
        return signal("consumers", None, "unmeasured", "no baseline release to regenerate against", generator=regen.name)
    consumers = regen.consumers(component)
    if not consumers:
        return signal("consumers", None, "unmeasured", "no registered consumer builds with %s" % component,
                      generator=regen.name)
    per, cls, sources = [], "none", []
    for c in consumers:
        b = regen.regenerate(c, component, base)
        a = regen.regenerate(c, component, head)
        sources += b.sources + a.sources
        k, reasons = diff_outputs(b.outputs, a.outputs)
        per.append({"consumer": c, "class": k, "reasons": reasons[:20]})
        cls = semver.max_class([cls, k])
    return signal("consumers", cls, "measured", "%d consumer(s) regenerated with %s" % (len(consumers), regen.name),
                  generator=regen.name, consumers=per, sources=sources)


def build_signal(component: str, base: str | None, head: str, regen: Regenerator | None) -> dict:
    """C7: build-affecting when any consumer's file list differs in any variant. Computed, never set by hand."""
    regen = regen or NoGenerator()
    ok, why = regen.available()
    if not ok or base is None:
        return signal("build", None, "unmeasured", why if not ok else "no baseline release", build_affecting=None)
    changed = []
    for c in regen.consumers(component):
        b, a = regen.regenerate(c, component, base), regen.regenerate(c, component, head)
        for variant in sorted(set(b.flists) | set(a.flists)):
            if b.flists.get(variant) != a.flists.get(variant):
                changed.append("%s/%s" % (c, variant))
    return signal("build", None, "measured", ("file lists changed: " + ", ".join(changed)) if changed
                  else "no file list changed", build_affecting=bool(changed))


# ---------------------------------------------------------------------------------------------------------
# Signals from text
# ---------------------------------------------------------------------------------------------------------
def declared_signal(classes: dict[str, list] | None, changelog_present: bool) -> dict:
    """C3 1/3 from the Unreleased entries {class: [entries]} (C2)."""
    if not changelog_present:
        return signal("declared", None, "undeclared", "no CHANGELOG.md: nothing declared (C2)")
    known = [c for c in (classes or {}) if c in semver.CLASS_RANK and classes[c]]
    if not known:
        return signal("declared", None, "undeclared", "no classed entry in [Unreleased]")
    top = semver.max_class(known)
    counts = ", ".join("%d %s" % (len(classes[c]), c) for c in reversed(semver.CLASSES) if classes.get(c))
    return signal("declared", top, "measured", "Unreleased: %s" % counts)


def api_signal(before: str | None, after: str | None) -> dict:
    """C3 2/3: the diff of API.md (C12) from the baseline release to the head."""
    if after is None:
        return signal("api", None, "unmeasured", "no API.md at the head (C12: the public interface is not declared)")
    if before is None:
        return signal("api", None, "unmeasured", "no API.md at the baseline release: nothing to diff against")
    norm = lambda t: [" ".join(x.split()) for x in t.splitlines() if x.strip()]
    b, a = norm(before), norm(after)
    if b == a:
        return signal("api", "none", "measured", "API.md unchanged")
    sm = difflib.SequenceMatcher(a=b, b=a, autojunk=False)
    removed = sum(i2 - i1 for op, i1, i2, _, _ in sm.get_opcodes() if op in ("delete", "replace"))
    added = sum(j2 - j1 for op, _, _, j1, j2 in sm.get_opcodes() if op in ("insert", "replace"))
    if removed:
        return signal("api", "breaking", "measured", "API.md: %d line(s) removed or changed, %d added" % (removed, added))
    return signal("api", "feature", "measured", "API.md: %d line(s) added, none removed" % added)


def reexport_signal(passed: list[dict]) -> dict:
    """C6: `passed` = [{component, from, to, class}] for each re-exported component whose pin moved."""
    if not passed:
        return signal("reexports", None, "measured", "no re-exported component moved", passes=[])
    measured = [p for p in passed if p.get("class")]
    cls = semver.max_class(p["class"] for p in measured)
    unm = [p["component"] for p in passed if not p.get("class")]
    return signal("reexports", cls, "measured" if not unm else "unmeasured",
                  "passes on " + ", ".join("%s %s" % (p["component"], p.get("class") or "(class unmeasured)") for p in passed),
                  passes=passed)


def host_move_class(old_host_serves_same_commits: bool | None, old_host_frozen_or_removed: bool | None) -> str | None:
    """C8: a move is a fix while the old host still serves the same commits; freezing or removing the old host is
    breaking. None when neither is known."""
    if old_host_frozen_or_removed:
        return "breaking"
    if old_host_serves_same_commits is True:
        return "fix"
    if old_host_serves_same_commits is False:
        return "breaking"
    return None


def host_move_signal(moves: list[dict]) -> dict:
    """C8 over the component's recorded host moves: [{from, serves_same_commits, frozen_or_removed, detail}]."""
    if not moves:
        return signal("host_move", None, "measured", "no host move recorded")
    classes = [host_move_class(m.get("serves_same_commits"), m.get("frozen_or_removed")) for m in moves]
    if any(c is None for c in classes):
        return signal("host_move", semver.max_class(classes), "unmeasured",
                      "; ".join(m.get("detail", "") for m in moves), moves=moves)
    return signal("host_move", semver.max_class(classes), "measured",
                  "; ".join(m.get("detail", "") for m in moves), moves=moves)


_HINT = [("breaking", re.compile(r"(^[a-z]+(\([^)]*\))?!:|BREAKING[ -]CHANGE)", re.M)),
         ("feature", re.compile(r"^(feat|feature)(\([^)]*\))?:", re.I)),
         ("fix", re.compile(r"^(fix|bugfix|hotfix)(\([^)]*\))?:", re.I))]


def commit_hint(messages: list[str]) -> dict:
    """What conventional commit prefixes would suggest. A HINT, shown beside the class and never used (C3)."""
    counts = {"breaking": 0, "feature": 0, "fix": 0, "other": 0}
    for m in messages:
        for cls, rx in _HINT:
            if rx.search(m):
                counts[cls] += 1
                break
        else:
            counts["other"] += 1
    hint = next((c for c in ("breaking", "feature", "fix") if counts[c]), None)
    return {"class": hint, "commits": len(messages), "counts": counts,
            "note": "commit-message hint only; never used as the class (C3)"}


def classify(signals: list[dict]) -> dict:
    """The class (C3: the most severe signal with a class), and whether it is complete: every C3 signal measured."""
    cls = semver.max_class(s["class"] for s in signals if s["class"] and s["status"] in ("measured", "unmeasured"))
    missing = [s["signal"] for s in signals if s["signal"] in C3_SIGNALS and s["status"] != "measured"]
    status = "complete" if not missing else ("partial" if cls else "unmeasured")
    reason = None
    if missing:
        reason = "unmeasured or undeclared: %s" % ", ".join(missing)
    return {"class": cls, "status": status, "reason": reason, "signals": signals}
