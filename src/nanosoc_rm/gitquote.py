"""git's C-style path quoting (quote.c quote_c_style, with core.quotePath on, git's default), for the paths
written into diff headers. git quotes a path holding a control character, `"`, `\\` or a byte above 0x7f, and
`git apply` refuses an unquoted header holding a tab or a newline (ROB-property-13)."""
from __future__ import annotations

__all__ = ["quote_path"]

_NAMED = {0x07: "\\a", 0x08: "\\b", 0x09: "\\t", 0x0a: "\\n", 0x0b: "\\v", 0x0c: "\\f", 0x0d: "\\r",
          0x22: '\\"', 0x5c: "\\\\"}


def _needs(b: int) -> bool:
    return b < 0x20 or b == 0x22 or b == 0x5c or b >= 0x7f


def quote_path(path: str) -> str:
    """`path` as git writes it in a diff header: unchanged when nothing needs quoting, else in double quotes
    with C escapes (`\\t`, `\\n`, `\\"`, `\\\\`, ...) and every other such byte of its UTF-8 as `\\ooo`."""
    raw = path.encode("utf-8", "surrogateescape")
    if not any(_needs(b) for b in raw):
        return path
    out = []
    for b in raw:
        if b in _NAMED:
            out.append(_NAMED[b])
        elif _needs(b):
            out.append("\\%03o" % b)
        else:
            out.append(chr(b))
    return '"%s"' % "".join(out)
