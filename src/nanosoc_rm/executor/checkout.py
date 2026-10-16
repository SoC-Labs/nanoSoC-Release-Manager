"""Lay the commit under test out from the agent's read-only mirror (interfaces.md section 27.4 step 2).

    lay_out(mirror_root, task, tree) -> Layout

Each checkout (the root at `tree`, then each pin at `tree/<path>`, outermost first) is a fresh repo whose
`objects/info/alternates` points at `<mirror_root>/<mirror>.git/objects`: nothing is copied, nothing is fetched,
and nothing is written to the mirror (git writes only into the scratch repo; the sandbox binds the mirror
read-only). A commit that is not in the mirror, or a pin path the tree cannot hold, is `missing`, and the agent
reports INFRA without running the command. Every git call goes through nanosoc_rm.gitrun (isolated config).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm.gitrun import run_git

__all__ = ["Layout", "lay_out", "mirror_repo", "GIT_TIMEOUT_S"]

GIT_TIMEOUT_S = 900


@dataclass
class Layout:
    laid_out: list[str] = field(default_factory=list)       # tree paths, "" = the root
    missing: list[str] = field(default_factory=list)        # "<path>: why"


def mirror_repo(mirror_root: Path, mirror: str) -> Path | None:
    """`<mirror_root>/<mirror>.git` when it is a bare repo inside the mirror root, else None."""
    root = Path(mirror_root).resolve()
    p = (root / ("%s.git" % mirror)).resolve()
    if root not in p.parents or not (p / "objects").is_dir() or not (p / "HEAD").is_file():
        return None
    return p


def _one(mirror_root: Path, mirror: str, commit: str, dest: Path) -> str | None:
    src = mirror_repo(mirror_root, mirror)
    if src is None:
        return "mirror %s is not in the agent's mirror root" % mirror
    if dest.exists() and (not dest.is_dir() or dest.is_symlink() or any(dest.iterdir())):
        return "the tree already holds something at this path"
    dest.mkdir(parents=True, exist_ok=True)
    if not run_git("init", "-q", dest, isolate=True, timeout=60).ok:
        return "git init failed"
    info = dest / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(str(src / "objects") + "\n")
    if (src / "shallow").is_file():
        (dest / ".git" / "shallow").write_text((src / "shallow").read_text())
    if not run_git("cat-file", "-e", commit + "^{commit}", cwd=dest, isolate=True, timeout=60).ok:
        return "commit %s is not in mirror %s (sync the agent's mirror)" % (commit[:12], mirror)
    r = run_git("checkout", "-q", "--detach", commit, cwd=dest, isolate=True, timeout=GIT_TIMEOUT_S)
    if not r.ok:
        tail = (r.stderr or "").strip().splitlines()
        return "checkout of %s failed: %s" % (commit[:12], tail[-1][:200] if tail else "exit %d" % r.returncode)
    return None


def lay_out(mirror_root: Path, task: dict, tree: Path) -> Layout:
    out = Layout()
    tree = Path(tree)
    why = _one(Path(mirror_root), task["mirror"], task["commit"], tree)
    if why:
        out.missing.append(".: " + why)
        return out
    out.laid_out.append("")
    for pin in sorted(task.get("pins") or [], key=lambda p: p["path"].count("/")):
        dest = (tree / pin["path"])
        if tree.resolve() not in dest.resolve().parents:
            out.missing.append("%s: outside the tree" % pin["path"])
            continue
        why = _one(Path(mirror_root), pin["mirror"], pin["commit"], dest)
        if why:
            out.missing.append("%s: %s" % (pin["path"], why))
        else:
            out.laid_out.append(pin["path"])
    return out
