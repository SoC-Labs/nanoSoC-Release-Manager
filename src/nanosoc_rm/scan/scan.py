"""The bundle and tree scanner (interfaces.md section 11; D11, D2).

The method is the toolkit's `check-vendor-collateral.sh`:

1. Arm every rule on its invented specimen (and its counter-specimens) BEFORE scanning. A rule that is not armed
   makes the scan `unverified`: a rule that cannot fire proves nothing by staying quiet.
2. Census the corpus: zero files scanned is `unverified`, never `clean`.
3. Allowlist entries each carry a reason and an owner (rules.py).

Findings are LOCATIONS ONLY: `{rule, file, line}`. The matched text is never returned, logged or stored,
because the log of a scan is itself a copy. A file that cannot be scanned (unreadable, a symlink in a bundle,
larger than MAX_FILE_BYTES) is a finding of rule `scan.unscannable` with line null: the caller deletes it like
any flagged file.

Two engines, one result:

- this package's own (`arm.banner`, plus any private regex rule file; nanosoc_rm.scan.rules), which reads a
  directory (`scan_dir`, a bundle), files in place (`scan_paths`, read-only), and a commit's tree in a local repo
  (`scan_tree`, the T0 gate `t0.vendor.no-arm-ip`), through the one git runner;
- the toolkit's `check-vendor-collateral.sh` (the lab's rule set, private: GATE2 Q4), when a pinned copy is named
  by `toolkit=` or $RM_SCAN_TOOLKIT (nanosoc_rm.scan.toolkit). It runs on a throwaway git repo over a directory
  and on a fetched tree in place, and its findings are merged in. It is armed as `armed["toolkit"]`; when it
  cannot vouch for the corpus the scan is `unverified`. `scan_paths` (the lab-only realism check of one vendor
  file, read in place) uses this package's rules only, so no copy of that file is ever made.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal

from nanosoc_rm.gitrun import run_git
from nanosoc_rm.scan.rules import RuleSet, RulesError, arm, rule_set
from nanosoc_rm.scan.toolkit import ARMED_KEY, Toolkit, ToolkitRun, run_toolkit, scan_dir_toolkit, toolkit

__all__ = ["ScanResult", "scan_dir", "scan_paths", "scan_tree", "scan_texts", "MAX_FILE_BYTES", "UNSCANNABLE"]

MAX_FILE_BYTES = 64 * 1024 * 1024
UNSCANNABLE = "scan.unscannable"


@dataclass
class ScanResult:
    verdict: Literal["clean", "finding", "unverified"]
    findings: list[dict]                      # [{rule, file, line}], locations only
    files_scanned: int
    armed: dict[str, bool]                    # rule id -> fired on its specimen (and not on its near misses)
    tool: str = "nanosoc-rm scan"
    why: str = ""                             # why the verdict is unverified, when it is
    flagged_files: list[str] = field(default_factory=list)   # distinct files with a finding, in order

    def as_doc(self) -> dict:
        return {"verdict": self.verdict, "findings": list(self.findings), "files_scanned": self.files_scanned,
                "armed": dict(self.armed), "tool": self.tool, "why": self.why}


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan_one(rules: RuleSet, rel: str, data: bytes) -> list[dict]:
    text = data.decode("latin-1")             # lossless: a byte is a character, so line numbers are exact
    out, seen = [], set()
    for r in rules.rules:
        if rules.allowed(r.id, rel):
            continue
        for off in r.matches(text):
            line = _line_of(text, off)
            if (r.id, line) not in seen:
                seen.add((r.id, line))
                out.append({"rule": r.id, "file": rel, "line": line})
    return out


def scan_texts(items: Iterable[tuple[str, bytes | None]], rules: RuleSet | None = None) -> ScanResult:
    """The engine. `items` yields (relative path, content); content None means the file could not be read
    (a finding of rule scan.unscannable). Every rule is armed first; the census is the number of items."""
    rules = rules or rule_set()
    armed = arm(rules.rules)
    findings, n = [], 0
    for rel, data in items:
        n += 1
        if data is None:
            findings.append({"rule": UNSCANNABLE, "file": rel, "line": None})
            continue
        findings.extend(_scan_one(rules, rel, data))
    flagged = list(dict.fromkeys(f["file"] for f in findings))
    if not all(armed.values()):
        verdict, why = "unverified", "rule(s) not armed on their specimen: %s" % ", ".join(
            sorted(k for k, v in armed.items() if not v))
    elif findings:
        verdict, why = "finding", ""
    elif n == 0:
        verdict, why = "unverified", "nothing to scan: 0 files"
    else:
        verdict, why = "clean", ""
    return ScanResult(verdict, findings, n, armed, rules.describe(), why, flagged)


_FROM_ENV = object()       # toolkit= default: the copy $RM_SCAN_TOOLKIT names, if any


def _toolkit_or_why(tk) -> tuple[Toolkit | None, str]:
    """(the toolkit to run, or None; why it cannot be used). `tk`: a Toolkit, a path, None (no toolkit) or
    _FROM_ENV."""
    if isinstance(tk, Toolkit):
        return tk, ""
    try:
        return toolkit(None if tk is _FROM_ENV else tk) if tk is not None else None, ""
    except RulesError as e:
        return None, str(e)


def _merge(res: ScanResult, run: ToolkitRun | None, tk: Toolkit | None, broken: str = "") -> ScanResult:
    """The native result with the toolkit's findings and arming merged in (README of this module)."""
    if run is None and not broken:
        return res
    armed = dict(res.armed)
    armed[ARMED_KEY] = bool(run and run.armed)
    findings = list(res.findings) + (list(run.findings) if run else [])
    flagged = list(dict.fromkeys(f["file"] for f in findings))
    tool = res.tool + (" + " + tk.describe() if tk else " + toolkit (unusable)")
    if res.verdict == "unverified":
        verdict, why = "unverified", res.why
    elif broken or (run and run.why):
        verdict, why = "unverified", broken or run.why
    elif findings:
        verdict, why = "finding", ""
    else:
        verdict, why = "clean", ""
    return ScanResult(verdict, findings, res.files_scanned, armed, tool, why, flagged)


