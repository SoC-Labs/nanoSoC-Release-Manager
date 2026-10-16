"""Composition across a superproject, the run set of a bump, and which host can run what (interfaces.md section 26).

    nodes_of(walk_docs, root) -> {tree path: Node}       the tree a walk resolved ("" is the root)
    composed(nodes, reader) -> Composition                 every tree's file and the configuration's composed set
    plan_bump(now, after, moved, reader) -> [PlanRow]      what a move of `moved` paths re-runs, on both sides
    host_check(cap, tools, site) -> [reason]               [] when a host's site data can run the capability
    hosts_for(cap, tools, sites) -> [host id]

**Composition.** The configuration's composed set is its root file's capabilities, plus, for each `compose` entry
`{path: P, run: [ids]}` of a file, those ids from P's own file read at the commit the tree pins P; recursively (a
composed tree's own `compose` is followed, paths relative to it). Each item keeps its OWNER (the tree path of the
file it came from), so it runs in that tree's directory with that tree's `env`, and its siblings resolve to where
the SUPERPROJECT pins them.

**Bump run set.** For a move of the pins at tree paths M (every path of one component) from A to B:

1. Own gates of each moved path P: the ids the nearest ancestor's `compose` names for P (and whose `when` includes
   on_bump); with no such entry, every capability of P's own file that is `gate: block`, not `manual`, and runs
   `on_bump`. Read at A for Now and at B for After.
2. Consumer gates: every capability of each ancestor of P (the root included) whose `affected_by` names P relative to
   that ancestor, a path above P, or the moved component's id; not `manual`, and run `on_bump`.

Both sides run every row; a row present on one side only is compared as `new` or `gone`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Node", "nodes_of", "Item", "Composition", "composed", "PlanRow", "plan_bump", "host_check", "hosts_for",
           "version_ge", "owner_label"]


@dataclass(frozen=True)
class Node:
    tree_path: str                  # "" = the configuration's root
    component: str | None
    commit: str | None
    host: str | None = None


def nodes_of(walk_docs: list[dict], root: Node) -> dict[str, Node]:
    """{tree path: Node} of a walk (pin_state documents) under `root`."""
    out = {"": root}
    for d in walk_docs:
        f = d["fact"]
        child = f.get("child") or {}
        for tp in f.get("tree_paths") or []:
            out.setdefault(tp, Node(tp, child.get("component"), child.get("commit"),
                                    (child.get("repo") or {}).get("host")))
    return out


def owner_label(owner: str) -> str:
    return owner or "."


def _join(a: str, b: str) -> str:
    return "/".join(x for x in (a.strip("/"), b.strip("/")) if x and x != ".")


def _ancestors(tp: str, nodes: dict[str, Node]) -> list[str]:
    """Tree paths above `tp` that hold a pin (nearest first), then the root."""
    parts = tp.split("/")
    out = [p for p in ("/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)) if p in nodes]
    return out + [""]


def _rel(tp: str, ancestor: str) -> str:
    return tp[len(ancestor) + 1:] if ancestor else tp


@dataclass
class Item:
    owner: str                      # tree path of the file the capability came from ("" = root)
    component: str | None
    commit: str | None
    cap: dict
    origin: str                     # "own" | "composed by <path>"

    @property
    def key(self) -> str:
        return "%s:%s" % (owner_label(self.owner), self.cap["id"])


@dataclass
class Composition:
    nodes: dict[str, Node]
    reads: dict[str, object]        # tree path -> manifest.Read
    items: list[Item] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def tree_of_component(self, component: str) -> str | None:
        hits = sorted(tp for tp, n in self.nodes.items() if n.component == component and tp)
        return hits[0] if hits else None


def composed(nodes: dict[str, Node], reader) -> Composition:
    """Every tree's manifest (read at its pinned commit) and the configuration's composed set (module docstring)."""
    reads = {tp: reader.at(n.component, n.commit, n.host) for tp, n in nodes.items()}
    comp = Composition(nodes, reads)
    seen: set[str] = set()

    def add(owner: str, cap: dict, origin: str) -> None:
        it = Item(owner, nodes[owner].component, nodes[owner].commit, cap, origin)
        if it.key not in seen:
            seen.add(it.key)
            comp.items.append(it)

    def follow(owner: str, depth: int = 0) -> None:
        r = reads.get(owner)
        if r is None or not r.ok or depth > 10:
            return
        for c in r.doc["compose"]:
            sub = _join(owner, c["path"])
            if sub not in nodes:
                comp.problems.append("%s composes %s, which this tree does not pin" % (owner_label(owner), sub))
                continue
            sr = reads[sub]
            if not sr.ok:
                comp.problems.append("%s composes %s, whose capabilities file is %s%s" % (
                    owner_label(owner), sub, sr.status, ": " + "; ".join(sr.problems) if sr.problems else ""))
                continue
            by_id = {x["id"]: x for x in sr.capabilities()}
            for cid in c["run"]:
                if cid not in by_id:
                    comp.problems.append("%s composes %s:%s, which %s@%s does not declare" % (
                        owner_label(owner), sub, cid, sr.component, (sr.commit or "")[:12]))
                    continue
                add(sub, dict(by_id[cid], when=list(c["when"])), "composed by %s" % owner_label(owner))
            follow(sub, depth + 1)

    root = reads.get("")
    if root is not None and root.ok:
        for cap in root.capabilities():
            add("", cap, "own")
    follow("")
    return comp


@dataclass
class PlanRow:
    key: str
    owner: str
    id: str
    why: str
    now: Item | None
    after: Item | None

    def cap(self) -> dict:
        return (self.after or self.now).cap

    def as_dict(self) -> dict:
        c = self.cap()
        return {"key": self.key, "owner": owner_label(self.owner), "id": self.id, "why": self.why,
                "kind": c["kind"], "tier": c.get("tier"), "gate": c["gate"], "runtime": c["runtime"],
                "command": c["run"] if isinstance(c["run"], str) else " ".join(c["run"]),
                "tools": list(c["tools"]),
                "now_commit": self.now.commit if self.now else None,
                "after_commit": self.after.commit if self.after else None}


def _runs_on_bump(cap: dict) -> bool:
    return "on_bump" in (cap.get("when") or ["on_bump"]) and not cap.get("manual")


def _side(nodes: dict[str, Node], reads: dict, moved: list[str], component: str | None) -> dict[str, tuple]:
    """key -> (owner, cap, why) of one side's run set."""
    out: dict[str, tuple] = {}
    for p in moved:
        if p not in nodes:
            continue
        anc = _ancestors(p, nodes)
        named = None
        for a in anc:
            ra = reads.get(a)
            if ra is not None and ra.ok:
                entry = next((c for c in ra.doc["compose"] if _join(a, c["path"]) == p), None)
                if entry is not None:
                    named = (a, entry)
                    break
        rp = reads.get(p)
        own = rp.capabilities() if rp is not None and rp.ok else []
        if named is not None:
            a, entry = named
            if "on_bump" in entry["when"]:
                by_id = {c["id"]: c for c in own}
                for cid in entry["run"]:
                    if cid in by_id and not by_id[cid].get("manual"):
                        out.setdefault("%s:%s" % (p, cid), (p, by_id[cid], "own gate of %s, composed by %s"
                                                            % (p, owner_label(a))))
        else:
            for c in own:
                if c["gate"] == "block" and _runs_on_bump(c):
                    out.setdefault("%s:%s" % (p, c["id"]), (p, c, "own gate of %s" % p))
        for a in anc:
            ra = reads.get(a)
            if ra is None or not ra.ok:
                continue
            rel = _rel(p, a)
            for c in ra.capabilities():
                if not _runs_on_bump(c):
                    continue
                hit = next((x for x in c["affected_by"] if x.strip("/") == rel or rel.startswith(x.strip("/") + "/")
                            or (component and x == component)), None)
                if hit is not None:
                    out.setdefault("%s:%s" % (owner_label(a), c["id"]),
                                   (a, c, "%s's capability is affected_by %s" % (owner_label(a), hit)))
    return out


