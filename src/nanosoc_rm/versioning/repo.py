"""Reading one component for the versioning functions: its mirror (or a local checkout), tags, files at a commit.

Read-only by construction. Every git call goes through `nanosoc_rm.gitrun.run_git` and is recorded (A3). The
mirrors are blob-less partial clones (interfaces.md section 3), so a file at a commit is read as quality does:
the missing blobs are fetched in one anonymous batch from the mirror's promisor remote, then read with one
`cat-file --batch`. Nothing is checked out and nothing is written to any repository.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.gitrun import GitResult, run_git
from nanosoc_rm.versioning import semver, tags as tagmod

__all__ = ["VersioningError", "VersioningUsage", "RepoView", "open_repo", "read_files", "registry_policy",
           "web_url", "component_for_url", "baseline", "GIT_TIMEOUT_S"]

GIT_TIMEOUT_S = 300
_SHA = re.compile(r"^[0-9a-f]{40}\Z")


class VersioningError(Exception):
    """The question is valid but cannot be answered (no mirror, an unreadable ref): CLI exit 2."""
    exit_code = 2


class VersioningUsage(VersioningError):
    """The question cannot be asked (unknown component, bad ref or version): CLI exit 3."""
    exit_code = 3


class VersioningRefused(VersioningUsage):
    """The request is refused because of a finding (a release draft of a plan that FAILs): CLI exit 1."""
    exit_code = 1


def _git(cwd, *args: str, sources: list[dict], input: bytes | None = None, text: bool = True,
         timeout: float = GIT_TIMEOUT_S) -> GitResult:
    r = run_git("--no-optional-locks", *args, cwd=cwd, input=input, text=text, timeout=timeout)
    sources.append(r.source())
    return r


def _fetch_blobs(repo: Path, blob_ids: list[str], sources: list[dict]) -> None:
    """Fetch blobs a partial-clone mirror lacks, in one batch from its promisor remote (as quality.source does)."""
    if not blob_ids:
        return
    r = _git(repo, "rev-list", "--objects", "--no-walk", "--missing=print", *blob_ids, sources=sources)
    missing = "".join(line[1:].split()[0] + "\n" for line in str(r.stdout).splitlines() if line.startswith("?")) \
        if r.ok else ""
    if not missing:
        return
    remotes = _git(repo, "config", "--get-regexp", r"^remote\..*\.promisor$", sources=sources)
    for line in (str(remotes.stdout).splitlines() if remotes.ok else []):
        key, _, val = line.partition(" ")
        if val.strip().lower() not in ("true", "1", "yes", "on"):
            continue
        name = key[len("remote."):-len(".promisor")]
        f = _git(repo, "-c", "fetch.negotiationAlgorithm=noop", "fetch", name, "--no-tags", "--no-write-fetch-head",
                 "--recurse-submodules=no", "--filter=blob:none", "--stdin", sources=sources, input=missing.encode())
        if f.ok:
            return


def read_files(repo: Path, commit: str, paths: list[str], sources: list[dict]) -> dict[str, str | None]:
    """{path: text or None (absent)} of `paths` at `commit` in a git repository (a mirror or a local repo)."""
    out: dict[str, str | None] = {p: None for p in paths}
    if not paths:
        return out
    r = _git(repo, "ls-tree", "-z", commit, "--", *paths, sources=sources)
    if not r.ok:
        return out
    blobs: dict[str, str] = {}
    for rec in str(r.stdout).split("\0"):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    _fetch_blobs(repo, sorted(set(blobs.values())), sources)
    if not blobs:
        return out
    known = list(blobs.items())
    batch = "".join("%s\n" % sha for _, sha in known).encode()
    c = _git(repo, "cat-file", "--batch", sources=sources, input=batch, text=False)
    if not c.ok:
        return out
    data: bytes = c.stdout  # type: ignore[assignment]
    pos = 0
    for path, _sha in known:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[path] = data[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return out


def _cfg(config) -> Config | None:
    """The service config: a Config, a path, or None ($NANOSOC_RM_CONFIG; None when that is not set, so a
    --path read needs no config). A config that is named but broken is an error, never silently ignored."""
    import os
    if isinstance(config, Config):
        return config
    if config is None and not os.environ.get("NANOSOC_RM_CONFIG"):
        return None
    return load_config(config)


def web_url(config: Config | None, repo: dict | None) -> str | None:
    """The canonical repo's https web URL (`hosts.<host>.web/<path>`), or None when it is not https."""
    if config is None or repo is None:
        return None
    try:
        base = config.host(repo["host"])["web"].rstrip("/")
    except (ConfigError, KeyError):
        return None
    return "%s/%s" % (base, repo["path"]) if base.startswith("https://") else None


def registry_policy(config: Config | None) -> tuple[dict, list[dict]]:
    """(the registry's optional `versioning.yaml`, its sources). {} when there is no registry or no such file.
    VersioningError when it exists but breaks its contract (tool-release#/$defs/versioning_file)."""
    from nanosoc_rm import contracts
    if config is None:
        return {}, []
    try:
        reg = Path(config.require("registry", "path"))
    except ConfigError:
        return {}, []
    f = reg / "versioning.yaml"
    if not f.is_file():
        return {}, []
    doc = contracts.load_yaml(f.read_text())
    problems = contracts.validate("tool-release", doc, "/$defs/versioning_file")
    if problems:
        raise VersioningError("%s breaks its contract: %s" % (f, "; ".join(problems[:5])))
    return doc, [{"kind": "registry", "command": "versioning.yaml"}]


