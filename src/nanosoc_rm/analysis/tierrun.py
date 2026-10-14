"""Local T1/T2 tier runs inside `nanosoc-rm compat --tier` (CR-12, MVP S3; interfaces.md section 9a "Tiers").

    check_spec(spec, tier, environ) -> None             TierUsage (exit 3) for a command the tool refuses to run
    materialise(git, root, dest, pins=None) -> Layout   the resolved (overridden) tree as a working checkout
    run_tier(spec, tier, tree, work, nonce, ...) -> dict    one compat_tier document (observation $defs)
    skipped(spec, tier, reason) -> dict                 the compat_tier of a tier that did not run (T0 FAIL)

1. **Spec.** The registry's `tiers.yaml` (eligibility#/$defs/tiers_file, loader rule 14) gives, per configuration
   and tier, `command` (argv list or shell string), tree-relative `cwd`, explicit `env`, `budget_s`, `needs`,
   optional `verdicts` (a toolkit-format verdicts.tsv the command writes), `pins` (the pins it needs),
   `regenerates` and `ip_files` (a file list the command writes; removed before the run like `verdicts`). A command that sources a set_env.sh is refused (schema and here); `regenerates: true` is refused
   unless the caller's environment has NANOSOC_GEN_OK=1.
2. **Sandbox tree.** Every pin of the walked tree (the T0 gates' own walk, overrides applied) is checked out at
   its commit at its tree path: a fresh repo per pin borrowing the objects of the T0 run's anonymous fetch
   (alternates; no copy, no network), else one anonymous `fetch --depth=1` of its canonical https URL. A needed
   pin that cannot be laid out makes the tier UNVERIFIED without running.
3. **Run.** A clean environment (PATH=/usr/bin:/bin, HOME and TMPDIR in the sandbox, TREE, NANOSOC_RM_TIER,
   NANOSOC_RM_VERDICTS, then `env`; nothing inherited, so no ARM_IP_LIBRARY_PATH unless declared), its own
   process group, stdin closed, output to a log. Over `budget_s` is `over_budget` (a warning); at budget x 1.5
   the whole process group is killed and the tier is FAIL. `needs: [vcs-seat]` holds a host-local lock for the
   run (busy: INFRA, not run). An absolute path in the tier's own `env` that does not exist on this host (outside
   the sandbox) is a missing tool: INFRA "tool not on this host: VAR=path does not exist", not run.
   Just before the command, `<work>/tree-layout.json` records the laid-out tree paths and each one's tracked changes
   (`worktree_changes`), so the matrix's re-read can tell a removed or rewritten input (interfaces section 18 item 8).
4. **Rows.** `tier.<tier>`: PASS on exit 0, else FAIL. A declared verdicts file adds `tier.<tier>.<gate>` per row
   (status kept); a declared file that is missing or unreadable adds one UNVERIFIED row. Tier verdict: FAIL >
   UNVERIFIED > INFRA > WARN > PASS over the rows.
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Mapping

from nanosoc_rm import clock
from nanosoc_rm.t0 import gates as G
from nanosoc_rm.t0.gitwork import Pin, iter_pins, repo_key, url_kind

__all__ = ["TierUsage", "RUNNABLE", "KILL_FACTOR", "check_spec", "materialise", "Layout", "run_tier", "skipped",
           "tier_verdict", "parse_verdicts", "command_text", "missing_tools", "worktree_changes", "LAYOUT_RECORD",
           "read_layout_record"]

RUNNABLE = ("t1", "t2")
KILL_FACTOR = 1.5
STATUSES = ("PASS", "FAIL", "UNVERIFIED", "WARN", "SKIP", "INFRA")
SOURCES_SET_ENV = re.compile(r"(^|[;&|(`]|\s)(source|\.)\s+[^;&|]*set_env")
RESERVED_ENV = ("TREE", "NANOSOC_RM_TIER", "NANOSOC_RM_VERDICTS", "NANOSOC_GEN_OK")
LOG_TAIL = 20
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_GATE_PART = re.compile(r"[^a-z0-9_.-]+")
LAYOUT_RECORD = "tree-layout.json"  # in the work dir, beside the tree: what was laid out, as it stood before the command


class TierUsage(ValueError):
    """A tier the tool will not run as asked: none declared, a set_env.sh sourced, `regenerates` without
    NANOSOC_GEN_OK=1, an env entry naming an unknown variable. Exit 3."""
    exit_code = 3


def command_text(command) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def check_spec(spec: Mapping | None, tier: str, configuration: str, environ: Mapping[str, str] | None = None) -> dict:
    """The spec, or TierUsage. Checked before any work starts."""
    environ = os.environ if environ is None else environ
    if tier not in RUNNABLE:
        raise TierUsage("--tier %s: only %s run locally" % (tier, " and ".join(RUNNABLE)))
    if not spec:
        raise TierUsage("the registry's tiers.yaml declares no %s command for %s" % (tier, configuration))
    cmd = spec.get("command")
    parts = [cmd] if isinstance(cmd, str) else list(cmd or [])
    if not parts or any(not isinstance(p, str) or not p for p in parts):
        raise TierUsage("%s %s: command must be a non-empty string or argv list" % (configuration, tier))
    if any(SOURCES_SET_ENV.search(p) for p in parts):
        raise TierUsage("%s %s: the command sources a set_env.sh; tier commands never do (declare `env` instead)"
                        % (configuration, tier))
    bad = sorted(k for k in (spec.get("env") or {}) if k in RESERVED_ENV)
    if bad:
        raise TierUsage("%s %s: env may not set %s (the tool sets it)" % (configuration, tier, ", ".join(bad)))
    if spec.get("regenerates") and environ.get("NANOSOC_GEN_OK") != "1":
        raise TierUsage("%s %s regenerates build_soc (regenerates: true): set NANOSOC_GEN_OK=1 to allow it"
                        % (configuration, tier))
    return dict(spec)


# ---------------------------------------------------------------------------------------------------------
# The sandbox tree
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Layout:
    laid_out: list[str] = field(default_factory=list)       # tree paths ("" = the root)
    missing: list[str] = field(default_factory=list)        # needed pins that could not be laid out
    not_laid_out: int = 0                                    # pins left out because `pins` does not need them


def _needed(tree_path: str, pins) -> bool:
    if pins is None:
        return True
    return any(tree_path == n or tree_path.startswith(n + "/") or n.startswith(tree_path + "/") for n in pins)


def _checkout(git, pin: Pin, dest: Path) -> bool:
    """`dest` as a git checkout of pin.commit: borrow the objects of the run's fetch, else fetch the canonical."""
    if not pin.commit:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    if not git.run("init", "-q", dest).ok:
        return False
    f = git.fetch_commit(pin.resolved or pin.url, pin.commit)
    if f.ok and f.repo is not None:
        src = Path(f.repo) / ".git"
        (dest / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (dest / ".git" / "objects" / "info" / "alternates").write_text(str(src / "objects") + "\n")
        if (src / "shallow").is_file():
            (dest / ".git" / "shallow").write_text((src / "shallow").read_text())
        if git.run("checkout", "-q", "--detach", pin.commit, cwd=dest).ok:
            return True
    url = pin.canonical
    if url and url_kind(url) in ("https", "other"):
        if git.run("fetch", "-q", "--no-tags", "--depth=1", url, pin.commit, cwd=dest).ok:
            return git.run("checkout", "-q", "--detach", pin.commit, cwd=dest).ok
    return False


def materialise(git, root: Pin, dest: Path, pins=None) -> Layout:
    """Lay the walked tree out under `dest`: the root at `dest`, each needed pin at its tree path. A pin the walk
    marked duplicate (same repo and commit seen earlier) gets the subtree of its first occurrence."""
    first: dict[tuple, Pin] = {}
    for p in [root] + list(iter_pins(root)):
        if not p.duplicate:
            first.setdefault((repo_key(p.resolved or p.url), p.commit), p)
    out = Layout()
    wanted = set(pins) if pins is not None else None

    def source(p: Pin) -> Pin:
        return first.get((repo_key(p.resolved or p.url), p.commit), p) if p.duplicate else p

    def count(p: Pin) -> int:
        return 1 + sum(count(c) for c in source(p).children)

    def lay(p: Pin, d: Path, tree_path: str) -> None:
        if not _checkout(git, p, d):
            out.missing.append(tree_path or ".")
            return
        out.laid_out.append(tree_path)
        src = source(p)
        for c in src.children:
            rel = c.tree_path[len(src.tree_path) + 1:] if src.tree_path else c.tree_path
            tp = "/".join(x for x in (tree_path, rel) if x)
            if not _needed(tp, wanted):
                out.not_laid_out += count(c)
                continue
            lay(c, d / rel, tp)

    lay(root, Path(dest), "")
    if wanted is not None:      # a declared pin the tree does not hold at all
        out.missing += sorted(n for n in wanted if n not in out.laid_out and n not in out.missing
                              and not any(m == n or n.startswith(m + "/") for m in out.missing))
    return out


def worktree_changes(repo: Path) -> dict[str, str | None] | None:
    """Tracked files of the repo at `repo` that differ from its checked-out commit (`git diff HEAD`: index and
    working file against HEAD; untracked files and submodules ignored): repo-relative path -> sha256 of the working
    file (a symlink: its target text; None: deleted). None: not readable."""
    from nanosoc_rm.gitrun import run_git
    r = run_git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--ignore-submodules=all", "HEAD",
                "--", cwd=repo)
    if not r.ok:
        return None
    out: dict[str, str | None] = {}
    for rel in (x for x in r.stdout.split("\0") if x):
        f = Path(repo) / rel
        try:
            out[rel] = os.readlink(f) if f.is_symlink() else \
                hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
        except OSError:
            out[rel] = None
    return out


