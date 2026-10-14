"""SemVer 2.0 with the Cargo pre-1.0 rule (C1) and Cargo-style version ranges (C11 `requires.range`).

    parse("0.3.1") -> Version(0, 3, 1)            parse_tag("v0.3.1") -> Version   (None when not vX.Y.Z[-pre])
    bump(Version, "breaking"|"feature"|"fix"|"none") -> Version | None
    Range.parse("^0.1").allows(parse("0.1.7"))    Cargo semantics: ^, ~, =, <, <=, >, >=, *, and "a, b"

The bump rule (C1): at 1.0.0 and above, breaking bumps the major, feature the minor, fix the patch. Below 1.0.0,
breaking bumps the middle number and feature or fix bump the last. `none` makes no release. A pre-release
(`-rc.N`) of the target is released as the target itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

__all__ = ["Version", "parse", "parse_tag", "is_release_tag", "bump", "implied_class", "CLASSES", "CLASS_RANK", "max_class",
           "Range", "RangeError", "SEMVER_RX"]

SEMVER_RX = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?\Z")
CLASSES = ("none", "fix", "feature", "breaking")                 # C2, least to most severe
CLASS_RANK = {c: i for i, c in enumerate(CLASSES)}


@dataclass(frozen=True, order=False)
class Version:
    major: int
    minor: int
    patch: int
    pre: str | None = None
    build: str | None = None

    def __str__(self) -> str:
        return "%d.%d.%d%s%s" % (self.major, self.minor, self.patch, "-" + self.pre if self.pre else "",
                                 "+" + self.build if self.build else "")

    @property
    def core(self) -> "Version":
        return Version(self.major, self.minor, self.patch)

    def _pre_key(self) -> tuple:
        if self.pre is None:
            return (1,)                                          # a release sorts after its pre-releases
        out = []
        for part in self.pre.split("."):
            out.append((0, int(part), "") if part.isdigit() else (1, 0, part))
        return (0, tuple(out))

    def key(self) -> tuple:
        return (self.major, self.minor, self.patch, self._pre_key())

    def __lt__(self, other: "Version") -> bool:
        return self.key() < other.key()

    def __le__(self, other: "Version") -> bool:
        return self.key() <= other.key()

    def __gt__(self, other: "Version") -> bool:
        return self.key() > other.key()

    def __ge__(self, other: "Version") -> bool:
        return self.key() >= other.key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())


def parse(text: str) -> Version:
    """A SemVer 2.0 version without a leading v (defs.schema.json#/$defs/semver). ValueError otherwise."""
    m = SEMVER_RX.match(str(text).strip())
    if not m:
        raise ValueError("%r is not a SemVer version X.Y.Z[-pre][+build]" % text)
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), m.group(5))


def parse_tag(tag: str) -> Version | None:
    """The version of a C10 release or candidate tag: `vX.Y.Z` or `vX.Y.Z-rc.N`. None for any other name."""
    if not tag.startswith("v"):
        return None
    try:
        v = parse(tag[1:])
    except ValueError:
        return None
    if v.build is not None or (v.pre is not None and not re.match(r"^rc\.(0|[1-9][0-9]*)$", v.pre)):
        return None
    return v


def is_release_tag(tag: str) -> bool:
    v = parse_tag(tag)
    return v is not None and v.pre is None


def max_class(classes: Iterable[str | None]) -> str | None:
    """The most severe class (C3: a change's class is the most severe of its signals); None when none given."""
    known = [c for c in classes if c in CLASS_RANK]
    return max(known, key=CLASS_RANK.__getitem__) if known else None


def bump(current: Version | None, change_class: str) -> Version | None:
    """The next release version after `current` for a change of `change_class` (C1, Cargo pre-1.0 rule).
    None for `none` (no release). `current` None (never released) gives 0.1.0 for any releasing class."""
    if change_class not in CLASS_RANK:
        raise ValueError("unknown change class %r (C2: one of %s)" % (change_class, ", ".join(CLASSES)))
    if change_class == "none":
        return None
    if current is None:
        return Version(0, 1, 0)
    c = current
    if c.pre is not None:                                      # a candidate of X.Y.Z releases as X.Y.Z
        return c.core
    if c.major == 0:
        if change_class == "breaking":
            return Version(0, c.minor + 1, 0)
        return Version(0, c.minor, c.patch + 1)
    if change_class == "breaking":
        return Version(c.major + 1, 0, 0)
    if change_class == "feature":
        return Version(c.major, c.minor + 1, 0)
    return Version(c.major, c.minor, c.patch + 1)


