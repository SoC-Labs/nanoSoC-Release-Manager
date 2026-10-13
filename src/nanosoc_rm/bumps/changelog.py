"""Changelog excerpts and the class each entry declares (E1, C2).

A changelog is one of CHANGELOG.md, CHANGELOG, CHANGES.md, HISTORY.md at the root of the tagged commit. Its
version sections are headings that name a version (`## [0.2.0] - 2026-10-01`, `## v0.2.0`, `# 0.2.0`). The
excerpt is every section after the pinned version up to and including the proposed one.

The declared class (C2) of a section: a line `Class: fix` (also `Change class:`, any case, list markers allowed),
else the most severe of its sub-headings (`### Breaking`, `### Features`/`### Added`, `### Fixes`/`### Fixed`).
The excerpt's class is the most severe of its sections; a section that declares nothing makes the result
`unknown`, never a guess from the version number.
"""
from __future__ import annotations

import re

from nanosoc_rm.bumps.versions import Version, parse_tag

__all__ = ["NAMES", "pick_file", "sections", "excerpt", "declared_class", "SEVERITY", "most_severe", "MAX_EXCERPT"]

NAMES = ("CHANGELOG.md", "CHANGELOG", "CHANGES.md", "HISTORY.md")
SEVERITY = ("none", "fix", "feature", "breaking")          # least to most severe
MAX_EXCERPT = 4000
_HEAD = re.compile(r"^(#{1,3})\s+\[?(v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[0-9A-Za-z.-]+)?)\]?(?:\s|$)")
_CLASS_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:change\s+)?class\s*:\s*\**\s*(breaking|feature|fix|none)\b", re.I)
_SUB = (("breaking", re.compile(r"^#{2,4}\s+(breaking|removed|incompatible)", re.I)),
        ("feature", re.compile(r"^#{2,4}\s+(features?|added|new)\b", re.I)),
        ("fix", re.compile(r"^#{2,4}\s+(fix(es|ed)?|bug ?fixes|security)\b", re.I)))


def pick_file(names: list[str]) -> str | None:
    """The changelog among a commit's top-level names (case-insensitive), in NAMES order."""
    lower = {n.lower(): n for n in names}
    for want in NAMES:
        if want.lower() in lower:
            return lower[want.lower()]
    return None


def sections(text: str) -> list[tuple[Version, str]]:
    """(version, section text including its heading) for every version heading, in file order."""
    out: list[tuple[Version, list[str]]] = []
    level = None
    for line in text.splitlines():
        m = _HEAD.match(line)
        v = parse_tag(m.group(2)) if m else None
        if v is not None and (level is None or len(m.group(1)) <= level):
            level = len(m.group(1))
            out.append((v, [line]))
        elif out:
            out[-1][1].append(line)
    return [(v, "\n".join(lines).strip()) for v, lines in out]


def excerpt(text: str, after: Version | None, upto: Version) -> list[tuple[Version, str]]:
    """The sections for versions in (after, upto]; only `upto`'s own section when `after` is unknown."""
    out = []
    for v, body in sections(text):
        if after is None:
            if v.key() == upto.key():
                out.append((v, body))
        elif after.key() < v.key() <= upto.key():
            out.append((v, body))
    return out


def declared_class(section: str) -> str | None:
    """The class one section declares, or None."""
    for line in section.splitlines():
        m = _CLASS_LINE.match(line)
        if m:
            return m.group(1).lower()
    found = [cls for cls, rx in _SUB if any(rx.match(line) for line in section.splitlines())]
    return most_severe(found) if found else None


def most_severe(classes) -> str:
    return max(classes, key=SEVERITY.index)
