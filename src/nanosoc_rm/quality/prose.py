"""Q4: how concise each Markdown file and section is.

Measured on prose only (code blocks, tables, HTML comments and headings are left out):

- words, sentences, mean sentence length, share of sentences over 25 words;
- filler and hedge phrases per 100 words (FILLER);
- passive voice share (a form of "to be" followed by a past participle);
- sentences repeated inside the file;
- paragraphs duplicated across files: 5-word shingles, Jaccard similarity of at least 0.8 between paragraphs of
  at least 25 words.

Score, 0 to 100, higher is more concise:
    100 - 4 x filler per 100 words - 1.5 x (mean sentence length - 18, if positive) - 30 x long-sentence share
        - 25 x passive share - 5 x repeated sentences - 40 x share of words in duplicated paragraphs
    (clamped to 0..100)
"""
from __future__ import annotations

import re
from collections import Counter

from nanosoc_rm.quality.corpus import Corpus

__all__ = ["score_text", "score_docs", "FILLER", "sections_of", "prose_of"]

FILLER = ("very", "really", "basically", "actually", "simply", "just", "quite", "rather", "somewhat", "perhaps",
          "maybe", "probably", "essentially", "generally", "various", "a number of", "in order to", "it should be noted",
          "note that", "please note", "in fact", "of course", "obviously", "clearly", "it is important to",
          "at this point in time", "due to the fact that", "for the purpose of", "in the event that", "with regard to",
          "in terms of", "it is worth noting", "needless to say", "as a matter of fact", "kind of", "sort of",
          "fairly", "pretty much", "a bit", "a little", "might", "could possibly", "should be able to",
          "is able to", "make sure to", "feel free to", "going forward", "currently", "etc")
_BE = r"(?:is|are|was|were|be|been|being|am)"
_IRREGULAR = ("built|done|made|run|set|put|kept|sent|written|given|taken|shown|known|seen|found|held|read|"
              "left|meant|paid|said|told|thought|brought|bought|caught|taught|chosen|driven|frozen|hidden|"
              "spoken|stolen|broken|forgotten|gotten|begun|drawn|grown|thrown|worn|torn|split|cut|shut|hit")
_PASSIVE = re.compile(r"\b%s\s+(?:\w+ly\s+)?(?:\w+ed|%s)\b" % (_BE, _IRREGULAR), re.I)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")


def prose_of(text: str) -> list[tuple[int, str]]:
    """(line number, prose line) outside code fences, tables, HTML comments and headings."""
    out, fence, html = [], None, False
    for n, line in enumerate(text.split("\n"), 1):
        st = line.strip()
        if fence:
            if st.startswith(fence):
                fence = None
            continue
        m = re.match(r"^(```+|~~~+)", st)
        if m:
            fence = m.group(1)
            continue
        if html:
            html = "-->" not in st
            continue
        if st.startswith("<!--"):
            html = "-->" not in st
            continue
        if not st or st.startswith(("#", "|", "<", "[![")):
            out.append((n, ""))
            continue
        out.append((n, re.sub(r"`[^`]*`", "CODE", line)))
    return out


