"""C1 (SemVer, the Cargo pre-1.0 rule), C2 (classed changelog and its linter), C5 (lint rule), C10 (tag names),
C3/C7/C8 signal rules. Pure functions: no git, no config."""
from __future__ import annotations

import pytest

from nanosoc_rm.versioning import changelog as cl
from nanosoc_rm.versioning import classify as cf
from nanosoc_rm.versioning import semver, tags

pytestmark = [pytest.mark.requirement("C1"), pytest.mark.requirement("C2")]

V = semver.parse


@pytest.mark.parametrize("cur, cls, want", [
    ("0.1.0", "breaking", "0.2.0"), ("0.1.3", "feature", "0.1.4"), ("0.1.3", "fix", "0.1.4"),
    ("0.0.3", "breaking", "0.1.0"), ("1.2.3", "breaking", "2.0.0"), ("1.2.3", "feature", "1.3.0"),
    ("1.2.3", "fix", "1.2.4"), ("0.3.0-rc.2", "fix", "0.3.0"), (None, "fix", "0.1.0")])
def test_c1_bump_follows_the_cargo_pre_1_0_rule(cur, cls, want):
    assert str(semver.bump(V(cur) if cur else None, cls)) == want


def test_c1_none_makes_no_release_and_unknown_class_is_refused():
    assert semver.bump(V("0.1.0"), "none") is None
    with pytest.raises(ValueError):
        semver.bump(V("0.1.0"), "minor")


def test_semver_order_and_tags():
    assert V("0.2.0-rc.1") < V("0.2.0") < V("0.10.0") and V("1.0.0-rc.2") < V("1.0.0-rc.10")
    assert semver.parse_tag("v0.2.0") == V("0.2.0") and semver.parse_tag("v0.2.0-rc.3").pre == "rc.3"
    for bad in ("0.2.0", "v0.2", "v1.0", "v0.2.0-beta", "v0.2.0+b1", "release-0.2.0"):
        assert semver.parse_tag(bad) is None, bad
    assert semver.max_class(["fix", None, "breaking", "feature"]) == "breaking" and semver.max_class([]) is None


@pytest.mark.parametrize("rng, yes, no", [
    ("^0.1", ["0.1.0", "0.1.9"], ["0.2.0", "0.0.9", "1.0.0"]),
    ("^0.0.3", ["0.0.3"], ["0.0.4", "0.1.0"]),
    ("^1.2", ["1.2.0", "1.9.9"], ["2.0.0", "1.1.9"]),
    ("~1.2", ["1.2.5"], ["1.3.0"]),
    (">=0.1.0, <0.3.0", ["0.1.0", "0.2.9"], ["0.3.0"]),
    ("=0.1.2", ["0.1.2"], ["0.1.3"]),
    ("*", ["0.0.1", "9.9.9"], []),
])
def test_cargo_ranges(rng, yes, no):
    r = semver.Range.parse(rng)
    assert all(r.allows(V(v)) for v in yes) and not any(r.allows(V(v)) for v in no)


def test_a_prerelease_satisfies_only_a_range_naming_it():
    assert not semver.Range.parse("^0.2").allows(V("0.2.1-rc.1"))
    assert semver.Range.parse(">=0.2.1-rc.1").allows(V("0.2.1-rc.1"))
    with pytest.raises(semver.RangeError):
        semver.Range.parse("about 0.2")


CLEAN = """# Changelog

## [Unreleased]
### breaking
- Renamed port HSEL_O to HSEL.
### fix
- Fix the flist order.

## [0.2.0] - 2026-10-01
### breaking
- Dropped the PL230 window.
### fix
- Fix a typo
  that wraps a line.

## [0.1.0] - 2026-09-01
### feature
- First release.

[Unreleased]: https://github.com/SoC-Labs/X/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/SoC-Labs/X/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SoC-Labs/X/releases/tag/v0.1.0
"""
TAGS = {"v0.1.0": {}, "v0.2.0": {}}


def rules(text, **kw):
    return sorted({f["rule"] for f in cl.lint(text, **kw)})


@pytest.mark.requirement("C5")
def test_the_clean_changelog_lints_clean_and_parses():
    assert cl.lint(CLEAN, tags=TAGS, base_url="https://github.com/SoC-Labs/X") == []
    p = cl.parse(CLEAN)
    assert [s.version_text for s in p.releases] == ["0.2.0", "0.1.0"]
    assert cl.unreleased_classes(p) == {"breaking": [{"text": "Renamed port HSEL_O to HSEL.", "line": 5}],
                                        "fix": [{"text": "Fix the flist order.", "line": 7}]}
    assert p.releases[0].entries[1].text == "Fix a typo that wraps a line."


