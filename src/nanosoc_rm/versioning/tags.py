"""C10 tag names, read from a component's mirror (never from a host: the mirror holds every tag of every repo).

    kind_of("v0.2.0") -> "release"        "candidate", "silicon", "keep", or None (outside the scheme)
    read_tags(mirror, repo) -> {name: {commit, annotated, created_at, moves: [commits]}}
    check(tags_by_repo, canonical_key, *, scheme_from, grandfathered) -> rows (one per tag name)

The scheme: releases vX.Y.Z (annotated, only on the canonical host, never moved or deleted); candidates
vX.Y.Z-rc.N; backport branches release/X.Y; silicon silicon/<chip>-rNpM; protective keep/<config>/<component>.
Existing tags are kept as they are (grandfathered): a tag created before `scheme_from`, or named in the registry's
`versioning.yaml` `grandfathered` list, is reported but never a finding. With no `scheme_from` (the scheme is not
adopted yet: Phase 3's gate is not met) every existing tag is grandfathered and the check is advisory.

Moves. The mirror keeps a reflog on every ref, never expired (interfaces.md section 3). A tag whose reflog peels to
more than one commit has MOVED; for a release tag that is always a finding (C10: never moved), grandfathered or not.
"""
from __future__ import annotations

import re
from pathlib import Path

from nanosoc_rm import clock
from nanosoc_rm.gitrun import run_git
from nanosoc_rm.versioning import semver

__all__ = ["KINDS", "kind_of", "branch_ok", "read_tags", "check", "SILICON_RX", "KEEP_RX", "BACKPORT_RX"]

KINDS = ("release", "candidate", "silicon", "keep")
SILICON_RX = re.compile(r"^silicon/[a-z0-9][a-z0-9-]*-r[0-9]+p[0-9]+$")
KEEP_RX = re.compile(r"^keep/[A-Za-z0-9@][A-Za-z0-9_.@-]*/[a-z0-9][a-z0-9_.-]*$")
BACKPORT_RX = re.compile(r"^release/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def kind_of(name: str) -> str | None:
    """The C10 kind of a tag name, or None when the name is outside the scheme."""
    v = semver.parse_tag(name)
    if v is not None:
        return "release" if v.pre is None else "candidate"
    if SILICON_RX.match(name):
        return "silicon"
    if KEEP_RX.match(name):
        return "keep"
    return None


def branch_ok(name: str) -> bool | None:
    """C10 backport branches: a branch under release/ must be release/X.Y. None for any other branch."""
    if not name.startswith("release/"):
        return None
    return bool(BACKPORT_RX.match(name))


def read_tags(mirror: Path, prefix: str, sources: list[dict]) -> dict[str, dict]:
    """Every tag of one repo in its mirror namespace `prefix` (refs/r/<key>): commit it peels to, annotated or
    lightweight, creation time, and every commit its reflog has peeled to (moves)."""
    fmt = "%(refname)%09%(objecttype)%09%(objectname)%09%(*objectname)%09%(creatordate:iso-strict)"
    r = run_git("for-each-ref", "--format=" + fmt, prefix + "/tags/", cwd=mirror, timeout=120)
    sources.append(r.source())
    out: dict[str, dict] = {}
    if not r.ok:
        return out
    for line in str(r.stdout).splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        ref, otype, oid, peeled, created = parts[:5]
        name = ref[len(prefix) + len("/tags/"):]
        out[name] = {"commit": peeled or oid, "annotated": otype == "tag", "created_at": _utc(created),
                     "ref": ref, "moves": []}
    for name, t in out.items():
        values = _reflog_values(Path(mirror), t["ref"])
        if len(set(values)) <= 1:
            continue
        sources.append({"kind": "derived", "command": "read the reflog of %s in the mirror (logs/%s)" % (t["ref"], t["ref"])})
        peeled_commits = []
        for v in reversed(values):                            # oldest first
            p = run_git("rev-parse", "--verify", "--quiet", v + "^{commit}", cwd=mirror, timeout=30)
            c = str(p.stdout).strip() if p.ok else v
            if not peeled_commits or peeled_commits[-1] != c:
                peeled_commits.append(c)
        if len(peeled_commits) > 1:
            t["moves"] = peeled_commits
    return out


def _reflog_values(mirror: Path, ref: str) -> list[str]:
    """Every value the ref has held, oldest first, from the mirror's reflog file. `git reflog show` walks commits
    and prints nothing for a ref to an annotated tag object, so the file is read directly (read-only)."""
    gd = mirror if (mirror / "HEAD").is_file() else mirror / ".git"
    try:
        text = (gd / "logs" / ref).read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and re.match(r"^[0-9a-f]{40}$", parts[1]) and set(parts[1]) != {"0"}:
            out.append(parts[1])
    return out


def _utc(iso: str) -> str | None:
    if not iso:
        return None
    try:
        return clock.iso(clock.parse(iso))
    except ValueError:
        return None


def check(tags_by_repo: dict[str, dict[str, dict]], canonical_key: str, *, scheme_from: str | None = None,
          grandfathered: set[str] | frozenset = frozenset()) -> list[dict]:
    """One row per tag name across the component's repos:
    {tag, kind, conforms, grandfathered, hosts: {key: commit}, annotated, created_at, moved, problems: [..]}.
    A problem is a finding unless the row is grandfathered (a moved release tag is a finding regardless)."""
    names = sorted({n for tags in tags_by_repo.values() for n in tags})
    rows = []
    for n in names:
        per = {k: tags[n] for k, tags in tags_by_repo.items() if n in tags}
        canon = per.get(canonical_key)
        first = canon or next(iter(per.values()))
        kind = kind_of(n)
        created = min((t["created_at"] for t in per.values() if t["created_at"]), default=None)
        gf = n in grandfathered or scheme_from is None or (created is not None and clock.parse(created)
                                                           < clock.parse(scheme_from))
        problems: list[str] = []
        if kind is None:
            problems.append("name outside the C10 scheme")
        if kind == "release":
            if canon is None:
                problems.append("release tag not on the canonical host")
            elif not canon["annotated"]:
                problems.append("release tag is lightweight, not annotated")
            commits = {t["commit"] for t in per.values()}
            if len(commits) > 1:
                problems.append("release tag names different commits on different hosts")
        moved = any(t["moves"] for t in per.values())
        finding = bool(problems) and not gf
        if moved and kind == "release":
            problems.append("release tag MOVED: %s" % " -> ".join(
                c[:12] for c in next(t["moves"] for t in per.values() if t["moves"])))
            finding = True
        rows.append({"tag": n, "kind": kind, "conforms": kind is not None, "grandfathered": gf,
                     "hosts": {k: t["commit"] for k, t in sorted(per.items())}, "annotated": first["annotated"],
                     "created_at": created, "moved": moved, "problems": problems, "finding": finding})
    return rows
