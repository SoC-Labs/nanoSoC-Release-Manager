"""Property tests: the classed changelog (versioning/changelog.py parse, lint, release).

Generated changelogs follow the C2 format with a C1-consistent version chain, so the linter must find no error in
them; G1's release rewrite must move exactly the Unreleased entries under the new version, leave every older
release untouched, keep the file lint-clean and the links complete; and parse/lint/release never raise on
arbitrary text (release only its documented ValueError).
"""
from __future__ import annotations

from prop_helpers import budget, example, given, st  # loads the profile; skips without hypothesis

from nanosoc_rm.versioning import changelog as C, semver

BASE = "https://github.example/o/r"
WORD = st.from_regex(r"[A-Za-z][a-z0-9]{0,6}", fullmatch=True)
ENTRY = st.lists(WORD, min_size=1, max_size=5).map(" ".join)
BLOCK = st.dictionaries(st.sampled_from(["breaking", "feature", "fix"]), st.lists(ENTRY, min_size=1, max_size=3),
                        min_size=1, max_size=3)
BULLET = st.sampled_from(["- ", "* ", "+ ", "1. ", "  - "])


def render(unrel: dict, rels: list[dict], bullet: str, eol: str = "\n") -> tuple[str, list]:
    lines = ["# Changelog", ""]

    def body(d):
        for cls, entries in d.items():
            lines.append("### " + cls)
            lines.extend(bullet + e for e in entries)
            lines.append("")
    lines += ["## [Unreleased]", ""]
    body(unrel)
    chain, v = [], None
    for d in reversed(rels):
        v = semver.bump(v, semver.max_class(d.keys()))
        chain.append((v, d))
    chain.reverse()
    for i, (v, d) in enumerate(chain):
        lines += ["## [%s] - 2026-%02d-01" % (v, 9 - i), ""]
        body(d)
    lines.append("[Unreleased]: %s/compare/v%s...HEAD" % (BASE, chain[0][0]))
    for i, (v, _d) in enumerate(chain):
        lines.append("[%s]: %s/compare/v%s...v%s" % (v, BASE, chain[i + 1][0], v) if i + 1 < len(chain)
                     else "[%s]: %s/releases/tag/v%s" % (v, BASE, v))
    return eol.join(lines) + eol, chain


def errors(text: str) -> list[dict]:
    return [f for f in C.lint(text, base_url=BASE) if f["severity"] == "error"]


def shape(sections) -> list:
    return [(str(s.version), [(e.cls, e.text) for e in s.entries]) for s in sections]


@budget(100)
@given(BLOCK, st.lists(BLOCK, min_size=1, max_size=4), BULLET)
def test_a_c1_consistent_changelog_lints_clean(unrel, rels, bullet):
    text, _chain = render(unrel, rels, bullet)
    assert errors(text) == []
    assert [f for f in C.lint(text, base_url=BASE) if f["rule"] == "CL11"] == []


@given(BLOCK, st.lists(BLOCK, min_size=1, max_size=4), BULLET, st.sampled_from(["\n", "\r\n"]))
def test_release_moves_exactly_the_unreleased_entries(unrel, rels, bullet, eol):
    text, chain = render(unrel, rels, bullet, eol)
    before = C.parse(text)
    moved = [(e.cls, e.text) for e in before.unreleased.entries]
    assert sorted(c for c, _ in moved) == sorted(c for c, es in unrel.items() for _ in es)
    new_v = semver.bump(chain[0][0], semver.max_class(unrel.keys()))
    out = C.release(text, str(new_v), "2026-10-01", previous_tag="v%s" % chain[0][0], compare_base=BASE)
    after = C.parse(out)
    assert after.unreleased is not None and after.unreleased.entries == []
    assert str(after.releases[0].version) == str(new_v) and after.releases[0].date == "2026-10-01"
    assert [(e.cls, e.text) for e in after.releases[0].entries] == moved
    assert shape(after.releases[1:]) == shape(before.releases)
    assert errors(out) == []
    assert [f for f in C.lint(out, base_url=BASE) if f["rule"] == "CL11"] == []


@given(BLOCK, st.lists(BLOCK, min_size=1, max_size=3), BULLET)
def test_a_wrong_version_is_caught_by_cl10(unrel, rels, bullet):
    """The linter is not vacuous: releasing with a version C1 does not give for the classes is an error."""
    text, chain = render(unrel, rels, bullet)
    want = semver.bump(chain[0][0], semver.max_class(unrel.keys()))
    wrong = (semver.Version(want.major + 1, 0, 0) if want.major > 0
             else semver.Version(0, want.minor + 1, want.patch + 1))
    out = C.release(text, str(wrong), "2026-10-01", previous_tag="v%s" % chain[0][0], compare_base=BASE)
    assert "CL10" in {f["rule"] for f in errors(out)}


@given(st.text(max_size=300))
def test_parse_lint_release_never_raise(text):
    C.parse(text)
    C.lint(text, tags={"v1.0.0": {}, "v0.1.0-rc.1": {}}, base_url=BASE)
    try:
        C.release(text, "1.0.0", "2026-10-01", previous_tag=None, compare_base=BASE)
    except ValueError as e:
        assert "Unreleased" in str(e)


_NOISE = st.sampled_from(["## [Unreleased]", "### fix", "### Added", "- x", "1) y", "<!-- c", "-->", "```", "    z",
                          "## [0.1.0] - 2026-02-30", "## [0.1.0]", "[0.1.0]: http://x", "text", "", "# Changelog"])


@given(st.lists(_NOISE, max_size=12).map("\n".join))
def test_lint_on_structured_noise_never_raises_and_cites_real_lines(text):
    n = len(text.splitlines())
    for f in C.lint(text, base_url=BASE):
        assert f["rule"] in C.RULES and f["severity"] in ("error", "warning")
        assert f["line"] is None or 1 <= f["line"] <= max(n, 1), f


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@example("\r\n")
@given(st.sampled_from(["\r\n", "\n"]), )
def test_rob_property_7_release_keeps_line_endings(eol):
    text = eol.join(["# Changelog", "", "## [Unreleased]", "### fix", "- a\u2028b", "",
                     "[Unreleased]: %s/commits/HEAD" % BASE]) + eol
    out = C.release(text, "0.1.0", "2026-10-01", previous_tag=None, compare_base=None)
    assert out.replace(eol + eol + "## [0.1.0] - 2026-10-01", "", 1) == text
