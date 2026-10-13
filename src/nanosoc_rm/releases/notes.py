"""Release notes from classed changelogs (C2 classes; G1/G2 release notes).

    entries(config, component, repo, old, new) -> (entries, source)   classed changes between two commits
    render(...) -> str                                                 RELEASE_NOTES.md

The versioning module (Wave B, C2) owns changelog classes. This module codes against one small interface and
uses it when that module is installed:

    nanosoc_rm.versioning.changelog_entries(component, old, new, *, config) -> [{class, summary, commit}]
        class in breaking | feature | fix | none; old None for "everything up to new"

Until then (or when it raises LookupError for a component it cannot class), the fallback reads the commit log of
the component's mirror between the two commits (`git log old..new`) and classes each commit from its message:
a `Change-Class:` trailer first (declared), then a breaking marker (`!:` or `BREAKING CHANGE`), then the
conventional-commit type (feat -> feature, fix -> fix, docs/test/chore/ci/refactor/style/build -> none); anything
else is `unclassed`, shown as such (C2: every entry needs a class, so an unclassed entry is a warning).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from nanosoc_rm.config import Config
from nanosoc_rm.mirror.layout import mirror_path, run_in
from nanosoc_rm.releases.core import PUBLISH_NOTE

__all__ = ["entries", "classify", "render", "CLASSES", "MAX_ENTRIES"]

CLASSES = ("breaking", "feature", "fix", "none", "unclassed")
MAX_ENTRIES = 200
_TRAILER = re.compile(r"^Change-Class:\s*(breaking|feature|fix|none)\s*$", re.I | re.M)
_CONV = re.compile(r"^(?P<type>[a-z]+)(\([^)]*\))?(?P<bang>!)?:\s")
_NONE_TYPES = {"docs", "test", "tests", "chore", "ci", "refactor", "style", "build", "perf"}


def classify(message: str) -> tuple[str, str]:
    """(class, how) for one commit message: how is declared | inferred | none."""
    m = _TRAILER.search(message)
    if m:
        return m.group(1).lower(), "declared"
    subject = message.strip().splitlines()[0] if message.strip() else ""
    c = _CONV.match(subject)
    if (c and c.group("bang")) or "BREAKING CHANGE" in message:
        return "breaking", "inferred"
    if c:
        t = c.group("type")
        if t in ("feat", "feature"):
            return "feature", "inferred"
        if t == "fix":
            return "fix", "inferred"
        if t in _NONE_TYPES:
            return "none", "inferred"
    return "unclassed", "none"


def _external(component: str, old: str | None, new: str, config: Config) -> list[dict] | None:
    try:
        mod = importlib.import_module("nanosoc_rm.versioning")
    except ModuleNotFoundError as e:
        if e.name in ("nanosoc_rm.versioning",):
            return None
        raise
    fn = getattr(mod, "changelog_entries", None)
    if not callable(fn):
        return None
    try:
        return [dict(x, how="declared") for x in fn(component, old, new, config=config)]
    except LookupError:
        return None


def entries(config: Config, component: str, old: str | None, new: str) -> tuple[list[dict], str]:
    """Classed entries for `component` from `old` (exclusive; None = the first release, not listed) to `new`, and
    the command that produced them."""
    ext = _external(component, old, new, config)
    if ext is not None:
        return ext[:MAX_ENTRIES], "nanosoc_rm.versioning.changelog_entries(%s, %s, %s)" % (component, old, new)
    if old is None:
        return [], "first release: the history before it is not listed"
    mirror = mirror_path(config, component)
    if not (Path(mirror) / "HEAD").is_file():
        return [], "no mirror of %s" % component
    r = run_in(mirror, "log", "--no-merges", "--format=%H%x1f%B%x1e", "-n", str(MAX_ENTRIES), "%s..%s" % (old, new))
    if not r.ok:
        return [], "git log %s..%s failed in the mirror of %s" % (old[:12], new[:12], component)
    out = []
    for rec in r.stdout.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, msg = rec.strip().split("\x1f", 1)
        cls, how = classify(msg)
        out.append({"class": cls, "how": how, "summary": (msg.strip().splitlines() or [""])[0][:200], "commit": sha})
    return out, public_log(r.command, config)


def public_log(cmd: str, config: Config) -> str:
    return cmd.replace(str(config.mirrors_dir), "<mirrors>")


def render(release: dict, notes: dict[str, Any], errata_revision: int, errata_records: int, pending: list[str]) -> str:
    """RELEASE_NOTES.md. `notes`: {since, root: [entries], changed: [{component, tree_path, old, new, entries}],
    pins: int, first: bool, sources: [str]}."""
    lines = ["# %s %s (%s)" % (release["component"], release["version"], release["configuration"]), "",
             "- Tag `%s` on commit `%s` (a person makes it: see CHECKLIST.md)." % (release["tag"], release["commit"]),
             "- Release kind: %s%s." % (release["kind"], ", silicon" if release.get("silicon") else ""),
             "- Changes since: %s." % (notes["since"] or "no earlier release (first release)"),
             "- Draft. %s" % PUBLISH_NOTE, ""]

    def section(title: str, items: list[dict]) -> None:
        lines.extend(["## %s" % title, ""])
        if not items:
            lines.extend(["None listed.", ""])
            return
        for cls in CLASSES:
            group = [e for e in items if e["class"] == cls]
            if group:
                lines.append("### %s" % {"breaking": "Breaking", "feature": "Features", "fix": "Fixes", "none": "Other",
                                         "unclassed": "Unclassed (no change class: C2 needs one)"}[cls])
                lines.extend("- %s (`%s`)%s" % (e["summary"], e["commit"][:7], "" if e.get("how") != "inferred"
                                                 else " [class inferred]") for e in group)
                lines.append("")

    section("Changes in %s" % release["component"], notes["root"])
    if notes["changed"]:
        lines += ["## Components that moved since %s" % notes["since"], ""]
        for c in notes["changed"]:
            lines.append("### %s at `%s`: `%s` -> `%s`" % (c["component"], c["tree_path"], (c["old"] or "new")[:7],
                                                           c["new"][:7]))
            lines.append("")
            lines += ["- [%s] %s (`%s`)" % (e["class"], e["summary"], e["commit"][:7]) for e in c["entries"]] or ["- none listed"]
            lines.append("")
    elif not notes["first"]:
        lines += ["## Components", "", "No pin moved since %s." % notes["since"], ""]
    else:
        lines += ["## Components", "", "First release: %d pins, all listed in the release lock." % notes["pins"], ""]
    lines += ["## Errata", "", "ERRATA.md, document revision %d, %d record(s)." % (errata_revision, errata_records), ""]
    if pending:
        lines += ["## Not complete in this draft", ""] + ["- %s" % p for p in pending] + [""]
    lines += ["## How these notes were made", ""] + ["- `%s`" % s for s in notes["sources"]] + [""]
    return "\n".join(lines)