def _record_layout(work: Path, tree: Path, layout: Layout) -> None:
    """D4 (matrix, CR-WD-matrix-t1): which tree paths were laid out, and their tracked changes just before the
    command (after the declared output files were removed), so a re-read after the command can tell a repo the
    command removed (not 'not laid out') and a tracked file it changed (not one the layout left changed)."""
    before = {}
    for tp in layout.laid_out:
        ch = worktree_changes(tree / tp if tp else tree)
        before[tp] = ch if ch is not None else {}
    (work / LAYOUT_RECORD).write_text(json.dumps({"laid_out": list(layout.laid_out), "changes_before": before},
                                                 sort_keys=True))


def read_layout_record(work: Path) -> dict | None:
    try:
        d = json.loads((Path(work) / LAYOUT_RECORD).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("laid_out"), list) else None


# ---------------------------------------------------------------------------------------------------------
# Rows and verdict
# ---------------------------------------------------------------------------------------------------------
def _row(gate: str, status: str, detail: str, nonce: str, utc: str | None = None) -> dict:
    return {"utc": utc if utc and _UTC.match(utc) else clock.iso(), "status": status, "gate": gate,
            "detail": G.clean_detail(detail), "nonce": nonce}


def _gate_id(tier: str, gate: str, n: int) -> str:
    part = _GATE_PART.sub("-", gate.strip().lower()).strip(".-")
    part = re.sub(r"\.{2,}", ".", part)
    gid = "tier.%s.%s" % (tier, part)
    return gid if part and re.match(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$", gid) else \
        "tier.%s.row-%d" % (tier, n)


def parse_verdicts(path: Path, tier: str, nonce: str) -> list[dict]:
    """Rows `tier.<tier>.<gate>` from a toolkit verdicts.tsv (`<utc>\\t<STATUS>\\t<gate>\\t<detail>[\\t...]`).
    A missing, unreadable or empty file, or a malformed line, is one UNVERIFIED row: declared evidence that is
    not there is never read as a pass."""
    where = "tier.%s.verdicts" % tier
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [_row(where, "UNVERIFIED", "declared verdicts file %s was not written (%s)"
                     % (path.name, e.strerror or e), nonce)]
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        f = line.split("\t")
        if len(f) < 4 or f[1].strip().upper() not in STATUSES:
            rows.append(_row(where, "UNVERIFIED", "line %d of %s is not a verdicts row: %s" % (n, path.name, line[:120]),
                             nonce))
            continue
        rows.append(_row(_gate_id(tier, f[2], n), f[1].strip().upper(), f[3], nonce, f[0].strip()))
    if not rows:
        rows.append(_row(where, "UNVERIFIED", "declared verdicts file %s holds no rows" % path.name, nonce))
    return rows


def tier_verdict(rows: list[dict]) -> str:
    st = [r["status"] for r in rows]
    for v in ("FAIL", "UNVERIFIED", "INFRA", "WARN"):
        if v in st:
            return v
    return "PASS" if "PASS" in st else "UNVERIFIED"


def _doc(spec: Mapping, tier: str, **kw) -> dict:
    budget = int(spec.get("budget_s") or 0)
    d = {"tier": tier, "verdict": "UNVERIFIED", "reason": None, "forced": False,
         "command": command_text(spec.get("command") or "?"), "cwd": spec.get("cwd") or ".",
         "env_keys": list((spec.get("env") or {}).keys()), "needs": list(spec.get("needs") or []),
         "regenerates": bool(spec.get("regenerates")), "budget_s": budget, "timeout_s": budget * KILL_FACTOR,
         "seconds": None, "over_budget": False, "killed": False, "exit_code": None,
         "verdicts_file": spec.get("verdicts"), "rows": [], "missing_paths": [], "not_laid_out": 0, "log_tail": []}
    d.update(kw)
    return d


def skipped(spec: Mapping, tier: str, reason: str) -> dict:
    return _doc(spec, tier, verdict="NOT-RUN", reason=reason)


# ---------------------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------------------
def _env(spec: Mapping, tier: str, tree: Path, work: Path) -> dict[str, str]:
    home, tmp = work / "tier-home", work / "tier-tmp"
    for d in (home, tmp):
        d.mkdir(parents=True, exist_ok=True)
    try:
        user = getpass.getuser()
    except Exception:            # noqa: BLE001 - no passwd entry: run without USER
        user = ""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "TREE": str(tree), "NANOSOC_RM_TIER": tier,
           # git in the command is anonymous too: no prompt, no system config, no agent or key of the caller
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_SSH_COMMAND": "ssh -F /dev/null -o BatchMode=yes -o IdentityAgent=none -o IdentitiesOnly=yes "
                              "-o IdentityFile=/dev/null"}
    if user:
        env.update(USER=user, LOGNAME=user)
    if spec.get("verdicts"):
        env["NANOSOC_RM_VERDICTS"] = str(tree / spec["verdicts"])
    for k, v in (spec.get("env") or {}).items():
        try:
            env[k] = Template(str(v)).substitute(env)
        except (KeyError, ValueError) as e:
            raise TierUsage("tier env %s=%r names %s, which is not set before it (only the tool's variables and "
                            "earlier env entries expand)" % (k, v, e)) from None
    if spec.get("regenerates"):
        env["NANOSOC_GEN_OK"] = "1"
    return env