PLANTED = {
    "CL1": None,
    "CL2": CLEAN.replace("# Changelog", "# History"),
    "CL3": CLEAN.replace("## [Unreleased]\n", ""),
    "CL4": CLEAN.replace("## [0.2.0] - 2026-10-01", "## [0.2] - 2026-10-01"),
    "CL5": CLEAN.replace("## [0.2.0] - 2026-10-01", "## [0.0.9] - 2026-10-01"),
    "CL6": CLEAN.replace("## [0.1.0] - 2026-09-01", "## [0.2.0] - 2026-09-01"),
    "CL7": CLEAN.replace("### fix\n- Fix the flist", "### Fixed\n- Fix the flist"),
    "CL8": CLEAN.replace("## [Unreleased]\n### breaking", "## [Unreleased]\n- A loose entry.\n### breaking"),
    "CL10": CLEAN.replace("## [0.2.0] - 2026-10-01\n### breaking", "## [0.2.0] - 2026-10-01\n### feature"),
    "CL11": CLEAN.replace("[0.1.0]: https://github.com/SoC-Labs/X/releases/tag/v0.1.0\n", ""),
    "CL12": CLEAN,
    "CL13": CLEAN.replace("- Fix the flist order.", "- The HSEL lint now fails by default."),
}


@pytest.mark.parametrize("rule", sorted(PLANTED))
def test_each_lint_rule_fires_on_its_planted_defect_and_not_on_the_clean_twin(rule):
    kw = {"tags": dict(TAGS, **({"v0.3.0": {}} if rule == "CL12" else {})), "base_url": "https://github.com/SoC-Labs/X"}
    assert rule in rules(PLANTED[rule], **kw), (rule, cl.lint(PLANTED[rule], **kw))
    assert rule not in rules(CLEAN, tags=TAGS, base_url=kw["base_url"])


@pytest.mark.requirement("C5")
def test_c5_a_warning_turned_into_a_failure_must_be_breaking():
    for text in ("Lint warnings are now errors.", "Turn the HSEL warning into a failure.", "fail-by-default HSEL check"):
        bad = CLEAN.replace("- Fix the flist order.", "- " + text)
        assert "CL13" in rules(bad), text
        good = bad.replace("### fix\n- " + text, "### breaking\n- " + text)
        assert "CL13" not in rules(good), text


def test_c2_an_entry_without_a_class_is_rejected():
    """C2 done-when: the linter rejects an entry without a class (an error, so the verdict is FAIL)."""
    f = [x for x in cl.lint(PLANTED["CL8"]) if x["rule"] == "CL8"]
    assert f and f[0]["severity"] == "error" and f[0]["line"] == 4


def test_release_moves_unreleased_under_the_version_and_still_lints_clean():
    new = cl.release(CLEAN, "0.3.0", "2026-10-13", previous_tag="v0.2.0", compare_base="https://github.com/SoC-Labs/X")
    p = cl.parse(new)
    assert [s.version_text for s in p.releases][:1] == ["0.3.0"] and not p.unreleased.entries
    assert cl.lint(new, tags=dict(TAGS, **{"v0.3.0": {}}), base_url="https://github.com/SoC-Labs/X") == []
    assert "[0.3.0]: https://github.com/SoC-Labs/X/compare/v0.2.0...v0.3.0" in new
    gl = cl.release(CLEAN, "0.3.0", "2026-10-13", previous_tag="v0.2.0", compare_base="https://gitlab.example.invalid/g/x")
    assert "[0.3.0]: https://gitlab.example.invalid/g/x/-/compare/v0.2.0...v0.3.0" in gl


@pytest.mark.requirement("C10")
def test_c10_tag_kinds():
    assert [tags.kind_of(t) for t in ("v0.2.0", "v0.2.0-rc.1", "silicon/eth-chiplet-r0p0", "keep/eth-rc4/ahb_qspi",
                                      "v1.0", "integ-step1", "v0.2.0-beta")] == \
        ["release", "candidate", "silicon", "keep", None, None, None]
    assert tags.branch_ok("release/0.2") is True and tags.branch_ok("release/0.2.1") is False
    assert tags.branch_ok("main") is None


