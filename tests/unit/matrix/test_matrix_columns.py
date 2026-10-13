"""D3 columns: version tags, Cargo ranges, and each row's pinned | in-range | latest-tag | head commits (B7 rows)."""
from __future__ import annotations

import pytest

from nanosoc_rm.matrix import columns as C
from nanosoc_rm.matrix import versions as V

pytestmark = [pytest.mark.requirement("D3"), pytest.mark.requirement("B7")]


def test_version_tags_and_latest_release():
    assert V.parse("v1.2.3") == (1, 2, 3, None) and V.parse("1.2") == (1, 2, 0, None)
    assert V.parse("v1.0-rc1") == (1, 0, 0, "rc1")
    for not_a_version in ("keep/eth-rc4/tidelink", "rtl-freeze-2026-05-19", "silicon", "v1", "v01.2"):
        assert V.parse(not_a_version) is None, not_a_version
    tags = ["v0.90", "v1.0", "v1.0-rc4-phc-deferred", "v2026.07.16-chiplet-verified", "keep/x/y", "v1-RC"]
    assert V.latest(tags) == "v1.0"                       # pre-releases never count as latest
    assert V.latest(["keep/a", "archive/b"]) is None


@pytest.mark.parametrize("rng, inside, outside", [
    ("~1.0", ["v1.0.0", "v1.0.9"], ["v1.1.0", "v0.9.0", "v1.0.1-rc1"]),
    ("^0.1", ["v0.1.0", "v0.1.7"], ["v0.2.0"]),
    ("^1.2.3", ["v1.2.3", "v1.9.0"], ["v2.0.0", "v1.2.2"]),
    ("^0.0.3", ["v0.0.3"], ["v0.0.4"]),
    (">=0.1.0, <0.3.0", ["v0.1.0", "v0.2.9"], ["v0.3.0"]),
    ("1.2.3", ["v1.4.0"], ["v2.0.0"]),
    ("=1.2", ["v1.2.5"], ["v1.3.0"]),
    ("*", ["v9.9.9"], ["v1.0.0-rc1"]),
])
def test_cargo_ranges(rng, inside, outside):
    comps = V.parse_range(rng)
    for t in inside:
        assert V.satisfies(t, comps), (rng, t)
    for t in outside:
        assert not V.satisfies(t, comps), (rng, t)


def test_unreadable_range_is_an_error():
    with pytest.raises(V.RangeError):
        V.parse_range("latest please")


def test_rows_and_their_column_commits(scene):
    with scene.env.active():
        from nanosoc_rm.config import load_config
        rows, tree = C.rows("top-main", load_config())
    by = {r.component: r for r in rows}
    assert list(by) == ["alpha", "beta"]                  # tree order; the root is not a row
    a = by["alpha"].targets
    assert (a["in-range"].ref, a["in-range"].commit, a["in-range"].range) == ("v1.0.1", scene.a[1], "~1.0")
    assert (a["latest-tag"].ref, a["latest-tag"].commit) == ("v1.1.0", scene.a[2])
    assert (a["head"].ref, a["head"].commit) == ("main", scene.a[3])
    b = by["beta"].targets
    assert b["in-range"].commit is None and "names no range for beta" in b["in-range"].why
    assert b["latest-tag"].commit is None and "no version tag" in b["latest-tag"].why
    assert b["head"].commit == scene.b1 and C.same_as_pinned(by["beta"], scene.b1)
    assert C.overrides_for(by["alpha"], scene.a[2]) == {"a": scene.a[2]}
    assert all(s["command"].startswith("git ") for s in a["latest-tag"].sources)


def test_a_split_component_moves_every_pin_but_not_one_nested_below_another():
    row = C.Row("gen", [{"tree_path": "x/gen", "tree_paths": ["x/gen"], "commit": "1" * 40},
                        {"tree_path": "y/gen", "tree_paths": ["y/gen", "z/gen"], "commit": "2" * 40},
                        {"tree_path": "x/gen/sub/gen", "tree_paths": ["x/gen/sub/gen"], "commit": "3" * 40}],
                split=True, catalogued=True)
    assert C.overrides_for(row, "9" * 40) == {"x/gen": "9" * 40, "y/gen": "9" * 40, "z/gen": "9" * 40}
    assert C.overrides_for(row, "2" * 40) == {"x/gen": "2" * 40}      # y/gen and z/gen already there
    assert not C.same_as_pinned(row, "2" * 40)


def test_unknown_component_is_a_usage_problem(scene):
    from nanosoc_rm.analysis._inputs import UsageProblem
    from nanosoc_rm.config import load_config
    with scene.env.active(), pytest.raises(UsageProblem, match="not in top-main's stored tree"):
        C.rows("top-main", load_config(), ["gamma"])
