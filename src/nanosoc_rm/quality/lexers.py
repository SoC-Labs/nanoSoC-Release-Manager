"""Per-language lexers for comment density and comment findings (Q1, Q2).

Each lexer returns a `Lexed`: one kind per line (code, comment, docstring, blank) and every comment with its
text. A line holding any code token is code, even with a trailing comment (that comment is `inline`). Comment
markers inside string literals are never comments: each lexer knows its language's strings.

Languages and their rules:

- python: the standard `tokenize` module (COMMENT tokens) plus `ast` for docstrings (the first statement of a
  module, class or function when it is a string). A file tokenize refuses falls back to the hash scanner.
- verilog (Verilog and SystemVerilog), c (C, C++, headers, and other C-family files): `//` and `/* */`;
  strings "..." with backslash escapes; in C also character literals; in Verilog `'` is a size/cast tick.
- shell: `#` at the start of a word, outside '...' and "..."; here-document bodies are code.
- make: `#` anywhere unless escaped; a recipe line (starting with a tab) follows shell rules; `\\` continues a
  comment line.
- tcl: `#` only where a command starts (line start or after `;`); "..." strings.
- yaml: `#` at line start or after whitespace, outside quotes; block scalar (`|`, `>`) bodies are code.
- toml: `#` outside "...", '...', and the triple-quoted forms.
- markdown: prose, fenced code, blank and HTML comment lines (counted as documentation, not code).
"""
from __future__ import annotations

import ast
import inspect
import io
import re
import tokenize
from dataclasses import dataclass, field

__all__ = ["CODE", "COMMENT", "DOC", "BLANK", "PROSE", "Comment", "Lexed", "lex", "LEXERS"]

CODE, COMMENT, DOC, BLANK, PROSE = "code", "comment", "docstring", "blank", "prose"


@dataclass
class Comment:
    line: int                  # 1-based first line
    end: int                   # 1-based last line
    text: str                  # without markers; lines joined with "\n"
    style: str                 # "line" | "block" | "doc"
    inline: bool = False       # code precedes it on its first line
    col: int = 0               # 0-based column of the marker on its first line


@dataclass
class Lexed:
    kinds: list[str]
    comments: list[Comment] = field(default_factory=list)
    code: list[str] = field(default_factory=list)       # per line: the code characters only (comments removed)
    fallback: bool = False                                # the language's own lexer refused; a simpler one ran


# ---------------------------------------------------------------------------------------------------------
# A character scanner shared by the C family, Verilog, shell, make, Tcl, YAML and TOML
# ---------------------------------------------------------------------------------------------------------
class _Scan:
    def __init__(self, text: str):
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "" and text.endswith("\n"):
            self.lines.pop()
        n = len(self.lines)
        self.has_code = [False] * n
        self.has_comment = [False] * n
        self.code = [[] for _ in range(n)]
        self.comments: list[Comment] = []

    def code_char(self, ln: int, ch: str) -> None:
        if not ch.isspace():
            self.has_code[ln] = True
        self.code[ln].append(ch)

    def code_str(self, ln: int, s: str) -> None:
        for ch in s:
            self.code_char(ln, ch)

    def line_comment(self, ln: int, col: int, text: str) -> None:
        self.has_comment[ln] = True
        inline = self.has_code[ln]
        self.comments.append(Comment(ln + 1, ln + 1, text, "line", inline, col))

    def result(self) -> Lexed:
        kinds = []
        for i, line in enumerate(self.lines):
            if self.has_code[i]:
                kinds.append(CODE)
            elif self.has_comment[i]:
                kinds.append(COMMENT)
            else:
                kinds.append(BLANK)
        return Lexed(kinds, self.comments, ["".join(c) for c in self.code])


def _strip_block(body: str) -> str:
    """The text of a /* */ block, without its leading `*` gutter."""
    out = []
    for ln in body.split("\n"):
        out.append(re.sub(r"^\s*\*(?!/)\s?", "", ln).rstrip())
    return "\n".join(out).strip("\n")