@pytest.mark.requirement("C10")
def test_c10_check_rules_grandfathering_and_moves():
    t = lambda **k: dict({"commit": "a" * 40, "annotated": True, "created_at": "2026-09-01T00:00:00Z", "moves": []}, **k)
    repos = {"github/x": {"v0.1.0": t(), "v0.2.0": t(annotated=False, created_at="2026-11-01T00:00:00Z"),
                          "integ-step1": t(), "late-name": t(created_at="2026-11-02T00:00:00Z"),
                          "v0.0.9": t(moves=["b" * 40, "a" * 40])},
             "gitlab/x": {"v0.3.0": t(created_at="2026-11-03T00:00:00Z")}}
    rows = {r["tag"]: r for r in tags.check(repos, "github/x", scheme_from="2026-10-15T00:00:00Z")}
    assert not rows["v0.1.0"]["finding"] and not rows["integ-step1"]["finding"]           # grandfathered
    assert rows["v0.2.0"]["finding"] and "lightweight" in rows["v0.2.0"]["problems"][0]
    assert rows["late-name"]["finding"] and rows["v0.3.0"]["finding"]                       # outside / not canonical
    assert rows["v0.0.9"]["finding"] and rows["v0.0.9"]["moved"], "a moved release tag is a finding, grandfathered or not"
    adv = {r["tag"]: r for r in tags.check(repos, "github/x", scheme_from=None)}
    assert [n for n, r in adv.items() if r["finding"]] == ["v0.0.9"], "no scheme date: advisory except moves"


@pytest.mark.requirement("C3")
def test_c3_the_class_is_the_most_severe_signal_and_complete_only_when_all_measured():
    sig = cf.signal
    k = cf.classify([sig("declared", "fix", "measured", ""), sig("api", "breaking", "measured", ""),
                     sig("consumers", None, "unmeasured", "no generator")])
    assert k["class"] == "breaking" and k["status"] == "partial" and "consumers" in k["reason"]
    k = cf.classify([sig("declared", "fix", "measured", ""), sig("api", "none", "measured", ""),
                     sig("consumers", "feature", "measured", "")])
    assert k == {"class": "feature", "status": "complete", "reason": None, "signals": k["signals"]}
    assert cf.classify([sig("declared", None, "undeclared", ""), sig("api", None, "unmeasured", ""),
                        sig("consumers", None, "unmeasured", "")])["status"] == "unmeasured"


@pytest.mark.requirement("C3")
def test_c3_api_and_regenerated_output_diffs():
    assert cf.api_signal("- a\n- b\n", "- a\n- b\n- c\n")["class"] == "feature"
    assert cf.api_signal("- a\n- b\n", "- a\n")["class"] == "breaking"
    assert cf.api_signal(None, "- a\n")["status"] == "unmeasured" and cf.api_signal("x", None)["status"] == "unmeasured"
    before = {"soc.v": "module soc(\n  input wire clk,\n  output wire hsel_o\n);\n  cpu_ss u_cpu (\n);\n"}
    assert cf.diff_outputs(before, dict(before))[0] == "none"
    assert cf.diff_outputs(before, {"soc.v": before["soc.v"].replace("hsel_o", "hsel")})[0] == "breaking"
    assert cf.diff_outputs(before, {"soc.v": before["soc.v"] + "  input wire irq_in,\n"})[0] == "feature"
    assert cf.diff_outputs(before, {"soc.v": before["soc.v"] + "// a comment\n"})[0] == "fix"
    assert cf.diff_outputs(before, {})[0] == "breaking" and cf.diff_outputs({}, before)[0] == "feature"
    assert cf.consumer_signal("x", "a" * 40, "b" * 40, None)["status"] == "unmeasured"


@pytest.mark.requirement("C8")
def test_c8_host_move_rule():
    assert cf.host_move_class(True, False) == "fix"
    assert cf.host_move_class(True, True) == "breaking" and cf.host_move_class(False, None) == "breaking"
    assert cf.host_move_class(None, None) is None
    s = cf.host_move_signal([{"from": "gitlab", "serves_same_commits": True, "frozen_or_removed": False, "detail": "x"}])
    assert s["class"] == "fix" and s["status"] == "measured"


def test_commit_hint_is_labelled_and_never_the_class():
    h = cf.commit_hint(["feat: a", "fix: b", "refactor!: c", "docs"])
    assert h["class"] == "breaking" and h["counts"] == {"breaking": 1, "feature": 1, "fix": 1, "other": 1}
    assert "never used" in h["note"]
