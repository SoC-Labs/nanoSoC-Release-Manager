"""Property tests: the per-language quality lexers (quality/lexers.py), Q1/Q2.

For every language: total on any text (unicode, lone CR, form feeds, unterminated strings and blocks), one kind
per line, comment spans inside the file; a comment marker inside a string literal is never a comment; a trailing
or own-line comment is found with its exact text; an unterminated block comment runs to the end of the file; and
a file reads the same with CRLF as with LF line ends.
"""
from __future__ import annotations

from prop_helpers import assume, example, given, st  # loads the profile; skips without hypothesis

from nanosoc_rm.quality import lexers as L

LANGS = sorted(L.LEXERS)
KINDS = (L.CODE, L.COMMENT, L.DOC, L.BLANK, L.PROSE)


def n_lines(text: str) -> int:
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return len(lines)


ANY = st.text(st.characters(blacklist_categories=("Cs",)), max_size=200)
_FRAG = st.sampled_from(["x", "=", " ", "1", "#", "//", "/*", "*/", '"', "'", "\\", ";", "|", ":", "<<EOF", "EOF",
                         "'''", '"""', "\t", "-", "{", "<!--", "-->", "```", "\u00e9", "\u2028", "\r", "\x0c"])
STRUCTURED = st.lists(st.lists(_FRAG, max_size=6).map("".join), max_size=6).map("\n".join)


@given(st.sampled_from(LANGS), st.one_of(ANY, STRUCTURED))
def test_total_one_kind_per_line_and_spans_inside_the_file(lang, text):
    r = L.lex(lang, text)
    n = n_lines(text)
    assert len(r.kinds) == n and len(r.code) == n
    assert all(k in KINDS for k in r.kinds)
    for c in r.comments:
        assert 1 <= c.line <= c.end <= max(n, 1), c
        assert c.style in ("line", "block", "doc")


# A string literal per language, holding text that may contain any comment marker.
_STRING = {"c": 'x = "%s";', "verilog": 'x = "%s";', "shell": 'x="%s"', "tcl": 'set x "%s"', "yaml": 'k: "%s"',
           "toml": 'k = "%s"', "python": 'x = "%s"'}
_SAFE = st.text(st.characters(blacklist_categories=("Cs", "Zl", "Zp", "Cc")), max_size=10).filter(
    lambda s: not set(s) & set("\"'\\$`"))
_INNER = st.lists(st.one_of(_SAFE, st.sampled_from(["#", "//", "/*", "*/", ";#", " # ", "--", "<!--"])),
                  max_size=5).map("".join)


@given(st.sampled_from(sorted(_STRING)), _INNER, st.sampled_from(["\n", "\r\n"]))
@example("c", "a // b /* c", "\n")
@example("shell", "a # b", "\n")
def test_a_marker_inside_a_string_is_never_a_comment(lang, inner, eol):
    line = _STRING[lang] % inner
    r = L.lex(lang, line + eol + line + eol)
    assert r.comments == [] and r.kinds == [L.CODE, L.CODE], (lang, line)


_MARK = {"c": "//", "verilog": "//", "shell": "#", "make": "#", "tcl": ";#", "yaml": "#", "toml": "#", "python": "#"}
_CODE = {"c": "x = 1;", "verilog": "assign x = 1;", "shell": "x=1", "make": "X := 1", "tcl": "set x 1",
         "yaml": "k: 1", "toml": "k = 1", "python": "x = 1"}
TEXT = st.from_regex(r"[A-Za-z0-9\u00e9\u4e2d][A-Za-z0-9 ,.:()=\u00e9\u4e2d-]{0,20}[A-Za-z0-9.)]", fullmatch=True)


@given(st.sampled_from(sorted(_MARK)), TEXT, st.booleans())
def test_a_trailing_or_own_line_comment_has_its_exact_text(lang, text, own_line):
    line = (_MARK[lang].lstrip(";") if own_line else _CODE[lang] + " " + _MARK[lang]) + " " + text
    r = L.lex(lang, "\n".join([_CODE[lang], line, _CODE[lang]]) + "\n")
    assert [(c.line, c.text, c.inline) for c in r.comments] == [(2, text, not own_line)]
    assert r.kinds == [L.CODE, L.COMMENT if own_line else L.CODE, L.CODE]