def component_for_url(url: str, config: Config | None, registry=None, parent: dict | None = None) -> str | None:
    """The catalogue component a submodule URL names, or None (unknown URL, B9)."""
    if config is None or registry is None:
        return None
    from nanosoc_rm.registry import normalise_url
    try:
        kind, repo = normalise_url(url, config.get("hosts") or {}, parent)
    except Exception:                                                   # noqa: BLE001 - an odd URL is just unknown
        return None
    if repo is None:
        return None
    key = (repo.host, repo.path.lower())
    for c in registry.components():
        for r in c.repos:
            if (r["host"], r["path"].lower()) == key:
                return c.id
    return None


@dataclass
class RepoView:
    """One component (or local checkout) at one commit."""
    target: str
    git_dir: Path | None                    # the mirror or the local repository; None outside git
    commit: str | None
    ref: str | None
    worktree: bool
    prefixes: dict[str, str]                # repo key -> ref namespace (refs/r/<key>, or "refs" for a local repo)
    canonical_key: str
    comp_class: str | None = None
    canonical: dict | None = None
    component: object | None = None         # analysis._inputs.Component
    registry: object | None = None          # analysis._inputs.Registry
    config: Config | None = None
    base_url: str | None = None
    source: object | None = None            # quality.source.Source of the head
    sources: list[dict] = field(default_factory=list)
    _tags: dict[str, dict[str, dict]] | None = None

    def tags(self) -> dict[str, dict[str, dict]]:
        """{repo key: {tag: info}} (read once)."""
        if self._tags is None:
            self._tags = {}
            if self.git_dir is not None:
                for key, prefix in self.prefixes.items():
                    self._tags[key] = tagmod.read_tags(self.git_dir, prefix, self.sources)
        return self._tags

    def canonical_tags(self) -> dict[str, dict]:
        return self.tags().get(self.canonical_key, {})

    def head_text(self, path: str) -> str | None:
        if self.source is not None:
            return self.source.text(path)                                  # type: ignore[attr-defined]
        return None

    def files_at(self, commit: str, paths: list[str]) -> dict[str, str | None]:
        if self.git_dir is None or not commit:
            return {p: None for p in paths}
        return read_files(self.git_dir, commit, paths, self.sources)

    def commits(self, base: str | None, head: str, limit: int = 2000) -> tuple[int | None, list[str]]:
        """(count, messages) of base..head (every commit of head when base is None)."""
        if self.git_dir is None or not head:
            return None, []
        rng = "%s..%s" % (base, head) if base else head
        r = _git(self.git_dir, "log", "--format=%B%x00", "-n", str(limit), rng, "--", sources=self.sources)
        if not r.ok:
            return None, []
        msgs = [m.strip() for m in str(r.stdout).split("\0") if m.strip()]
        n = _git(self.git_dir, "rev-list", "--count", rng, sources=self.sources)
        return (int(str(n.stdout).strip()) if n.ok and str(n.stdout).strip().isdigit() else len(msgs)), msgs

    def log(self, base: str | None, head: str, limit: int = 2000) -> list[dict]:
        """[{sha, subject, body}] of base..head, newest first."""
        if self.git_dir is None or not head:
            return []
        rng = "%s..%s" % (base, head) if base else head
        r = _git(self.git_dir, "log", "--format=%H%x1f%cI%x1f%B%x1e", "-n", str(limit), rng, "--", sources=self.sources)
        out = []
        for rec in str(r.stdout).split("\x1e") if r.ok else []:
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, at, body = (rec.split("\x1f", 2) + ["", ""])[:3]
            out.append({"sha": sha.strip(), "at": at, "body": body})
        return out

    def is_ancestor(self, a: str, b: str) -> bool | None:
        if self.git_dir is None:
            return None
        r = _git(self.git_dir, "merge-base", "--is-ancestor", a, b, sources=self.sources)
        return r.returncode == 0 if r.returncode in (0, 1) else None

    def gitlinks(self, commit: str) -> dict[str, str]:
        """{path: commit} of every gitlink at `commit`."""
        if self.git_dir is None or not commit:
            return {}
        r = _git(self.git_dir, "ls-tree", "-r", "-z", commit, sources=self.sources)
        out = {}
        for rec in str(r.stdout).split("\0") if r.ok else []:
            meta, _, path = rec.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "commit":
                out[path] = parts[2]
        return out


