"""Q2: comment problems with evidence and a suggested edit.

Kinds (each fires on a planted fixture and stays quiet on its clean twin, tests/unit/test_quality_findings.py):

- `commented_out_code`: a run of comment lines that parse as code. Python: `ast.parse` of the uncommented text,
  ignoring bare names, constants and annotations (`Note: x` parses but is prose). HDL, C, shell, Tcl, make,
  YAML, TOML: per-language patterns (statement endings, keywords, assignments), requiring a strong line.
  A run inside a comment paragraph (prose on both sides), or an example the prose introduces (a colon, or a
  deeper indent than the prose around it, across blank comment lines), is documentation and not reported. A run
  with prose on one side is reported without an edit: deleting it could leave the prose pointing at nothing.
- `restates_code`: a one-line comment whose words are all in the next code line (identifiers split on case and
  `_`, with a few verb synonyms such as increment for `+=`), and that gives no reason (no "because", "so", ...).
- `banner`: decoration-only comment lines (`#####`, `// ------`) and titles boxed in decoration.
- `boilerplate`: header fields that repeat what git records (File, Author, Date, Revision, `$Id$`...).
  Licence and copyright lines are never touched.
- `stale_todo`: TODO / FIXME / XXX / HACK whose line `git blame` dates older than `stale_todo_days`.
- `unknown_identifier`: a comment naming an identifier (`snake_case`, `camelCase`, `name()`, or a backticked
  name) that no code line, text file, or file or directory name of the repository contains. Reported when this
  file has a close match (a likely rename, given as `did_you_mean`; never an edit, since a close name can mean the
  opposite), or for `name()` / a backticked name in a
  repository without submodules (whose code is not read).

`edit` is a mechanical change (delete or replace whole lines) when one is safe; the patch applies only those.
"""
from __future__ import annotations

import ast
import difflib
import math
import re
import textwrap
import time
from dataclasses import dataclass

from nanosoc_rm.quality.corpus import Corpus, Doc
from nanosoc_rm.quality.lexers import CODE, Comment

__all__ = ["find", "KINDS", "identifier_index", "is_code_line"]

KINDS = ("commented_out_code", "restates_code", "banner", "boilerplate", "stale_todo", "unknown_identifier")


@dataclass
class _Block:
    """Consecutive whole-line comments (or one block comment): what a reader sees as one comment."""
    start: int
    end: int
    texts: list[str]          # per line, markers removed
    comments: list[Comment]
    style: str                # "line" | "block" | "doc"


def _blocks(doc: Doc) -> list[_Block]:
    out: list[_Block] = []
    for c in doc.lexed.comments:
        if c.inline:
            continue
        if c.style in ("block", "doc"):
            # kept even when it shares a line with code (`*/ return a;`): findings still report it, and
            # _whole_lines withholds the edit
            out.append(_Block(c.line, c.end, c.text.split("\n"), [c], c.style))
            continue
        if out and out[-1].style == "line" and out[-1].end == c.line - 1:
            b = out[-1]
            b.end = c.line
            b.texts.append(c.text)
            b.comments.append(c)
        else:
            out.append(_Block(c.line, c.line, [c.text], [c], "line"))
    return out


# ---------------------------------------------------------------------------------------------------------
# Commented-out code
# ---------------------------------------------------------------------------------------------------------
_PRAGMA = re.compile(r"^\s*(type:|noqa|pylint:|pragma\b|fmt:|isort:|mypy:|flake8|-\*-|vim?:|coding[:=]|"
                     r"synopsys|synthesis|lint_off|lint_on|verilator|spyglass|coverage\s+(off|on)|"
                     r"shellcheck|eslint|clang-format|NOLINT|cppcheck|@)", re.I)


def _python_code(text: str) -> bool:
    t = text.strip()
    if not t or _PRAGMA.match(t) or t.startswith(">>>"):
        return False
    try:
        tree = ast.parse(t + ("\n    pass" if t.endswith(":") else ""))
    except (SyntaxError, ValueError):
        return False
    punct = bool(re.search(r"[=()\[\]:.,'\"+\-*/%<>]", t))
    for st in tree.body:
        if isinstance(st, ast.Expr) and not any(isinstance(n, (ast.Call, ast.Subscript, ast.Await, ast.Yield,
                                                                ast.Lambda)) for n in ast.walk(st)):
            continue                                      # "Rows and verdict", "x is y": prose that parses
        if isinstance(st, (ast.Return, ast.Raise, ast.Global, ast.Nonlocal, ast.Delete, ast.Pass, ast.Break,
                           ast.Continue)) and not punct:
            continue                                      # "return value", "pass through"
        if isinstance(st, ast.AnnAssign) and st.value is None:
            continue                                      # "Note: something"
        if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id[:1].isupper():
            continue                                      # "Returns: x=..." prose
        return True
    return False