@given(st.sampled_from(["c", "verilog"]), st.lists(TEXT, min_size=1, max_size=4))
def test_an_unterminated_block_comment_runs_to_the_end(lang, body):
    text = "x = 1; /* " + "\n".join(body) + "\n"
    r = L.lex(lang, text)
    assert r.kinds[0] == L.CODE and all(k == L.COMMENT for k in r.kinds[1:])
    assert len(r.comments) == 1 and r.comments[0].end == len(r.kinds) and r.comments[0].inline


@given(st.lists(TEXT, min_size=1, max_size=4))
def test_python_docstring_is_doc_and_its_hash_is_not_a_comment(body):
    text = 'def f():\n    """%s # not a comment\n    """\n    return 1  # real\n' % "\n    ".join(body)
    r = L.lex("python", text)
    assert [c.text for c in r.comments if c.style == "line"] == ["real"]
    assert r.kinds[1:-1] == [L.DOC] * (len(r.kinds) - 2) and r.kinds[-1] == L.CODE


@given(st.sampled_from(["shell", "yaml", "tcl"]), st.from_regex(r"[a-z]{1,4}#[a-z]{1,4}", fullmatch=True))
@example("shell", "a#b")
def test_a_hash_inside_a_word_is_not_a_comment(lang, word):
    line = {"shell": "echo %s ${#x} $#", "yaml": "k: %s", "tcl": "puts %s"}[lang] % word
    r = L.lex(lang, line + "\n")
    assert r.comments == [] and r.kinds == [L.CODE], (lang, line, r.comments)


_CRLF_SAFE = st.sampled_from(["x", "=", " ", "1", "#", "//", "/*", "*/", '"', "'", ";", "|", ":", "<<EOF", "EOF",
                              "'''", '"""', "-", "{"])          # no backslash: see ROB-property-9
CRLF_LINES = st.lists(st.lists(_CRLF_SAFE, max_size=6).map("".join), min_size=1, max_size=5)


@given(st.sampled_from(LANGS), CRLF_LINES)
def test_crlf_reads_like_lf(lang, lines):
    a = L.lex(lang, "\n".join(lines) + "\n")
    b = L.lex(lang, "\r\n".join(lines) + "\r\n")
    assert a.kinds == b.kinds
    # comment text compared without CR: a stray CR in the text is ROB-property-8, recorded below
    def norm(r):
        # drop the CR, then re-apply the C lexer's own strip set (" \t/!") that the CR blocked
        return [(c.line, c.end, c.text.replace("\r", "").strip(" \t/!")) for c in r.comments]
    assert norm(a) == norm(b)


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@example("c", ["x = 1; // note"])
@example("python", ['"""doc', "more", '"""'])
# A comment each language reads as one (`//` is not a Python comment, so Python gets `#`).
@given(st.sampled_from(["c", "verilog", "python"]),
       st.sampled_from([["x = 1; // note"], ["x = 1  # note"], ['"""doc', "more", '"""']]))
def test_rob_property_8_comment_text_has_no_cr(lang, lines):
    assume((lang == "python") == (lines[0] != "x = 1; // note"))
    r = L.lex(lang, "\r\n".join(lines) + "\r\n")
    assert r.comments and all("\r" not in c.text for c in r.comments), [c.text for c in r.comments]


@example("make", ["# comment \\", "continued", "X := 1"])
@example("tcl", ["# comment \\", "continued", "set x 1"])
@given(st.sampled_from(["make", "tcl"]), st.just(["# comment \\", "continued", "x"]))
def test_rob_property_9_continuation_under_crlf(lang, lines):
    assert L.lex(lang, "\r\n".join(lines) + "\r\n").kinds == L.lex(lang, "\n".join(lines) + "\n").kinds
