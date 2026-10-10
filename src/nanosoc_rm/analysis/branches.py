"""CR8 (MVP S1): the branch inventory of one repo, from its local mirror only.

    branches(target, all_repos=False, stale_days=90, sync=False) -> Answer of branch_state observations
    delete_list(docs) -> the deletable rows (MERGED or STALE, no pin, no citation, no tag)

`target` is a catalogue component (or external) id, or one repo as `<host>:<path>`. A component is judged on
its canonical repo; `all_repos` adds its copies. Everything is read from `<data_dir>/mirrors/<component>.git`
(interfaces.md section 3) through the one git runner: no host is asked, and no mirror means CannotAnswer
(exit 2) unless `sync=True` fetched one first.

Per branch (every head of the repo's namespace except the default branch itself):

- `tip`, `last_commit_at` (committer date), `ahead`/`behind` the default branch (`rev-list --left-right --count`),
  `merged` (the tip is an ancestor of the default branch);
- `tags`: tags whose tip IS this tip (`for-each-ref --points-at`), `keep_tags` the keep/* subset;
- `pins`: registered pins (every pin of every stored walk, section 4 "Storage": never re-walked) of this
  component whose commit the tip reaches and the default branch does not: deleting the branch would orphan them;
- `cited`: the registry's structured citations of this component (defect benches, fixes, detector sources;
  gates.registry_citations) that THIS branch holds (CR-10, David 2026-10-10: "citations hold a branch only on
  branch-unique commits"). A citation c the default branch does not reach is held by at most one branch:
    1. none, when a tag of the repo reaches c (the delete list never touches a tag, so c stays reachable);
    2. else, when exactly one branch reaches c (c is unique to it), that branch;
    3. else, one of the branches that reach c: one already kept by a tag on its tip or a pin if there is one,
       otherwise the one whose tip is fewest commits past c (`rev-list --count c..tip`), ties by branch name.
  Every other branch that also reaches c is not held by it. So deleting every deletable branch never loses a cited
  commit, and a cited commit shared by many branches no longer pins all of them. Pins are unchanged: a pin holds
  every branch that reaches it;
- `verdict`, first match: MERGED, PROTECTED (a tag on the tip), PINNED (a pin or citation), STALE (unmerged,
  nothing holds it, last commit older than `stale_days`), ACTIVE;
- `deletable`: MERGED or STALE with no pin, no citation and no tag. `delete_list` prints the git command for a
  PERSON to review and run. This module never deletes anything (A2: no write path; the git runner refuses push).

A commit a pin names that the mirror does not hold answers "not reached" (it cannot be in any branch here).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from nanosoc_rm import clock, db
from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.analysis._inputs import Answer, CannotAnswer, UsageProblem
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import GitResult

__all__ = ["branches", "delete_list", "delete_commands", "VERDICTS", "DEFAULT_STALE_DAYS"]

VERDICTS = ("MERGED", "PROTECTED", "PINNED", "STALE", "ACTIVE")
DEFAULT_STALE_DAYS = 90


def _resolve_target(reg: inp.Registry, target: str) -> tuple[inp.Component, list[dict]]:
    """(component, the repos to judge: canonical first). `host:path` names one repo of a catalogued component."""
    if ":" in target:
        host, _, path = target.partition(":")
        for c in reg.components() + [reg.component(e["id"]) for e in reg.catalogue.get("external", [])]:
            for r in c.repos:
                if r["host"] == host and r["path"].casefold() == path.casefold():
                    return c, [r]
        raise UsageProblem("unknown repo %r: not a canonical or copy repo of any catalogue component" % target)
    return reg.component(target), []


def _refs(mirror: Path, prefix: str, kind: str, sources: list[dict]) -> list[tuple[str, str, str]]:
    """[(short name, sha, committer date iso-strict)] of `refs/r/<key>/<kind>/*`, with the read recorded."""
    base = "%s/%s" % (prefix, kind)
    r = inp.git(mirror, "for-each-ref", "--format=%(refname) %(objectname) %(committerdate:iso-strict)", base)
    sources.append(r.source())
    r.check()
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        name = parts[0][len(base) + 1:]
        out.append((name, parts[1], parts[2] if len(parts) > 2 else ""))
    return out


def _is_ancestor(mirror: Path, a: str, b: str, sources: list[dict] | None = None) -> bool:
    r = inp.git(mirror, "merge-base", "--is-ancestor", a, b)
    if sources is not None:
        sources.append(r.source())
    if r.returncode not in (0, 1):
        r.check()
    return r.returncode == 0


def _has_commit(mirror: Path, sha: str) -> bool:
    return inp.git(mirror, "cat-file", "-e", "%s^{commit}" % sha).ok


def _counts(mirror: Path, default_ref: str, tip: str, sources: list[dict]) -> tuple[int, int]:
    r: GitResult = inp.git(mirror, "rev-list", "--left-right", "--count", "%s...%s" % (default_ref, tip))
    sources.append(r.source())
    r.check()
    behind, ahead = (int(x) for x in r.stdout.split())
    return ahead, behind


def _utc(iso_strict: str) -> str:
    """git's %(committerdate:iso-strict) (`2026-09-29T15:04:11+01:00`) as a contract timestamp."""
    try:
        return clock.iso(dt.datetime.fromisoformat(iso_strict))
    except ValueError:
        return clock.iso()