_HDL_KW = re.compile(r"^\s*(assign|always(_ff|_comb|_latch)?|initial|wire|reg|logic|input|output|inout|"
                     r"module|endmodule|begin|end|else|case[xz]?|endcase|localparam|parameter|genvar|generate|"
                     r"endgenerate|function|endfunction|task|endtask|`(define|include|ifdef|ifndef|endif|else))\b")
_C_KW = re.compile(r"^\s*(#\s*(include|define|if|ifdef|ifndef|endif|else|pragma)\b|return\b|break;|continue;|"
                   r"(if|for|while|switch)\s*\(|else\b|case\b.*:|default:|[{}]\s*$|(static\s+|const\s+)*"
                   r"(int|char|void|float|double|bool|unsigned|uint\d+_t|int\d+_t|size_t|struct\s+\w+)\b)")
_ASSIGN_STMT = re.compile(r"^\s*[A-Za-z_][\w\[\]\.:>\-\*]*\s*(<=|=|\+=|-=|\|=|&=)\s*[^=].*;\s*$")
_CALL_STMT = re.compile(r"^\s*[\$A-Za-z_][\w\.\->]*\s*\(.*\)\s*;\s*$")
_SH_KW = re.compile(r"^\s*(export|source|cd|echo|printf|unset|if\s+\[|fi\b|then\b|else\b|elif\b|for\s+\w+\s+in\b|"
                    r"done\b|while\s|case\s|esac\b|local\s|return\b|exit\b|python3?\s|make\s|git\s|rm\s|cp\s|"
                    r"mv\s|mkdir\s|ln\s|sed\s|awk\s|grep\s|exec\s|\.\s+\S)")
_SH_ASSIGN = re.compile(r"^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*=\S")
_TCL_KW = re.compile(r"^\s*(set|puts|proc|if|foreach|for|while|source|return|global|incr|lappend|append|"
                     r"create_\w+|set_\w+|read_\w+|report_\w+|current_\w+|get_\w+|write_\w+|add_\w+|"
                     r"connect_\w+|place_\w+|route_\w+|link\w*|elaborate|analyze|compile\w*)\s")
_CODE_CHARS = re.compile(r"[\$\[\]\{\};=|\"']|\s-\w")
_MAKE_RULE = re.compile(r"^\s*[\w./%$()-]+\s*:{1,2}(\s|$)(?!=)")
_YAML_KV = re.compile(r"^\s*-?\s*[a-z_][a-z0-9_.-]*:\s*(\S+|\"[^\"]*\"|'[^']*')?\s*$")
_TOML_KV = re.compile(r"^\s*[A-Za-z_][\w.-]*\s*=\s*(\"[^\"]*\"|'[^']*'|-?\d[\d_.]*|true|false|\[.*\]|\{.*\})\s*$")
_NOT_KEYS = re.compile(r"^\s*-?\s*(todo|fixme|note|xxx|hack|warning|example|see|e\.g|i\.e|usage|returns?|args?|"
                       r"params?|default|why|because|nb)\s*:", re.I)


def _prose(t: str) -> bool:
    words = t.split()
    return len(words) >= 4 and t.rstrip().endswith((".", "?", "!")) and not _CODE_CHARS.search(t)


_C_WORDS = set("auto break case char const continue default do double else enum extern float for goto if inline int "
               "long register return short signed sizeof static struct switch typedef union unsigned void volatile "
               "while bool wire reg logic input output inout assign always begin end module endmodule parameter "
               "localparam integer genvar generate function task initial posedge negedge or and not".split())


def _c_prose(t: str) -> bool:
    """A C/HDL comment line that is a sentence even though it ends in `;` or holds `=`: three plain words in a
    row (`so a short bound suffices;`) that are not a declaration (`unsigned long long`), a dash, or a
    `Label: text` lead."""
    if re.search(r"[–—]|\s--\s", t) or re.match(r"^\s*[A-Z][a-z]+(\s[a-z]+)*:\s+[a-z]", t):
        return True
    words = t.split()
    for k in range(len(words) - 2):
        w = words[k:k + 3]
        if all(re.fullmatch(r"[a-z]+", x) for x in w) and sum(x in _C_WORDS for x in w) <= 1:
            return True
    return False