def _paragraph_lines(prose: list[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    paras, cur, start = [], [], 0
    for n, line in prose:
        if not line.strip() or re.match(r"^\s*([-*+]|\d+[.)])\s", line):
            if cur:
                paras.append((start, cur))
            cur = []
            if line.strip():
                start, cur = n, [re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line).strip()]
            continue
        if not cur:
            start = n
        cur.append(line.strip())
    if cur:
        paras.append((start, cur))
    return paras


def _paragraphs(prose: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(n, " ".join(lines)) for n, lines in _paragraph_lines(prose)]


def _sentences(lines: list[str]) -> list[str]:
    """Sentences of one paragraph. A multi-line paragraph with almost no sentence punctuation is a list of
    lines (pin tables, wiring notes) and each line counts as one sentence."""
    text = " ".join(lines).strip()
    words = len(_WORD.findall(text))
    stops = len(re.findall(r"[.!?](\s|$)", text))
    if len(lines) > 1 and words / max(1, stops) > 40:
        return [ln for ln in lines if _WORD.search(ln)]
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9`(\"])", text)
    return [p.strip() for p in parts if _WORD.search(p)]


def _filler_count(text: str) -> tuple[int, Counter]:
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    c: Counter = Counter()
    for f in FILLER:
        k = len(re.findall(r"(?<![\w-])%s(?![\w-])" % re.escape(f), low))
        if k:
            c[f] = k
    return sum(c.values()), c


def score_text(text: str) -> dict:
    """The metrics and score of one piece of Markdown (a file or a section)."""
    prose = prose_of(text)
    sents = [s for _, lines in _paragraph_lines(prose) for s in _sentences(lines)]
    words = sum(len(_WORD.findall(s)) for s in sents)
    lens = [len(_WORD.findall(s)) for s in sents]
    mean = round(sum(lens) / len(lens), 1) if lens else 0.0
    long_share = round(sum(1 for x in lens if x > 25) / len(lens), 3) if lens else 0.0
    filler, which = _filler_count(" ".join(sents))
    passive = sum(1 for s in sents if _PASSIVE.search(s))
    passive_share = round(passive / len(sents), 3) if sents else 0.0
    norm = Counter(re.sub(r"\W+", " ", s.lower()).strip() for s in sents if len(_WORD.findall(s)) >= 5)
    repeated = sum(v - 1 for v in norm.values() if v > 1)
    per100 = round(100.0 * filler / words, 2) if words else 0.0
    return {"words": words, "sentences": len(sents), "mean_sentence": mean, "long_share": long_share,
            "filler": filler, "filler_per_100": per100, "filler_words": dict(which.most_common(8)),
            "passive": passive, "passive_share": passive_share, "repeated": repeated, "duplicated": 0,
            "duplicated_share": 0.0,
            "score": _score(per100, mean, long_share, passive_share, repeated, 0)}


def _score(per100: float, mean: float, long_share: float, passive_share: float, repeated: int,
           duplicated_share: float) -> float:
    s = 100 - 4 * per100 - 1.5 * max(0.0, mean - 18) - 30 * long_share - 25 * passive_share - 5 * repeated \
        - 40 * duplicated_share
    return round(max(0.0, min(100.0, s)), 1)


def sections_of(text: str) -> list[dict]:
    """[{title, level, line, end, text}] of each heading's own text (up to the next heading of any level)."""
    lines = text.split("\n")
    out, fence = [], None
    cur = {"title": "(top)", "level": 0, "line": 1}
    for i, line in enumerate(lines):
        st = line.strip()
        m = re.match(r"^(```+|~~~+)", st)
        if fence:
            if st.startswith(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            cur["end"] = i
            out.append(cur)
            cur = {"title": h.group(2).strip(), "level": len(h.group(1)), "line": i + 1}
    cur["end"] = len(lines)
    out.append(cur)
    for s in out:
        s["text"] = "\n".join(lines[s["line"] - 1:s["end"]])
    return [s for s in out if s["text"].strip()]


def _shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    w = [x.lower() for x in _WORD.findall(text)]
    return {tuple(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def score_docs(corpus: Corpus) -> dict:
    """The doc_score fact (without the envelope): per file and section, duplicates across files, ranking."""
    files, paras_all = [], []
    for d in corpus.markdown_docs():
        m = score_text(d.text)
        secs = []
        for s in sections_of(d.text):
            sm = score_text(s["text"])
            if sm["words"] >= 20:
                secs.append({"title": s["title"], "line": s["line"], "end": s["end"], **sm})
        files.append({"path": d.path, **m, "sections": secs})
        for start, p in _paragraphs(prose_of(d.text)):
            if len(_WORD.findall(p)) >= 25:
                paras_all.append((d.path, start, p, _shingles(p)))
    dups = []
    by_shingle: dict = {}
    for i, (_, _, _, sh) in enumerate(paras_all):
        for x in sh:
            by_shingle.setdefault(x, []).append(i)
    pairs = set()
    for ids in by_shingle.values():
        if 1 < len(ids) <= 50:                       # a shingle in 50+ paragraphs is boilerplate, not a pairing
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    pairs.add((ids[x], ids[y]))
    for i, k in sorted(pairs):
        a, b = paras_all[i], paras_all[k]
        if a[0] == b[0] or not a[3] or not b[3]:
            continue
        j = len(a[3] & b[3]) / len(a[3] | b[3])
        if j >= 0.8:
            dups.append({"a": "%s:%d" % (a[0], a[1]), "b": "%s:%d" % (b[0], b[1]), "similarity": round(j, 3),
                         "excerpt": a[2][:140]})
    dup_paras: dict[str, set[int]] = {}
    for dp in dups:
        for side in (dp["a"], dp["b"]):
            path, line = side.rsplit(":", 1)
            dup_paras.setdefault(path, set()).add(int(line))
    words_at = {(p, n): len(_WORD.findall(t)) for p, n, t, _ in paras_all}
    for f in files:
        lines = dup_paras.get(f["path"], set())
        f["duplicated"] = len(lines)
        f["duplicated_share"] = round(min(1.0, sum(words_at.get((f["path"], n), 0) for n in lines)
                                          / f["words"]), 3) if f["words"] else 0.0
        f["score"] = _score(f["filler_per_100"], f["mean_sentence"], f["long_share"], f["passive_share"],
                            f["repeated"], f["duplicated_share"])
    ranked = sorted(files, key=lambda f: (f["score"], -f["words"], f["path"]))
    words = sum(f["words"] for f in files)
    overall = round(sum(f["score"] * f["words"] for f in files) / words, 1) if words else 100.0
    return {"score": overall, "files": ranked, "duplicates": dups, "words": words}