def missing_tools(spec: Mapping, env: Mapping[str, str], tree: Path, work: Path) -> list[str]:
    """Absolute paths the tier's own `env` names (each `:`-separated part) that do not exist on this host,
    outside the sandbox: a tool or tool directory this host does not have. `VAR=path` per missing path."""
    out = []
    inside = (Path(tree).resolve(), Path(work).resolve())
    for k in (spec.get("env") or {}):
        for part in str(env.get(k, "")).split(":"):
            if not part.startswith("/"):
                continue
            p = Path(part)
            if any(p == d or d in p.parents for d in inside) or p.exists():
                continue
            out.append("%s=%s does not exist" % (k, part))
    return out


def _seat(needs, lock_dir: Path):
    """An exclusive non-blocking lock per seat-like need, or None when one is held elsewhere."""
    held = []
    for need in needs:
        if need != "vcs-seat":
            continue
        fd = os.open(str(Path(lock_dir) / ("nanosoc-rm-%s.lock" % need)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            for h in held:
                os.close(h)
            return None
        held.append(fd)      # non-inheritable (PEP 446): the tier command never holds it
    return held


def _killpg(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _tail(log: Path) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [G.clean_detail(x)[:300] for x in lines[-LOG_TAIL:]]


def run_tier(spec: Mapping, tier: str, tree: Path, work: Path, nonce: str, *, layout: Layout | None = None,
             lock_dir: str | Path | None = None) -> dict:
    """Run one tier command in the sandbox tree (module docstring, steps 3 and 4). One compat_tier document."""
    tree, work = Path(tree).absolute(), Path(work).absolute()
    layout = layout or Layout()
    base = dict(missing_paths=list(layout.missing), not_laid_out=layout.not_laid_out)
    if layout.missing:
        return _doc(spec, tier, reason="the sandbox checkout lacks needed pin(s) %s (no anonymous fetch reached "
                    "them): the tier was not run" % ", ".join(layout.missing), **base)
    cwd = (tree / (spec.get("cwd") or ".")).resolve()
    if not cwd.is_dir() or not (cwd == tree.resolve() or tree.resolve() in cwd.parents):
        return _doc(spec, tier, reason="cwd %s does not exist in the checked-out tree" % spec.get("cwd"), **base)
    env = _env(spec, tier, tree, work)
    vfile = tree / spec["verdicts"] if spec.get("verdicts") else None
    if vfile is not None and vfile.exists():
        vfile.unlink()                     # a committed or stale file is never read as this run's evidence
    ifile = tree / spec["ip_files"] if spec.get("ip_files") else None
    if ifile is not None and (ifile.is_file() or ifile.is_symlink()):
        ifile.unlink()                     # the same for the declared IP file list (D4 ip_hash, matrix section 18)
    absent = missing_tools(spec, env, tree, work)
    if absent:
        return _doc(spec, tier, verdict="INFRA", reason="tool not on this host: %s; the tier was not run"
                    % "; ".join(absent), **base)
    held = _seat(spec.get("needs") or (), Path(lock_dir) if lock_dir else Path(tempfile.gettempdir()))
    if held is None:
        return _doc(spec, tier, verdict="INFRA", reason="the vcs-seat lock is held by another local tier run",
                    **base)
    try:
        _record_layout(work, tree, layout)
    except Exception:                      # noqa: BLE001 - never fails the run; the re-read reports it
        pass                               # no record: the matrix re-read says the layout is unknown (UNVERIFIED)
    cmd = spec["command"]
    argv = ["/bin/bash", "--noprofile", "--norc", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    budget = int(spec["budget_s"])
    limit = budget * KILL_FACTOR
    log = work / ("tier-%s.log" % tier)
    killed = False
    try:
        t_start = time.monotonic()
        with open(log, "wb") as out:
            try:
                proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                return _doc(spec, tier, verdict="FAIL", reason="the command did not start: %s" % (e.strerror or e),
                            rows=[_row("tier.%s" % tier, "FAIL", "did not start: %s" % (e.strerror or e), nonce)],
                            **base)
            try:
                rc = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                killed = True
                _killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                rc = proc.returncode
            _killpg(proc.pid, signal.SIGKILL)          # nothing of the run outlives it
        seconds = round(time.monotonic() - t_start, 3)
    finally:
        for fd in held:
            os.close(fd)
    if killed:
        head = _row("tier.%s" % tier, "FAIL", "killed after %.1f s: past %g s (budget %d s x %g)"
                    % (seconds, limit, budget, KILL_FACTOR), nonce)
    else:
        head = _row("tier.%s" % tier, "PASS" if rc == 0 else "FAIL", "exit %d in %.1f s" % (rc, seconds), nonce)
    rows = [head] + (parse_verdicts(vfile, tier, nonce) if vfile is not None else [])
    verdict = tier_verdict(rows)
    reason = None
    if verdict != "PASS":
        bad = [r for r in rows if r["status"] == verdict]
        reason = "; ".join("%s: %s" % (r["gate"], r["detail"]) for r in bad)[:500]
    return _doc(spec, tier, verdict=verdict, reason=reason, seconds=seconds, over_budget=seconds > budget,
                killed=killed, exit_code=None if killed else rc, rows=rows, log_tail=_tail(log), **base)
