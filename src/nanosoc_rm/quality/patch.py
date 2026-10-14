"""Unified-diff patches a person applies with `git apply` (Q2, Q3, Q4, Q5). The tools never edit a file.

An edit is `{start, end, replacement}`: lines start..end (1-based, inclusive) of the file at the analysed
commit become `replacement`. Several edits of one file are combined line by line; where two edits change the
same line differently, the first wins and the other is reported as not applied.
"""
from __future__ import annotations

import difflib

from nanosoc_rm.gitquote import quote_path

__all__ = ["file_patch", "patch_for", "combine"]


def _ops(original: list[str], edit: dict) -> dict[int, str | None] | None:
    """{line number: None (delete) | new text} for one edit, or None when it inserts lines (not combinable)."""
    s, e = edit["start"], edit["end"]
    old = original[s - 1:e]
    new = list(edit["replacement"])
    ops: dict[int, str | None] = {}
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for i in range(i1, i2):
                ops[s + i] = None
        elif tag == "replace":
            for k, i in enumerate(range(i1, i2)):
                ops[s + i] = new[j1 + k] if j1 + k < j2 else None
            if (j2 - j1) > (i2 - i1):
                return None
        else:
            return None
    return ops


def combine(original: list[str], edits: list[dict]) -> tuple[list[str], list[dict]]:
    """(the new lines, the edits that could not be applied)."""
    merged: dict[int, str | None] = {}
    skipped = []
    for ed in sorted(edits, key=lambda x: (x["start"], x["end"])):
        if ed["start"] < 1 or ed["end"] > len(original) or ed["start"] > ed["end"]:
            skipped.append(ed)
            continue
        ops = _ops(original, ed)
        if ops is None:
            if any(n in merged for n in range(ed["start"], ed["end"] + 1)):
                skipped.append(ed)
                continue
            for n in range(ed["start"], ed["end"] + 1):
                merged[n] = None
            if ed["replacement"]:
                merged[ed["start"]] = "\n".join(ed["replacement"])
            continue
        if any(n in merged and merged[n] != v for n, v in ops.items()):
            skipped.append(ed)
            continue
        merged.update(ops)
    out = []
    for n, line in enumerate(original, 1):
        if n not in merged:
            out.append(line)
        elif merged[n] is not None:
            out.extend(merged[n].split("\n"))
    return out, skipped


def file_patch(path: str, text: str, new_lines: list[str]) -> str:
    """The unified diff of one file (a/ and b/ prefixes, as git apply expects)."""
    ends_nl = text.endswith("\n")
    old_lines = text.split("\n") if text else []           # an empty file has no lines (ROB-property-12)
    if ends_nl:
        old_lines.pop()
    a = [ln + "\n" for ln in old_lines]
    b = [ln + "\n" for ln in new_lines]
    if not ends_nl:
        if a:
            a[-1] = a[-1][:-1]
        if b:
            b[-1] = b[-1][:-1]
    out = []
    qa, qb = quote_path("a/" + path), quote_path("b/" + path)   # git's C quoting (ROB-property-13)
    for line in difflib.unified_diff(a, b, qa, qb, n=3):
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    if not out:
        return ""
    return "diff --git %s %s\n" % (qa, qb) + "".join(out)


def patch_for(source, findings: list[dict]) -> tuple[str, list[dict]]:
    """(one patch over every finding with an edit, the findings whose edit was not applied)."""
    by_path: dict[str, list[dict]] = {}
    for f in findings:
        if f.get("edit"):
            by_path.setdefault(f["path"], []).append(f)
    parts, skipped = [], []
    texts = source.read_many(sorted(by_path))
    for path in sorted(by_path):
        raw = texts.get(path)
        if raw is None:
            skipped += by_path[path]
            continue
        text = raw.decode("utf-8", "replace")
        if "\r\n" in text:
            skipped += by_path[path]                     # CRLF files: a person edits those by hand
            continue
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        edits = [f["edit"] for f in by_path[path]]
        new, bad = combine(lines, edits)
        skipped += [f for f in by_path[path] if f["edit"] in bad]
        parts.append(file_patch(path, text, new))
    return "".join(p for p in parts if p), skipped
