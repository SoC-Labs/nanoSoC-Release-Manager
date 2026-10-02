"""B4: which configurations contain commit X, per host (interfaces.md section 6).

    contains(commit, component=None, configurations=None) -> [containment observation]; one snapshot per query

1. The prefix is resolved to ONE full SHA among the commit objects of the mirrors (the component's mirror, or
   every mirror when no component is given). Nothing, or several commits, or one commit in several component
   mirrors: UsageProblem (CLI exit 3), listing the candidates.
2. For each host where the component has a repo, X is proven to exist THERE:
   `git for-each-ref --count=1 --contains X refs/r/<key>/heads refs/r/<key>/tags`. An object that is in the
   shared mirror but reachable from no head or tag of that host does not count.
3. For each configuration (None = every registered one) and each pin of the component in its latest stored
   tree (every split pin, every tree path), the pin commit P is proven the same way, then
   `git merge-base --is-ancestor X P` decides. If either proof fails, the pin is `contains: null` with the
   reason: an unprovable commit is never read as "not contained".
4. One row per (configuration, host where the component has a repo), `absent` included. The answer follows
   from the pins: yes all true, no all false, partial both, unknown any null, absent no pins.
5. A decided row's `sources` carry the existence proofs of both commits and the merge-base command (A3).

A configuration that was never walked has no stored tree, so it has no row (never `absent`): it is named in the
result's `unanswered` and the CLI exits 2 after printing the rows of the configurations that were walked. When
none of the asked configurations was walked: CannotAnswer (exit 2). A prefix found in no mirror while some of
the searched components have no mirror yet is also CannotAnswer (exit 2): "not there" is unproven. Exit 3 is
kept for a prefix that resolves to nothing, or to several commits, among mirrors that all exist.
"""
from __future__ import annotations

import re
from pathlib import Path

from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.analysis._inputs import Answer, CannotAnswer, UsageProblem
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import run_git

__all__ = ["contains", "resolve_commit", "ANSWERS"]

ANSWERS = ("yes", "partial", "no", "absent", "unknown")
_HEX = re.compile(r"^[0-9a-f]{4,40}$")


def _commits_in(mirror: Path, prefix: str) -> tuple[list[str], dict]:
    """Full SHAs of the commit objects in `mirror` that start with `prefix`, and the command that listed them."""
    if len(prefix) == 40:
        r = inp.git(mirror, "cat-file", "-t", prefix)
        return ([prefix] if r.ok and r.stdout.strip() == "commit" else []), r.source()
    r = inp.git(mirror, "rev-parse", "--disambiguate=%s" % prefix)
    objs = [line.strip() for line in r.stdout.splitlines() if line.strip()] if r.ok else []
    if not objs:
        return [], r.source()
    t = run_git("cat-file", "--batch-check=%(objectname) %(objecttype)", cwd=mirror, input="\n".join(objs) + "\n")
    found = [line.split()[0] for line in t.stdout.splitlines() if line.endswith(" commit")] if t.ok else []
    return found, r.source()


def resolve_commit(prefix: str, component: str | None = None, *, config: Config | str | Path | None = None
                   ) -> tuple[str, str, list[dict]]:
    """(component id, full SHA, sources) for a commit prefix, searched in the component's mirror or, with no
    component, in every catalogued mirror. UsageProblem when it resolves to nothing or to several commits."""
    cfg = inp.config_of(config)
    reg = inp.registry(cfg)
    p = prefix.strip().lower()
    if not _HEX.match(p):
        raise UsageProblem("%r is not a commit id or prefix (4 to 40 hex characters)" % prefix)
    if component is not None:
        ids = [reg.component(component).id]
    else:
        ids = [c["id"] for c in reg.catalogue["components"]] + [e["id"] for e in reg.catalogue.get("external", [])]
    found: dict[str, list[str]] = {}
    sources: list[dict] = []
    searched, missing = [], []
    for cid in ids:
        mirror = inp.mirror_path(cfg, cid)
        if not (mirror / "HEAD").is_file():
            missing.append(cid)
            continue
        searched.append(cid)
        shas, src = _commits_in(mirror, p)
        if shas:
            sources.append(src)
        for sha in shas:
            found.setdefault(sha, []).append(cid)
    if not found:
        if missing:                                         # not found where we looked; not looked everywhere
            raise CannotAnswer("%s resolves to no commit in %s, but %s not been mirrored yet (%s): run "
                               "`nanosoc-rm mirror sync` first" % (
                                   prefix, ("the %d mirror(s) that exist" % len(searched)) if searched else "any mirror",
                                   ("%s has" % missing[0]) if len(missing) == 1 else ("%d components have" % len(missing)),
                                   ", ".join(missing[:8]) + (", ..." if len(missing) > 8 else "")))
        where = ("the %s mirror" % component) if component else ("%d mirror(s)" % len(searched))
        raise UsageProblem("%s resolves to no commit in %s" % (prefix, where))
    if len(found) > 1:
        raise UsageProblem("%s is ambiguous: %s" % (prefix, ", ".join(
            "%s (%s)" % (sha, ", ".join(cids)) for sha, cids in sorted(found.items()))))
    sha, cids = next(iter(found.items()))
    if len(cids) > 1:
        raise UsageProblem("%s is in the mirrors of %s: name one with --component" % (sha, ", ".join(cids)))
    return cids[0], sha, sources


