"""Repository, line and submodule pin read differently everywhere (evaluation 2026-10-16 section 7; David
2026-10-16: evaluation fix wave). A configuration is a line of its repository ("branch main @ sha", "tag v1.0.0
@ sha"), a submodule pin is "path @ sha", two gitlinks of one repository are two rows that read differently and
carry PINNED ×2, and spec codes print as words with the code in a tooltip.

The estate (fpfix.estate) plus a tidelink that pins tidelink-gpio-phy twice, at two paths and two commits, as
the live eth-main tree does."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm.config import load_config
from nanosoc_rm.web import lines, shell, terms
from fpfix import local_site, put_tree, sign_in
from fpfix import ETH_RC4

GPIO_A = "6ee8418" + "a" * 33
GPIO_B = "5c76e76" + "b" * 33
ETH_MAIN = [
    {"tree_path": "tidelink", "component": "tidelink", "behind": 335, "bump_days": 70},
    {"tree_path": "tidelink/deps/tidelink-gpio-phy", "component": "tidelink-gpio-phy", "behind": 12,
     "bump_days": 127, "pinned": GPIO_A},
    {"tree_path": "tidelink/deps/tidelink-phy", "component": "tidelink-gpio-phy", "behind": 0, "bump_days": 93,
     "pinned": GPIO_B},
    {"tree_path": "ASIC/asic-flows", "component": "asic-flow", "behind": 3, "bump_days": 75},
]


@pytest.fixture
def fp(tmp_path, monkeypatch, seeded_db):
    s = local_site(tmp_path, monkeypatch, seeded_db)
    c = sign_in(TestClient(s["app"]))
    c.get("/")                                            # reads the registry records into the store
    put_tree(s["cfg"], "eth-main", "nanosoc-ethernet-chiplet", ETH_MAIN, root_days=68)
    put_tree(s["cfg"], "eth-rc4", "nanosoc-ethernet-chiplet", ETH_RC4, frozen=True, root_days=46)
    s["client"] = c
    s["config"] = load_config(s["cfg"])
    return s


def _row(text, key):
    m = re.search(r'data-row="%s".*?</li>' % re.escape(key), text, re.S)
    assert m, key
    return m.group(0)


def _name(row):
    """The row's visible name (a pin's path carries <wbr> break hints after each "/")."""
    return re.search(r'<span class="fp-name k-\w+">(.*?)</span>', row).group(1).replace("<wbr>", "")


OPEN = "/?v=1&o=p:nanosoc-ethernet-chiplet&o=c:eth-main&o=c:eth-rc4&o=n:eth-main:tidelink"


# ----- Home -------------------------------------------------------------------------------------------------
def test_a_configuration_row_reads_as_a_line_of_its_repository(fp):
    text = fp["client"].get(OPEN).text
    main, rc4 = _row(text, "c:eth-main"), _row(text, "c:eth-rc4")
    assert 'class="glyph g-branch fp-glyph k-cfg"' in main and 'aria-hidden="true"' in main
    assert _name(main) == "branch main @ 2222222"                              # the scanned root commit
    assert '<span class="fp-alias">eth-main</span>' in main                    # the configuration id beside it
    assert 'aria-label="Line branch main at 2222222 of nanosoc-ethernet-chiplet, configuration eth-main' in main
    assert 'class="glyph g-tag fp-glyph k-cfg"' in rc4
    assert _name(rc4).startswith("tag v1.0.0 @ ") and "not counted" in rc4
    proj = _row(text, "p:nanosoc-ethernet-chiplet")
    assert 'class="glyph g-repo fp-glyph k-proj"' in proj


def test_two_gitlinks_of_one_repository_are_two_rows_that_read_differently(fp):
    """The live defect: two sibling rows both named tidelink-gpio-phy, nothing on either telling them apart."""
    text = fp["client"].get(OPEN).text
    a = _row(text, "n:eth-main:tidelink/deps/tidelink-gpio-phy")
    b = _row(text, "n:eth-main:tidelink/deps/tidelink-phy")
    assert _name(a) == "deps/tidelink-gpio-phy @ 6ee8418"                     # path relative to tidelink, sha
    assert _name(b) == "deps/tidelink-phy @ 5c76e76"
    assert '<span class="fp-alias">' not in a                                  # the path already names it
    assert '<span class="fp-alias">tidelink-gpio-phy</span>' in b             # it differs from the path
    for r in (a, b):
        assert '<span class="fp-chip c-again" data-pinned-again><span class="w">PINNED </span>×2</span>' in r
        assert 'class="glyph g-pin fp-glyph k-pin"' in r
    labels = [re.search(r'aria-label="([^"]+)"', r).group(1) for r in (a, b)]
    assert labels[0] != labels[1] and "pinned at 2 commits in this tree" in labels[0]
    # a repository pinned once carries no chip, and a top-level pin keeps its full path
    flow = _row(text, "n:eth-main:ASIC/asic-flows")
    assert re.fullmatch(r"ASIC/asic-flows @ [0-9a-f]{7}", _name(flow))
    assert '<span class="fp-alias">asic-flow</span>' in flow and "data-pinned-again" not in flow


def test_the_pin_panel_names_the_repo_of_a_branch_and_the_other_pin(fp):
    text = fp["client"].get(OPEN + "&sel=n:eth-main:tidelink/deps/tidelink-phy").text
    d = re.search(r'<aside aria-label="Details"[^>]*>(.*?)</aside>', text, re.S).group(1)
    f = dict(re.findall(r"<dt>([^<]+)</dt><dd[^>]*>([^<]*)</dd>", d))
    assert '<h2 class="fp-title t-mono">tidelink/deps/tidelink-phy @ 5c76e76</h2>' in d
    assert "Submodule pin in eth-main (branch main)" in d
    assert f["Commits behind"] == "0 behind tidelink-gpio-phy:main"            # never a bare "main"
    assert f["Also pinned at"] == "tidelink/deps/tidelink-gpio-phy @ 6ee8418"
    assert '<span class="fp-chip">PINNED ×2</span>' in d


def test_the_pin_panel_shows_what_a_pin_tracks(fp, monkeypatch):
    """`.gitmodules` `branch =` (walker branch_field) prints as "Tracks <repo>:<branch>", apart from the pin."""
    from nanosoc_rm.web import views
    real = views.pins
    fake = [{"fact": {"tree_paths": ["tidelink"], "branch_field": "main"}}]
    monkeypatch.setattr(views, "pins", lambda config, cid: fake if cid == "eth-main" else real(config, cid))
    f = dict(re.findall(r"<dt>([^<]+)</dt><dd[^>]*>([^<]*)</dd>",
                        fp["client"].get(OPEN + "&sel=n:eth-main:tidelink").text))
    assert f["Tracks"].startswith("tidelink:main (.gitmodules branch =")
    f = dict(re.findall(r"<dt>([^<]+)</dt><dd[^>]*>([^<]*)</dd>",
                        fp["client"].get(OPEN + "&sel=n:eth-main:ASIC/asic-flows").text))
    assert "Tracks" not in f


def test_a_host_copy_that_differs_reads_hosts_differ(monkeypatch, fp):
    from nanosoc_rm.web import compspace, views
    doc = {"fact": {"relation": "AHEAD", "canonical": {"repo": {"host": "github"}, "default_branch": "main"},
                    "copy": {"repo": {"host": "gitlab"}}, "canonical_only": 23, "copy_only": 0}}
    monkeypatch.setattr(views, "host_compares", lambda config, comp=None: [doc])
    hdr = compspace.header(fp["config"], "tidelink", branch_rows=[], qv={})
    assert ("HOSTS DIFFER: github ahead 23", shell.tone("AHEAD")) in hdr["flags"]
    assert not any(t.startswith("SPLIT") or t.startswith("github AHEAD") for t, _ in hdr["flags"])


def test_how_is_this_judged_has_the_legend(fp):
    for url in ("/", "/matrix/"):
        text = fp["client"].get(url).text
        leg = re.search(r'<p class="fp-legend" data-legend>(.*?)</p>', text, re.S)
        assert leg, url
        for kind, words, _ in lines.LEGEND:
            assert 'data-glyph="%s"' % kind in leg.group(1) and words in leg.group(1)


# ----- Matrix, search, rail, configuration and component pages ------------------------------------------------
def test_the_matrix_root_column_says_root_not_not_used(fp):
    """A repository that is one column's own root reads "root @ sha" there, never "not used"."""
    put_tree(fp["cfg"], "eth-main", "nanosoc-ethernet-chiplet",
             ETH_MAIN + [{"tree_path": "deps/eth", "component": "nanosoc-ethernet-chiplet", "behind": 2}],
             root_days=68)
    text = fp["client"].get("/matrix/?rows=all").text
    row = re.search(r'data-matrix-row="nanosoc-ethernet-chiplet".*?</tr>', text, re.S).group(0)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    rc4 = cells[[m for m in re.findall(r'data-column="([^"]+)"', text)].index("eth-rc4")]
    assert re.search(r"data-cell-root>root @ [0-9a-f]{7}<", rc4), rc4
    assert re.search(r'colspan="2" data-column-repo="nanosoc-ethernet-chiplet"', text)