def _anchors(cfg: Config, reg: inp.Registry, component: str) -> tuple[list[dict], list[dict]]:
    """(pins, citations) of this component: every stored walk's pins (distinct configuration, tree path, commit),
    and the registry's structured citations (file, pointer, commit)."""
    pins, seen = [], set()
    for doc in db.latest("pin_state", config=cfg):
        f = doc["fact"]
        if f["child"].get("component") != component or not f["child"].get("commit"):
            continue
        for tp in f["tree_paths"]:
            key = (f["configuration"], tp, f["child"]["commit"])
            if key not in seen:
                seen.add(key)
                pins.append({"configuration": key[0], "tree_path": tp, "commit": key[2]})
    from nanosoc_rm.t0.gates import registry_citations
    cited, _unreadable = registry_citations(reg.path)
    cites = [{"file": c["file"], "pointer": c["pointer"], "commit": c["commit"]} for c in cited
             if c["component"] == component]
    return pins, cites


def _reaching(mirror: Path, anchors: list[dict], default_ref: str, tip: str, held: dict[str, bool]) -> list[dict]:
    """The anchors whose commit the tip reaches and the default branch does not. `held` caches, per commit,
    whether the default branch (or nothing in the mirror) already holds it."""
    out = []
    for a in anchors:
        sha = a["commit"]
        if sha not in held:
            held[sha] = (not _has_commit(mirror, sha)) or _is_ancestor(mirror, sha, default_ref)
        if held[sha]:
            continue
        if _is_ancestor(mirror, sha, tip):
            out.append(dict(a))
    return out


def _tag_reaches(mirror: Path, prefix: str, sha: str, sources: list[dict]) -> bool:
    r = inp.git(mirror, "for-each-ref", "--count=1", "--contains", sha, "--format=%(refname)", "%s/tags" % prefix)
    sources.append(r.source())
    r.check()
    return bool(r.stdout.strip())


def _distance(mirror: Path, sha: str, tip: str) -> int:
    r = inp.git(mirror, "rev-list", "--count", "%s..%s" % (sha, tip))
    r.check()
    return int(r.stdout.strip() or 0)


