"""Rows and columns of a configuration's compatibility matrix (D3, B7), read from stored data only.

    rows(configuration, config, components=None) -> (rows, tree_docs)

One row per component in the configuration's latest stored walk (`walk CONFIG` stores it), in tree order. A
component pinned at more than one commit (B7 split) is one row; its pins are all listed, and a cell moves every
one of them (`overrides`). The columns, each a commit of the component's CANONICAL repo read from its mirror:

    pinned      the tree as registered: no override. One run serves every row (it is also the D9 baseline)
    in-range    the highest release tag inside the range the parent's component manifest
                (nanosoc-component.yaml `requires[]`, matched by id and gitlink path) accepts; "no range" when no
                parent manifest names one
    latest-tag  the highest release tag (versions.latest: vMAJOR.MINOR[.PATCH], no pre-release; keep/, archive/
                and freeze names are not versions)
    head        the head of the repo's default branch, as the host advertised it at the last mirror sync

A column with no commit ("no range", "no version tag", no mirror) is a cell with a reason and nothing to run. A
cell whose commit equals every pinned commit of the component is "same as pinned": it would run the pinned tree
again, so it shows the pinned result instead of running.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from nanosoc_rm import contracts
from nanosoc_rm.analysis import _inputs as I
from nanosoc_rm.matrix import versions as V

__all__ = ["COLUMNS", "PINNED", "Row", "Target", "rows", "overrides_for", "MANIFEST"]

PINNED, IN_RANGE, LATEST_TAG, HEAD = "pinned", "in-range", "latest-tag", "head"
COLUMNS = (PINNED, IN_RANGE, LATEST_TAG, HEAD)
MANIFEST = "nanosoc-component.yaml"


@dataclass
class Target:
    """What one column means for one row: a commit, or why there is none."""
    column: str
    commit: str | None
    ref: str | None = None            # tag or branch name
    why: str | None = None            # set when commit is None
    range: str | None = None          # the in-range column's range text
    sources: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"column": self.column, "commit": self.commit, "ref": self.ref, "why": self.why, "range": self.range}


@dataclass
class Row:
    component: str
    pins: list[dict]                  # [{tree_path, tree_paths, commit, parent_component, parent_commit, path}]
    split: bool
    catalogued: bool
    targets: dict[str, Target] = field(default_factory=dict)
    mirror_at: str | None = None      # the last successful sync of the canonical repo (mirror_status), if stored

    @property
    def tree_paths(self) -> list[str]:
        return [tp for p in self.pins for tp in p["tree_paths"]]

    @property
    def commits(self) -> set[str]:
        return {p["commit"] for p in self.pins if p["commit"]}


def overrides_for(row: Row, commit: str) -> dict[str, str]:
    """{tree path: commit} that moves every pin of the row's component to `commit`. A path below another moved
    path of the same component is left out (the outer override already replaces its subtree), and a path already
    at `commit` needs no override."""
    paths = sorted({tp for p in row.pins if p["commit"] != commit for tp in p["tree_paths"]})
    outer = [tp for tp in paths if not any(tp.startswith(o + "/") for o in paths if o != tp)]
    return {tp: commit for tp in outer}


def _tags(mirror, repo: dict) -> tuple[dict[str, str], dict]:
    """{tag name: peeled commit} of one repo in its component mirror, and the source of the read."""
    prefix = I.ref_prefix(repo) + "/tags/"
    r = I.git(mirror, "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)", prefix)
    out = {}
    for line in r.stdout.splitlines() if r.ok else []:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(prefix):
            out[parts[0][len(prefix):]] = parts[2] if len(parts) > 2 and parts[2] else parts[1]
    return out, r.source()


def _manifest_range(cfg, reg: I.Registry, pin: dict, component: str) -> tuple[str | None, str | None, list[dict]]:
    """(range, why-none, sources) from the parent's component manifest at the parent's pinned commit."""
    parent = pin.get("parent_component")
    if not parent or not pin.get("parent_commit"):
        return None, "no range: the parent of %s is not a catalogued component" % pin["tree_path"], []
    try:
        mirror = I.mirror_path(cfg, parent)
    except Exception:                                             # noqa: BLE001 - reported as no range
        return None, "no range: no mirror of %s" % parent, []
    if not (mirror / "HEAD").is_file():
        return None, "no range: no mirror of %s" % parent, []
    commit = pin["parent_commit"]
    ls = I.git(mirror, "ls-tree", commit, "--", MANIFEST)
    srcs = [ls.source()]
    if not ls.ok:
        return None, "no range: %s@%s is not in its mirror" % (parent, commit[:7]), srcs
    if not ls.stdout.strip():
        return None, "no range: %s@%s has no %s" % (parent, commit[:7], MANIFEST), srcs
    show = I.git(mirror, "cat-file", "-p", "%s:%s" % (commit, MANIFEST))
    srcs.append(show.source())
    if not show.ok:
        return None, "no range: %s@%s:%s could not be read" % (parent, commit[:7], MANIFEST), srcs
    try:
        doc = contracts.load_yaml(show.stdout)
    except Exception as e:                                        # noqa: BLE001 - an unreadable manifest
        return None, "no range: %s@%s:%s is not YAML (%s)" % (parent, commit[:7], MANIFEST, e), srcs
    for req in (doc or {}).get("requires") or []:
        if isinstance(req, dict) and req.get("id") == component and str(req.get("path", "")).strip("/") == pin["path"]:
            return str(req.get("range") or "") or None, None, srcs
    return None, "no range: %s@%s:%s names no range for %s at %s" % (parent, commit[:7], MANIFEST, component,
                                                                     pin["path"]), srcs


def _targets(cfg, reg: I.Registry, row: Row, policy: V.VersionPolicy) -> dict[str, Target]:
    out = {PINNED: Target(PINNED, None, why=None)}
    if not row.catalogued:
        why = "%s is not a catalogued component: no mirror, no tags" % row.component
        return dict(out, **{c: Target(c, None, why=why) for c in COLUMNS[1:]})
    comp = reg.component(row.component)
    repo = comp.canonical or comp.repos[0]
    mirror = I.mirror_path(cfg, row.component)
    if not (mirror / "HEAD").is_file():
        why = "no mirror of %s: run `nanosoc-rm mirror sync --component %s`" % (row.component, row.component)
        return dict(out, **{c: Target(c, None, why=why) for c in COLUMNS[1:]})
    tags, tsrc = _tags(mirror, repo)
    # head
    branch, bsrcs = I.default_branch(cfg, mirror, repo)
    if branch:
        ref = "%s/heads/%s" % (I.ref_prefix(repo), branch)
        r = I.git(mirror, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        out[HEAD] = (Target(HEAD, r.stdout.strip(), ref=branch, sources=list(bsrcs) + [r.source()]) if r.ok
                     else Target(HEAD, None, why="default branch %s is not in the mirror" % branch,
                                 sources=list(bsrcs) + [r.source()]))
    else:
        out[HEAD] = Target(HEAD, None, why="default branch of %s:%s unknown: sync its mirror" % (
            repo["host"], repo["path"]), sources=list(bsrcs))
    # latest tag
    lt = policy.latest(tags)
    out[LATEST_TAG] = (Target(LATEST_TAG, tags[lt], ref=lt, sources=[tsrc]) if lt else
                       Target(LATEST_TAG, None, why="no version tag on %s:%s (%d tag(s), none vMAJOR.MINOR[.PATCH])"
                              % (repo["host"], repo["path"], len(tags)), sources=[tsrc]))
    # in range: every parent's manifest must name one; split pins with different ranges must all hold
    ranges, whys, srcs = [], [], [tsrc]
    for pin in row.pins:
        rng, why, s = _manifest_range(cfg, reg, pin, row.component)
        srcs += s
        (ranges.append(rng) if rng else whys.append(why))
    if whys or not ranges:
        out[IN_RANGE] = Target(IN_RANGE, None, why=whys[0] if whys else "no range", sources=srcs)
    else:
        text = ", ".join(dict.fromkeys(ranges))
        try:
            comps = [c for r in dict.fromkeys(ranges) for c in policy.parse_range(r)]
        except V.RangeError as e:
            out[IN_RANGE] = Target(IN_RANGE, None, why="range unreadable: %s" % e, range=text, sources=srcs)
        else:
            t = policy.latest_in_range(tags, comps)
            out[IN_RANGE] = (Target(IN_RANGE, tags[t], ref=t, range=text, sources=srcs) if t else
                             Target(IN_RANGE, None, why="no release tag in range %s" % text, range=text,
                                    sources=srcs))
    return out


def rows(configuration: str, cfg, components: Sequence[str] | None = None,
         policy: V.VersionPolicy = V.DEFAULT_POLICY) -> tuple[list[Row], list[dict]]:
    """(rows in tree order, the stored pin_state docs they came from). UsageProblem for an unknown configuration
    or a --component the tree does not hold; CannotAnswer when the configuration was never walked."""
    reg = I.registry(cfg)
    reg.configuration(configuration)                              # UsageProblem when unknown
    tree = I.stored_tree(configuration, cfg)
    by: dict[str, Row] = {}
    for d in tree:
        f = d["fact"]
        child = f["child"]
        comp = child.get("component")
        if not comp:
            continue                                              # an unknown repo: no catalogue entry, no row
        parent = f.get("parent") or {}
        pin = {"tree_path": f["tree_paths"][0], "tree_paths": list(f["tree_paths"]), "commit": child.get("commit"),
               "path": f.get("path") or f["tree_paths"][0].rsplit("/", 1)[-1],
               "parent_component": parent.get("component"), "parent_commit": parent.get("commit")}
        row = by.setdefault(comp, Row(comp, [], False, child.get("catalogued_as") in ("component", "external")))
        row.pins.append(pin)
        row.split = row.split or bool(f.get("split"))
    wanted = list(components or [])
    missing = [c for c in wanted if c not in by]
    if missing:
        raise I.UsageProblem("not in %s's stored tree: %s (components there: %s)" % (
            configuration, ", ".join(missing), ", ".join(sorted(by))))
    out = [r for r in by.values() if not wanted or r.component in wanted]
    for r in out:
        r.targets = _targets(cfg, reg, r, policy)
        st = I.mirror_status(cfg, (reg.component(r.component).canonical or reg.component(r.component).repos[0])) \
            if r.catalogued else None
        r.mirror_at = (st or {}).get("fact", {}).get("last_success_at") if st else None
    return out, tree


def root_commit(tree: list[dict], configuration_record: dict) -> str | None:
    """The configuration's root commit: the registered one (a frozen configuration), else the commit the stored
    walk resolved (the parent of its top-level pins)."""
    if configuration_record.get("commit"):
        return configuration_record["commit"]
    for d in tree:
        f = d["fact"]
        if f.get("depth") == 1 and (f.get("parent") or {}).get("commit"):
            return f["parent"]["commit"]
    return None


def pinned_commits(row: Row) -> list[dict]:
    return [{"tree_path": p["tree_path"], "commit": p["commit"]} for p in row.pins]


def same_as_pinned(row: Row, commit: str | None) -> bool:
    return bool(commit) and row.commits == {commit}


def iter_cells(rws: Iterable[Row], columns: Sequence[str]):
    """(row, column, target) for every non-pinned column of every row."""
    for r in rws:
        for c in columns:
            if c != PINNED:
                yield r, c, r.targets[c]
