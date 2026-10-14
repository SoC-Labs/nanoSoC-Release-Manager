"""Version tags and Cargo-style ranges, as the matrix columns need them (D3).

    parse(tag) -> (major, minor, patch, pre) | None     `v1.2.3`, `1.2`, `v1.0-rc1`; a tag with a `/` is never a version
    latest(tags) -> tag | None                           highest RELEASE tag (no pre-release part)
    parse_range(text) -> [comparators]                   `^0.1`, `~1.2.3`, `>=0.1.0, <0.3.0`, `1.2.3` (= ^1.2.3), `*`
    satisfies(tag, comparators) -> bool

This is the small interface the matrix codes against (`VersionPolicy`). The Phase 3 versioning module (C1, SemVer
and change classes) may supply its own policy with the same three functions; until it does, this one is used.
Pre-release tags are left out of "latest" and of every range, as Cargo does: a range names releases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

__all__ = ["parse", "latest", "parse_range", "satisfies", "latest_in_range", "RangeError", "VersionPolicy",
           "DEFAULT_POLICY"]

_TAG = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?\Z")
_CMP = re.compile(r"^(\^|~|>=|<=|>|<|=)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\Z")


class RangeError(ValueError):
    """A range string this policy cannot read."""


def parse(tag: str) -> tuple[int, int, int, str | None] | None:
    """(major, minor, patch, pre-release or None) of a version tag, else None (keep/..., freeze names, dates)."""
    m = _TAG.match(tag or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4)


def latest(tags: Iterable[str]) -> str | None:
    """The highest release tag (no pre-release part); ties (v1.0 and 1.0.0) go to the first name in sort order."""
    best: tuple[tuple[int, int, int], str] | None = None
    for t in sorted(set(tags)):
        v = parse(t)
        if v is None or v[3] is not None:
            continue
        if best is None or v[:3] > best[0]:
            best = (v[:3], t)
    return best[1] if best else None


def _bounds(op: str, major: int, minor: int | None, patch: int | None) -> list[tuple[str, tuple[int, int, int]]]:
    lo = (major, minor or 0, patch or 0)
    if op in ("^", ""):
        if major > 0 or minor is None:
            hi = (major + 1, 0, 0)
        elif minor > 0 or patch is None:
            hi = (0, minor + 1, 0)
        else:
            hi = (0, 0, patch + 1)
        return [(">=", lo), ("<", hi)]
    if op == "~":
        hi = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        return [(">=", lo), ("<", hi)]
    if op == "=":
        if minor is None:
            return [(">=", lo), ("<", (major + 1, 0, 0))]
        if patch is None:
            return [(">=", lo), ("<", (major, minor + 1, 0))]
        return [("=", lo)]
    return [(op, lo)]


def parse_range(text: str) -> list[tuple[str, tuple[int, int, int]]]:
    """Comparators of a Cargo-style range (component-manifest `requires[].range`). RangeError when unreadable."""
    text = (text or "").strip()
    if not text:
        raise RangeError("empty range")
    if text == "*":
        return []
    out = []
    for part in text.split(","):
        m = _CMP.match(part.strip())
        if not m:
            raise RangeError("cannot read range %r (at %r)" % (text, part.strip()))
        op = m.group(1) or ""
        major = int(m.group(2))
        minor = int(m.group(3)) if m.group(3) is not None else None
        patch = int(m.group(4)) if m.group(4) is not None else None
        out += _bounds(op, major, minor, patch)
    return out


_OPS: dict[str, Callable[[tuple, tuple], bool]] = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    "=": lambda a, b: a == b}


def satisfies(tag: str, comparators: list[tuple[str, tuple[int, int, int]]]) -> bool:
    v = parse(tag)
    if v is None or v[3] is not None:
        return False
    return all(_OPS[op](v[:3], bound) for op, bound in comparators)


def latest_in_range(tags: Iterable[str], comparators: list[tuple[str, tuple[int, int, int]]]) -> str | None:
    return latest(t for t in tags if satisfies(t, comparators))


class VersionPolicy(Protocol):
    def latest(self, tags: Iterable[str]) -> str | None: ...
    def parse_range(self, text: str) -> list: ...
    def latest_in_range(self, tags: Iterable[str], comparators: list) -> str | None: ...


@dataclass(frozen=True)
class _Default:
    latest: Callable = staticmethod(latest)
    parse_range: Callable = staticmethod(parse_range)
    latest_in_range: Callable = staticmethod(latest_in_range)


DEFAULT_POLICY: VersionPolicy = _Default()
