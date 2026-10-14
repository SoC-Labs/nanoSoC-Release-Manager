"""Property tests: SemVer and Cargo-style ranges.

Three modules read versions and ranges: versioning/semver.py (C1, C11), bumps/versions.py (E1 proposals) and
matrix/versions.py (D3 matrix columns). All three document Cargo semantics for `requires[].range`, so on release
versions they must agree; precedence is checked against a SemVer 2.0 section 11 reference implemented here; the
C1 bump rule is checked for its algebraic properties.
"""
from __future__ import annotations

import pytest
from prop_helpers import example, given, st  # loads the profile; skips without hypothesis

from nanosoc_rm.bumps import versions as bv
from nanosoc_rm.matrix import versions as mv
from nanosoc_rm.versioning import semver

NUM = st.integers(0, 4)
CORE = st.tuples(NUM, NUM, NUM)
OPS_ALL = ["", "^", "~", "=", ">=", ">", "<=", "<"]
PARTIAL = st.one_of(CORE.map("%d.%d.%d".__mod__), st.tuples(NUM, NUM).map("%d.%d".__mod__), NUM.map(str))


def comparator(ops):
    return st.tuples(st.sampled_from(ops), PARTIAL).map("".join)


def ranges(ops):
    return st.lists(comparator(ops), min_size=1, max_size=2).map(", ".join)


def verdicts(rng: str, v: tuple[int, int, int]) -> dict[str, bool]:
    return {"semver": semver.Range.parse(rng).allows(semver.Version(*v)),
            "bumps": bv.allows(rng, bv.Version(*v)),
            "matrix": mv.satisfies("v%d.%d.%d" % v, mv.parse_range(rng))}


# Operators whose partial forms all three read the same way (">" and "<=" are ROB-property-5).
@given(ranges(["", "^", "~", "=", ">=", "<"]), CORE)
def test_three_range_readers_agree_on_release_versions(rng, v):
    got = verdicts(rng, v)
    assert len(set(got.values())) == 1, (rng, v, got)


@given(st.sampled_from(["", "^", "~", "=", ">=", ">", "<=", "<"]), CORE, CORE)
def test_full_versions_agree_for_every_operator(op, bound, v):
    rng = op + "%d.%d.%d" % bound
    got = verdicts(rng, v)
    assert len(set(got.values())) == 1, (rng, v, got)


@given(CORE, CORE)
def test_caret_is_the_c1_compatibility_rule(base, v):
    """^X.Y.Z allows exactly the versions a C1 non-breaking bump chain from X.Y.Z can reach."""
    b, x = semver.Version(*base), semver.Version(*v)
    same_series = (x.major == b.major and (b.major > 0 or (x.minor == b.minor and (b.minor > 0 or x.patch == b.patch))))
    assert semver.Range.parse("^%s" % b).allows(x) == (x >= b and same_series)


_IDENT = st.one_of(st.integers(0, 20).map(str), st.from_regex(r"[A-Za-z-][0-9A-Za-z-]{0,4}", fullmatch=True))
PRE = st.one_of(st.none(), st.lists(_IDENT, min_size=1, max_size=3).map(".".join))
VER = st.tuples(st.integers(0, 3), st.integers(0, 3), st.integers(0, 3), PRE)


def ref_cmp(a, b) -> int:
    """SemVer 2.0.0 section 11 precedence."""
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    pa, pb = a[3], b[3]
    if pa == pb:
        return 0
    if pa is None or pb is None:
        return 1 if pa is None else -1
    xa, xb = pa.split("."), pb.split(".")
    for i, j in zip(xa, xb):
        if i == j:
            continue
        if i.isdigit() and j.isdigit():
            return -1 if int(i) < int(j) else 1
        if i.isdigit() or j.isdigit():
            return -1 if i.isdigit() else 1
        return -1 if i < j else 1
    return (len(xa) > len(xb)) - (len(xa) < len(xb))


def text(v) -> str:
    return "%d.%d.%d%s" % (v[0], v[1], v[2], "-" + v[3] if v[3] else "")


def sign(a, b) -> int:
    return (a > b) - (a < b)


