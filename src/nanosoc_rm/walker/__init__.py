"""Pin walker (interfaces.md section 4; README section 5; B2, B3, B7, B9).

    walk(configuration_id) -> list[dict]     one pin_state observation per pin; stored as one snapshot

1. The configuration resolves to a commit on its component's canonical host. A frozen configuration always uses its
   recorded `commit`; if its `ref` is a TAG that no longer peels to that commit, a `tag_moved` alert is raised (a
   branch that moves on raises nothing). A non-frozen configuration reads its branch or tag in the mirror.
2. At each (repo, commit) the walker reads that commit's OWN `.gitmodules` (`git cat-file -p <c>:.gitmodules`)
   and, per entry, the parent tree's entry (`git ls-tree <c> -- <path>`). Anything but a gitlink is pin-level state
   NO_GITLINK and is kept. `branch=` is recorded in `branch_field` and never used.
3. URLs are normalised against the config's host table (relative URLs against the parent's pinned URL; aliases to
   their component's repo). A local path is pin-level state LOCAL_URL. A URL naming no catalogue entry is
   `catalogued_as: unknown` and raises an `unknown_url` alert. So does a URL naming an `ignored` repo: a repo a
   registered tree pins is a dependency, so it is flagged to be classified, not hidden. The walker recurses into
   each (child repo, child
   commit) once, at depth <= 10, and records every tree path at which a pin occurs.
4. Every host holding a copy of the child gets a state (walker.states), judged only on that host's namespace.
5. `split` = the child component is pinned at more than one commit in the tree (B7); `effective` stays unknown.

A pin is identified by (parent repo, parent commit, path); `parent.repo` is the repo the parent was fetched from
(the one its own pinned URL names, after alias normalisation). The walk reads only the mirrors, plus the hosts'
read APIs for commits that no head or tag reaches (GET only).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.mirror.catalogue import Catalogue, load_catalogue
from nanosoc_rm.mirror.layout import alert_once, git_env, mirror_path, repo_key, run_in, snapshot_id
from nanosoc_rm.gitmodules import parse_gitmodules
from nanosoc_rm.walker.states import GREEN_STATES, RED_STATES, HostJudge, WalkError
from nanosoc_rm.walker.urls import classify, repo_url

__all__ = ["walk", "stored_pins", "pin_verdict", "UnknownConfiguration", "WalkError", "OverrideError", "MAX_DEPTH",
           "RED_STATES", "GREEN_STATES"]

MAX_DEPTH = 10


class UnknownConfiguration(KeyError):
    """No registered configuration has this id. CLI exit 3."""


class OverrideError(ValueError):
    """A pin override (`compat --pin`) names a tree path at which the tree holds no gitlink pin. CLI exit 3."""


@dataclass
class _Edge:
    path: str
    url: str
    url_kind: str
    branch_field: str | None
    entry_type: str
    commit: str | None
    component: str | None
    catalogued_as: str
    repo: dict | None
    resolved_url: str
    sources: list = field(default_factory=list)
    override_of: str | None = None              # The gitlink a pin override replaced at this tree path

    @property
    def child_key(self):
        return (self.component, self.repo["host"], self.repo["path"], self.commit) if self.repo else None


@dataclass
class _Node:
    component: str
    repo: dict
    commit: str
    url: str                                    # the absolute URL the node was pinned by (relative URLs resolve here)
    edges: list = field(default_factory=list)


def _read_node(config: Config, node: _Node, catalogue: Catalogue) -> None:
    """Fill node.edges from the node commit's own .gitmodules and tree.

    The root's commit is checked to be in its mirror before the walk starts. Below the root, a commit the mirror
    lacks has no green state on its pinned host (green and BRANCH_ONLY commits are reachable from a mirrored ref,
    a never-synced mirror stops the walk in HostJudge), so its pin is already red when its subtree is unread."""
    mirror = mirror_path(config, node.component)
    if not (mirror / "HEAD").is_file():
        return
    env = git_env(config, node.repo["host"])
    present = run_in(mirror, "cat-file", "-e", "%s^{commit}" % node.commit, env=env)
    if not present.ok:
        return
    has = run_in(mirror, "ls-tree", node.commit, "--", ".gitmodules", env=env)
    if not has.ok:
        # The commit is here but its tree is not readable (a corrupt or truncated pack): only a successful, empty
        # answer means "no .gitmodules". Reading a failure as "no submodules" would store fewer pins.
        raise WalkError("cannot read the tree of %s at %s: %s" % (node.component, node.commit,
                                                                  has.stderr.strip()[-300:]))
    if not has.stdout.strip():
        return
    blob = run_in(mirror, "cat-file", "-p", "%s:.gitmodules" % node.commit, env=env)
    if not blob.ok:
        raise WalkError("cannot read .gitmodules of %s at %s: %s" % (node.component, node.commit,
                                                                      blob.stderr.strip()[-300:]))
    hosts = config.get("hosts", {})
    for module in parse_gitmodules(blob.stdout):
        path, url = module.get("path"), module.get("url")
        if not path or not url:
            continue
        path = path.strip("/")
        tree = run_in(mirror, "ls-tree", "-z", node.commit, "--", path, env=env)
        if not tree.ok:
            raise WalkError("cannot read %s in the tree of %s at %s: %s" % (path, node.component, node.commit,
                                                                            tree.stderr.strip()[-300:]))
        entry_type, gitlink = "absent", None
        for rec in tree.stdout.split("\0"):
            meta, _, name = rec.partition("\t")
            if name == path and meta:
                _mode, otype, sha = meta.split()
                entry_type, gitlink = otype, (sha if otype == "commit" else None)
        kind, named, resolved = classify(url, hosts, node.url)
        component, catalogued_as, repo = None, "unknown", None
        if named is not None:
            component, role, repo = catalogue.normalise(*named)
            if component is None:
                repo = {"host": named[0], "path": named[1]}
            else:
                catalogued_as = "external" if role == "external" else "component"
                repo = dict(repo)
        branch = module.get("branch") or None
        node.edges.append(_Edge(path, url, kind, branch, entry_type, gitlink, component, catalogued_as,
                                repo if kind != "local" else None, resolved, [blob.source(), tree.source()]))


def _resolve_commit(config: Config, catalogue: Catalogue, cfg: dict, alerts: bool = True) -> tuple[str, list[dict]]:
    """(commit, sources) of a configuration (step 1)."""
    entry = catalogue.entry(cfg["component"])
    canon = entry.canonical
    mirror = mirror_path(config, entry.id)
    ns = "refs/r/%s" % repo_key(canon["host"], canon["path"])
    ref = cfg["ref"]
    target = "%s/heads/%s" % (ns, ref["branch"]) if "branch" in ref else "%s/tags/%s" % (ns, ref["tag"])
    have_mirror = (mirror / "HEAD").is_file()
    if cfg.get("frozen") and cfg.get("commit"):
        src = [{"kind": "registry", "command": "eligibility.yaml %s commit %s" % (cfg["id"], cfg["commit"])}]
        if "tag" in ref and have_mirror:
            refs = run_in(mirror, "for-each-ref", "--count=1", "--format=%(refname)", ns + "/")
            if refs.ok and refs.stdout.strip():
                peeled = run_in(mirror, "rev-parse", "--verify", "--quiet", target + "^{commit}")
                now = peeled.stdout.strip() if peeled.ok else None
                if now != cfg["commit"] and alerts:
                    alert_once("tag_moved", "pin_state:%s" % cfg["id"],
                               "configuration %s: tag %s on %s:%s %s, not the frozen commit %s"
                               % (cfg["id"], ref["tag"], canon["host"], canon["path"],
                                  "peels to %s" % now if now else "is gone", cfg["commit"]),
                               [peeled.source()], config=config)
        return cfg["commit"], src
    if not have_mirror:
        raise WalkError("configuration %s: component %s has no mirror yet (run `nanosoc-rm mirror sync`)"
                        % (cfg["id"], entry.id))
    r = run_in(mirror, "rev-parse", "--verify", "--quiet", target + "^{commit}")
    if not r.ok or not r.stdout.strip():
        raise WalkError("configuration %s: %s is not in the mirror of %s" % (cfg["id"], target, entry.id))
    return r.stdout.strip(), [r.source()]


def walk(configuration_id: str, *, config: Config | None = None, store: bool = True,
         overrides: dict[str, str] | None = None, alerts: bool = True,
         replaced: dict | None = None, at: str | None = None) -> list[dict]:
    """Walk one registered configuration's whole tree. Returns (and stores, as one snapshot) its pin_state
    observations, ordered by tree path. UnknownConfiguration (exit 3); WalkError when it cannot answer (exit 2).

    `overrides` ({tree path: sha}, `compat --pin`): the gitlink at that tree path is replaced by the sha,
    and the tree below it is walked from that commit's own .gitmodules. A what-if is never stored and raises no
    alert (`store` must be False). A tree path that holds no gitlink pin in the (overridden) tree is OverrideError.
    `alerts=False` raises no alert (tag_moved, unknown_url): a read that must leave the store untouched.
    `replaced`, when given, is filled with {tree path: the gitlink each override replaced}.
    `at`: walk the tree from this root commit instead of the one the configuration resolves to now (a
    release re-check of the commit it drafted). Like an override it is a what-if: never stored, no alert."""
    overrides = dict(overrides or {})
    if (overrides or at) and store:
        raise ValueError("a walk with pin overrides or a given root commit is a what-if: it is never stored")
    if overrides or at:
        alerts = False
    config = config if config is not None else load_config()
    catalogue = load_catalogue(config)
    try:
        cfg = catalogue.configuration(configuration_id)
    except KeyError:
        raise UnknownConfiguration(configuration_id) from None
    if not catalogue.has(cfg["component"]):
        raise WalkError("configuration %s names component %s, which is not in the catalogue"
                        % (configuration_id, cfg["component"]))
    started = clock.iso()
    root_entry = catalogue.entry(cfg["component"])
    if at:
        root_commit = at
        root_sources = [{"kind": "derived", "command": "walk %s at the given root commit %s" % (configuration_id, at)}]
    else:
        root_commit, root_sources = _resolve_commit(config, catalogue, cfg, alerts)
    canon = root_entry.canonical
    # The root commit must be in the mirror: otherwise its .gitmodules cannot be read, and an empty walk would be
    # stored and read as "no pins" (README section 4: a producer that cannot finish writes no snapshot).
    root_mirror = mirror_path(config, root_entry.id)
    if not (root_mirror / "HEAD").is_file():
        raise WalkError("configuration %s: component %s has no mirror yet (run `nanosoc-rm mirror sync`)"
                        % (configuration_id, root_entry.id))
    held = run_in(root_mirror, "cat-file", "-e", "%s^{commit}" % root_commit, env=git_env(config, canon["host"]))
    if not held.ok:
        raise WalkError("configuration %s: commit %s is not in the mirror of %s (not on %s:%s, or not synced since)"
                        % (configuration_id, root_commit, root_entry.id, canon["host"], canon["path"]))
    hosts = config.get("hosts", {})
    root_url = repo_url(hosts, canon["host"], canon["path"]) if canon["host"] in hosts else ""
    root = _Node(root_entry.id, dict(canon), root_commit, root_url)

    # Phase 1: the graph of (repo, commit) nodes, each read once (breadth first, depth <= MAX_DEPTH).
    nodes: dict = {(root.component, canon["host"], canon["path"], root_commit): root}
    depth_of = {id(root): 0}

    def grow(queue: list) -> None:
        while queue:
            node = queue.pop(0)
            _read_node(config, node, catalogue)
            for e in node.edges:
                key = e.child_key
                if (e.entry_type != "commit" or e.url_kind == "local" or e.component is None or key is None
                        or depth_of[id(node)] + 1 >= MAX_DEPTH or key in nodes):
                    continue
                child = _Node(e.component, e.repo, e.commit, e.resolved_url)
                nodes[key] = child
                depth_of[id(child)] = depth_of[id(node)] + 1
                queue.append(child)

    grow([root])

    # Phase 2: every tree path at which each pin occurs, and its smallest depth.
    pins: dict = {}
    applied: set = set()

    def visit(node: _Node, prefix: str, depth: int) -> None:
        for e in node.edges:
            tree_path = "%s/%s" % (prefix, e.path) if prefix else e.path
            if tree_path in overrides:
                if e.entry_type != "commit" or e.url_kind == "local":
                    raise OverrideError("--pin %s: the tree holds no gitlink pin there (%s)"
                                        % (tree_path, "local URL" if e.url_kind == "local" else e.entry_type))
                applied.add(tree_path)
                if replaced is not None:
                    replaced[tree_path] = e.commit
                e = replace(e, commit=overrides[tree_path], override_of=e.commit)
                key = e.child_key
                if key is not None and e.component is not None and key not in nodes and depth < MAX_DEPTH:
                    child = _Node(e.component, e.repo, e.commit, e.resolved_url)
                    nodes[key] = child
                    depth_of[id(child)] = depth
                    grow([child])
            # the child commit is part of the key: an override makes the same (parent, path) a different pin
            k = (node.repo["host"], node.repo["path"], node.commit, e.path, e.commit)
            rec = pins.setdefault(k, {"node": node, "edge": e, "tree_paths": [], "depth": depth})
            if tree_path not in rec["tree_paths"]:
                rec["tree_paths"].append(tree_path)
            rec["depth"] = min(rec["depth"], depth)
            child = nodes.get(e.child_key) if e.entry_type == "commit" and e.url_kind != "local" else None
            if child is not None and depth < MAX_DEPTH:
                visit(child, tree_path, depth + 1)

    visit(root, "", 1)
    missing = sorted(set(overrides) - applied)
    if missing:
        raise OverrideError("--pin %s: no pin at that tree path in %s's tree%s" % (
            ", ".join(missing), configuration_id, " (with the other overrides applied)" if len(overrides) > 1 else ""))

    # Phase 3: per-host states, split, documents.
    commits_of: dict[str, set] = {}
    for rec in pins.values():
        e = rec["edge"]
        if e.component and e.commit:
            commits_of.setdefault(e.component, set()).add(e.commit)
    scope = "pin_state:%s" % configuration_id
    snap = {"id": snapshot_id(scope), "scope": scope}
    judge = HostJudge(config)
    docs = []
    try:
        for rec in sorted(pins.values(), key=lambda r: r["tree_paths"][0]):
            docs.append(_pin_doc(config, catalogue, judge, configuration_id, rec, commits_of, snap, started,
                                 root_sources if rec["depth"] == 1 else []))
    finally:
        judge.close()
    for d in docs if alerts else []:
        f = d["fact"]
        if f["child"]["catalogued_as"] == "unknown" and f["url_kind"] != "local":
            alert_once("unknown_url", d["subject"], "%s pins %s, which names no catalogue component or external"
                       % (d["subject"], f["url"]), d["sources"][:1], config=config)
    if store:
        db.put_snapshot(scope, docs, config=config)
    return docs


def _pin_doc(config, catalogue, judge, configuration_id, rec, commits_of, snap, started, extra_sources) -> dict:
    node, e = rec["node"], rec["edge"]
    if e.url_kind == "local":
        state = "LOCAL_URL"
    elif e.entry_type != "commit":
        state = "NO_GITLINK"
    else:
        state = None
    hosts = {}
    if state is None and e.component is not None and e.repo is not None:
        entry = catalogue.entry(e.component)
        mirror = mirror_path(config, entry.id)
        for repo, role in entry.repos:
            if repo["host"] in hosts:
                continue
            hosts[repo["host"]] = judge.judge(mirror, entry, repo, role, repo["host"] == e.repo["host"], e.commit)
    times = [started] + [h["observed_at"] for h in hosts.values()]
    observed_at = min(times, key=clock.parse)
    fact = {
        "configuration": configuration_id,
        "parent": {"component": node.component, "repo": dict(node.repo), "commit": node.commit},
        "path": e.path, "url": e.url, "url_kind": e.url_kind, "branch_field": e.branch_field,
        "entry_type": e.entry_type, "state": state,
        "child": {"component": e.component, "catalogued_as": e.catalogued_as, "repo": e.repo, "commit": e.commit},
        "tree_paths": list(rec["tree_paths"]), "depth": rec["depth"],
        "split": len(commits_of.get(e.component, ())) > 1 if e.component else False,
        "effective": "unknown", "hosts": hosts,
    }
    return {"schema": 1, "kind": "pin_state", "subject": "pin_state:%s:%s" % (configuration_id, rec["tree_paths"][0]),
            "observed_at": observed_at, "sources": list(extra_sources) + list(e.sources), "snapshot": snap,
            "fact": fact}


def stored_pins(configuration_id: str, *, config: Config | None = None) -> list[dict]:
    """The latest stored walk of a configuration (empty when it was never walked)."""
    prefix = "pin_state:%s:" % configuration_id
    docs = db.latest("pin_state", prefix, config=config)
    return sorted((d for d in docs if d["fact"]["configuration"] == configuration_id),
                  key=lambda d: d["fact"]["tree_paths"][0])


def pin_verdict(fact: dict, frozen: bool) -> str:
    """"red", "green" or "unknown" for one pin, judged on its pinned host (section 4 CLI): LOCAL_URL, NO_GITLINK and
    every red state are red; BRANCH_ONLY is red in a frozen or silicon configuration, amber (not red) otherwise; a
    pin with no host state (an unknown URL) cannot be judged."""
    if fact["state"] in ("LOCAL_URL", "NO_GITLINK"):
        return "red"
    pinned = next((h for h in fact["hosts"].values() if h["pinned"]), None)
    if pinned is None:
        return "unknown"
    if pinned["state"] in RED_STATES or (pinned["state"] == "BRANCH_ONLY" and frozen):
        return "red"
    return "green"
