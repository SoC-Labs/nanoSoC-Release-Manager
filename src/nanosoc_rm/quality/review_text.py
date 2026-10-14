"""Real text for a comment finding, read only from what this host already holds (Wave C spec 4.5 and build-first 5).

The component mirrors are blob-less partial clones (`fetch --filter=blob:none`): asking git for a blob it does not
hold (`cat-file -e`, `cat-file --batch`, `ls-tree -l`) makes git fetch it from the promisor remote, which a page
request must never do. So this module reads a blob only after proving it is local:

- `git ls-tree -r -z <commit>` lists the tree (trees and commits are always local in a blob-less clone);
- `git rev-list --objects --missing=print --no-walk <commit>` prints `?<sha>` for every blob of the commit that is
  NOT local, and never fetches;
- `git cat-file --batch` then reads only the blobs that are local.

    LocalSource(repo, commit, label=)       a quality Source that never fetches; `missing` is the absent blob set
    local_source(config, target, fact)      the Source a stored finding fact was analysed from, local reads only
    excerpt(source, path, start, end, around=3, finding=None) -> {start, lines: [(n, text, flagged)]} | None
    edit_lines(lines, finding) -> [(sign, text)]   the suggested edit as removed ("-") and added ("+") lines
    SEVERITY, severity(finding), rank(findings)    derived severity (the finding has no severity field)
    caution(finding, lines=None) -> str | None     why a person should check before accepting

An optional `context: {start, lines}` in a finding (CR-W4, written at analysis time) is used first; then no git
call is made at all.

Severity (CR-WD-Q2): one level per finding kind, written into each finding at analysis time (`severity`,
quality.findings.SEVERITY_BY_KIND); a finding stored before that field existed gets the same level from its kind.

    High    stale_todo
    Medium  commented_out_code; a README row whose status is missing
    Check   unknown_identifier (a close name is a guess); any other README row
    Low     restates_code, banner, boilerplate

Order (CR-WD-Q3): findings that have a suggested edit first (a person can accept those in one step), then
High, Medium, Check, Low, then path and line.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from nanosoc_rm.gitrun import run_git
from nanosoc_rm.quality.source import FileEntry, Source, SourceError, WorktreeSource

__all__ = ["LocalSource", "local_source", "excerpt", "edit_lines", "SEVERITY", "severity", "rank", "rank_key",
           "caution", "NOT_LOCAL"]

GIT_TIMEOUT_S = 60
SEVERITY = ("High", "Medium", "Check", "Low")
EDIT_KINDS = ("restates_code", "boilerplate", "banner", "commented_out_code")
STALE_HIGH_DAYS = 180
NOT_LOCAL = "Excerpt not on this host yet"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_CACHE: dict[tuple[str, str], tuple[float, "LocalSource"]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 30.0
_CACHE_MAX = 6


class LocalSource(Source):
    """One commit of a git repository, read without ever fetching a blob (module docstring)."""

    def __init__(self, repo: str | Path, commit: str, *, label: str):
        super().__init__(label=label, commit=commit, ref=None, worktree=False)
        self.repo = Path(repo)
        if not _SHA.match(commit or ""):
            raise SourceError("not a full commit id: %r" % (commit or "")[:60])
        r = run_git("--no-optional-locks", "ls-tree", "-r", "-z", "--full-tree", commit, cwd=self.repo,
                    timeout=GIT_TIMEOUT_S)
        self.sources.append(r.source())
        if not r.ok:
            raise SourceError("cannot list %s at %s" % (label, commit[:12]))
        self._blob: dict[str, str] = {}
        for rec in str(r.stdout).split("\0"):
            if not rec:
                continue
            meta, _, path = rec.partition("\t")
            parts = meta.split()
            if len(parts) < 3:
                continue
            if parts[1] == "commit":
                self._gitlinks.add(path)
            elif parts[1] == "blob":
                self._files[path] = FileEntry(path, parts[0], 0)
                self._blob[path] = parts[2]
        m = run_git("--no-optional-locks", "rev-list", "--objects", "--missing=print", "--no-walk", commit,
                    cwd=self.repo, timeout=GIT_TIMEOUT_S)
        self.sources.append(m.source())
        if not m.ok:
            raise SourceError("cannot tell which blobs of %s are on this host" % label)
        self.missing = {ln[1:].split()[0] for ln in str(m.stdout).splitlines() if ln.startswith("?")}

    def local(self, path: str) -> bool:
        sha = self._blob.get(path)
        return sha is not None and sha not in self.missing

    def _read_many(self, paths: list[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {p: None for p in paths}
        known = [(p, self._blob[p]) for p in paths if self.local(p) and self._files[p].mode != "120000"]
        if not known:
            return out
        r = run_git("--no-optional-locks", "cat-file", "--batch", cwd=self.repo, timeout=GIT_TIMEOUT_S,
                    input="".join("%s\n" % sha for _, sha in known).encode(), text=False)
        if not r.ok:
            return out
        data: bytes = r.stdout  # type: ignore[assignment]
        pos = 0
        for path, _sha in known:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].split()
            pos = nl + 1
            if len(header) < 3 or header[1] == b"missing":
                continue
            size = int(header[2])
            out[path] = data[pos:pos + size]
            pos += size + 1
        return out


def _cached(repo: Path, commit: str, label: str) -> LocalSource:
    key = (str(repo), commit)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    src = LocalSource(repo, commit, label=label)
    with _CACHE_LOCK:
        _CACHE[key] = (now, src)
        for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:-_CACHE_MAX]:
            _CACHE.pop(k, None)
    return src


def local_source(config: Any, target: str, fact: dict) -> Source:
    """The snapshot a stored finding fact (quality_finding, readme_check) was analysed from, read locally only:
    a `[quality] paths` work tree as it is on disk, or a local repo or a component mirror at `fact.commit`.
    SourceError when it cannot be opened (no mirror, unknown commit)."""
    from nanosoc_rm.quality.settings import quality_config
    named = (quality_config(config).get("paths") or {}) if config is not None else {}
    commit = fact.get("commit")
    if target in named:
        root = Path(named[target]).expanduser()
        if fact.get("worktree"):
            return WorktreeSource(root, label=target)
        if not commit:
            raise SourceError("%s was analysed without a commit" % target)
        return _cached(root, commit, target)
    if not commit:
        raise SourceError("%s was analysed without a commit" % target)
    from nanosoc_rm.analysis import _inputs as inp
    mirror = inp.mirror_path(config, target)
    if not (mirror / "HEAD").is_file():
        raise SourceError("component %s has no mirror on this host" % target)
    return _cached(mirror, commit, target)


def _lines_of(source: Source, path: str) -> list[str] | None:
    try:
        text = source.text(path)
    except (SourceError, OSError):
        return None
    if text is None:
        return None
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def excerpt(source: Source | None, path: str, start: int, end: int, around: int = 3,
            finding: dict | None = None) -> dict | None:
    """Lines start-around .. end+around of `path`, each (number, text, flagged). The finding's stored `context`
    (CR-W4) first; else the local blob; None when neither is here (the caller says NOT_LOCAL)."""
    ctx = (finding or {}).get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("lines"), list) and isinstance(ctx.get("start"), int):
        first = ctx["start"]
        return {"start": first, "lines": [(first + i, str(t), start <= first + i <= end)
                                          for i, t in enumerate(ctx["lines"])], "all": None}
    if source is None:
        return None
    lines = _lines_of(source, path)
    if lines is None:
        return None
    lo, hi = max(1, start - around), min(len(lines), end + around)
    return {"start": lo, "lines": [(n, lines[n - 1], start <= n <= end) for n in range(lo, hi + 1)], "all": lines}


def edit_lines(lines: list[str] | None, finding: dict) -> list[tuple[str, str]]:
    """The suggested edit as text: ("-", original line) for lines edit.start..end, then ("+", replacement line).
    With no file text, the removed lines read "line N" (the old preview). No edit: []."""
    ed = finding.get("edit") or None
    if not ed:
        return []
    out: list[tuple[str, str]] = []
    for n in range(int(ed["start"]), int(ed["end"]) + 1):
        if lines is not None and 1 <= n <= len(lines):
            out.append(("-", lines[n - 1]))
        else:
            out.append(("-", "line %d" % n))
    out += [("+", str(t)) for t in ed.get("replacement") or []]
    return out


def edit_lines_for(source: Source | None, finding: dict) -> tuple[list[tuple[str, str]], bool]:
    """(edit_lines, True when the original text was read). Context lines (CR-W4) count as read."""
    ed = finding.get("edit") or None
    if not ed:
        return [], False
    ctx = finding.get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("lines"), list) and isinstance(ctx.get("start"), int):
        first, cl = ctx["start"], ctx["lines"]
        if first <= ed["start"] and ed["end"] < first + len(cl):
            padded = [""] * (first - 1) + [str(t) for t in cl]
            return edit_lines(padded, finding), True
    lines = _lines_of(source, finding["path"]) if source is not None else None
    return edit_lines(lines, finding), lines is not None


# ---------------------------------------------------------------------------------------------------------
# Severity, order and caution
# ---------------------------------------------------------------------------------------------------------
def severity(f: dict) -> str:
    """The severity of one finding (its `severity`, else its kind's level) or one readme_check row (which has
    `readme` and `status`)."""
    from nanosoc_rm.quality.findings import SEVERITY_BY_KIND
    if "readme" in f and "status" in f:
        return "Medium" if f.get("status") == "missing" else "Check"
    if f.get("severity") in SEVERITY:
        return f["severity"]
    return SEVERITY_BY_KIND.get(str(f.get("kind")), "Check")


def rank_key(f: dict) -> tuple:
    return (0 if f.get("edit") else 1, SEVERITY.index(severity(f)), f.get("path") or f.get("readme") or "",
            int(f.get("line") or 0), str(f.get("kind") or ""))


def rank(findings: Iterable[dict]) -> list[dict]:
    """Findings in review order: those with a suggested edit first, then High, Medium, Check, Low, then by path
    and line."""
    return sorted(findings, key=rank_key)


def caution(f: dict, lines: list[str] | None = None) -> str | None:
    """A reason to check before accepting, from rules only (never free text of the finding beyond evidence)."""
    ident, dym = str(f.get("identifier") or ""), f.get("did_you_mean")
    n = int(f.get("line") or 0)
    here = lines[n - 1] if lines is not None and 1 <= n <= len(lines) else str(f.get("evidence") or "")
    nxt = lines[n] if lines is not None and 0 < n < len(lines) else None
    if f.get("kind") == "unknown_identifier" and isinstance(dym, str) and dym:
        if re.search(r"(?<![\w])%s(?![\w])" % re.escape(dym), here):
            return "The suggestion already appears on this line: accepting would list it twice; use Edit."
        if ident.endswith("s") and ident[:-1] == dym:
            return "A plural in prose, probably not a finding."
    if nxt is not None:
        if ident and re.search(r"(?<![\w])%s(?![\w])" % re.escape(ident), nxt):
            return "Line %d has the same pattern; use Edit to fix both." % (n + 1)
        if f.get("kind") == "stale_todo" and re.search(r"\b(TODO|FIXME|XXX|HACK)\b", nxt):
            return "Line %d has the same pattern; use Edit to fix both." % (n + 1)
    return None