def _read(p: Path) -> bytes | None:
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
        return p.read_bytes()
    except OSError:
        return None


def _dir_items(root: Path) -> Iterator[tuple[str, bytes | None]]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        d = Path(dirpath)
        for name in sorted(filenames) + sorted(x for x in dirnames if (d / x).is_symlink()):
            p = d / name
            yield p.relative_to(root).as_posix(), _read(p)


def scan_dir(path: str | os.PathLike, rules: RuleSet | None = None, toolkit=_FROM_ENV) -> ScanResult:
    """Scan every file under `path` (a bundle directory). Symlinks are never followed: each is unscannable.
    `toolkit`: the toolkit scanner to run as well (a Toolkit or a path; default $RM_SCAN_TOOLKIT; None: none)."""
    root = Path(path)
    if not root.is_dir():
        return ScanResult("unverified", [], 0, arm((rules or rule_set()).rules), (rules or rule_set()).describe(),
                          "not a directory: %s" % root)
    res = scan_texts(_dir_items(root), rules)
    tk, broken = _toolkit_or_why(toolkit)
    if broken or (tk is not None and res.files_scanned):
        return _merge(res, scan_dir_toolkit(tk, root) if tk is not None else None, tk, broken)
    return res


def scan_paths(paths: list[str | os.PathLike], rules: RuleSet | None = None) -> ScanResult:
    """Scan files in place, read-only (the lab-only realism check reads one real vendor file this way).
    Each finding's `file` is the path as given. Nothing is copied or written."""
    return scan_texts(((str(p), _read(Path(p))) for p in paths), rules)


def _tree_items(repo: str | os.PathLike, commit: str, prefix: str, env: dict | None,
                isolate: bool) -> Iterator[tuple[str, bytes | None]]:
    ls = run_git("ls-tree", "-r", "-l", "-z", "--full-tree", commit, cwd=repo, env=env, isolate=isolate,
                 timeout=300).check()
    batch: list[tuple[str, str]] = []
    size = 0

    def flush():
        if not batch:
            return
        payload = ("\n".join(sha for _rel, sha in batch) + "\n").encode()
        out = run_git("cat-file", "--batch", cwd=repo, env=env, isolate=isolate, input=payload, text=False,
                      timeout=600).check().stdout
        pos = 0
        for rel, sha in batch:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode().split()
            if len(header) < 3 or header[1] == "missing":
                pos = nl + 1
                yield rel, None
                continue
            n = int(header[2])
            yield rel, out[nl + 1:nl + 1 + n]
            pos = nl + 1 + n + 1
        batch.clear()

    for entry in ls.stdout.split("\0"):
        if not entry:
            continue
        meta, rel = entry.split("\t", 1)
        mode, kind, sha, bsize = meta.split()
        if kind != "blob":
            continue                              # gitlinks are other trees, scanned on their own
        rel = (prefix + "/" + rel) if prefix else rel
        if bsize.isdigit() and int(bsize) > MAX_FILE_BYTES:
            yield rel, None
            continue
        batch.append((rel, sha))
        size += int(bsize) if bsize.isdigit() else 0
        if len(batch) >= 500 or size >= MAX_FILE_BYTES:
            yield from flush()
            size = 0
    yield from flush()


def scan_tree(repo: str | os.PathLike, commit: str, rules: RuleSet | None = None, prefix: str = "",
              env: dict | None = None, isolate: bool = True, toolkit=_FROM_ENV) -> ScanResult:
    """Scan the tree of `commit` in local repo `repo` (blobs must be present: a depth-1 fetch without a filter;
    the toolkit scanner also needs a non-bare repo). Finding paths are `prefix/<path in the tree>`. Reads
    objects only; writes nothing in `repo`."""
    res = scan_texts(_tree_items(repo, commit, prefix, env, isolate), rules)
    tk, broken = _toolkit_or_why(toolkit)
    if broken or (tk is not None and res.files_scanned):
        return _merge(res, run_toolkit(tk, repo, commit, prefix) if tk is not None else None, tk, broken)
    return res
