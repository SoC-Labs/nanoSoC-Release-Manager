"""Q4 optional: a shorter rewrite of each wordy Markdown section, proposed by Claude, emitted as a patch.

Only when an Anthropic API key is configured ($ANTHROPIC_API_KEY, or `[quality] api_key_file`). Without one,
`rewrite_sections` returns no proposals and makes NO network call: the HTTP client is constructed only after a
key is found (tests/unit/test_quality_*.py proves it by refusing every socket).

The call is `POST v1/messages` on the Messages API: a POST that writes nothing (WRITE_CALLS effect `read-post`,
guarded by the key check). Each section is sent with a strict instruction: shorten without losing facts, and
keep every command, path, code span, link and code block verbatim. A rewrite that drops any backticked token,
link target or code block, or is not shorter, is discarded and reported, never proposed.

Note for whoever enables it: the rewrite sends the section's text to the Anthropic API.
"""
from __future__ import annotations

import re

import httpx

from nanosoc_rm.quality import prose
from nanosoc_rm.quality.patch import combine, file_patch
from nanosoc_rm.quality.settings import api_key, model as configured_model

__all__ = ["rewrite_sections", "preserved_tokens", "SYSTEM_PROMPT", "API_BASE", "MIN_SCORE"]

API_BASE = "https://api.anthropic.com/"
MIN_SCORE = 80.0               # sections scoring at or above this are concise enough: not sent
MAX_SECTIONS = 40              # per run, so a large repository cannot run up a large bill unnoticed

SYSTEM_PROMPT = (
    "You edit technical Markdown for concision. Rewrite the section you are given so it is shorter and plainer "
    "without losing any fact, number, condition, warning or step. Rules: keep the heading line exactly as it is; "
    "keep every inline code span (text in backticks), every command, path, URL, link target and fenced code "
    "block exactly as written; keep lists as lists and tables as tables; prefer active voice and short "
    "sentences; drop filler and hedging words; do not add anything new. Reply with the rewritten Markdown only, "
    "no preface and no code fence around it.")


def preserved_tokens(text: str) -> list[str]:
    """What a rewrite must keep verbatim: inline code spans, link targets and fenced code blocks."""
    toks = re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text)
    toks += re.findall(r"\]\(([^)\s]+)", text)
    toks += [m.group(0) for m in re.finditer(r"(?ms)^(```|~~~).*?^\1\s*$", text)]
    return toks


def _ask(client: httpx.Client, key: str, model: str, section: str) -> tuple[str | None, str]:
    body = {"model": model, "max_tokens": 16000, "system": SYSTEM_PROMPT, "fallbacks": "default",
            "messages": [{"role": "user", "content": section}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "anthropic-beta": "server-side-fallback-2026-07-01", "content-type": "application/json"}
    try:
        r = client.post("v1/messages", json=body, headers=headers)       # WRITE_CALLS: read-post
    except httpx.HTTPError as e:
        return None, "request failed: %s" % type(e).__name__
    if r.status_code != 200:
        return None, "HTTP %d" % r.status_code
    doc = r.json()
    if doc.get("stop_reason") == "refusal":
        return None, "declined (refusal)"
    if doc.get("stop_reason") == "max_tokens":
        return None, "truncated (max_tokens)"
    text = "".join(b.get("text", "") for b in doc.get("content", []) if b.get("type") == "text").strip()
    return (text or None), ("ok" if text else "empty reply")


def rewrite_sections(source, score_fact: dict, *, config=None, client: httpx.Client | None = None,
                     min_score: float = MIN_SCORE, max_sections: int = MAX_SECTIONS) -> dict:
    """{enabled, model, proposals: [{path, title, line, end, before_words, after_words}], rejected: [...],
    patch}. With no key: enabled False and nothing else happens."""
    key = api_key(config)
    if not key:
        return {"enabled": False, "reason": "no API key ($ANTHROPIC_API_KEY or [quality] api_key_file)",
                "proposals": [], "rejected": [], "patch": ""}
    mdl = configured_model(config)
    todo = []
    for f in score_fact["files"]:
        for s in f.get("sections", []):
            if s["score"] < min_score:
                todo.append((f["path"], s))
    todo.sort(key=lambda t: t[1]["score"])
    todo = todo[:max_sections]
    own = client is None
    client = client or httpx.Client(base_url=API_BASE, timeout=120.0)
    proposals, rejected, edits_by_path = [], [], {}
    try:
        for path, s in todo:
            text = source.text(path) or ""
            lines = text.split("\n")
            if text.endswith("\n"):
                lines.pop()
            start, end = s["line"], min(s["end"], len(lines))
            while end > start and not lines[end - 1].strip():
                end -= 1
            original = "\n".join(lines[start - 1:end])
            new, why = _ask(client, key, mdl, original)
            row = {"path": path, "title": s["title"], "line": start, "end": end,
                   "before_words": prose.score_text(original)["words"]}
            if new is None:
                rejected.append(dict(row, reason=why))
                continue
            missing = [t for t in preserved_tokens(original) if t not in new]
            after = prose.score_text(new)["words"]
            if missing:
                rejected.append(dict(row, reason="dropped %d verbatim token(s): %s" % (len(missing), missing[:3])))
                continue
            if after >= row["before_words"]:
                rejected.append(dict(row, reason="not shorter"))
                continue
            proposals.append(dict(row, after_words=after))
            edits_by_path.setdefault(path, []).append({"start": start, "end": end, "replacement": new.split("\n")})
    finally:
        if own:
            client.close()
    parts = []
    for path, edits in sorted(edits_by_path.items()):
        text = source.text(path) or ""
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        new_lines, _ = combine(lines, edits)
        parts.append(file_patch(path, text, new_lines))
    return {"enabled": True, "model": mdl, "proposals": proposals, "rejected": rejected, "patch": "".join(parts)}