def open_repo(target: str | None, *, ref: str | None = None, path=None, config=None) -> RepoView:
    """The component `target` read from its mirror at `ref` (default: its canonical default branch), or the local
    checkout `path` (its work tree, or `ref` in it). VersioningUsage for an unknown target or ref."""
    from nanosoc_rm import quality
    from nanosoc_rm.quality.source import GitSource
    cfg = _cfg(config)
    try:
        src = quality.open_source(target, ref=ref, path=path, config=cfg)
    except quality.UsageError as e:
        raise VersioningUsage(str(e)) from None
    except quality.QualityError as e:
        raise VersioningError(str(e)) from None
    view = RepoView(target=src.label, git_dir=None, commit=src.commit, ref=src.ref, worktree=src.worktree,
                    prefixes={}, canonical_key="local", config=cfg, source=src, sources=list(src.sources))
    if path is not None:
        p = Path(path).expanduser()
        r = run_git("rev-parse", "--git-dir", cwd=p, timeout=30)
        if r.ok:
            gd = Path(str(r.stdout).strip())
            view.git_dir = gd if gd.is_absolute() else (p / gd)
            view.prefixes = {"local": "refs"}
            if view.commit is None:
                h = run_git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", cwd=p, timeout=30)
                view.commit = str(h.stdout).strip() if h.ok and _SHA.match(str(h.stdout).strip()) else None
        if cfg is not None and target:
            _attach_registry(view, cfg, target, strict=False)
        return view
    if isinstance(src, GitSource):
        view.git_dir = src.repo
    _attach_registry(view, cfg, target, strict=True)
    return view


def _attach_registry(view: RepoView, cfg: Config, target: str, strict: bool) -> None:
    from nanosoc_rm.analysis import _inputs as inp
    try:
        reg = inp.registry(cfg)
        comp = reg.component(target)
    except (inp.UsageProblem, inp.CannotAnswer, ConfigError):
        if strict:
            raise
        return
    view.registry, view.component = reg, comp
    entry = next((c for c in reg.catalogue["components"] if c["id"] == comp.id), {})
    view.comp_class = entry.get("class")
    view.canonical = comp.canonical
    view.base_url = web_url(cfg, comp.canonical)
    if strict:
        view.prefixes = {inp.repo_key(r): inp.ref_prefix(r) for r in comp.repos}
        view.canonical_key = inp.repo_key(comp.canonical) if comp.canonical else next(iter(view.prefixes))


def baseline(canonical_tags: dict[str, dict], yanked: set[str] | frozenset = frozenset(),
             confirmed: set[str] | frozenset = frozenset()) -> dict:
    """The last release on the canonical host: the highest `vX.Y.Z` tag (C10). Without one, the highest
    grandfathered version-like tag (`vX.Y`, `vX.Y.Z`), marked as such, with a note when it is not the newest by
    date. {tag, version, kind: release|grandfathered|none, commit, created_at, notes, grandfathered}."""
    rel = [(semver.parse_tag(t), t, i) for t, i in canonical_tags.items() if semver.is_release_tag(t)]
    rel = [x for x in rel if x[0] is not None]
    lenient_rx = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?\Z")
    gf = []
    for t, i in canonical_tags.items():
        m = lenient_rx.match(t)
        if m and not semver.is_release_tag(t):
            gf.append((semver.Version(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)), t, i))
    notes: list[str] = []
    out = {"tag": None, "version": None, "kind": "none", "commit": None, "created_at": None,
           "grandfathered": [{"tag": t, "version": str(v), "created_at": i.get("created_at")}
                             for v, t, i in sorted(gf, key=lambda x: x[0].key(), reverse=True)],
           "notes": notes}
    if rel:
        v, t, i = max(rel, key=lambda x: x[0].key())
        out.update(tag=t, version=str(v), kind="release", commit=i["commit"], created_at=i.get("created_at"))
        if str(v) in yanked:
            notes.append("the latest release %s is yanked (G6); it is still the baseline for the next number" % t)
        return out
    if gf:
        mine = [x for x in gf if x[1] in confirmed]
        if mine:                                                   # a person confirmed it (versioning.yaml)
            v, t, i = max(mine, key=lambda x: x[0].key())
            out.update(tag=t, version=str(v), kind="grandfathered", commit=i["commit"], created_at=i.get("created_at"))
            notes.append("no vX.Y.Z release tag: the baseline is the grandfathered tag %s, read as %s, confirmed in "
                         "the registry's versioning.yaml" % (t, v))
            return out
        v, t, i = max(gf, key=lambda x: x[0].key())
        out.update(tag=t, version=str(v), kind="grandfathered", commit=i["commit"], created_at=i.get("created_at"))
        notes.append("no vX.Y.Z release tag: the baseline is the grandfathered tag %s, read as %s (C10 keeps "
                     "existing tags as they are)" % (t, v))
        newest = max(gf, key=lambda x: x[2].get("created_at") or "")
        if newest[1] != t:
            # review item 4: the highest version is not the newest tag (tidelink: v1.0 in May, v0.90 in August).
            # Which one is the release line decides the bump and whether C9's 1.0.0 gate applies, so no next
            # version is computed until a person confirms the baseline.
            out["ambiguous"] = True
            notes.append("the newest version-like tag by date is %s (%s), not %s: the baseline is ambiguous, so "
                         "there is no next version until a person confirms one (versioning.yaml "
                         "grandfathered[].baseline: true)" % (newest[1], (newest[2].get("created_at") or "")[:10], t))
    return out