@given(VER, VER)
@example((1, 0, 0, "rc.2"), (1, 0, 0, "rc.10"))
@example((1, 0, 0, "rc.1"), (1, 0, 0, "rc.1.1"))
@example((1, 0, 0, "9"), (1, 0, 0, "alpha"))
def test_semver_precedence_is_section_11(a, b):
    va, vb = semver.parse(text(a)), semver.parse(text(b))
    assert sign(va, vb) == ref_cmp(a, b), (text(a), text(b))
    assert (va == vb) == (ref_cmp(a, b) == 0)
    assert str(va) == text(a) and semver.parse(str(va)) == va


@given(VER, st.sampled_from(["", "+build.1", "+sha.abc"]))
def test_parse_tag_reads_only_c10_tags(v, build):
    tag = "v" + text(v) + build
    got = semver.parse_tag(tag)
    is_c10 = build == "" and (v[3] is None or (v[3].startswith("rc.") and v[3][3:].isdigit()
                                               and str(int(v[3][3:])) == v[3][3:]))
    assert (got is not None) == is_c10, tag
    assert semver.parse_tag(text(v)) is None                       # no leading v: not a tag


CLASSES = ["fix", "feature", "breaking"]


@given(st.one_of(st.none(), CORE.map(lambda c: semver.Version(*c))), st.sampled_from(CLASSES))
def test_bump_moves_forward_and_implies_its_class(cur, cls):
    new = semver.bump(cur, cls)
    assert new is not None and new.pre is None
    if cur is None:
        assert new == semver.Version(0, 1, 0)
        return
    assert new > cur
    implied = semver.implied_class(cur, new)
    if cls == "breaking":
        assert implied == "breaking"
    elif cur.major == 0:
        assert implied == "fix-or-feature"
    else:
        assert implied == cls
    if (cur.major, cur.minor) != (0, 0):        # ^0.0.Z is exact in Cargo: see the observation in the report
        assert semver.Range.parse("^%s" % cur).allows(new) == (cls != "breaking")


@given(CORE.map(lambda c: semver.Version(*c)))
def test_bump_is_monotonic_in_the_class(cur):
    fix, feat, brk = (semver.bump(cur, c) for c in CLASSES)
    assert fix <= feat <= brk
    assert semver.bump(cur, "none") is None


@given(st.lists(st.sampled_from(["none", "fix", "feature", "breaking", None, "other"]), max_size=6))
def test_max_class_is_order_independent(classes):
    m = semver.max_class(classes)
    assert m == semver.max_class(list(reversed(classes)))
    known = [c for c in classes if c in semver.CLASS_RANK]
    assert m == (max(known, key=semver.CLASS_RANK.__getitem__) if known else None)


@given(st.text(max_size=20))
def test_range_parsers_never_raise_anything_but_their_error(rng):
    for parse in (semver.Range.parse, bv.comparators, mv.parse_range):
        try:
            parse(rng)
        except ValueError:                                         # every RangeError is a ValueError
            pass


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ROB-property-5: matrix/versions.py reads a partial `>X`/`>X.Y` as `>X.0.0` "
                                       "and `<=X.Y` as `<=X.Y.0` (matrix/versions.py:65), not Cargo's `>=X+1`/"
                                       "`<X.Y+1`; the matrix then picks a version outside the declared range")
@example(">0", (0, 0, 1))
@example("<=1.2", (1, 2, 5))
@given(st.tuples(st.sampled_from([">", "<="]), st.one_of(st.tuples(NUM, NUM).map("%d.%d".__mod__), NUM.map(str)))
       .map("".join), CORE)
def test_rob_property_5_matrix_partial_gt_and_le(rng, v):
    got = verdicts(rng, v)
    assert len(set(got.values())) == 1, (rng, v, got)


@pytest.mark.xfail(strict=True, reason="ROB-property-6: bumps Version.key() orders pre-releases as strings "
                                       "(bumps/versions.py:42), so rc.10 sorts before rc.2 (SemVer 11.4.1)")
@example(("0.1.1-2", "0.1.1-10"))
@given(st.tuples(st.integers(2, 9), st.integers(10, 30)).map(lambda t: ("1.0.0-rc.%d" % t[0], "1.0.0-rc.%d" % t[1])))
def test_rob_property_6_bumps_prerelease_order_is_numeric(pair):
    lo, hi = pair
    assert bv.parse_version(lo).key() < bv.parse_version(hi).key()