def _c_like(text: str, *, char_literals: bool) -> Lexed:
    s = _Scan(text)
    ln = 0
    lines = s.lines
    state = None            # None | "block" | "str" | "chr"
    block_start = (0, 0, False)
    block_buf: list[str] = []
    while ln < len(lines):
        line = lines[ln]
        i = 0
        if state is None and "/" not in line and '"' not in line and "'" not in line:
            if line.strip():
                s.has_code[ln] = True                        # fast path: no comment or string can start here
            s.code[ln].append(line)
            ln += 1
            continue
        if state == "block" and not line.strip():
            s.has_comment[ln] = True                         # a blank line inside a block comment
            block_buf.append("")
            i = len(line)
        while i < len(line):
            ch = line[i]
            if state == "block":
                end = line.find("*/", i)
                if end < 0:
                    block_buf.append(line[i:])
                    s.has_comment[ln] = True
                    i = len(line)
                    break
                block_buf.append(line[i:end])
                s.has_comment[ln] = True
                sl, scol, sinl = block_start
                s.comments.append(Comment(sl + 1, ln + 1, _strip_block("\n".join(block_buf)), "block", sinl, scol))
                block_buf = []
                state = None
                i = end + 2
                continue
            if state in ("str", "chr"):
                q = '"' if state == "str" else "'"
                if ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                i += 1
                if ch == q:
                    state = None
                continue
            if line.startswith("//", i):
                s.line_comment(ln, i, line[i + 2:].strip(" \t/!"))
                i = len(line)
                break
            if line.startswith("/*", i):
                state = "block"
                s.has_comment[ln] = True
                block_start = (ln, i, s.has_code[ln])
                block_buf = []
                i += 2
                if i < len(line) and line[i] in "*!":
                    i += 1                                   # /** or /*! doc block opener
                continue
            if ch == '"':
                state = "str"
                s.code_char(ln, ch)
                i += 1
                continue
            if ch == "'" and char_literals:
                # a C character literal: 'x', '\n'; a C++14 digit separator (1'000) is not one
                if i > 0 and line[i - 1].isalnum():
                    s.code_char(ln, ch)
                    i += 1
                    continue
                state = "chr"
                s.code_char(ln, ch)
                i += 1
                continue
            s.code_char(ln, ch)
            i += 1
        if state in ("str", "chr") and not line.endswith("\\"):
            state = None                                     # an unterminated literal ends at the line end
        ln += 1
    if state == "block":
        sl, scol, sinl = block_start
        s.comments.append(Comment(sl + 1, len(lines), _strip_block("\n".join(block_buf)), "block", sinl, scol))
    return s.result()


def lex_c(text: str) -> Lexed:
    return _c_like(text, char_literals=True)


def lex_verilog(text: str) -> Lexed:
    return _c_like(text, char_literals=False)


