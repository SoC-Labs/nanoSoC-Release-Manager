"""Property tests: the .gitmodules reader (nanosoc_rm.gitmodules) against git itself.

The oracle is `git config -f <file> --list`: whatever git reads from a .gitmodules is what `git submodule` acts
on, so the walker and the T0 gates must read the same thing. Two generators: files written by git's own writer
(round trip), and raw text built from the syntax's sharp edges (quotes, escapes, comment markers, legacy
sections, CRLF). Divergences found are recorded as xfail(strict) tests named ROB-property-<n>.
"""
from __future__ import annotations

import re

import pytest
from prop_helpers import (as_set, assume, budget, example, git_config_modules, git_config_write, given,
                          st)

from nanosoc_rm.gitmodules import parse_gitmodules

# Characters git's config format cannot hold in a value or that only the known defects below mishandle.
_LINE_BREAKS = "\0\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_value_chars = st.characters(blacklist_categories=("Cs",), blacklist_characters=_LINE_BREAKS)
VALUE = (st.text(_value_chars, min_size=1, max_size=12).map(str.strip)
         .filter(lambda v: v != "" and not v.endswith("\\")))
NAME = st.text(_value_chars, min_size=1, max_size=8)
KEYS = st.sampled_from(["path", "url", "branch", "update", "shallow", "ignore"])
MODULES = st.lists(st.tuples(NAME, st.dictionaries(KEYS, VALUE, min_size=1, max_size=3)),
                   min_size=1, max_size=3, unique_by=lambda t: t[0])


@budget(50)
@given(MODULES)
def test_reads_what_gits_own_writer_wrote(modules):
    text = git_config_write(modules)
    assert as_set(parse_gitmodules(text)) == as_set(git_config_modules(text))


@given(st.text(max_size=300))
def test_never_raises_and_every_module_has_a_name(text):
    for m in parse_gitmodules(text):
        assert isinstance(m["name"], str)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in m.items())


# Raw text from the syntax's sharp edges.
_TOK = st.sampled_from(["a", "b", "x/y", ".", "-", " ", "#", ";", '"', '\\"', "\\\\", "\\n", "\\t", "\\", "="])
_WORD = st.lists(_TOK, max_size=6).map("".join)
_SECTION = st.one_of(
    st.builds(lambda n: '[submodule "%s"]' % n, st.sampled_from(["a", "b", "a.b", "x y", 'q\\"r'])),
    st.sampled_from(["[submodule.a]", "[core]", '[Submodule "a"]', '[ submodule "b" ]']))
_LINE = st.one_of(
    _SECTION,
    st.builds("{}{}{}".format, st.sampled_from(["path", "url", "Branch", "URL", "update", "x-y"]),
              st.sampled_from(["=", " = "]), _WORD),
    st.sampled_from(["# c", "; c", "", "  "]),
    st.builds('[submodule "a"] path = {}'.format, _WORD))
RAW = (st.lists(st.tuples(_LINE, st.sampled_from(["\n", "\r\n"])), max_size=8)
       .map(lambda ls: "".join(a + b for a, b in ls)))


def _outside_known_defects(text: str, ref: list[dict]) -> bool:
    """False for inputs that only hit ROB-property-1 (edge whitespace) or ROB-property-3 (continuations)."""
    if re.search(r"(?<!\\)(\\\\)+\r?\n", text) or re.search(r"[#;][^\n]*\\\r?\n", text):
        return False
    return all(v == v.strip() for m in ref for v in m.values())


@budget(120)
@given(RAW)
def test_raw_text_reads_as_git_reads_it(text):
    ref = git_config_modules(text)
    assume(ref is not None)                              # git refuses the file: nothing to compare
    assume(_outside_known_defects(text, ref))
    assert as_set(parse_gitmodules(text)) == as_set(ref)


def test_raw_generator_reaches_quotes_comments_and_crlf():
    """The raw generator is not vacuous: the oracle accepts files with each feature and the parser agrees."""
    for text in ['[submodule "a"]\r\n\tpath = "x # y" ; c\r\n\turl = u\\"v\r\n',
                 '[submodule.a]\npath=x\\\ny\n', '[submodule "a"] path = p\n[core]\nurl = no\n']:
        ref = git_config_modules(text)
        assert ref, text
        assert as_set(parse_gitmodules(text)) == as_set(ref)


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ROB-property-1: quoted or escaped leading/trailing whitespace in a value "
                                       "is stripped (gitmodules.py:33); git keeps it")
@example('[submodule "a"]\n\tpath = " x"\n')
@given(st.builds('[submodule "a"]\n\tpath = "{}{}{}"\n'.format, st.sampled_from([" ", "\\t", "\\n"]),
                 st.sampled_from(["x", "sub/dir"]), st.sampled_from(["", " "])))
def test_rob_property_1_quoted_edge_whitespace_is_kept(text):
    assert as_set(parse_gitmodules(text)) == as_set(git_config_modules(text))


@pytest.mark.xfail(strict=True, reason="ROB-property-2: str.splitlines() splits a value at \\x0b \\x0c \\x1c-\\x1e "
                                       "\\x85 U+2028 U+2029 (gitmodules.py:41); git ends a line only at \\n, so a "
                                       "crafted value makes the tool read a different url/path than git")
@example("\u2028")
@given(st.sampled_from(["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]))
def test_rob_property_2_only_newline_ends_a_line(sep):
    text = ('[submodule "a"]\n\tpath = a\n\turl = https://host.example/good.git%s'
            'url = https://host.example/other.git\n' % sep)
    assert as_set(parse_gitmodules(text)) == as_set(git_config_modules(text))


@pytest.mark.xfail(strict=True, reason="ROB-property-3: line continuations are joined before quoting and comments "
                                       "are read (gitmodules.py:39), so a value ending in an escaped backslash, or "
                                       "a comment ending in a backslash, swallows the next line")
@example('[submodule "a"]\n\tpath = x\\\\\n\turl = u\n')
@example('[submodule "a"]\n\tpath = x # note \\\n\turl = u\n')
@given(st.sampled_from(['[submodule "a"]\n\tpath = x\\\\\n\turl = u\n',
                        '[submodule "a"]\n\tpath = x ; note \\\n\turl = u\n']))
def test_rob_property_3_backslash_before_newline(text):
    assert as_set(parse_gitmodules(text)) == as_set(git_config_modules(text))


@pytest.mark.xfail(strict=True, reason="ROB-property-4: the legacy [submodule.Name] form keeps the name's case "
                                       "(gitmodules.py:50); git lower-cases it")
def test_rob_property_4_legacy_section_name_is_lower_cased():
    text = "[submodule.Ethernet]\n\tpath = eth\n"
    assert as_set(parse_gitmodules(text)) == as_set(git_config_modules(text))