def test_the_compatibility_tab_has_a_ref_column(fp):
    text = fp["client"].get("/matrix/?tab=compat").text
    row = re.search(r'<tr data-configuration="eth-main">(.*?)</tr>', text, re.S).group(1)
    assert re.search(r'<td data-ref><svg class="glyph g-branch"[^>]*>.*?</svg> branch main</td>', row, re.S)
    assert "follows the branch" in row                                       # not a blank commit
    row = re.search(r'<tr data-configuration="eth-rc4">(.*?)</tr>', text, re.S).group(1)
    assert re.search(r"</svg> tag v1\.0\.0</td>", row)


def test_search_and_rail_name_a_configuration_as_a_line_of_its_repo(fp):
    c = fp["client"]
    hits = shell.search(fp["config"], "eth-")
    sub = {h["label"]: h["sub"] for h in hits if h["kind"] == "config"}
    assert sub["eth-main"] == "line of nanosoc-ethernet-chiplet · branch main"
    assert sub["eth-rc4"].startswith("line of nanosoc-ethernet-chiplet · tag v1.0.0")
    text = c.get("/configurations/eth-main").text
    assert text.count('data-rail-repo="nanosoc-ethernet-chiplet"') == 1           # one heading over its lines
    row = re.search(r'data-rail-configuration="eth-main">(.*?)</a>', text, re.S).group(1)
    assert 'data-glyph="branch">' in row and '</svg> branch main</span><span class="cause"><span class="rail-cid">eth-main</span> · ' in row
    head = re.search(r'<p class="note" data-line>(.*?)</p>', text, re.S).group(1)
    assert head == "Line <code>nanosoc-ethernet-chiplet · branch main · follows the branch</code>"   # no sha invented
    head = re.search(r'<p class="note" data-line>(.*?)</p>', c.get("/configurations/eth-rc4").text, re.S).group(1)
    assert re.fullmatch(r"Line <code>nanosoc-ethernet-chiplet · tag v1\.0\.0 @ [0-9a-f]{7} · frozen at the tag</code>", head)


