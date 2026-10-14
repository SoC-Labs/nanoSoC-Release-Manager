"""A reader for `.gitmodules` text (git-config syntax): the one parser, shared by the walker (which reads the blob at
the parent commit, `git cat-file -p <commit>:.gitmodules`, without writing anything) and the T0 gates.

A character-level port of git's own config reader (config.c: parse_file, get_base_var, get_extended_base_var,
get_value, parse_value), so the tool reads the url and path `git submodule` would act on (ROB-property-1..4):

- only `\\n` ends a line (`\\r\\n` reads as `\\n`); other line separators are ordinary characters;
- `[submodule "name"]` keeps the name's case; the legacy `[submodule.Name]` is lower-cased, as git does;
- keys are case-insensitive; a value is unquoted with `\\"`, `\\\\`, `\\n`, `\\t`, `\\b` escapes; unquoted
  leading and trailing whitespace is dropped and an inner run is one space per character; quoted whitespace is
  kept; `#`/`;` outside quotes start a comment; a backslash before a newline continues the value, but not
  inside a comment;
- git refuses a file it cannot parse; this reader stops at that point and returns the modules read before it
  (git config lists those entries, then fails).

Returns the modules in file order; a later key overrides an earlier one, as git does for single-valued keys.
"""
from __future__ import annotations

__all__ = ["parse_gitmodules"]

_SPACE = " \t\n\r"                     # git's isspace (sane_ctype): not \v or \f
_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", '"': '"', "\\": "\\"}


class _Refused(Exception):
    """git would refuse the file here."""


def _alpha(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def _keychar(c: str) -> bool:
    return _alpha(c) or ("0" <= c <= "9") or c == "-"


class _Reader:
    def __init__(self, text: str):
        if text.startswith("﻿"):
            text = text[1:]
        self.text, self.i, self.eof = text, 0, False

    def next(self) -> str:
        """The next character, `\\r\\n` as `\\n`; `\\n` (and eof set) at the end, as git's get_next_char."""
        if self.i >= len(self.text):
            self.eof = True
            return "\n"
        c = self.text[self.i]
        self.i += 1
        if c == "\r" and self.text[self.i:self.i + 1] == "\n":
            self.i += 1
            return "\n"
        return c

    def section(self) -> str:
        """After `[`: the section name, base lower-cased, `base.subsection` for the quoted form."""
        name = []
        while True:
            c = self.next()
            if self.eof:
                raise _Refused
            if c == "]":
                if not name:                        # `[]`: git refuses an empty section name
                    raise _Refused
                return "".join(name)
            if c in _SPACE:
                return self._extended("".join(name), c)
            if not _keychar(c) and c != ".":
                raise _Refused
            name.append(c.lower())

    def _extended(self, base: str, c: str) -> str:
        while True:                                 # get_extended_base_var: a newline before the quote refuses
            if c == "\n":
                raise _Refused
            c = self.next()
            if c not in _SPACE:
                break
        if c != '"':
            raise _Refused
        sub = []
        while True:
            c = self.next()
            if c == "\n":
                raise _Refused
            if c == '"':
                break
            if c == "\\":
                c = self.next()
                if c == "\n":
                    raise _Refused
            sub.append(c)
        if self.next() != "]":
            raise _Refused
        return base + "." + "".join(sub)

    def value(self) -> str:
        """After `=`: git's parse_value."""
        out: list[str] = []
        quote = comment = False
        space = 0
        while True:
            c = self.next()
            if c == "\n":
                if quote:
                    raise _Refused
                return "".join(out)
            if comment:
                continue
            if c in _SPACE and not quote:
                if out:
                    space += 1
                continue
            if not quote and c in ";#":
                comment = True
                continue
            out.extend(" " * space)
            space = 0
            if c == "\\":
                c = self.next()
                if c == "\n":                       # a continuation (at the end of the text: nothing)
                    continue
                if c not in _ESCAPES:
                    raise _Refused
                out.append(_ESCAPES[c])
                continue
            if c == '"':
                quote = not quote
                continue
            out.append(c)


def parse_gitmodules(text: str) -> list[dict[str, str]]:
    """[{"name": ..., "path": ..., "url": ..., "branch": ..., ...}] for every submodule section, in file order."""
    modules: dict[str, dict[str, str]] = {}
    r = _Reader(text)
    section = ""
    comment = False
    try:
        while True:
            c = r.next()
            if c == "\n":
                if r.eof:
                    break
                comment = False
                continue
            if comment or c in _SPACE:
                continue
            if c in "#;":
                comment = True
                continue
            if c == "[":
                section = r.section()
                base, dot, rest = section.partition(".")
                if base == "submodule" and dot:
                    modules.setdefault(rest, {"name": rest})
                continue
            if not _alpha(c):
                raise _Refused
            key = [c.lower()]
            while True:
                c = r.next()
                if r.eof or not _keychar(c):
                    break
                key.append(c.lower())
            while c in " \t":
                c = r.next()
            if c == "\n":
                val = "true"                        # a bare key: git's implicit boolean
            elif c == "=":
                val = r.value()
            else:
                raise _Refused
            base, dot, rest = section.partition(".")
            if base == "submodule" and dot:
                modules.setdefault(rest, {"name": rest})["".join(key)] = val
            if r.eof:
                break
    except _Refused:
        pass
    return list(modules.values())