def plan_bump(now: Composition, after: Composition, moved: list[str], component: str | None) -> list[PlanRow]:
    """The run set of a move (module docstring), one row per capability, with its Now and After item."""
    a = _side(now.nodes, now.reads, moved, component)
    b = _side(after.nodes, after.reads, moved, component)
    rows = []
    for key in sorted(set(a) | set(b), key=lambda k: (k.count("/"), k)):
        na, nb = a.get(key), b.get(key)
        owner, cap, why = nb or na
        it = lambda side, nodes: Item(side[0], nodes[side[0]].component, nodes[side[0]].commit, side[1],
                                      side[2]) if side else None
        rows.append(PlanRow(key, owner, cap["id"], why, it(na, now.nodes), it(nb, after.nodes)))
    return rows


# ---------------------------------------------------------------------------------------------------------
# Hosts (registry tiers.yaml `hosts`: site data only)
# ---------------------------------------------------------------------------------------------------------
_PART = re.compile(r"[0-9]+|[A-Za-z]+")


def version_ge(have: str, need: str) -> bool:
    """`have` is at least `need`, part by part (numbers as numbers, words as words): 4.028 >= 4.028,
    T-2022.06-SP2 >= T-2022.06, 22.03-s005 >= 22.03. A word against a number compares as text."""
    ha, na = _PART.findall(str(have)), _PART.findall(str(need))
    for x, y in zip(ha, na):
        if x.isdigit() and y.isdigit():
            if int(x) != int(y):
                return int(x) > int(y)
        elif x != y:
            return x > y
    return len(ha) >= len(na)


_RUNTIME_NEEDS = ("regen:", "after:", "siblings", "lease:")


def host_check(cap: dict, tools: dict, site: dict) -> list[str]:
    """Why a host (its site data) cannot run `cap`; [] when it can. `tools` is the manifest's tool table."""
    out = []
    have = site.get("tools") or {}
    licences = set(site.get("licences") or [])
    for t in cap.get("tools") or []:
        decl = tools.get(t) or {}
        h = have.get(t)
        if h is None:
            out.append("no %s" % t)
            continue
        if decl.get("min") and not version_ge(h.get("version", ""), decl["min"]):
            out.append("%s %s is older than %s" % (t, h.get("version"), decl["min"]))
        if decl.get("licence") and decl["licence"] not in licences:
            out.append("no %s licence" % decl["licence"])
    seats = {(tools.get(t) or {}).get("seat") for t in cap.get("tools") or []} - {None}
    extra = set(site.get("needs") or [])
    for n in cap.get("needs") or []:
        if n.startswith(_RUNTIME_NEEDS):
            continue
        if n.endswith("-seat") and (n[:-5] in seats or n[:-5] in have):
            continue
        if n not in extra:
            out.append("cannot meet need %s" % n)
    return out


def hosts_for(cap: dict, tools: dict, sites: dict) -> list[str]:
    return sorted(h for h, s in (sites or {}).items() if not host_check(cap, tools, s or {}))
