"""D4 provenance of one matrix cell run: what was asked for, what the run actually read, and where it ran.

    host_load() -> {cpus, load_start | load_end}           os.getloadavg, the 1-minute average
    tools() -> {git, python, nanosoc_rm}
    capture(run_dir, root_commit, requested, overrides) -> {superproject_commit, pins: [{tree_path, commit}]}
    moved(requested, captured) -> [{tree_path, requested, captured}]

**Capture, after the run.** A local T0 run (`analysis.compat`) fetches every pin it can into its own work directory
(`fetch --depth=1 <url> <sha>`, one empty repo per (repo, commit)) and never checks out a tree. After the run, the
cell keeps that directory and re-reads the tree from the objects the run fetched, offline: the superproject commit,
then each pin's gitlink in its parent's fetched tree (`git ls-tree <parent> -- <path>`), with the request's
overrides applied the way the gates apply them. A pin whose parent was not fetched (an ssh URL, a private repo)
is captured as null: not read, which the gate verdicts already report. A pin captured at a commit other than the
one requested is MOVED, and the cell is UNVERIFIED (D4): its verdict is not about the tree that was asked for.

For T0 nothing can move a submodule (there is no checkout and no setup script), so a mismatch means the run read
something other than what the walker resolved. The same rule covers a later tier run, whose setup scripts can.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Iterable, Mapping

from nanosoc_rm import __version__
from nanosoc_rm.gitrun import run_git

__all__ = ["host_load", "tools", "capture", "moved", "OVERLOAD_PER_CPU", "HARNESS"]

OVERLOAD_PER_CPU = 2.0          # 1-minute load per CPU above which a host counts as overloaded (D7)
HARNESS = "nanosoc-rm %s compat (T0, run mode local)" % __version__


def host_load() -> dict:
    try:
        one = os.getloadavg()[0]
    except OSError:                                               # pragma: no cover - not on Linux
        one = None
    return {"cpus": os.cpu_count() or 1, "load": None if one is None else round(one, 2)}


def tools() -> dict:
    from nanosoc_rm.t0.gitwork import git_version
    return {"git": git_version(), "python": platform.python_version(), "nanosoc_rm": __version__}


def _object_dirs(run_dir: Path) -> list[Path]:
    """Every object directory the run's fetches and clones wrote (`<slot>/.git/objects`, `<bare>/objects`)."""
    out = []
    for p in sorted(Path(run_dir).rglob("objects")):
        if p.is_dir() and (p.parent / "HEAD").is_file():
            out.append(p.resolve())
    return list(dict.fromkeys(out))


def _store(run_dir: Path, scratch: Path) -> Path:
    """An empty bare repo that borrows (alternates) every object the run fetched. No promisor remote: a missing
    object is an error here, never a fetch."""
    repo = Path(scratch) / "captured.git"
    if not (repo / "HEAD").is_file():
        run_git("init", "-q", "--bare", repo).check()
    alt = repo / "objects" / "info" / "alternates"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text("".join("%s\n" % d for d in _object_dirs(run_dir) if d != (repo / "objects").resolve()))
    return repo


def _has_commit(repo: Path, sha: str) -> bool:
    return run_git("cat-file", "-e", sha + "^{commit}", cwd=repo).ok


def _gitlink(repo: Path, sha: str, path: str) -> str | None:
    r = run_git("ls-tree", sha, "--", path, cwd=repo)
    if not r.ok or not r.stdout.strip():
        return None
    meta = r.stdout.split("\t", 1)[0].split()
    return meta[2] if len(meta) >= 3 and meta[1] == "commit" else None


def capture(run_dir: str | os.PathLike, root_commit: str, requested: Iterable[Mapping],
            overrides: Mapping[str, str] | None, scratch: str | os.PathLike) -> dict:
    """The tree as the run's own fetched objects hold it (module docstring). `requested` is
    [{tree_path, commit}] (every tree path of the walked tree, overrides applied)."""
    overrides = dict(overrides or {})
    repo = _store(Path(run_dir), Path(scratch))
    root = root_commit if _has_commit(repo, root_commit) else None
    paths = sorted({r["tree_path"] for r in requested}, key=lambda p: (p.count("/"), p))
    got: dict[str, str | None] = {"": root}
    for tp in paths:
        parent = max((p for p in got if p and tp.startswith(p + "/")), key=len, default="")
        pc = got.get(parent)
        if tp in overrides:
            # the gates judge the override's commit, fetched from the pin's URL: captured only if that fetch landed
            got[tp] = overrides[tp] if pc is not None and _has_commit(repo, overrides[tp]) else None
        elif pc is None:
            got[tp] = None
        else:
            got[tp] = _gitlink(repo, pc, tp[len(parent) + 1:] if parent else tp)
    return {"superproject_commit": root, "pins": [{"tree_path": tp, "commit": got[tp]} for tp in paths]}


def moved(requested: Iterable[Mapping], captured: Mapping) -> list[dict]:
    """Pins the run read at a commit other than the one requested (a null capture is 'not read', not moved)."""
    want = {r["tree_path"]: r["commit"] for r in requested}
    out = []
    for p in captured.get("pins") or []:
        tp, c = p["tree_path"], p["commit"]
        if c is not None and tp in want and want[tp] is not None and c != want[tp]:
            out.append({"tree_path": tp, "requested": want[tp], "captured": c})
    return out
