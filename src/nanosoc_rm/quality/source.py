"""Where the quality tools read a repository from (Q1-Q5). Read-only by construction.

- `GitSource`: one commit of a git repository (a component mirror, or a local repo with `--ref`). Files come from
  `git ls-tree`, contents from one `git cat-file --batch`, line ages from `git blame`. Nothing is checked out.
- `WorktreeSource`: a local directory as it is on disk (`--path DIR` without `--ref`). Tracked files only when it
  is a git work tree (`git ls-files`), else every file below it; contents are read with open(), never written.

Every git call goes through `nanosoc_rm.gitrun.run_git` and is recorded in `sources` (A3).
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from nanosoc_rm.gitrun import GitResult, run_git

__all__ = ["FileEntry", "Source", "GitSource", "WorktreeSource", "SourceError", "parse_gitmodules"]

GIT_TIMEOUT_S = 300
_SHA = re.compile(r"^[0-9a-f]{40}\Z")


class SourceError(LookupError):
    """The repository or ref cannot be read."""


@dataclass(frozen=True)
class FileEntry:
    path: str          # POSIX, relative to the repository root
    mode: str          # "100644", "100755", "120000" (symlink)
    size: int

    @property
    def executable(self) -> bool:
        return self.mode == "100755"


def parse_gitmodules(text: str) -> dict[str, str]:
    """{submodule name: path} of a .gitmodules file."""
    out, name = {}, None
    for line in text.splitlines():
        m = re.match(r'^\s*\[submodule\s+"(.+)"\]\s*$', line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"^\s*path\s*=\s*(.+?)\s*$", line)
        if m and name is not None:
            out[name] = m.group(1)
    return out


@dataclass
class Source:
    """A readable snapshot of one repository. Subclasses fill `_files` and implement `_read_many`."""
    label: str
    commit: str | None
    ref: str | None
    worktree: bool
    sources: list[dict] = field(default_factory=list)
    _files: dict[str, FileEntry] = field(default_factory=dict)
    _gitlinks: set[str] = field(default_factory=set)
    _cache: dict[str, bytes | None] = field(default_factory=dict)

    # -- tree -----------------------------------------------------------------------------------------------
    def files(self) -> list[FileEntry]:
        return [self._files[p] for p in sorted(self._files)]

    def gitlinks(self) -> set[str]:
        return set(self._gitlinks)

    def entry(self, path: str) -> FileEntry | None:
        return self._files.get(_norm(path))

    def dirs(self) -> set[str]:
        out = set()
        for p in list(self._files) + list(self._gitlinks):
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    def exists(self, path: str) -> bool:
        p = _norm(path)
        return p == "" or p in self._files or p in self._gitlinks or p in self.dirs()

    def is_dir(self, path: str) -> bool:
        p = _norm(path)
        return p == "" or p in self.dirs() or p in self._gitlinks

    def glob(self, pattern: str) -> list[str]:
        pat = _norm(pattern)
        every = list(self._files) + list(self._gitlinks) + sorted(self.dirs())
        return [p for p in every if fnmatch.fnmatchcase(p, pat)]

    def submodules(self) -> dict[str, str]:
        raw = self.read(".gitmodules")
        return parse_gitmodules(raw.decode("utf-8", "replace")) if raw else {}

    # -- contents -------------------------------------------------------------------------------------------
    def read(self, path: str) -> bytes | None:
        p = _norm(path)
        if p not in self._cache:
            self._cache.update(self._read_many([p]))
        return self._cache.get(p)

    def read_many(self, paths: list[str]) -> dict[str, bytes | None]:
        want = [_norm(p) for p in paths if _norm(p) not in self._cache]
        if want:
            self._cache.update(self._read_many(want))
        return {_norm(p): self._cache.get(_norm(p)) for p in paths}

    def text(self, path: str) -> str | None:
        raw = self.read(path)
        return None if raw is None else raw.decode("utf-8", "replace")

    def _read_many(self, paths: list[str]) -> dict[str, bytes | None]:      # pragma: no cover - abstract
        raise NotImplementedError

    def blame_times(self, path: str, lines: list[int]) -> dict[int, int]:
        """{line: author time (epoch seconds)} for the given 1-based lines; {} when blame cannot answer."""
        return {}

    @property
    def state(self) -> str | None:
        """For a work-tree read: a digest of every listed file's path, mode, size and mtime, so a stored result
        can be told stale when the disk moved on under the same HEAD. None for a commit read."""
        return None

    def describe(self) -> str:
        where = self.commit[:12] if self.commit else "?"
        return "%s@%s%s" % (self.label, where, " (work tree)" if self.worktree else "")


def _norm(path: str) -> str:
    p = str(PurePosixPath(path.replace("\\", "/")))
    p = p.lstrip("/") if not p.startswith("//") else p
    return "" if p in (".", "") else (p[2:] if p.startswith("./") else p)


def _git(cwd: str | os.PathLike, *args: str, sources: list[dict], input: bytes | None = None,
         text: bool = True) -> GitResult:
    r = run_git("--no-optional-locks", *args, cwd=cwd, input=input, text=text, timeout=GIT_TIMEOUT_S)
    sources.append(r.source())
    return r


def _blame(cwd: str | os.PathLike, rev: str | None, path: str, lines: list[int], sources: list[dict]) -> dict[int, int]:
    if not lines:
        return {}
    args = ["blame", "--porcelain"]
    for n in sorted(set(lines)):
        args += ["-L", "%d,%d" % (n, n)]
    if rev:
        args.append(rev)
    args += ["--", path]
    r = _git(cwd, *args, sources=sources)
    if not r.ok:
        return {}
    out, cur_final, times = {}, None, {}
    commit_of_line: dict[int, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", line)
        if m:
            cur_final = (m.group(1), int(m.group(2)))
            commit_of_line[cur_final[1]] = cur_final[0]
            continue
        if line.startswith("author-time ") and cur_final is not None:
            times[cur_final[0]] = int(line.split()[1])
    for n, sha in commit_of_line.items():
        if sha in times:
            out[n] = times[sha]
    return out


def _prefetch_blobs(repo: Path, commit: str, sources: list[dict]) -> None:
    """A partial-clone mirror (`--filter=blob:none`) lacks the commit's blobs; `ls-tree -l` and `cat-file` would
    then fetch them one round trip each (minutes for a large tree). Fetch the missing ones in one anonymous batch
    from the mirror's promisor remote(s), as git's own lazy fetch does. A full repository fetches nothing."""
    r = _git(repo, "rev-list", "--objects", "--no-walk", "--missing=print", commit, sources=sources)
    missing = "".join(line[1:].split()[0] + "\n" for line in r.stdout.splitlines() if line.startswith("?")) \
        if r.ok else ""
    if not missing:
        return
    remotes = _git(repo, "config", "--get-regexp", r"^remote\..*\.promisor$", sources=sources)
    for line in (remotes.stdout.splitlines() if remotes.ok else []):
        key, _, val = line.partition(" ")
        if val.strip().lower() not in ("true", "1", "yes", "on"):
            continue
        name = key[len("remote."):-len(".promisor")]
        f = _git(repo, "-c", "fetch.negotiationAlgorithm=noop", "fetch", name, "--no-tags", "--no-write-fetch-head",
                 "--recurse-submodules=no", "--filter=blob:none", "--stdin", sources=sources, input=missing)
        if f.ok:
            return


class GitSource(Source):
    """One commit of a git repository (`repo` is a bare mirror or a work tree's repository)."""

    def __init__(self, repo: str | os.PathLike, commit: str, *, label: str, ref: str | None = None,
                 sources: list[dict] | None = None):
        super().__init__(label=label, commit=commit, ref=ref, worktree=False, sources=list(sources or []))
        self.repo = Path(repo)
        _prefetch_blobs(self.repo, commit, self.sources)
        r = _git(self.repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, sources=self.sources)
        if not r.ok:
            raise SourceError("cannot list %s at %s: %s" % (label, commit, (r.stderr or "").strip()[-200:]))
        self._blob = {}
        for rec in r.stdout.split("\0"):
            if not rec:
                continue
            meta, _, path = rec.partition("\t")
            parts = meta.split()
            if len(parts) < 4:
                continue
            mode, kind, sha, size = parts[0], parts[1], parts[2], parts[3]
            if kind == "commit":
                self._gitlinks.add(path)
            elif kind == "blob":
                self._files[path] = FileEntry(path, mode, int(size) if size.isdigit() else 0)
                self._blob[path] = sha

    @classmethod
    def resolve(cls, repo: str | os.PathLike, ref: str, *, label: str, prefixes: tuple[str, ...] = ("",),
                sources: list[dict] | None = None) -> "GitSource":
        """The commit `ref` names: a branch or tag under each of `prefixes` (a mirror namespace such as
        `refs/r/<key>/`), or a commit sha."""
        srcs = list(sources or [])
        candidates = []
        for p in prefixes:
            if p:
                candidates += ["%sheads/%s" % (p, ref), "%stags/%s" % (p, ref)]
            else:
                candidates += ["refs/heads/%s" % ref, "refs/tags/%s" % ref, "refs/remotes/origin/%s" % ref]
        candidates.append(ref)
        for c in candidates:
            if c == ref and prefixes != ("",) and not re.match(r"^[0-9a-f]{7,40}$", ref):
                continue                                   # in a mirror only a namespaced ref or a sha is meaningful
            r = _git(repo, "rev-parse", "--verify", "--quiet", "%s^{commit}" % c, sources=srcs)
            if r.ok and _SHA.match(r.stdout.strip()):
                return cls(repo, r.stdout.strip(), label=label, ref=ref, sources=srcs)
        raise SourceError("ref %r not found in %s" % (ref, label))

    def _read_many(self, paths: list[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {p: None for p in paths}
        known = [(p, self._blob[p]) for p in paths if p in self._blob]
        if not known:
            return out
        batch = "".join("%s\n" % sha for _, sha in known).encode()
        r = _git(self.repo, "cat-file", "--batch", sources=self._batch_sources(), input=batch, text=False)
        if not r.ok:
            raise SourceError("cat-file failed in %s: %s" % (self.label, r.stderr.strip()[-200:]))
        data: bytes = r.stdout  # type: ignore[assignment]
        pos = 0
        for path, sha in known:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].split()
            pos = nl + 1
            if len(header) < 3 or header[1] == b"missing":
                continue
            size = int(header[2])
            out[path] = data[pos:pos + size]
            pos += size + 1
        return out

    def _batch_sources(self) -> list[dict]:
        """Record the cat-file argv once (the blob ids it read come from the recorded ls-tree)."""
        return self.sources if not any("cat-file" in s["command"] for s in self.sources) else []

    def blame_times(self, path: str, lines: list[int]) -> dict[int, int]:
        return _blame(self.repo, self.commit, path, lines, self.sources)


class WorktreeSource(Source):
    """A local directory as it is now, read-only. Tracked files only when it is a git work tree."""

    SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".mypy_cache", ".pytest_cache"}

    def __init__(self, root: str | os.PathLike, *, label: str | None = None):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SourceError("%s is not a directory" % root)
        self.root = root
        self._mtimes: dict[str, int] = {}
        super().__init__(label=label or root.name, commit=None, ref=None, worktree=True)
        top = _git(root, "rev-parse", "--show-toplevel", sources=self.sources)
        self.is_git = top.ok and Path(top.stdout.strip()).resolve() == root
        if self.is_git:
            head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", sources=self.sources)
            self.commit = head.stdout.strip() if head.ok and _SHA.match(head.stdout.strip()) else None
            r = _git(root, "ls-files", "-s", "-z", sources=self.sources)
            if not r.ok:
                raise SourceError("cannot list tracked files of %s" % root)
            for rec in r.stdout.split("\0"):
                if not rec:
                    continue
                meta, _, path = rec.partition("\t")
                parts = meta.split()
                if not parts:
                    continue
                if parts[0] == "160000":
                    self._gitlinks.add(path)
                    continue
                self._add(path)
        else:
            self.sources.append({"kind": "derived", "command": "os.walk %s (not a git work tree)" % root})
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in self.SKIP_DIRS)
                for f in filenames:
                    self._add(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))

    def _add(self, path: str) -> None:
        full = self.root / path
        try:
            st = os.lstat(full)
        except OSError:
            return                                         # deleted in the work tree: not there to read
        if stat.S_ISLNK(st.st_mode):
            mode = "120000"
        elif not stat.S_ISREG(st.st_mode):
            return
        else:
            mode = "100755" if st.st_mode & 0o111 else "100644"
        self._files[path] = FileEntry(path, mode, st.st_size)
        self._mtimes[path] = st.st_mtime_ns

    @property
    def state(self) -> str | None:
        h = hashlib.sha1()
        for p in sorted(self._files):
            e = self._files[p]
            h.update(("%s\0%s\0%d\0%d\n" % (p, e.mode, e.size, self._mtimes.get(p, 0))).encode())
        for g in sorted(self._gitlinks):
            h.update(("%s\0gitlink\n" % g).encode())
        return h.hexdigest()[:16]

    def _read_many(self, paths: list[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        for p in paths:
            e = self._files.get(p)
            if e is None or e.mode == "120000":
                out[p] = None
                continue
            try:
                with open(self.root / p, "rb") as f:
                    out[p] = f.read()
            except OSError:
                out[p] = None
        return out

    def blame_times(self, path: str, lines: list[int]) -> dict[int, int]:
        if not self.is_git:
            return {}
        return _blame(self.root, None, path, lines, self.sources)
