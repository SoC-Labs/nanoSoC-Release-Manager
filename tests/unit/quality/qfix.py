"""Helpers for the quality unit tests: scratch git repositories with chosen contents, dates and modes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENV_BASE = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd, date: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, **ENV_BASE)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True, env=env)


def write(root: Path, files: dict[str, str], executable: tuple[str, ...] = ()) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel in executable:
            p.chmod(0o755)


def make_repo(root: Path, files: dict[str, str], *, date: str = "2026-10-01T10:00:00Z",
              executable: tuple[str, ...] = (), gitlinks: dict[str, str] | None = None) -> Path:
    """A git repo at `root` with one commit of `files`; `gitlinks` {path: sha} adds submodule entries."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", ".", cwd=root)
    write(root, files, executable)
    git("add", "-A", cwd=root)
    for path, sha in (gitlinks or {}).items():
        git("update-index", "--add", "--cacheinfo", "160000,%s,%s" % (sha, path), cwd=root)
    git("commit", "-q", "-m", "fixture", cwd=root, date=date)
    return root


def commit(root: Path, files: dict[str, str], date: str, msg: str = "change") -> str:
    write(root, files)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", msg, cwd=root, date=date)
    return git("rev-parse", "HEAD", cwd=root).stdout.strip()


def head(root: Path) -> str:
    return git("rev-parse", "HEAD", cwd=root).stdout.strip()