def test_the_component_page_lists_the_repos_own_lines(fp):
    text = fp["client"].get("/components/nanosoc-ethernet-chiplet").text
    sec = re.search(r'<ul class="lines" data-lines>(.*?)</ul>', text, re.S)
    assert sec, "a repository's own lines have a page"
    ids = re.findall(r'<li data-line="([^"]+)">', sec.group(1))
    assert set(ids) >= {"eth-main", "eth-rc4"}
    assert re.search(r'data-line="eth-rc4"><svg class="glyph g-tag"[^>]*>.*?</svg> <code>tag v1\.0\.0', sec.group(1), re.S)


# ----- the words -------------------------------------------------------------------------------------------
def test_codes_print_as_words_with_the_code_in_a_tooltip():
    assert str(terms.plain("github* ON_DEFAULT")) == 'github* <span title="ON_DEFAULT">on default branch</span>'
    assert str(terms.plain("gitlab PROTECTED_BY_TAG")) == 'gitlab <span title="PROTECTED_BY_TAG">kept by a tag</span>'
    assert str(terms.plain("Tags (C10)")) == 'Tags<span class="term-code" data-code="C10"></span>'
    assert str(terms.plain("a <b> T0")) == 'a &lt;b&gt; <span title="T0">resolve check</span>'
    assert str(terms.plain("UT0 T0x keep")) == "UT0 T0x keep"                  # only whole codes


def test_a_host_chip_on_the_tree_prints_words(fp):
    """The chip macro runs every chip through terms.plain: no UPPER_SNAKE host state is the visible text."""
    from nanosoc_rm.web import page
    html = page.env.from_string('{% import "_ui.html" as ui %}{{ ui.chip("github* BRANCH_ONLY", "c-warn") }}').render()
    assert html == '<span class="chip c-warn">github* <span title="BRANCH_ONLY">on a side branch only</span></span>'


def test_lines_helpers():
    assert lines.line({"id": "x", "component": "r", "ref": {"tag": "v1"}, "commit": "abcdef1234"})["text"] == \
        "tag v1 @ abcdef1"
    assert lines.line({"id": "x", "component": "r", "ref": {"branch": "main"}})["text"] == "branch main"
    p = lines.pin("tidelink/deps/x", "tidelink", "1234567890", "x")
    assert (p["text"], p["alias"]) == ("deps/x @ 1234567", "")
    assert lines.qualified("tidelink", "main") == "tidelink:main" and lines.qualified("t", None) == "[branch]"
    nodes = [{"component": "a", "pinned": "1"}, {"component": "a", "pinned": "2"}, {"component": "b", "pinned": "1"},
             {"component": "b", "pinned": "1"}]
    assert lines.pinned_again(nodes) == {"a": 2}


def _visible(template: str) -> str:
    """A template's text as a reader sees it: tooltips, Jinja comments, tags and expressions removed."""
    t = re.sub(r'title="[^"]*"', "", template)
    for pat in (r"\{#.*?#\}", r"\{%.*?%\}", r"\{\{.*?\}\}", r"<[^>]*>"):
        t = re.sub(pat, "", t, flags=re.S)
    return t


@pytest.mark.parametrize("rel", ["matrix/templates/matrix.html", "web/templates/compat.html"])
def test_matrix_and_compat_print_no_split_and_no_d_codes(rel):
    """Review of wave-g/subrepo-ui: the Matrix notes still printed (D4), (D7), (D8), (D9) and "split component",
    and the compat pin table a bare "split" badge."""
    from pathlib import Path
    import nanosoc_rm
    text = _visible((Path(nanosoc_rm.__file__).parent / rel).read_text())
    assert not re.search(r"\(D\d+\)", text), rel
    assert not re.search(r"\bsplit\b", text, re.I), rel