class _Prover:
    """Existence proofs on one host, cached per (host, commit): reachable from a head or tag of that host."""

    def __init__(self, mirror: Path, comp: inp.Component):
        self.mirror, self.comp, self.cache = mirror, comp, {}

    def exists(self, host: str, sha: str) -> tuple[bool, dict | None]:
        if (host, sha) not in self.cache:
            if not self.mirror.is_dir():
                self.cache[(host, sha)] = (False, None)
            else:
                patterns = []
                for repo in self.comp.repos_on(host):
                    patterns += [inp.ref_prefix(repo) + "/heads", inp.ref_prefix(repo) + "/tags"]
                r = inp.git(self.mirror, "for-each-ref", "--count=1", "--contains", sha, "--format=%(refname)",
                            *patterns)
                self.cache[(host, sha)] = (bool(r.ok and r.stdout.strip()), r.source())
        return self.cache[(host, sha)]


def _row(cfg: Config, comp: inp.Component, sha: str, configuration: str, host: str, tree: list[dict],
         prover: _Prover, resolved_by: list[dict]) -> dict:
    sources: list[dict] = list(resolved_by)
    x_ok, x_src = prover.exists(host, sha)
    if x_src:
        sources.append(x_src)
    pins = []
    for fact in (d["fact"] for d in tree):
        child = fact["child"]
        if child["component"] != comp.id or not child["commit"]:
            continue
        p = child["commit"]
        p_ok, p_src = prover.exists(host, p)
        if p_src and p_src not in sources:
            sources.append(p_src)
        value, reason = None, None
        if not x_ok:
            reason = "%s is not reachable from any branch or tag of %s (%s)" % (sha[:12], host, "no mirror"
                                                                                 if x_src is None else "not proven")
        elif not p_ok:
            reason = "pin %s is not reachable from any branch or tag of %s" % (p[:12], host)
        else:
            mb = inp.git(prover.mirror, "merge-base", "--is-ancestor", sha, p)
            if mb.source() not in sources:
                sources.append(mb.source())
            if mb.returncode in (0, 1):
                value = mb.returncode == 0
            else:
                reason = "merge-base --is-ancestor %s %s failed (exit %s)" % (sha[:12], p[:12], mb.returncode)
        for path in fact["tree_paths"]:
            pins.append({"tree_path": path, "commit": p, "contains": value, "reason": reason})
    values = [p["contains"] for p in pins]
    answer = ("absent" if not values else "unknown" if None in values else
              "yes" if all(values) else "no" if not any(values) else "partial")
    snap = {d["snapshot"]["id"] for d in tree}
    sources.append({"kind": "derived", "command": "pins of %s in the latest stored walk of %s (%s)"
                    % (comp.id, configuration, ", ".join(sorted(snap)) or "an empty tree")})
    times = [d["observed_at"] for d in tree]
    for repo in comp.repos_on(host):
        st = inp.mirror_status(cfg, repo)
        if st:
            times.append(st["fact"]["last_success_at"] or st["observed_at"])
    return {"schema": 1, "kind": "containment",
            "subject": "containment:%s:%s:%s:%s" % (comp.id, sha, configuration, host),
            "observed_at": inp.oldest(*times), "sources": sources,
            "fact": {"query": {"component": comp.id, "commit": sha}, "configuration": configuration, "host": host,
                     "answer": answer, "pins": pins}}


def contains(commit: str, component: str | None = None, configurations: list[str] | None = None, *,
             config: Config | str | Path | None = None, store: bool = True) -> Answer:
    """containment observations, one per (configuration, host where the component has a repo), stored as one
    snapshot of scope `containment:<component>:<commit>`. See the module docstring. A configuration with no
    stored tree has no row and is named in `.unanswered`; CannotAnswer when none of them has one."""
    cfg = inp.config_of(config)
    reg = inp.registry(cfg)
    cid, sha, resolved_by = resolve_commit(commit, component, config=cfg)
    comp = reg.component(cid)
    if configurations is None:
        configurations = [c["id"] for c in reg.configurations()]
    else:
        for c in configurations:
            reg.configuration(c)                            # UsageProblem for an unknown configuration
    trees, unanswered = {}, []
    for c in configurations:
        try:
            trees[c] = inp.stored_tree(c, cfg)
        except CannotAnswer as e:
            unanswered.append(str(e))
    if unanswered and not trees:
        raise CannotAnswer("; ".join(unanswered))
    prover = _Prover(inp.mirror_path(cfg, cid), comp)
    docs = Answer([_row(cfg, comp, sha, c, host, trees[c], prover, resolved_by)
                   for c in configurations if c in trees for host in comp.hosts()], unanswered)
    scope = "containment:%s:%s" % (cid, sha)
    snap = inp.new_snapshot(scope)
    for d in docs:
        d["snapshot"] = dict(snap)
    if store:
        inp.store_snapshot(scope, docs, cfg)
    return docs

