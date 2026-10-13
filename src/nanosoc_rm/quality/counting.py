"""Q1: comment density per file, directory, language and repository, with outliers against thresholds.

Ratio = (comment + docstring lines) / (code + comment + docstring lines); blank lines never count. A line with
code and a trailing comment is a code line (its comment is counted in `inline`). Markdown is documentation: it
is reported in `docs` (prose, code-block, blank lines and words), never in the code ratios.
"""
from __future__ import annotations

import re

from nanosoc_rm.quality.corpus import Corpus
from nanosoc_rm.quality.lexers import BLANK, CODE, COMMENT, DOC, PROSE

__all__ = ["density", "file_row", "ratio", "DIR_DEPTH"]

DIR_DEPTH = 2          # directories are aggregated to this depth (".": files at the repository root)


def ratio(code: int, comment: int, doc: int) -> float:
    total = code + comment + doc
    return round((comment + doc) / total, 4) if total else 0.0


def file_row(d, settings) -> dict:
    k = d.lexed.kinds
    code, comment, doc, blank = k.count(CODE), k.count(COMMENT), k.count(DOC), k.count(BLANK)
    inline = sum(1 for c in d.lexed.comments if c.inline)
    t = settings.threshold(d.language)
    r = ratio(code, comment, doc)
    nonblank = code + comment + doc
    outlier = r > t["max_ratio"] and nonblank >= t["min_lines"]
    return {"path": d.path, "language": d.language, "code": code, "comment": comment, "docstring": doc,
            "blank": blank, "inline": inline, "ratio": r, "max_ratio": t["max_ratio"], "outlier": outlier,
            "excess_lines": _excess(code, comment + doc, t["max_ratio"]) if outlier else 0}


def _excess(code: int, comments: int, max_ratio: float) -> int:
    """Comment lines above the threshold: how many to remove for the file to meet max_ratio."""
    if max_ratio >= 1:
        return 0
    allowed = int(max_ratio * code / (1 - max_ratio))
    return max(0, comments - allowed)


def _sum(rows: list[dict]) -> dict:
    out = {"files": len(rows), "code": 0, "comment": 0, "docstring": 0, "blank": 0, "inline": 0}
    for r in rows:
        for k in ("code", "comment", "docstring", "blank", "inline"):
            out[k] += r[k]
    out["ratio"] = ratio(out["code"], out["comment"], out["docstring"])
    return out


def _dir_of(path: str, depth: int) -> str:
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else "."


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", text))


def density(corpus: Corpus) -> dict:
    """The density fact (observation kind quality_density, without the envelope)."""
    rows = [file_row(d, corpus.settings) for d in corpus.code_docs()]
    langs = []
    for lang in sorted({r["language"] for r in rows}):
        lr = [r for r in rows if r["language"] == lang]
        s = _sum(lr)
        t = corpus.settings.threshold(lang)
        s.update(language=lang, max_ratio=t["max_ratio"], min_lines=t["min_lines"],
                 outliers=sum(1 for r in lr if r["outlier"]), over=s["ratio"] > t["max_ratio"])
        langs.append(s)
    dirs = []
    keys = set()
    for r in rows:
        parts = r["path"].split("/")[:-1]
        if not parts:
            keys.add(".")
        for i in range(1, min(DIR_DEPTH, len(parts)) + 1):
            keys.add("/".join(parts[:i]))
    for key in sorted(keys):
        if key == ".":
            dr = [r for r in rows if "/" not in r["path"]]
        else:
            dr = [r for r in rows if r["path"].startswith(key + "/")]
        s = _sum(dr)
        s["dir"] = key
        dirs.append(s)
    md = corpus.markdown_docs()
    docs = {"files": len(md), "prose": 0, "code_block": 0, "blank": 0, "comment": 0, "words": 0}
    for d in md:
        k = d.lexed.kinds
        docs["prose"] += k.count(PROSE)
        docs["code_block"] += k.count(CODE)
        docs["blank"] += k.count(BLANK)
        docs["comment"] += k.count(COMMENT)
        docs["words"] += sum(_words(ln) for ln, kind in zip(d.lines, k) if kind == PROSE)
    outliers = sorted((r for r in rows if r["outlier"]), key=lambda r: (-r["excess_lines"], -r["ratio"], r["path"]))
    return {"totals": _sum(rows), "languages": langs, "dirs": dirs, "files": rows,
            "outliers": [r["path"] for r in outliers], "docs": docs, "skipped": dict(corpus.skipped),
            "thresholds": {"origin": corpus.settings.origin, "defaults": corpus.settings["defaults"],
                           "languages": corpus.settings["languages"]},
            "fallback_lexed": sorted(d.path for d in corpus.docs if d.lexed.fallback)}