# ---------------------------------------------------------------------------------------------------------
# Hash-comment languages
# ---------------------------------------------------------------------------------------------------------
_HEREDOC = re.compile(r"<<-?\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def _shell_line(s: _Scan, ln: int, line: str, start: int, state: str | None) -> str | None:
    """Scan one line (from column `start`) with shell rules; returns the open quote state at its end."""
    i = start
    while i < len(line):
        ch = line[i]
        if state == "'":
            s.code_char(ln, ch)
            i += 1
            if ch == "'":
                state = None
            continue
        if state == '"':
            if ch == "\\":
                s.code_str(ln, line[i:i + 2])
                i += 2
                continue
            s.code_char(ln, ch)
            i += 1
            if ch == '"':
                state = None
            continue
        if ch == "\\":
            s.code_str(ln, line[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            state = ch
            s.code_char(ln, ch)
            i += 1
            continue
        if ch == "#" and (i == start or line[i - 1] in " \t;|&()"):
            s.line_comment(ln, i, line[i + 1:].strip())
            return state
        s.code_char(ln, ch)
        i += 1
    return state


def lex_shell(text: str) -> Lexed:
    s = _Scan(text)
    state = None
    heredoc: str | None = None
    for ln, line in enumerate(s.lines):
        if heredoc is not None:
            s.code_str(ln, line)
            if line.strip() == heredoc:
                heredoc = None
            continue
        if ln == 0 and line.startswith("#!"):
            s.code_str(ln, line)                               # the interpreter line is code
            continue
        state = _shell_line(s, ln, line, 0, state)
        if state is None:
            m = _HEREDOC.search("".join(s.code[ln]))
            if m:
                heredoc = m.group(1) or m.group(2) or m.group(3)
    return s.result()


def lex_make(text: str) -> Lexed:
    s = _Scan(text)
    cont_comment = False
    for ln, line in enumerate(s.lines):
        if cont_comment:
            s.has_comment[ln] = True
            s.comments[-1].text += "\n" + line.strip()
            s.comments[-1].end = ln + 1
            cont_comment = line.endswith("\\")
            continue
        if line.startswith("\t"):
            _shell_line(s, ln, line, 0, None)
            continue
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line):
                s.code_str(ln, line[i:i + 2])
                i += 2
                continue
            if ch == "#":
                s.line_comment(ln, i, line[i + 1:].strip())
                cont_comment = line.endswith("\\")
                break
            s.code_char(ln, ch)
            i += 1
    return s.result()


def lex_tcl(text: str) -> Lexed:
    s = _Scan(text)
    cont_comment = False
    in_str = False
    for ln, line in enumerate(s.lines):
        if cont_comment:
            s.has_comment[ln] = True
            s.comments[-1].text += "\n" + line.strip()
            s.comments[-1].end = ln + 1
            cont_comment = line.endswith("\\")
            continue
        i, cmd_start = 0, not in_str
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                if ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == "\\":
                s.code_str(ln, line[i:i + 2])
                i += 2
                cmd_start = False
                continue
            if ch.isspace():
                s.code_char(ln, ch)
                i += 1
                continue
            if ch == "#" and cmd_start:
                s.line_comment(ln, i, line[i + 1:].strip())
                cont_comment = line.endswith("\\")
                break
            if ch == ";":
                s.code_char(ln, ch)
                cmd_start = True
                i += 1
                continue
            if ch == '"':
                in_str = True
            s.code_char(ln, ch)
            cmd_start = False
            i += 1
    return s.result()


def lex_yaml(text: str) -> Lexed:
    s = _Scan(text)
    block_indent: int | None = None                         # inside a block scalar deeper than this indent
    for ln, line in enumerate(s.lines):
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                s.code_str(ln, line)
                continue
            block_indent = None
        i, state = 0, None
        while i < len(line):
            ch = line[i]
            if state == "'":
                s.code_char(ln, ch)
                if ch == "'":
                    if line[i + 1:i + 2] == "'":
                        s.code_char(ln, "'")
                        i += 2
                        continue
                    state = None
                i += 1
                continue
            if state == '"':
                if ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                if ch == '"':
                    state = None
                i += 1
                continue
            if ch == "#" and (i == 0 or line[i - 1] in " \t"):
                s.line_comment(ln, i, line[i + 1:].strip())
                break
            if ch in "'\"" and (i == 0 or line[i - 1] in " \t:[{,-"):
                state = ch
            s.code_char(ln, ch)
            i += 1
        code = "".join(s.code[ln]).rstrip()
        if re.search(r"(^|[:\s-])[|>][+-]?[0-9]?$", code):
            block_indent = indent
    return s.result()


def lex_toml(text: str) -> Lexed:
    s = _Scan(text)
    state: str | None = None
    for ln, line in enumerate(s.lines):
        i = 0
        while i < len(line):
            ch = line[i]
            if state in ('"""', "'''"):
                if line.startswith(state, i):
                    s.code_str(ln, state)
                    i += 3
                    state = None
                    continue
                if state == '"""' and ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                i += 1
                continue
            if state == '"':
                if ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                if ch == '"':
                    state = None
                i += 1
                continue
            if state == "'":
                s.code_char(ln, ch)
                if ch == "'":
                    state = None
                i += 1
                continue
            if line.startswith('"""', i) or line.startswith("'''", i):
                state = line[i:i + 3]
                s.code_str(ln, state)
                i += 3
                continue
            if ch in "'\"":
                state = ch
                s.code_char(ln, ch)
                i += 1
                continue
            if ch == "#":
                s.line_comment(ln, i, line[i + 1:].strip())
                break
            s.code_char(ln, ch)
            i += 1
        if state in ("'", '"'):
            state = None
    return s.result()


# ---------------------------------------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------------------------------------
def _docstring_lines(text: str) -> set[int]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


_SKIP_TOK = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
             tokenize.ENDMARKER, tokenize.COMMENT}


def lex_python(text: str) -> Lexed:
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    n = len(lines)
    has_code, has_comment = [False] * n, [False] * n
    code = [[] for _ in range(n)]
    comments: list[Comment] = []
    docs = _docstring_lines(text)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        lexed = lex_shell_like_python(text)
        lexed.fallback = True
        return lexed
    for tok in toks:
        (sr, sc), (er, _) = tok.start, tok.end
        if tok.type == tokenize.COMMENT:
            if 1 <= sr <= n:
                has_comment[sr - 1] = True
                comments.append(Comment(sr, sr, tok.string[1:].strip(), "line", has_code[sr - 1], sc))
            continue
        if tok.type in _SKIP_TOK:
            continue
        if tok.type == tokenize.STRING and sr in docs:
            continue
        for r in range(sr, min(er, n) + 1):
            if 1 <= r <= n:
                has_code[r - 1] = True
    comment_col = {c.line: c.col for c in comments}
    for i in range(n):
        if has_code[i]:
            code[i].append(lines[i][:comment_col[i + 1]] if (i + 1) in comment_col else lines[i])
    doc_blocks = _doc_blocks(docs)
    for start, end in doc_blocks:
        body = "\n".join(lines[start - 1:end])
        m = re.match(r'^\s*[rRbBuU]{0,2}("""|\'\'\'|"|\')(.*)\1\s*$', body, re.S)
        comments.append(Comment(start, end, inspect.cleandoc(m.group(2) if m else body), "doc", False, 0))
    kinds = []
    for i in range(n):
        if has_code[i]:
            kinds.append(CODE)
        elif (i + 1) in docs and lines[i].strip():
            kinds.append(DOC)
        elif has_comment[i]:
            kinds.append(COMMENT)
        elif (i + 1) in docs:
            kinds.append(DOC)
        else:
            kinds.append(BLANK)
    return Lexed(kinds, sorted(comments, key=lambda c: (c.line, c.col)), ["".join(c) for c in code])


def _doc_blocks(lines: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def lex_shell_like_python(text: str) -> Lexed:
    """Fallback for Python that tokenize refuses (Python 2): # outside '...' and "..." strings."""
    s = _Scan(text)
    for ln, line in enumerate(s.lines):
        i, state = 0, None
        while i < len(line):
            ch = line[i]
            if state:
                if ch == "\\":
                    s.code_str(ln, line[i:i + 2])
                    i += 2
                    continue
                s.code_char(ln, ch)
                if ch == state:
                    state = None
                i += 1
                continue
            if ch in "'\"":
                state = ch
            elif ch == "#":
                s.line_comment(ln, i, line[i + 1:].strip())
                break
            s.code_char(ln, ch)
            i += 1
    return s.result()


# ---------------------------------------------------------------------------------------------------------
# Markdown (documentation, counted separately)
# ---------------------------------------------------------------------------------------------------------
def lex_markdown(text: str) -> Lexed:
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    kinds, fence, in_html = [], None, False
    comments: list[Comment] = []
    for i, line in enumerate(lines):
        st = line.strip()
        if fence:
            kinds.append(CODE)
            if st.startswith(fence):
                fence = None
            continue
        if in_html:
            kinds.append(COMMENT)
            if "-->" in st:
                in_html = False
            continue
        m = re.match(r"^(```+|~~~+)", st)
        if m:
            fence = m.group(1)
            kinds.append(CODE)
            continue
        if st.startswith("<!--"):
            kinds.append(COMMENT)
            comments.append(Comment(i + 1, i + 1, st[4:].replace("-->", "").strip(), "block"))
            in_html = "-->" not in st
            continue
        kinds.append(BLANK if not st else PROSE)
    return Lexed(kinds, comments, [ln if k == CODE else "" for ln, k in zip(lines, kinds)])


LEXERS = {
    "python": lex_python,
    "verilog": lex_verilog,
    "c": lex_c,
    "shell": lex_shell,
    "make": lex_make,
    "tcl": lex_tcl,
    "yaml": lex_yaml,
    "toml": lex_toml,
    "markdown": lex_markdown,
}


def lex(language: str, text: str) -> Lexed:
    """The lexed lines of `text` in `language` (a key of LEXERS). A CRLF line ending is read as LF first (the
    line count is the same), so no comment text keeps a CR and a backslash before CRLF still continues a line
    (ROB-property-8/9)."""
    return LEXERS[language](text.replace("\r\n", "\n"))
