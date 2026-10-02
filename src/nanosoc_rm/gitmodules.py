"""A reader for `.gitmodules` text (git-config syntax): the one parser, shared by the walker (which reads the blob at
the parent commit, `git cat-file -p <commit>:.gitmodules`, without writing anything) and the T0 gates.

Handles `[submodule "name"]` (and the legacy `[submodule.name]`), case-insensitive keys, quoted values with
`\\"`, `\\\\`, `\\n`, `\\t` escapes, `#`/`;` comments outside quotes, and backslash line continuations. Returns the
modules in file order; a later key overrides an earlier one, as git does for single-valued keys.
"""
from __future__ import annotations

import re

__all__ = ["parse_gitmodules"]

_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", '"': '"', "\\": "\\"}


def _value(raw: str) -> str:
    out, quoted, i = [], False, 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip() if not quoted else "".join(out)


def parse_gitmodules(text: str) -> list[dict[str, str]]:
    """[{"name": ..., "path": ..., "url": ..., "branch": ..., ...}] for every submodule section, in file order."""
    text = re.sub(r"\\\r?\n", "", text)                     # line continuations
    modules: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        m = _SECTION.match(s)
        if m:
            section, sub = m.group(1), m.group(2)
            if sub is None and "." in section:
                section, sub = section.split(".", 1)
            if section.lower() == "submodule" and sub is not None:
                name = re.sub(r"\\(.)", r"\1", sub)
                current = modules.setdefault(name, {"name": name})
            else:
                current = None
            rest = s[m.end():].strip()
            if not rest:
                continue
            s = rest
        if current is None:
            continue
        key, eq, raw = s.partition("=")
        key = key.strip().lower()
        if not re.match(r"^[a-z][a-z0-9-]*$", key):
            continue
        current[key] = _value(raw) if eq else "true"
    return list(modules.values())