def _cite_holders(mirror: Path, prefix: str, cites: list[dict], reach: dict[str, list[dict]],
                  kept: set[str], tips: dict[str, str], sources: list[dict]) -> dict[str, list[dict]]:
    """CR-10: branch -> the citations it holds. `reach` = branch -> the citations its tip reaches that the default
    branch does not; `kept` = branches already kept by a tag on the tip or a pin (module docstring, rules 1-3)."""
    out: dict[str, list[dict]] = {b: [] for b in reach}
    for c in cites:
        cand = sorted(b for b, cs in reach.items() if c in cs)
        if not cand or _tag_reaches(mirror, prefix, c["commit"], sources):
            continue
        if len(cand) == 1:
            holder = cand[0]
        else:
            pool = [b for b in cand if b in kept] or cand
            holder = min(pool, key=lambda b: (_distance(mirror, c["commit"], tips[b]), b))
        out[holder].append(dict(c))
    return out


def _verdict(merged: bool, tags: list[str], pins: list[dict], cited: list[dict], last: str, stale_days: int,
             now: dt.datetime) -> str:
    if merged:
        return "MERGED"
    if tags:
        return "PROTECTED"
    if pins or cited:
        return "PINNED"
    if (now - clock.parse(last)) > dt.timedelta(days=stale_days):
        return "STALE"
    return "ACTIVE"


def _repo_rows(cfg: Config, reg: inp.Registry, comp: inp.Component, repo: dict, stale_days: int,
               unanswered: list[str], now: dt.datetime) -> list[dict]:
    mirror = inp.mirror_path(cfg, comp.id)
    if not (mirror / "HEAD").is_file():
        raise CannotAnswer("no mirror of %s at %s: run `nanosoc-rm mirror sync --component %s` (or `branches --sync`)"
                           % (comp.id, mirror, comp.id))
    prefix = inp.ref_prefix(repo)
    base_sources: list[dict] = []
    heads = _refs(mirror, prefix, "heads", base_sources)
    if not heads:
        unanswered.append("%s:%s: the mirror holds no branch of this repo (never synced?)" % (repo["host"], repo["path"]))
        return []
    default, dsrc = inp.default_branch(cfg, mirror, repo)
    names = {n for n, _, _ in heads}
    if default is None or default not in names:
        guess = next((n for n in ("main", "master") if n in names), None)
        if guess is None:
            unanswered.append("%s:%s: default branch unknown (%s) and neither main nor master exists"
                              % (repo["host"], repo["path"], default or "never recorded"))
            return []
        base_sources.append({"kind": "derived", "command": "default branch of %s:%s not recorded by the mirror; %s assumed"
                             % (repo["host"], repo["path"], guess)})
        default = guess
    else:
        base_sources += dsrc
    default_ref = "%s/heads/%s" % (prefix, default)
    tags_at: dict[str, list[str]] = {}
    for name, sha, _ in _refs(mirror, prefix, "tags", base_sources):
        peeled = inp.git(mirror, "rev-parse", "%s^{commit}" % sha)
        tags_at.setdefault(peeled.stdout.strip() if peeled.ok else sha, []).append(name)
    pins, cites = _anchors(cfg, reg, comp.id)
    if pins or cites:
        base_sources.append({"kind": "derived", "command": "%d registered pin(s) of stored walks and %d registry citation(s) "
                             "of %s, each tested with merge-base --is-ancestor against the tip and %s"
                             % (len(pins), len(cites), comp.id, default)})
    held: dict[str, bool] = {}
    scope = "branch_state:%s:%s" % (repo["host"], repo["path"])
    snap = inp.new_snapshot(scope)
    rows = []
    for name, tip, date in sorted(heads):
        if name == default:
            continue
        sources = list(base_sources)
        ahead, behind = _counts(mirror, default_ref, tip, sources)
        merged = _is_ancestor(mirror, tip, default_ref, sources)
        tags = sorted(tags_at.get(tip, []))
        reach_p = _reaching(mirror, pins, default_ref, tip, held) if not merged else []
        reach_c = _reaching(mirror, cites, default_ref, tip, held) if not merged else []
        rows.append((name, tip, date, sources, ahead, behind, merged, tags, reach_p, reach_c))
    cite_sources: list[dict] = []
    holders = _cite_holders(mirror, prefix, cites, {r[0]: r[9] for r in rows},
                            {r[0] for r in rows if r[7] or r[8]}, {r[0]: r[1] for r in rows}, cite_sources)
    if cites:
        base_sources.append({"kind": "derived", "command": "CR-10: each registry citation off the default branch holds at "
                             "most one branch (none when a tag reaches it; else the one branch reaching it, or the nearest "
                             "branch already kept, or the nearest by rev-list --count)"})
    docs = []
    for name, tip, date, sources, ahead, behind, merged, tags, reach_p, _all_c in rows:
        reach_c = holders.get(name, [])
        if reach_c:
            sources = sources + cite_sources + base_sources[-1:]
        last = _utc(date)
        verdict = _verdict(merged, tags, reach_p, reach_c, last, stale_days, now)
        fact = {"component": comp.id, "repo": dict(repo), "branch": name, "tip": tip, "default_branch": default,
                "ahead": ahead, "behind": behind, "merged": merged, "last_commit_at": last, "tags": tags,
                "keep_tags": [t for t in tags if t.startswith("keep/")], "pins": reach_p, "cited": reach_c,
                "stale_days": stale_days, "verdict": verdict,
                "deletable": verdict in ("MERGED", "STALE") and not (tags or reach_p or reach_c)}
        docs.append({"schema": 1, "kind": "branch_state", "subject": "%s:%s" % (scope, name),
                     "observed_at": clock.iso(now), "sources": sources, "snapshot": dict(snap), "fact": fact})
    return docs


