"""Code and documentation quality (requirements Q1-Q6, CR-14).

One function per capability; the CLI (`commands.py`) and the web pages (`web.py`) are front ends over these.
Each returns one observation (kinds quality_density, quality_finding, readme_check, doc_score), stored per
(target, commit) when asked, so a reduction shows as a trend (Q5). Nothing here writes to a repository: the
tools read a component mirror at a ref (git ls-tree / cat-file / blame, no checkout) or a local directory, and
produce reports and unified-diff patches a person applies (Q5).

    open_source(target, ref=None, path=None, config=None) -> Source
    density(target, ...)  -> quality_density observation      (Q1)
    comments(target, ...) -> quality_finding observation      (Q2)   patch: comment_patch(source, obs)
    readme(target, ...)   -> readme_check observation         (Q3)   patch: readme.layout_patch(source)
    docs(target, ...)     -> doc_score observation            (Q4)   rewrite: rewrite.rewrite_sections
    trend(kind, target)   -> stored observations of one target, oldest first (Q5)

Targets: a catalogue component id (read from its mirror; `--ref` a branch, tag or sha, default its default
branch), a name in the service config's `[quality] paths` table, or any label with `path=DIR`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nanosoc_rm import clock
from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.quality.corpus import build
from nanosoc_rm.quality.settings import load_settings, quality_config
from nanosoc_rm.quality.source import GitSource, Source, SourceError, WorktreeSource

__all__ = ["KINDS", "QualityError", "UsageError", "open_source", "density", "comments", "readme", "docs",
           "observe", "trend", "stored", "comment_patch", "targets", "Source", "SourceError"]

KINDS = {"density": "quality_density", "comments": "quality_finding", "readme": "readme_check",
         "docs": "doc_score"}


class QualityError(RuntimeError):
    """The question cannot be answered (no mirror, unreadable path): exit 2."""


class UsageError(LookupError):
    """Unknown target or ref, bad option: exit 3."""


def _cfg(config: Config | str | os.PathLike | None, required: bool) -> Config | None:
    from nanosoc_rm.quality.settings import _config_or_none
    cfg = _config_or_none(config)
    if cfg is None and required:
        raise UsageError("no service config: set NANOSOC_RM_CONFIG, or give --path DIR")
    return cfg


def targets(config: Config | str | os.PathLike | None = None) -> list[dict]:
    """Every target the GUI offers: catalogue components (mirror) and `[quality] paths` entries (path)."""
    out = []
    cfg = _cfg(config, False)
    if cfg is None:
        return out
    for name, p in sorted((quality_config(cfg).get("paths") or {}).items()):
        out.append({"target": name, "origin": "path", "where": str(p)})
    try:
        from nanosoc_rm.analysis import _inputs as inp
        reg = inp.registry(cfg)
        for c in reg.components():
            mp = inp.mirror_path(cfg, c.id)
            out.append({"target": c.id, "origin": "mirror", "where": str(mp), "mirrored": (mp / "HEAD").is_file()})
    except Exception:                                   # no registry configured: paths only
        pass
    return out


def open_source(target: str | None, *, ref: str | None = None, path: str | os.PathLike | None = None,
                config: Config | str | os.PathLike | None = None) -> Source:
    """The repository snapshot to read: `path` (work tree, or `ref` in it), a `[quality] paths` name, or a
    catalogue component's mirror at `ref` (default: its default branch)."""
    if path is None and target:
        cfg = _cfg(config, False)
        named = (quality_config(cfg).get("paths") or {}) if cfg else {}
        if target in named:
            path = named[target]
    if path is not None:
        p = Path(path).expanduser()
        label = target or p.resolve().name
        if not p.is_dir():
            raise UsageError("%s is not a directory" % p)
        try:
            if ref:
                return GitSource.resolve(p, ref, label=label)
            return WorktreeSource(p, label=label)
        except SourceError as e:
            raise UsageError(str(e)) from None
    if not target:
        raise UsageError("give a TARGET (catalogue component) or --path DIR")
    cfg = _cfg(config, True)
    from nanosoc_rm.analysis import _inputs as inp
    try:
        comp = inp.registry(cfg).component(target)
    except inp.UsageProblem as e:
        raise UsageError(str(e)) from None
    except inp.CannotAnswer as e:
        raise QualityError(str(e)) from None
    except ConfigError:
        raise UsageError("unknown target %r: not a [quality] paths name, and no registry is configured to look up "
                         "components (give --path DIR)" % target) from None
    mirror = inp.mirror_path(cfg, comp.id)
    if not (mirror / "HEAD").is_file():
        raise QualityError("component %s has no mirror at %s: run `nanosoc-rm mirror sync --component %s` first"
                           % (comp.id, mirror, comp.id))
    sources: list[dict] = []
    if ref is None:
        repo = comp.repos[0]
        ref, srcs = inp.default_branch(cfg, mirror, repo)
        sources += srcs
        if ref is None:
            ref = "main"
    prefixes = tuple(inp.ref_prefix(r) + "/" for r in comp.repos)
    try:
        return GitSource.resolve(mirror, ref, label=comp.id, prefixes=prefixes, sources=sources)
    except SourceError as e:
        raise UsageError(str(e)) from None