def is_code_line(language: str, text: str) -> tuple[bool, bool]:
    """(looks like code, strongly) for one comment line's text in `language`."""
    t = text.rstrip()
    if not t.strip() or _PRAGMA.match(t):
        return False, False
    if language == "python":
        ok = _python_code(t)
        return ok, ok
    if _prose(t) or re.search(r"[a-z]\.\s+[A-Z][a-z]", t):
        return False, False                               # a sentence, or two of them
    if language not in ("verilog", "c"):
        if re.search(r"<[A-Za-z_][\w-]*>", t):
            return False, False                           # `VAR=<path>`: a usage line with placeholders
        if t.rstrip().endswith(".") and ", " in t:
            return False, False                           # "A=1, B=0, C=2." describes values
        if "=" in t:
            value = t.split("=", 1)[1].strip()
            if len(value.split()) > 3 and not re.search(r"[$/\\\"'(){}\[\]]", value):
                return False, False                       # "ROM_RUN_DIR= still wins over both"
    if language in ("verilog", "c") and _c_prose(t):
        return False, False
    if language in ("verilog", "c"):
        strong = bool(_ASSIGN_STMT.match(t) or _CALL_STMT.match(t) or
                      (t.rstrip().endswith(";") and re.search(r"[=()\[\]]", t) and len(t.split()) <= 12))
        kw = bool((_HDL_KW if language == "verilog" else _C_KW).match(t))
        if language == "verilog" and kw and len(t.split()) > 8 and not t.rstrip().endswith((";", "begin", ")")):
            kw = False                                    # "output of the block is ..." prose
        return strong or kw, strong
    if language in ("shell", "make"):
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s{2,}[A-Za-z(]\S*\s+\S", t):
            return False, False                           # `QUIET=1    do not print...`: an option's help line
        if language == "make" and (_MAKE_RULE.match(t) and "$" in t or re.match(r"^\s*[A-Z_][A-Z0-9_]*\s*[:?+]?=", t)):
            return True, True
        assign = bool(_SH_ASSIGN.match(t))
        kw = bool(_SH_KW.match(t)) and bool(_CODE_CHARS.search(t) or len(t.split()) <= 3)
        return assign or kw, assign or (kw and "$" in t)
    if language == "tcl":
        kw = bool(_TCL_KW.match(t)) and bool(_CODE_CHARS.search(t))
        return kw, kw and bool(re.search(r"[\$\[\]]|\s-\w", t))
    if language == "yaml":
        if _NOT_KEYS.match(t):
            return False, False
        ok = bool(_YAML_KV.match(t))
        return ok, ok and (t.strip().endswith(":") or bool(re.search(r":\s*(\d|true|false|['\"\[{])", t)))
    if language == "toml":
        ok = bool(_TOML_KV.match(t))
        return ok, ok
    return False, False


def _prose_line(text: str, flag: tuple[bool, bool]) -> bool:
    """A non-empty comment line that does not read as code: deleting a code-looking line next to one would cut
    a sentence or an example out of the documentation."""
    return bool(text.strip()) and not flag[0] and not _decor(text)


def _example(doc: Doc, b: _Block, flags: list[tuple[bool, bool]], i: int, j: int) -> bool:
    """Lines i..j are an example inside documentation: the nearest prose line before them (across blank comment
    lines) introduces them with a colon, or they sit indented deeper than the prose on either side."""
    def _indent(k: int) -> int:                           # spaces after the comment marker
        body = re.sub(r"^\s*(//+|#+|--|;+|\*+)", "", doc.lines[b.start + k - 1])
        return len(body) - len(body.lstrip())
    depth = min(_indent(k) for k in range(i, j + 1) if b.texts[k].strip())
    k = i - 1
    while k >= 0 and not b.texts[k].strip():
        k -= 1
    if k >= 0 and _prose_line(b.texts[k], flags[k]):
        if b.texts[k].rstrip().endswith(":") or depth >= _indent(k) + 2:
            return True
    k = j + 1
    while k < len(flags) and not b.texts[k].strip():
        k += 1
    return k < len(flags) and _prose_line(b.texts[k], flags[k]) and depth >= _indent(k) + 2


def _commented_out(doc: Doc, b: _Block) -> list[dict]:
    if b.style == "doc":
        return []
    out = []
    flags = [is_code_line(doc.language, t) for t in b.texts]
    if doc.language == "python" and len(b.texts) > 1:
        whole = textwrap.dedent("\n".join(b.texts))
        if _python_code(whole) and not all(f[0] for f in flags) and \
                sum(1 for t in b.texts if t.strip()) >= 2:
            flags = [(bool(t.strip()), True) for t in b.texts]
    if b.style == "block":
        code = [f for f in flags if f[0]]
        nonempty = [t for t in b.texts if t.strip()]
        if nonempty and len(code) >= max(1, math.ceil(0.6 * len(nonempty))) and any(s for _, s in code):
            out.append(_finding(doc, b.start, b.end, "commented_out_code", nonempty[0].strip(),
                                "Delete the commented-out block: %d of %d lines read as %s code; git history keeps "
                                "it." % (len(code), len(nonempty), doc.language),
                                _delete(doc, b.start, b.end)))
        return out
    i = 0
    while i < len(flags):
        if not flags[i][0]:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and (flags[j + 1][0] or (not b.texts[j + 1].strip() and j + 2 < len(flags)
                                                          and flags[j + 2][0])):
            j += 1
        run = flags[i:j + 1]
        if (j > i and any(s for _, s in run)) or (j == i and run[0][1]):
            start, end = b.start + i, b.start + j
            before = i > 0 and _prose_line(b.texts[i - 1], flags[i - 1])
            after = j + 1 < len(flags) and _prose_line(b.texts[j + 1], flags[j + 1])
            if before and (after or b.texts[i - 1].rstrip().endswith(":")) or _example(doc, b, flags, i, j):
                i = j + 1
                continue                                  # mid-paragraph, or an example the prose introduces
            n = end - start + 1
            if n == 1:
                out.append(_finding(doc, start, end, "commented_out_code", b.texts[i].strip(),
                                    "One line that reads as %s code: delete it by hand if it is dead (no patch "
                                    "edit: a single line is as often a label, `# slot0 = 0x1`, as dead code)."
                                    % doc.language, None))
            elif before or after:
                out.append(_finding(doc, start, end, "commented_out_code", b.texts[i].strip(),
                                    "Reads as %s code (%d line%s) next to prose in the same comment: delete it "
                                    "by hand if it is dead code (no patch edit: the prose may refer to it)."
                                    % (doc.language, n, "" if n == 1 else "s"), None))
            else:
                out.append(_finding(doc, start, end, "commented_out_code", b.texts[i].strip(),
                                    "Delete the commented-out code (%d line%s); git history keeps it."
                                    % (n, "" if n == 1 else "s"), _delete(doc, start, end)))
        i = j + 1
    return out


# ---------------------------------------------------------------------------------------------------------
# Comments that restate the next line
# ---------------------------------------------------------------------------------------------------------
_STOP = set("the a an to of and or for in on is are be this that it its with as by we our from at into then "
            "here now just also all each any value values new do does".split())
_REASON = re.compile(r"\b(because|since|so that|otherwise|workaround|must|never|avoid|ensure|keep|until|unless|"
                     r"bug|hack|todo|fixme|why|note|warning|careful|assume|assumes|instead|but|only|not|"
                     r"required|requires|see|per|spec|errata|erratum)\b", re.I)
_SYN = {"increment": ("+=", "++", "+ 1"), "decrement": ("-=", "--", "- 1"), "set": ("=",), "assign": ("=", "assign"),
        "return": ("return",), "returns": ("return",), "check": ("if",), "loop": ("for", "while", "foreach"),
        "iterate": ("for", "foreach"), "import": ("import",), "initialise": ("=",), "initialize": ("=",),
        "init": ("=",), "print": ("print", "echo", "puts", "$display", "printf"), "call": ("(",),
        "add": ("+", "append", "add"), "append": ("append", "+="), "clear": ("= 0", "clear", "= ''"),
        "reset": ("= 0", "reset"), "create": ("=", "new", "mkdir"), "get": ("get", "="), "read": ("read", "open"),
        "write": ("write",), "open": ("open",), "close": ("close",), "exit": ("exit", "return")}


def _split_ident(tok: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok).replace("_", " ").split()
    return [p.lower() for p in parts if p]


def _words(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text):
        out += _split_ident(tok)
    return out


def _restates(doc: Doc, b: _Block) -> list[dict]:
    if b.style != "line" or b.start != b.end:
        return []
    text = b.texts[0]
    nxt = b.end + 1
    if nxt > len(doc.lexed.kinds) or doc.lexed.kinds[nxt - 1] != CODE or _REASON.search(text) or "?" in text:
        return []
    words = [w for w in _words(text) if w not in _STOP]
    if not 2 <= len(words) <= 8 or not re.match(r"^[A-Za-z][\w\s'.,()-]*$", text.strip()):
        return []
    code = doc.lexed.code[nxt - 1]
    cw = set(_words(code))
    matched = 0
    for w in words:
        if w in cw or (w.endswith("s") and w[:-1] in cw) or any(m in code for m in _SYN.get(w, ())):
            matched += 1
    if matched / len(words) < 0.75:
        return []
    return [_finding(doc, b.start, b.end, "restates_code", "%s  ->  %s" % (text.strip(), code.strip()),
                     "Delete the comment: it says what line %d already says. Keep a comment only for why." % nxt,
                     _delete(doc, b.start, b.end))]


# ---------------------------------------------------------------------------------------------------------
# Banners and boilerplate
# ---------------------------------------------------------------------------------------------------------
_DECOR = re.compile(r"^[\s\-=*#/_~+<>.|:\\]*$")
_TITLED = re.compile(r"^\s*[-=*#/_~+<>]{3,}\s*(\S.*?)\s*[-=*#/_~+<>]{3,}\s*$")
_LEGAL = re.compile(r"copyright|licen[cs]e|spdx|all rights reserved|warranty|confidential|proprietary", re.I)
_FIELD = re.compile(r"^\s*[*#/]*\s*(file\s*name|filename|file|author\(?s?\)?|date|created(\s+on|\s+by)?|"
                    r"create\s+date|modified(\s+by|\s+on)?|last\s+modified|last\s+updated|revision|rev|"
                    r"tool\s+versions?|engineer|\$id|\$revision|\$date|\$author|\$header)\s*[:=$]", re.I)


def _decor(text: str) -> bool:
    t = text.strip()
    return len(t) >= 3 and bool(_DECOR.match(t))


def _raw_decor(line: str) -> bool:
    t = line.strip()
    return len(t) >= 6 and bool(_DECOR.match(t))


def _banner(doc: Doc, b: _Block) -> list[dict]:
    if b.style == "doc":
        return []
    lines = doc.lines
    deco, titled = [], []
    for i in range(b.start, b.end + 1):
        raw = lines[i - 1]
        t = b.texts[i - b.start] if i - b.start < len(b.texts) else ""
        if _LEGAL.search(raw):
            continue
        if (b.style == "line" and (_decor(t) or _raw_decor(raw))) or (b.style == "block" and _raw_decor(raw)):
            deco.append(i)
        elif b.style == "line" and _TITLED.match(t):
            titled.append(i)
    if not deco and not titled:
        return []
    safe = _whole_lines(doc, b.start, b.end)
    edit = None
    if safe:
        new = []
        for i in range(b.start, b.end + 1):
            raw = lines[i - 1]
            if i in deco and (b.style == "line" or b.start < i < b.end):
                continue
            if i in titled:
                indent = raw[:len(raw) - len(raw.lstrip())]
                marker = _marker(raw)
                new.append("%s%s %s" % (indent, marker, _TITLED.match(b.texts[i - b.start]).group(1)))
                continue
            new.append(raw)
        if b.style == "block" and deco and set(range(b.start, b.end + 1)) <= set(deco):
            new = []                                      # a block comment that is all decoration
        if new != lines[b.start - 1:b.end]:
            edit = {"start": b.start, "end": b.end, "replacement": new}
    n = len(deco) + len(titled)
    return [_finding(doc, b.start, b.end, "banner", lines[(deco or titled)[0] - 1].strip()[:120],
                     "Remove %d decoration line%s; a heading needs no box." % (n, "" if n == 1 else "s"), edit)]


def _marker(raw: str) -> str:
    m = re.match(r"^\s*(//+|#+|--|;+|\*+)", raw)
    if not m:
        return "#"
    mk = m.group(1)
    return mk[:2] if mk.startswith("/") else mk[:1]


def _boilerplate(doc: Doc, b: _Block) -> list[dict]:
    if b.start > 60 or b.style == "doc":
        return []
    lines = doc.lines
    hits = []
    for i in range(b.start, b.end + 1):
        raw = lines[i - 1]
        if _LEGAL.search(raw):
            continue
        t = b.texts[i - b.start] if i - b.start < len(b.texts) else ""
        if _FIELD.match(t) or _FIELD.match(raw.strip().lstrip("/#*;- ")):
            if b.style == "line" or b.start < i < b.end:
                hits.append(i)
    if not hits:
        return []
    keep = [lines[i - 1] for i in range(b.start, b.end + 1) if i not in hits]
    edit = {"start": b.start, "end": b.end, "replacement": keep} if _whole_lines(doc, b.start, b.end) else None
    return [_finding(doc, hits[0], hits[-1], "boilerplate", lines[hits[0] - 1].strip()[:120],
                     "Delete %d header field%s that git already records (file name, author, date, revision)."
                     % (len(hits), "" if len(hits) == 1 else "s"), edit)]


# ---------------------------------------------------------------------------------------------------------
# Stale TODOs
# ---------------------------------------------------------------------------------------------------------
_TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _todo_lines(doc: Doc) -> list[tuple[int, str, str]]:
    out = []
    for c in doc.lexed.comments:
        if not _TODO.search(c.text):
            continue
        for n in range(c.line, c.end + 1):
            raw = doc.lines[n - 1] if n - 1 < len(doc.lines) else ""
            m = _TODO.search(raw[c.col:] if n == c.line else raw)
            if m:
                out.append((n, m.group(1), raw.strip()[:160]))
    return out


def _stale_todos(doc: Doc, corpus: Corpus, now: float) -> tuple[list[dict], int]:
    todos = _todo_lines(doc)
    if not todos:
        return [], 0
    days = int(corpus.settings.get("stale_todo_days") or 180)
    times = corpus.source.blame_times(doc.path, [n for n, _, _ in todos])
    out, undated = [], 0
    for n, word, text in todos:
        t = times.get(n)
        if t is None:
            undated += 1
            continue
        age = int((now - t) // 86400)
        if age >= days:
            f = _finding(doc, n, n, "stale_todo", text,
                         "%s is %d days old (blame). Do it, or record it as an issue and delete the comment."
                         % (word, age), None)
            f["age_days"] = age
            out.append(f)
    return out, undated


# ---------------------------------------------------------------------------------------------------------
# Identifiers that no longer exist
# ---------------------------------------------------------------------------------------------------------
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CANDIDATE = re.compile(r"(?<![\w./\-`$*])(`?)([A-Za-z_][A-Za-z0-9_]{3,})(\(\))?\1(?![\w./\-*])")


def identifier_index(corpus: Corpus) -> set[str]:
    """Every identifier-like token of the repository: code lines of the lexed files (comments excluded), all text
    of other text files, and every path component."""
    idx: set[str] = set()
    for d in corpus.docs:
        for line in d.lexed.code:
            idx.update(_IDENT.findall(line))
    lexed = {d.path for d in corpus.docs}
    limit = int(corpus.settings.get("max_file_bytes") or 1048576)
    others = [e.path for e in corpus.source.files()
              if e.path not in lexed and e.mode != "120000" and 0 < e.size <= limit]
    for path, raw in corpus.source.read_many(others).items():
        if raw and b"\0" not in raw[:8192]:
            idx.update(_IDENT.findall(raw.decode("utf-8", "replace")))     # JSON, XML, flists...: all text counts
    for e in corpus.source.files():
        for part in e.path.split("/"):
            idx.add(part)
            idx.update(_IDENT.findall(part))
    for g in corpus.source.gitlinks():
        idx.update(_IDENT.findall(g))
    return idx


def _identifier_like(tok: str, backticked: bool, call: bool) -> bool:
    if backticked or call:
        return True
    if "_" in tok.strip("_") and not tok.isupper():
        return any(ch.isalpha() for ch in tok)
    return bool(re.match(r"^[a-z][a-z0-9]*[A-Z]", tok))          # camelCase; PascalCase is usually a proper noun


def _file_identifiers(doc: Doc) -> list[str]:
    ids = set()
    for line in doc.lexed.code:
        ids.update(t for t in _IDENT.findall(line) if len(t) >= 4)
    return sorted(ids)


def _unknown_identifiers(doc: Doc, b: _Block, index: set[str], file_ids: list[str],
                         has_submodules: bool = False) -> list[dict]:
    """A name the repository does not contain. Reported when a close match exists in this file (likely a rename,
    reported as did_you_mean, never edited) or when the comment marks it as code (`name` or name()) and the repository has no
    submodules (whose code is not read, so a name there would look absent); a bare word with neither is too
    often prose or an external library's name to report."""
    out = []
    seen = set()
    for k, text in enumerate(b.texts):
        if re.search(r"https?://", text):
            text = re.sub(r"https?://\S+", " ", text)
        for m in _CANDIDATE.finditer(text):
            tok, bt, call = m.group(2), bool(m.group(1)), bool(m.group(3))
            if tok in seen or tok in index or not _identifier_like(tok, bt, call):
                continue
            if tok.lower() in ("todo", "fixme", "e_g", "i_e"):
                continue
            seen.add(tok)
            line = b.start + k if b.style == "line" else b.start
            close = difflib.get_close_matches(tok, file_ids, n=1, cutoff=0.85)
            if close:
                why = "`%s` is not in this repository; `%s` in this file is the likely current name." % (tok, close[0])
            elif (bt or call) and not has_submodules:
                why = "`%s` is not in any code line or file name of this repository: update the comment to the " \
                      "current name, or delete it." % tok
            else:
                continue
            # Never an edit: a close match is a guess (`tech_set` -> `tech_unset`, a deliberate typo in an example),
            # so it is reported as did_you_mean for a person to judge.
            f = _finding(doc, line, line, "unknown_identifier", text.strip()[:160], why, None)
            f["identifier"] = tok
            if close:
                f["did_you_mean"] = close[0]
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------------------------
def _whole_lines(doc: Doc, start: int, end: int) -> bool:
    """True when lines start..end hold only comment (no code), so whole-line edits are safe."""
    return all(doc.lexed.kinds[n - 1] != CODE for n in range(start, end + 1))


def _delete(doc: Doc, start: int, end: int) -> dict | None:
    return {"start": start, "end": end, "replacement": []} if _whole_lines(doc, start, end) else None


def _finding(doc: Doc, line: int, end: int, kind: str, evidence: str, suggestion: str, edit: dict | None,
             **extra) -> dict:
    return {"path": doc.path, "line": line, "end_line": end, "kind": kind, "language": doc.language,
            "evidence": evidence[:200], "suggestion": suggestion, "edit": edit, **extra}


def find(corpus: Corpus, *, now: float | None = None, kinds: tuple[str, ...] = KINDS) -> dict:
    """The findings fact (observation kind quality_finding, without the envelope)."""
    now = time.time() if now is None else now
    index = identifier_index(corpus) if "unknown_identifier" in kinds else set()
    has_subs = bool(corpus.source.gitlinks())
    findings: list[dict] = []
    undated = 0
    for doc in corpus.code_docs():
        blocks = _blocks(doc)
        file_ids = _file_identifiers(doc) if "unknown_identifier" in kinds else []
        claimed: set[int] = set()
        for b in blocks:
            if "commented_out_code" in kinds:
                fs = _commented_out(doc, b)
                for f in fs:
                    claimed.update(range(f["line"], f["end_line"] + 1))
                findings += fs
        for b in blocks:
            if any(n in claimed for n in range(b.start, b.end + 1)):
                continue
            if "restates_code" in kinds:
                findings += _restates(doc, b)
            if "banner" in kinds:
                findings += _banner(doc, b)
            if "boilerplate" in kinds:
                findings += _boilerplate(doc, b)
            if "unknown_identifier" in kinds:
                findings += _unknown_identifiers(doc, b, index, file_ids, has_subs)
        if "stale_todo" in kinds:
            fs, u = _stale_todos(doc, corpus, now)
            findings += fs
            undated += u
    findings.sort(key=lambda f: (f["path"], f["line"], KINDS.index(f["kind"])))
    counts = {k: sum(1 for f in findings if f["kind"] == k) for k in KINDS if k in kinds}
    return {"counts": counts, "total": len(findings), "findings": findings, "undated_todos": undated,
            "kinds": list(kinds), "stale_todo_days": int(corpus.settings.get("stale_todo_days") or 180)}
