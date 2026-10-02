"""B6: compare a component's copies on the two hosts (interfaces.md section 5).

    compare_hosts(component_id) -> [host_compare observation, one per copy]; stored as one snapshot

Counts come from `rev-list` in the component's ONE mirror, which holds every repo of the component under its
own namespace, so both hosts' commits are in one object store (README section 7 fact 4: one host's compare
API cannot see the other host's commits):

    canonical_only = git rev-list --count <copy head ref>..<canonical head ref>
    copy_only      = git rev-list --count <canonical head ref>..<copy head ref>

Relation: SAME 0/0; AHEAD n/0 (the canonical has commits the copy lacks); BEHIND 0/n; DIVERGED n/m; no merge
base UNRELATED (counts null); either side unreadable UNKNOWN (counts null). A side is unreadable when its
latest mirror_status is not ok, or its default-branch ref is absent from the mirror; its head is then null,
and its default branch too when that is unknown. The two default branches are compared even when their names
differ; `default_branch_mismatch` says so.
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm import db
from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.config import Config

__all__ = ["compare_hosts", "RELATIONS"]

RELATIONS = ("SAME", "AHEAD", "BEHIND", "DIVERGED", "UNRELATED", "UNKNOWN")


def _side(cfg: Config, mirror: Path, repo: dict) -> dict:
    """{repo, default_branch, head, ref, readable, why, sources, time} for one side of a compare."""
    sources: list[dict] = []
    status = inp.mirror_status(cfg, repo)
    branch, bsrc = inp.default_branch(cfg, mirror, repo)
    sources += bsrc
    side = {"repo": repo, "default_branch": branch, "head": None, "ref": None, "readable": False, "why": None,
            "sources": sources,
            "time": (status["fact"]["last_success_at"] or status["observed_at"]) if status else None}
    if status is not None and not status["fact"]["ok"]:
        side["why"] = "latest mirror_status of %s:%s is not ok (%s)" % (repo["host"], repo["path"],
                                                                        status["fact"]["error"] or "no error text")
        sources.append({"kind": "derived", "command": "stored %s" % status["subject"]})
        return side
    if not mirror.is_dir():
        side["why"] = "no mirror %s" % mirror
        return side
    if branch is None:
        side["why"] = "default branch of %s:%s is unknown" % (repo["host"], repo["path"])
        return side
    ref = "%s/heads/%s" % (inp.ref_prefix(repo), branch)
    r = inp.git(mirror, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    sources.append(r.source())
    if not r.ok or not r.stdout.strip():
        side["why"] = "default-branch ref %s is absent from the mirror" % ref
        return side
    side.update(head=r.stdout.strip(), ref=ref, readable=True)
    return side


def _count(mirror: Path, a: str, b: str) -> tuple[int | None, dict]:
    r = inp.git(mirror, "rev-list", "--count", "%s..%s" % (a, b))
    return (int(r.stdout.strip()) if r.ok and r.stdout.strip().isdigit() else None), r.source()


def _row(cfg: Config, component_id: str, mirror: Path, canonical: dict, copy: dict) -> dict:
    sources = canonical["sources"] + copy["sources"]
    relation, c_only, k_only = "UNKNOWN", None, None
    if canonical["readable"] and copy["readable"]:
        mb = inp.git(mirror, "merge-base", canonical["ref"], copy["ref"])
        sources.append(mb.source())
        if mb.returncode == 1 and not mb.stdout.strip():
            relation = "UNRELATED"
        elif mb.ok:
            c_only, s1 = _count(mirror, copy["ref"], canonical["ref"])
            k_only, s2 = _count(mirror, canonical["ref"], copy["ref"])
            sources += [s1, s2]
            if c_only is None or k_only is None:
                c_only = k_only = None                     # a count that failed is not a count: UNKNOWN
            else:
                relation = ("SAME" if c_only == 0 and k_only == 0 else "AHEAD" if k_only == 0 else
                            "BEHIND" if c_only == 0 else "DIVERGED")
    names = (canonical["default_branch"], copy["default_branch"])
    fact = {
        "component": component_id,
        "canonical": {k: canonical[k] for k in ("repo", "default_branch", "head")},
        "copy": {k: copy[k] for k in ("repo", "default_branch", "head")},
        "relation": relation, "canonical_only": c_only, "copy_only": k_only,
        "default_branch_mismatch": bool(all(names) and names[0] != names[1]),
    }
    for why in (canonical["why"], copy["why"]):
        if why:
            sources.append({"kind": "derived", "command": "unreadable: %s" % why})
    seen, unique = set(), []
    for s in sources:
        if (s["kind"], s["command"]) not in seen:
            seen.add((s["kind"], s["command"]))
            unique.append(s)
    return {"schema": 1, "kind": "host_compare",
            "subject": "host_compare:%s:%s:%s" % (component_id, copy["repo"]["host"], copy["repo"]["path"]),
            "observed_at": inp.oldest(canonical["time"], copy["time"]), "sources": unique, "fact": fact}


def compare_hosts(component_id: str, *, config: Config | str | Path | None = None, store: bool = True) -> list[dict]:
    """host_compare observations for every copy of `component_id`, stored as one snapshot of scope
    `host_compare:<component>` (an empty one when the component has no copy). UsageProblem for a component the
    catalogue does not list."""
    cfg = inp.config_of(config)
    comp = inp.registry(cfg).component(component_id)
    mirror = inp.mirror_path(cfg, component_id)
    docs = []
    if comp.copies:
        canonical = _side(cfg, mirror, comp.repos[0])
        for copy_repo in comp.copies:
            docs.append(_row(cfg, component_id, mirror, canonical, _side(cfg, mirror, copy_repo)))
    scope = "host_compare:%s" % component_id
    snap = inp.new_snapshot(scope)
    for d in docs:
        d["snapshot"] = dict(snap)
    if store:
        db.put_snapshot(scope, docs, config=cfg)
    return docs


def components_with_copies(*, config: Config | str | Path | None = None) -> list[str]:
    """Ids of every catalogue component that has at least one copy (the default set of `nanosoc-rm hosts`)."""
    cfg = inp.config_of(config)
    return [c.id for c in inp.registry(cfg).components() if c.copies]
