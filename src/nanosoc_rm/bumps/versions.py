"""Release tags and version ranges for bump proposals (E1; C1, C10, C11).

    parse_tag("v0.2.1") -> Version(0, 2, 1)     a release tag; None for anything else
    allows("^0.1", Version(0, 1, 4)) -> True    a manifest `requires[].range` (Cargo style)

Release tags (C10): `vX.Y.Z`, also without the `v` (external projects), and the two-part form `vX.Y` that some
existing tags use (grandfathered, read as X.Y.0 and marked so). A tag with a pre-release suffix (`-rc.1`,
`-chiplet-verified`) is a candidate or a label, never a release, and is not proposed.

Ranges (component-manifest `requires[].range`, Cargo semantics): comparators separated by commas, all of which
must hold: `^X.Y.Z`, `~X.Y.Z`, `=X.Y.Z`, `>X`, `>=X`, `<X`, `<=X`, `X.Y.*`, `*`, and a bare version (= caret).
Below 1.0.0 the caret keeps the middle number (C1: there a breaking change bumps the middle number).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Version", "RangeError", "parse_tag", "parse_version", "allows", "TAG_RX"]

TAG_RX = re.compile(r"^(?P<v>[vV]?)(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)(?:\.(?P<patch>0|[1-9][0-9]*))?"
                    r"(?P<pre>-[0-9A-Za-z.-]+)?\Z")
_PART = re.compile(r"^(0|[1-9][0-9]*|\*|x|X)\Z")


class RangeError(ValueError):
    """A range that does not parse. The proposal records it; it never guesses what was meant."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: str = ""                      # compare versions with `key()`, never the fields
    two_part: bool = False

    def key(self) -> tuple:
        """Sort key: a release sorts after its pre-releases (SemVer 11.3)."""
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, self.pre)

    @property
    def release(self) -> bool:
        return not self.pre

    def __str__(self) -> str:
        return "%d.%d.%d%s" % (self.major, self.minor, self.patch, self.pre)


def parse_tag(name: str) -> Version | None:
    """The version a tag name carries, or None when it is not a version tag."""
    m = TAG_RX.match(name or "")
    if not m:
        return None
    return Version(int(m["major"]), int(m["minor"]), int(m["patch"] or 0), m["pre"] or "", m["patch"] is None)


def parse_version(text: str) -> Version:
    v = parse_tag(text.strip())
    if v is None:
        raise RangeError("%r is not a version" % text)
    return v


def _partial(text: str) -> tuple[list[int | None], str]:
    """'1.2' -> ([1, 2, None], ''); '1.*' -> ([1, None, None], ''); '1.2.3-rc.1' -> ([1, 2, 3], '-rc.1')."""
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    pre = ""
    if "-" in text:
        text, pre = text.split("-", 1)
        pre = "-" + pre
    parts = text.split(".")
    if not 1 <= len(parts) <= 3 or not all(_PART.match(p) for p in parts):
        raise RangeError("%r is not a version or a partial version" % text)
    nums: list[int | None] = [None if p in ("*", "x", "X") else int(p) for p in parts]
    nums += [None] * (3 - len(nums))
    seen_wild = False
    for n in nums:                                  # 1.*.3 is not a range
        if n is None:
            seen_wild = True
        elif seen_wild:
            raise RangeError("%r has a number after a wildcard" % text)
    return nums, pre


def _v(nums: list[int | None]) -> tuple[int, int, int]:
    return tuple(n or 0 for n in nums)  # type: ignore[return-value]


def _bounds(op: str, nums: list[int | None]) -> list[tuple[str, tuple[int, int, int]]]:
    """One comparator as a list of (op, version) bounds on (major, minor, patch)."""
    major, minor, patch = nums
    if major is None:
        return []                                                       # '*': anything
    lo = _v(nums)
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
        if patch is not None:
            return [("=", lo)]
        hi = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        return [(">=", lo), ("<", hi)]
    if op in (">=", "<"):
        return [(op, lo)]
    if op == ">":
        if patch is not None:
            return [(">", lo)]
        return [(">=", (major + 1, 0, 0) if minor is None else (major, minor + 1, 0))]
    if op == "<=":
        if patch is not None:
            return [("<=", lo)]
        return [("<", (major + 1, 0, 0) if minor is None else (major, minor + 1, 0))]
    raise RangeError("unknown operator %r" % op)


_CMP = re.compile(r"^\s*(\^|~|=|>=|<=|>|<)?\s*([0-9vV*xX][0-9A-Za-z.*+-]*)\s*\Z")


def comparators(range_text: str) -> list[tuple[str, list[int | None], str]]:
    """Parse a range into (op, partial version, pre-release) comparators. RangeError when it does not parse."""
    if not isinstance(range_text, str) or not range_text.strip():
        raise RangeError("an empty range")
    out = []
    for part in range_text.split(","):
        m = _CMP.match(part)
        if not m:
            raise RangeError("%r is not a comparator (Cargo-style ranges: ^0.1, ~1.2, >=0.1.0, <0.3.0)" % part.strip())
        nums, pre = _partial(m.group(2).split("+", 1)[0])
        out.append((m.group(1) or "", nums, pre))
    return out


def allows(range_text: str, version: Version) -> bool:
    """Does the range allow `version`? A pre-release is allowed only by a comparator that names a pre-release
    of the same major.minor.patch (Cargo's rule)."""
    comps = comparators(range_text)
    v = (version.major, version.minor, version.patch)
    if version.pre and not any(pre and _v(nums) == v for _, nums, pre in comps):
        return False
    for op, nums, _ in comps:
        for bop, b in _bounds(op, nums):
            ok = {"=": v == b, ">=": v >= b, ">": v > b, "<": v < b, "<=": v <= b}[bop]
            if not ok:
                return False
    return True