def branches(target: str, *, all_repos: bool = False, stale_days: int = DEFAULT_STALE_DAYS, sync: bool = False,
             config: Config | str | Path | None = None, store: bool = True) -> Answer:
    """branch_state observations of one repo (`host:path`) or of a component's canonical repo (every repo with
    `all_repos`), from the local mirror. `sync` fetches the component first (mirror.sync_component); otherwise a
    missing mirror is CannotAnswer. Each repo's rows are stored as one snapshot `branch_state:<host>:<path>`.
    UsageProblem for an unknown target."""
    cfg = inp.config_of(config)
    reg = inp.registry(cfg)
    comp, repos = _resolve_target(reg, target)
    if not repos:
        repos = list(comp.repos) if all_repos else [comp.repos[0]]
    if sync:
        from nanosoc_rm.mirror.sync import sync_component
        sync_component(comp.id, config=cfg)
    now = clock.now()
    out = Answer()
    for repo in repos:
        docs = _repo_rows(cfg, reg, comp, repo, stale_days, out.unanswered, now)
        if store and docs:
            inp.store_snapshot(docs[0]["snapshot"]["scope"], docs, cfg)
        out += docs
    return out


def delete_list(docs: list[dict]) -> list[dict]:
    """The rows a person may delete: MERGED or STALE with no pin, no citation and no tag (fact.deletable)."""
    return [d for d in docs if d["fact"]["deletable"] and not (d["fact"]["pins"] or d["fact"]["cited"] or d["fact"]["tags"])
            and d["fact"]["verdict"] in ("MERGED", "STALE")]


def delete_commands(docs: list[dict], remote: str = "origin") -> list[str]:
    """TEXT for a person: one `git push <remote> --delete <branch>` per deletable row, grouped under a comment
    naming the repo. The tool never runs these."""
    lines, last = [], None
    for d in delete_list(docs):
        f = d["fact"]
        key = (f["repo"]["host"], f["repo"]["path"])
        if key != last:
            lines.append("# %s:%s (%s), default %s" % (key[0], key[1], f["component"], f["default_branch"]))
            last = key
        lines.append("git push %s --delete %s   # %s, last commit %s" % (remote, f["branch"], f["verdict"], f["last_commit_at"][:10]))
    return lines