def _subject_key(fact: dict) -> str:
    c = fact["commit"] or "worktree"
    return "%s:%s%s" % (fact["target"], c, "+worktree" if fact["worktree"] and fact["commit"] else "")


def observe(kind: str, source: Source, fact: dict, command: str) -> dict:
    """The observation envelope of one result (A3): sources are the git reads and the command that ran."""
    full = {"target": source.label, "commit": source.commit, "ref": source.ref, "worktree": source.worktree,
            **fact}
    if source.worktree and source.state:
        full["worktree_state"] = source.state
    seen, srcs = set(), []
    for s in [{"kind": "derived", "command": command}] + source.sources:
        k = (s["kind"], s["command"])
        if k not in seen:
            seen.add(k)
            srcs.append(s)
    return {"schema": 1, "kind": kind, "subject": "%s:%s" % (kind, _subject_key(full)),
            "observed_at": clock.iso(), "sources": srcs[:40], "fact": full}


def _store(obs: dict, config) -> None:
    from nanosoc_rm import db
    db.put(obs, config=_cfg(config, True))


def _run(kind: str, target, ref, path, config, store, thresholds, command, fn) -> tuple[dict, Source]:
    source = open_source(target, ref=ref, path=path, config=config)
    settings = load_settings(source.label, config=config if path is None or config else None, file=thresholds)
    fact = fn(source, settings)
    obs = observe(KINDS[kind], source, fact, command)
    if store:
        _store(obs, config)
    return obs, source


def density(target: str | None, *, ref=None, path=None, config=None, store=False, thresholds=None,
            command: str = "nanosoc-rm quality density") -> dict:
    from nanosoc_rm.quality.counting import density as dens
    return _run("density", target, ref, path, config, store, thresholds, command,
                lambda s, st: dens(build(s, st)))[0]


def comments(target: str | None, *, ref=None, path=None, config=None, store=False, thresholds=None,
             command: str = "nanosoc-rm quality comments", with_source: bool = False):
    from nanosoc_rm.quality.findings import find
    obs, src = _run("comments", target, ref, path, config, store, thresholds, command,
                    lambda s, st: find(build(s, st)))
    return (obs, src) if with_source else obs


def readme(target: str | None, *, ref=None, path=None, config=None, store=False, include_docs=False,
           command: str = "nanosoc-rm quality readme", with_source: bool = False):
    from nanosoc_rm.quality.readmes import check_readmes
    obs, src = _run("readme", target, ref, path, config, store, None, command,
                    lambda s, st: check_readmes(s, include_docs=include_docs))
    return (obs, src) if with_source else obs


def docs(target: str | None, *, ref=None, path=None, config=None, store=False, thresholds=None,
         command: str = "nanosoc-rm quality docs", with_source: bool = False):
    from nanosoc_rm.quality.prose import score_docs
    obs, src = _run("docs", target, ref, path, config, store, thresholds, command,
                    lambda s, st: score_docs(build(s, st)))
    return (obs, src) if with_source else obs


def comment_patch(source: Source, obs: dict) -> tuple[str, list[dict]]:
    from nanosoc_rm.quality.patch import patch_for
    return patch_for(source, obs["fact"]["findings"])


def stored(kind: str, target: str | None = None, *, config=None) -> list[dict]:
    """Every stored observation of `kind` (one per target and commit), oldest first."""
    from nanosoc_rm import db
    k = KINDS.get(kind, kind)
    prefix = "%s:%s:" % (k, target) if target else ""
    docs_ = db.latest(k, prefix, config=_cfg(config, True))
    return sorted(docs_, key=lambda d: d["observed_at"])


def trend(kind: str, target: str, *, config=None) -> list[dict[str, Any]]:
    """[{commit, observed_at, value}] for one target: the comment ratio, findings, mismatches or doc score."""
    rows = []
    for d in stored(kind, target, config=config):
        f = d["fact"]
        value = {"quality_density": lambda: f["totals"]["ratio"], "quality_finding": lambda: f["total"],
                 "readme_check": lambda: f["mismatch_count"],
                 "doc_score": lambda: f["score"] if f["words"] else None}[d["kind"]]()   # no words: no point
        rows.append({"commit": f["commit"], "worktree": f["worktree"], "observed_at": d["observed_at"],
                     "value": value, "subject": d["subject"]})
    return rows
