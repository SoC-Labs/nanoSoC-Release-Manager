"""Plain words for the codes the GUI used to print. A page prints the word; the code stays in a `title` tooltip, so a
reader who knows the spec can still find it, and a reader who does not is never shown it as the only text.

    WORDS                     {code: plain words}: host pin states, test tiers, requirement IDs
    word(code) -> str         the plain words of one code (the code itself when unknown)
    plain(text) -> Markup     a Jinja filter: every known code in `text` becomes its words, each in a
                              <span title="CODE"> (the text is escaped first; unknown words are kept)

The filter runs on chip text (`_ui.html` chip), so "github* ON_DEFAULT" reads "github* on default branch" with
the code in the tooltip. A parenthesised requirement ID after a heading ("Tags (C10)") is dropped from the visible text
and kept as an empty `<span class="term-code" data-code="C10">`: `plain("Tags (C10)")` reads "Tags". So is a
change-request id ("(CR-15)"). A later phase reads as what it switches on: "the Phase 4 entry gate" reads "the
bump-bot sign-off", with the phase in the tooltip.
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

__all__ = ["WORDS", "word", "plain", "STATE_WORDS", "TIER_WORDS", "PHASE_WORDS"]

# Per-host pin states (walker/states.py). The chip keeps its tone; only the words change.
STATE_WORDS = {
    "TAGGED": "tagged",
    "ON_DEFAULT": "on default branch",
    "PROTECTED_BY_TAG": "kept by a tag",
    "BRANCH_ONLY": "on a side branch only",
    "REWRITTEN": "history rewritten",
    "PR_ONLY": "in a pull request only",
    "ORPHAN": "on no branch or tag",
    "UNREADABLE": "repo unreadable",
    "LOCAL_URL": "local URL",
    "NO_GITLINK": "no gitlink",
}
# Test tiers (REQUIREMENTS.md "Use these test tiers").
TIER_WORDS = {"T0": "resolve check", "T1": "static and unit tests", "T2": "smoke simulation",
              "T3": "regression", "T4": "FPGA build", "T5": "hardware test", "T6": "ASIC flow"}
# Requirement IDs that appeared as UI text (REQUIREMENTS.md headings).
_REQ_WORDS = {
    "B7": "pinned more than once", "C1": "release tags", "C2": "classed changelog", "C3": "change class",
    "C5": "breaking-change notes", "C7": "build-affecting", "C9": "1.0.0 readiness", "C10": "tag scheme",
    "C11": "manifest", "C12": "API.md", "D3": "compatibility matrix", "D4": "run inputs", "D5": "pinned run trigger",
    "D7": "infrastructure failure", "D8": "re-run and quarantine", "D9": "same-night comparison",
    "D11": "vendor content scan", "D12": "release store", "E2": "shadow mode", "E3": "auto-merge rules",
    "E5": "never on silicon or release branches", "F4": "errata document", "G1": "tool release", "G2": "SoC release",
    "G3": "release lock", "G4": "release train", "G5": "approvals", "G6": "yank", "G7": "generated RTL archive",
}
# A later phase (docs/phase-gates.yaml) by what its sign-off switches on.
PHASE_WORDS = {"Phase 2": "nightly-matrix", "Phase 3": "tool-release", "Phase 4": "bump-bot", "Phase 5": "SoC-release"}
WORDS = {**STATE_WORDS, **TIER_WORDS, **_REQ_WORDS}

_STATE_RE = re.compile(r"\b(%s)\b" % "|".join(sorted(STATE_WORDS, key=len, reverse=True)))
_TIER_RE = re.compile(r"\b(T[0-6])\b")
# " (C10)", " (G1, G5)", " (E3, E5)", " (CR-15)": a parenthesised list of requirement or change-request IDs,
# dropped into the tooltip
_ID = r"(?:[A-HQ]\d{1,2}|PR\d{1,2}|CR-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*)"
_PAREN_RE = re.compile(r"\s*\((%s(?:\s*,\s*%s)*)\)" % (_ID, _ID))
_REQ_RE = re.compile(r"\b(%s)\b(?!-)" % "|".join(sorted(_REQ_WORDS, key=len, reverse=True)))
# "Phase 4 entry gate", "Phase 4 gate", "Phase 4"
_PHASE_RE = re.compile(r"\b(Phase [2-5])(?:( entry)? gate\b)?")


def word(code: str) -> str:
    return WORDS.get(code, code)


def _span(text: str, title: str) -> str:
    return '<span title="%s">%s</span>' % (escape(title), escape(text))


def plain(text) -> Markup:
    """`text` with each known code replaced by its words (code in a tooltip). Markup passes through as is."""
    if text is None:
        return Markup("")
    if isinstance(text, Markup):
        return text
    s = str(text)
    out: list[str] = []
    pos = 0
    # one pass over the patterns, leftmost match first
    pats = (_PAREN_RE, _STATE_RE, _TIER_RE, _REQ_RE, _PHASE_RE)
    while True:
        best = None
        for p in pats:
            m = p.search(s, pos)
            if m and (best is None or m.start() < best.start() or
                      (m.start() == best.start() and m.end() > best.end())):
                best = m
        if best is None:
            out.append(str(escape(s[pos:])))
            break
        out.append(str(escape(s[pos:best.start()])))
        code = best.group(1)
        if best.re is _PAREN_RE:
            codes = [c.strip() for c in code.split(",")]
            out.append('<span class="term-code" data-code="%s"></span>' % escape(" ".join(codes)))
        elif best.re is _PHASE_RE:
            what = PHASE_WORDS[code]
            out.append(_span("%s sign-off" % what if best.group(0) != code else "%s stage" % what, best.group(0)))
        else:
            out.append(_span(word(code), code))
        pos = best.end()
    return Markup("".join(out))