def implied_class(prev: Version | None, new: Version) -> str:
    """The class a release from `prev` to `new` implies under C1 (what the changelog linter compares against)."""
    if prev is None:
        return "feature"
    if new.major != prev.major:
        return "breaking"
    if new.major == 0:
        return "breaking" if new.minor != prev.minor else "fix-or-feature"
    return "feature" if new.minor != prev.minor else "fix"


# ---------------------------------------------------------------------------------------------------------
# Cargo-style ranges (component-manifest requires[].range)
# ---------------------------------------------------------------------------------------------------------
class RangeError(ValueError):
    """A range the Cargo syntax does not allow."""


_COMPARATOR = re.compile(r"^\s*(\^|~|=|<=|>=|<|>)?\s*v?(\*|\d+)(?:\.(\*|\d+))?(?:\.(\*|\d+))?(?:-([0-9A-Za-z.-]+))?\s*\Z")


def _bounds(op: str, parts: list[str | None], pre: str | None) -> list[tuple[str, Version]]:
    """[(op, version)] lower/upper bounds of one comparator, Cargo semantics."""
    nums = [p for p in parts if p is not None and p != "*"]
    if parts[0] == "*" or not nums:
        return [(">=", Version(0, 0, 0))]
    major = int(nums[0])
    minor = int(nums[1]) if len(nums) > 1 else None
    patch = int(nums[2]) if len(nums) > 2 else None
    low = Version(major, minor or 0, patch or 0, pre)
    if op == "^":
        if major > 0 or minor is None:
            up = Version(major + 1, 0, 0)
        elif minor > 0 or patch is None:
            up = Version(0, minor + 1, 0)
        else:
            up = Version(0, 0, patch + 1)
        return [(">=", low), ("<", up)]
    if op == "~":
        up = Version(major + 1, 0, 0) if minor is None else Version(major, minor + 1, 0)
        return [(">=", low), ("<", up)]
    if op == "=":
        if patch is not None:
            return [("=", low)]
        up = Version(major + 1, 0, 0) if minor is None else Version(major, minor + 1, 0)
        return [(">=", low), ("<", up)]
    if op in (">=", ">", "<", "<="):
        if op == ">" and patch is None:                       # >1.2 means >=1.3.0
            return [(">=", Version(major + 1, 0, 0) if minor is None else Version(major, minor + 1, 0))]
        if op == "<=" and patch is None:
            return [("<", Version(major + 1, 0, 0) if minor is None else Version(major, minor + 1, 0))]
        return [(op, low)]
    raise RangeError("unknown operator %r" % op)


@dataclass(frozen=True)
class Range:
    text: str
    bounds: tuple[tuple[str, Version], ...]

    @classmethod
    def parse(cls, text: str) -> "Range":
        """A Cargo version requirement: comma-separated comparators, every one must hold. A bare version means ^."""
        if not str(text).strip():
            raise RangeError("an empty range")
        bounds: list[tuple[str, Version]] = []
        for part in str(text).split(","):
            m = _COMPARATOR.match(part)
            if not m:
                raise RangeError("%r is not a Cargo version requirement" % part.strip())
            op = m.group(1) or "^"
            bounds += _bounds(op, [m.group(2), m.group(3), m.group(4)], m.group(5))
        return cls(str(text), tuple(bounds))

    def allows(self, v: Version) -> bool:
        """Cargo: a pre-release satisfies a range only when a comparator names the same X.Y.Z with a pre-release."""
        if v.pre is not None and not any(b.pre is not None and b.core == v.core for _, b in self.bounds):
            return False
        for op, b in self.bounds:
            ok = {"=": v == b, ">=": v >= b, ">": v > b, "<": v < b, "<=": v <= b}[op]
            if not ok:
                return False
        return True
