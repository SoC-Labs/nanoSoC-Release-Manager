"""The configuration workspace (spec section 3): one configuration as six tabs.

    GET /configurations/{id}                    Overview: the deciding gate, red gates first   (delegate)
    GET /configurations/{id}/tree               Submodule tree: nested pins, flags, the selected-pin aside
    GET /configurations/{id}/tree/node?path=    the selected-pin aside alone (htmx fragment)
    GET /configurations/{id}/whatif             What-if: result first (Now vs After), question, then the form
    GET /configurations/{id}/matrix             303 to the matrix plug-in's page, or a placeholder
    GET /configurations/{id}/silicon            Silicon protection, errata and waivers
    GET /configurations/{id}/history            jobs, stored compat results, CI T0 runs
    GET /compat?job=N | ?configuration=X        303 to the workspace (delegate; build-first fix 3)

Every route is a GET that reads stored observations, registry records, job records and the local mirrors' ref
lists (`views.component_refs`, one local for-each-ref). Nothing here walks, fetches, compares or queues work:
the What-if form posts to the existing `POST /compat/jobs`, the Run button to the existing Run route. This module
adds no POST route (it is not in `routers.WAVE_C_POSTS`).

The logic templates must not hold sits here, as plain functions with unit tests:

    tree(pins) -> Node                  the nested tree: a pin's parent is the longest tree-path prefix
    flags(node, ctx) -> [(text, tone)]  GATE FAIL, UNCOVERED, PINNED ×N, ssh URL, erratum id, FROZEN
    pin_headline(view) -> str           deciding gate, uncovered, red state, split, erratum, else "Fine."
    diff_gates(now, after) -> [row]     Now vs After per gate: same | better | worse | new | gone
    expectation(decision, comp, cid)    the one-line answer above the What-if form
    targets(config, cid, comp, pins)    where a component can move: default tip, newest tag, other pins, fixes
"""
from __future__ import annotations

import dataclasses
import hashlib
import urllib.parse
from typing import Any

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nanosoc_rm import clock, db, jobs
from nanosoc_rm.web import lines, registry_facts, shell, verdict as V, views
from nanosoc_rm.web.page import Page, env, human_age

__all__ = ["router", "configuration", "compat", "Node", "tree", "flags", "pin_headline", "diff_gates",
           "expectation", "targets", "TABS", "GATE_ROW_ORDER", "SHOW"]

TABS = (("overview", "Overview", ""), ("tree", "Submodule tree", "/tree"), ("whatif", "What-if", "/whatif"),
        ("matrix", "Matrix", "/matrix"), ("silicon", "Silicon", "/silicon"), ("history", "History", "/history"))
GATE_ROW_ORDER = ("FAIL", "INFRA", "UNVERIFIED", "WAIVED", "WARN", "NOT-RUN", "PASS")
SHOW = ("all", "attention", "splits", "uncovered")
SHOW_LABEL = {"all": "All pins", "attention": "Needs attention", "splits": "Pinned more than once",
              "uncovered": "Uncovered"}
# Now vs After: how bad a gate verdict is (a lower number is better).
SEVERITY = {"PASS": 0, "WAIVED": 1, "WARN": 1, "NOT-RUN": 2, "INFRA": 2, "UNVERIFIED": 3, "FAIL": 4}
TONE_ORDER = {"red": 0, "amber": 1, "ok": 2}
GATE_SHORT = {"ssh-pin": "ssh pin"}


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _base(cid: str) -> str:
    return "/configurations/%s" % _q(cid)


def _short_gate(gid: str) -> str:
    return gid[3:] if gid.startswith("t0.") else gid


def _span(a: str | None, b: str | None) -> str | None:
    """A measured duration between two stored times ("38 s", "2 min"), or None."""
    if not a or not b:
        return None
    try:
        s = (clock.parse(b) - clock.parse(a)).total_seconds()
    except (TypeError, ValueError):
        return None
    return human_age(max(0, s))


def _ago(at: str | None) -> str:
    if not at:
        return ""
    try:
        return human_age((clock.now() - clock.parse(at)).total_seconds()) + " ago"
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------------------------------------
# What one configuration's pages read (stored only)
# ---------------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class Ctx:
    cid: str
    record: dict | None
    frozen: bool
    pins: list[dict]
    docs: list[dict]                    # stored compat results, newest first
    doc: dict | None                    # the newest: the baseline
    decision: V.Decision
    causes: list[V.Cause]
    silicon: list[dict]
    errata: list[dict]
    defects: list[dict]
    splits: list[dict]
    waivers: list[dict]

    @property
    def rec(self) -> dict:
        return ((self.record or {}).get("fact") or {}).get("record") or {}

    @property
    def silicon_chip(self) -> str | None:
        s = self.rec.get("silicon")
        return ("silicon %s %s" % (s.get("chip", ""), s.get("revision", ""))).strip() if isinstance(s, dict) else None

    @property
    def tag(self) -> str | None:
        ref = self.rec.get("ref")
        return ref.get("tag") if isinstance(ref, dict) else None

    @property
    def commit(self) -> str | None:
        return self.rec.get("commit") or ((self.doc or {}).get("fact") or {}).get("commit")

    @property
    def root_commit(self) -> str | None:
        """The line's commit: the record's or the last check's, else the stored walk's root (a depth-1 pin's
        parent), so a branch line never checked still prints "branch main @ sha"."""
        if self.commit:
            return self.commit
        top = next((p for p in self.pins if p["fact"].get("depth") == 1), None)
        return ((top or {}).get("fact", {}).get("parent") or {}).get("commit")


def load(config: Any, cid: str) -> Ctx | None:
    """Everything the workspace tabs read for `cid`, or None when nothing is known about it."""
    if not views.is_valid_id(cid):
        return None
    record = views.configuration_record(config, cid)
    pins = views.pins(config, cid)
    docs = views.compat_results(config, cid)
    if record is None and not pins and not docs and not views.runs(config, cid):
        return None
    doc = docs[0] if docs else None
    return Ctx(cid=cid, record=record, frozen=views.is_frozen(record), pins=pins, docs=docs, doc=doc,
               decision=V.deciding(doc, record), causes=V.causes(doc, record),
               silicon=views.silicon_protection(config, cid), errata=views.errata(config, cid),
               defects=views.defects(config), splits=views.splits(pins), waivers=views.waivers_of(record))


# ---------------------------------------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class Node:
    path: str                           # tree path; "" for the root (the configuration itself)
    pin: dict | None                    # the pin_state observation (None for the root)
    children: list["Node"] = dataclasses.field(default_factory=list)
    depth: int = 0

    @property
    def component(self) -> str | None:
        return ((self.pin or {}).get("fact", {}).get("child") or {}).get("component")

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def tree(pins: list[dict]) -> Node:
    """The pins nested by tree path: each pin's parent is the pin with the longest tree path that is a prefix of
    its own (at a "/" boundary); a pin with none hangs off the root."""
    root = Node("", None)
    by: dict[str, Node] = {}
    for p in sorted(pins, key=lambda d: d["fact"]["tree_paths"][0]):
        path = p["fact"]["tree_paths"][0]
        parents = [k for k in by if path.startswith(k + "/")]
        parent = by[max(parents, key=len)] if parents else root
        node = Node(path, p, depth=parent.depth + 1)
        parent.children.append(node)
        by[path] = node
    return root


def _uncovered(ctx: Ctx) -> dict[str, dict]:
    """tree path -> the uncovered silicon row that names it."""
    out = {}
    for d in ctx.silicon:
        f = d["fact"]
        if not f.get("covered"):
            for tp in f.get("tree_paths") or []:
                out.setdefault(tp, d)
    return out


def _errata_by_component(ctx: Ctx, comps: set[str]) -> dict[str, list[str]]:
    """component -> ids of the configuration's open errata whose defect cites it."""
    defects = {d["fact"]["id"]: d["fact"].get("record") or {} for d in ctx.defects}
    out: dict[str, list[str]] = {}
    for e in ctx.errata:
        rec = e["fact"].get("record") or {}
        if rec.get("status") not in views.OPEN_ERRATUM_STATUSES:
            continue
        drec = defects.get(rec.get("defect"))
        if not drec:
            continue
        for c in comps:
            if views._cites_component(drec, c):
                out.setdefault(c, []).append(e["fact"]["id"])
    return out


@dataclasses.dataclass
class FlagCtx:
    """What `flags` reads, computed once per page."""
    deciding_paths: frozenset
    deciding_short: str
    uncovered: dict
    split_commits: dict                 # component -> number of distinct commits (split components only)
    errata: dict                        # component -> erratum ids
    frozen_tag: str | None
    frozen: bool


def flag_ctx(ctx: Ctx) -> FlagCtx:
    comps = {n.component for n in tree(ctx.pins).walk() if n.component}
    dec = ctx.decision
    short = GATE_SHORT.get(dec.kind) or (_short_gate(dec.gate) if dec.gate else "")
    return FlagCtx(deciding_paths=frozenset(dec.tree_paths if dec.gate else ()), deciding_short=short,
                   uncovered=_uncovered(ctx),
                   split_commits={s["component"]: len({p["fact"]["child"]["commit"] for p in s["pins"]})
                                  for s in ctx.splits},
                   errata=_errata_by_component(ctx, comps), frozen_tag=ctx.tag, frozen=ctx.frozen)


def flags(node: Node, fc: FlagCtx) -> list[tuple[str, str]]:
    """The flag chips of one tree row (spec 3.3), worst first. Every chip carries its text."""
    if node.pin is None:
        return [("FROZEN %s" % fc.frozen_tag if fc.frozen_tag else "FROZEN", "c-ink")] if fc.frozen else []
    f = node.pin["fact"]
    out = []
    if node.path in fc.deciding_paths:
        out.append(("GATE FAIL: %s" % fc.deciding_short, "c-fail"))
    if node.path in fc.uncovered:
        out.append(("UNCOVERED", "c-fail"))
    comp = node.component
    if comp in fc.split_commits:
        n = fc.split_commits[comp]
        out.append((lines.again_text(n) if n > 1 else "PINNED AGAIN", "c-split"))     # was "SPLIT xN"
    if f.get("url_kind") == "ssh":
        out.append(("ssh URL", "c-warn"))
    for eid in fc.errata.get(comp, []):
        out.append((eid, "c-warn"))
    return out


def _hosts(pin: dict, frozen: bool) -> list[tuple[str, str]]:
    f = pin["fact"]
    out = []
    if f.get("state"):
        out.append((f["state"], "c-fail"))
    for name, h in sorted((f.get("hosts") or {}).items()):
        st = h.get("state") or "UNKNOWN"
        t = shell.tone(st)
        if st in views.RED_STATES or (st == "BRANCH_ONLY" and frozen):
            t = "c-fail"
        out.append(("%s%s %s" % (name, "*" if h.get("pinned") else "", st), t))
    return out


def _lag(pin: dict) -> str:
    _, h = views.pinned_host(pin)
    lag = (h or {}).get("lag")
    return "-" if lag is None else str(lag)


def _tone(pin: dict | None, fl: list[tuple[str, str]], frozen: bool) -> str:
    if pin is None:
        return "ok"
    if views.pin_is_red(pin, frozen) or any(t == "c-fail" for _, t in fl):
        return "red"
    _, h = views.pinned_host(pin)
    if any(t in ("c-warn", "c-split") for _, t in fl) or (h or {}).get("state") == "BRANCH_ONLY":
        return "amber"
    return "ok"


def _node_id(path: str) -> str:
    return "n-" + hashlib.sha1(path.encode()).hexdigest()[:10]


def _not_reached_below(doc: dict | None, path: str, walked: set[str]) -> int:
    """Pins the compat result marks not-reached below `path` that the stored walk does not hold."""
    pins = ((doc or {}).get("fact") or {}).get("pins") or []
    return sum(1 for p in pins if p.get("tree_path", "").startswith(path + "/") and p["tree_path"] not in walked
               and "not-reached" in (p.get("items") or {}).values())


def tree_view(ctx: Ctx, *, node: str | None = None, show: str = "all", open_: str = "") -> dict:
    """The tree page's rows: nested (show=all) or flat and sorted red, amber, ok (any other filter)."""
    fc = flag_ctx(ctx)
    root = tree(ctx.pins)
    walked = {n.path for n in root.walk() if n.pin is not None}
    selected = node if node in walked else _default_node(ctx, root, fc)
    twins = set()
    sel_node = next((n for n in root.walk() if n.path == selected), None)
    if sel_node is not None and sel_node.component in fc.split_commits:
        twins = {n.path for n in root.walk() if n.component == sel_node.component and n.path != selected}
    base = _base(ctx.cid)
    views_by: dict[str, dict] = {}

    def view(n: Node) -> dict:
        fl = flags(n, fc)
        f = n.pin["fact"] if n.pin else {}
        child = f.get("child") or {}
        comp = child.get("component") or child.get("catalogued_as") or ""
        v = {"id": _node_id(n.path), "path": n.path, "pin": n.pin, "depth": n.depth,
             "comp": comp if comp and comp != n.path.rsplit("/", 1)[-1] else "",
             "component": child.get("component"), "commit": (child.get("commit") or "")[:7],
             "hosts": _hosts(n.pin, ctx.frozen) if n.pin else [], "flags": fl, "lag": _lag(n.pin) if n.pin else "-",
             "tone": _tone(n.pin, fl, ctx.frozen), "selected": n.path == selected, "twin": n.path in twins,
             "href": "%s/tree?node=%s" % (base, _q(n.path)),
             "hx": "%s/tree/node?path=%s" % (base, _q(n.path)),
             "below": _not_reached_below(ctx.doc, n.path, walked) if n.pin else 0,
             "children": [], "open": False, "split": child.get("component") in fc.split_commits}
        views_by[n.path] = v
        v["children"] = [view(c) for c in n.children]
        return v
    top = [view(c) for c in root.children]
    # open: the ancestors of the selected node and of red nodes; ?open=all|none overrides
    red = [p for p, v in views_by.items() if v["tone"] == "red"]
    keep_open = {k for p in red + ([selected] if selected else []) for k in views_by if p.startswith(k + "/")}
    for p, v in views_by.items():
        v["open"] = bool(v["children"]) and (open_ == "all" or (open_ != "none" and p in keep_open)
                                             or (open_ == "none" and selected and selected.startswith(p + "/")))
    counts = {"all": len(views_by), "attention": sum(1 for v in views_by.values() if v["tone"] != "ok"),
              "splits": sum(1 for v in views_by.values() if v["split"]),
              "uncovered": sum(1 for v in views_by.values() if any(t == "UNCOVERED" for t, _ in v["flags"]))}
    flat = None
    if show != "all":
        keep = {"attention": lambda v: v["tone"] != "ok", "splits": lambda v: v["split"],
                "uncovered": lambda v: any(t == "UNCOVERED" for t, _ in v["flags"])}[show]
        flat = sorted((v for v in views_by.values() if keep(v)), key=lambda v: (TONE_ORDER[v["tone"]], v["path"]))
    root_flags = flags(root, fc)
    return {"rows": top, "flat": flat, "counts": counts, "selected": selected, "show": show, "open": open_,
            "root": {"label": ctx.cid, "commit": (ctx.commit or "")[:7], "flags": root_flags,
                     "line": lines.describe(ctx.rec, ctx.root_commit)},
            "filters": [{"key": k, "label": SHOW_LABEL[k], "count": counts[k], "on": k == show,
                         "href": "%s/tree?show=%s" % (base, k)} for k in SHOW],
            "expand": "%s/tree?open=all%s" % (base, "&node=%s" % _q(selected) if selected else ""),
            "collapse": "%s/tree?open=none%s" % (base, "&node=%s" % _q(selected) if selected else "")}


def _default_node(ctx: Ctx, root: Node, fc: FlagCtx) -> str | None:
    """The pin the aside shows when none is chosen: the deciding cause's, else the first red, else the first."""
    nodes = [n for n in root.walk() if n.pin is not None]
    for n in nodes:
        if n.path in fc.deciding_paths:
            return n.path
    for n in nodes:
        if _tone(n.pin, flags(n, fc), ctx.frozen) == "red":
            return n.path
    return nodes[0].path if nodes else None


def pin_headline(v: dict, ctx: Ctx) -> str:
    """The aside's one sentence (spec 3.3): deciding gate, uncovered, red state, split, erratum, else fine."""
    f = v["pin"]["fact"]
    texts = [t for t, _ in v["flags"]]
    _, h = views.pinned_host(v["pin"])
    state = f.get("state") or (h or {}).get("state") or "UNKNOWN"
    if any(t.startswith("GATE FAIL") for t in texts):
        return "This pin holds the deciding gate: %s." % ctx.decision.label
    if "UNCOVERED" in texts:
        return "Silicon commit %s has no keep tag. It is lost if its branch is deleted." % v["commit"]
    if v["tone"] == "red":
        return "%s on the pinned host." % state
    split = next((t for t in texts if t.startswith("PINNED")), None)
    if split:
        n = split.split(" ×", 1)[1] if " ×" in split else "more than one"
        return "%s is pinned at %s commits in this tree." % (v["component"], n)
    erratum = next((t for t, tone in v["flags"] if tone == "c-warn" and t != "ssh URL"), None)
    if erratum:
        return "Erratum %s cites %s." % (erratum, v["component"])
    return "Fine. %s" % state


def pin_aside(config: Any, ctx: Ctx, path: str | None) -> dict | None:
    """The selected-pin aside: headline, hosts, lag, silicon coverage, split twins, notes, next step."""
    v = _selected_row(tree_view(ctx, node=path))
    if v is None:
        return None
    f = v["pin"]["fact"]
    comp = v["component"] or ""
    base = _base(ctx.cid)
    cover = [d for d in ctx.silicon if v["path"] in (d["fact"].get("tree_paths") or [])]
    covering = sorted({t for d in cover for t in d["fact"].get("covering_tags") or []})
    uncovered = [d for d in cover if not d["fact"].get("covered")]
    nxt, code, action, notes = _aside_next(config, ctx, v, uncovered)
    if f.get("url_kind") == "ssh":
        notes.append("Pinned by an ssh URL: an anonymous clone cannot fetch it.")
    if v["below"]:
        notes.append("%d trees below this pin were never reached: the walk could not fetch it." % v["below"])
    return {"v": v, "headline": pin_headline(v, ctx), "lag": v["lag"], "hosts": v["hosts"],
            **_aside_cover(ctx, covering, uncovered),
            "twins": _aside_twins(ctx, v, comp, base),
            "notes": notes, "next": nxt, "code": code, "action": action,
            # Test this bump on a pin row (the capabilities page queues it for an executor)
            "test_bump": None if ctx.frozen else _test_bump_href(ctx.cid, f["tree_paths"][0])}


def _aside_twins(ctx: Ctx, v: dict, comp: str, base: str) -> list[dict]:
    """The other pins of a split component in this tree."""
    twins = [p for p in ctx.pins if (p["fact"].get("child") or {}).get("component") == comp and comp
             and p["fact"]["tree_paths"][0] != v["path"]] if v["split"] else []
    return [{"path": t["fact"]["tree_paths"][0], "commit": t["fact"]["child"]["commit"][:7], "doc": t,
             "href": "%s/tree?node=%s" % (base, _q(t["fact"]["tree_paths"][0]))} for t in twins]


def _selected_row(tv: dict) -> dict | None:
    """The tree row whose path is the view's selected one, searched depth first."""
    stack = list(tv["rows"])
    while stack:
        x = stack.pop()
        if x["path"] == tv["selected"]:
            return x
        stack.extend(x["children"])
    return None


def _aside_next(config: Any, ctx: Ctx, v: dict, uncovered: list[dict]) -> tuple[str, str | None, dict | None,
                                                                                    list[str]]:
    """The aside's next step: (sentence, code, action, notes), by the first of deciding gate, uncovered silicon,
    split pin, else the component."""
    if any(t.startswith("GATE FAIL") for t, _ in v["flags"]):
        return _aside_gate_next(config, ctx, v["component"] or "") + ([],)
    comp = v["component"] or ""
    base = _base(ctx.cid)
    if uncovered:
        commit = v["pin"]["fact"]["child"]["commit"]
        code = "git tag keep/%s/%s %s\ngit push origin keep/%s/%s" % (ctx.cid, comp, commit, ctx.cid, comp)
        notes = ["UNREADABLE: run it from a checkout that can read this repo."] \
            if any(d["fact"].get("state") == "UNREADABLE" for d in uncovered) else []
        return ("Tag the commit so no branch delete can lose it.", code,
                {"label": "Open the Silicon tab", "href": "%s/silicon" % base}, notes)
    if v["split"]:
        return ("Decide whether the pins of %s should meet at one commit." % comp, None,
                {"label": "What-if for %s" % comp, "href": "%s/whatif?component=%s" % (base, _q(comp))}, [])
    nxt = "Nothing to do here." if v["tone"] == "ok" else "Read the component's host copies and branches."
    return nxt, None, ({"label": "Open component %s" % comp, "href": "/components/%s" % _q(comp)} if comp
                       else None), []


def _aside_gate_next(config: Any, ctx: Ctx, comp: str) -> tuple[str, str | None, dict]:
    """The next step when the selected pin holds the deciding gate."""
    base = _base(ctx.cid)
    act = V.next_action(ctx.decision, ctx.cid, V.https_url(config, comp) if ctx.decision.kind == "ssh-pin"
                        else None, ctx.doc)
    nxt = act["label"] if act else "Open the Overview"
    action = {"label": "What-if for %s" % comp, "href": "%s/whatif?component=%s" % (base, _q(comp))} \
        if ctx.decision.kind == "ssh-pin" else {"label": "Open the deciding gate",
                                                "href": "%s#gate-%s" % (base, ctx.decision.gate)}
    return nxt, (act or {}).get("code"), action


def _aside_cover(ctx: Ctx, covering: list[str], uncovered: list[dict]) -> dict:
    """The silicon-coverage line and its tone."""
    if covering and not uncovered:
        return {"cover": "covered by " + ", ".join(covering), "cover_tone": "c-pass"}
    if uncovered:
        return {"cover": "UNCOVERED", "cover_tone": "c-fail"}
    return {"cover": "not a silicon commit" if not ctx.frozen else "no row stored", "cover_tone": "c-neutral"}


def _test_bump_href(cid: str, tree_path: str) -> str | None:
    try:
        from nanosoc_rm.capabilities.web import bump_href
    except ImportError:                                          # the capabilities module is not built here
        return None
    return bump_href(cid, tree_path)


# ---------------------------------------------------------------------------------------------------------
# What-if
# ---------------------------------------------------------------------------------------------------------
def _gate_rows(doc: dict | None) -> dict[str, dict]:
    f = (doc or {}).get("fact") or {}
    out = {g["id"]: g for g in f.get("gates") or []}
    t = f.get("tier")
    if isinstance(t, dict) and t.get("tier"):
        out["tier.%s" % t["tier"]] = {"id": "tier.%s" % t["tier"], "verdict": t.get("verdict"),
                                      "detail": t.get("reason") or ""}
    return out


def diff_gates(now_doc: dict | None, after_doc: dict | None) -> list[dict]:
    """Now vs After per gate: [{gate, now, after, change, detail}], changed rows first, each group in the
    results' own gate order. change: same, better, worse, changed (another verdict of equal severity), new (no Now row) or gone (no After row)."""
    now, after = _gate_rows(now_doc), _gate_rows(after_doc)
    order = list(now) + [g for g in after if g not in now]
    rows = []
    for gid in order:
        a, b = now.get(gid), after.get(gid)
        nv, av = (a or {}).get("verdict"), (b or {}).get("verdict")
        if a is None:
            change = "new"
        elif b is None:
            change = "gone"
        elif nv == av:
            change = "same"
        elif SEVERITY.get(av, 2) == SEVERITY.get(nv, 2):
            change = "changed"
        else:
            change = "better" if SEVERITY.get(av, 2) < SEVERITY.get(nv, 2) else "worse"
        rows.append({"gate": gid, "now": nv or "-", "after": av or "-", "change": change,
                     "now_tone": shell.tone(nv), "after_tone": shell.tone(av),
                     "detail": (b or a or {}).get("detail") or ""})
    return sorted(rows, key=lambda r: r["change"] == "same")


CHANGE_TONE = {"same": "c-neutral", "better": "c-pass", "worse": "c-fail", "changed": "c-warn", "new": "c-infra",
               "gone": "c-notrun"}


def expectation(decision: V.Decision, component: str | None, cid: str, tree_paths: list[str] = ()) -> str:
    """One line on what moving `component` can change, from the stored decision alone (no run)."""
    if not component:
        return "Pick a component and where it moves to; the run compares every gate with the stored result."
    if decision.verdict == V.NO_RESULT:
        return "%s has no stored result yet, so there is nothing to compare with." % cid
    if decision.gate is None:
        return "%s has no red gate now. The run shows whether this move keeps it that way." % cid
    if decision.kind == "ssh-pin" and decision.component == component:
        return ("Moving %s alone will not turn %s green. Its ssh URL is the blocker, not its commit."
                % (component, cid))
    if set(tree_paths) & set(decision.tree_paths):
        return ("%s holds the deciding cause (%s). A different commit may clear it; the run will tell."
                % (component, decision.label))
    return ("%s is not where %s's deciding cause sits (%s). Expect that cause to stay."
            % (component, cid, decision.label))


def _matrix_targets(config: Any, cid: str, comp: str) -> list[dict]:
    """The matrix plug-in's stored cells for this component (in-range, latest-tag, head targets), when the
    plug-in is installed. Stored facts only: the matrix's own column resolution reads manifests from git."""
    try:
        from nanosoc_rm.matrix import cells as K
    except ModuleNotFoundError:
        return []
    out = []
    try:
        latest = K.latest_cells(cid, config)
    except Exception:                                            # noqa: BLE001 - a missing store: no targets
        return []
    for col, label in (("in-range", "Latest in range"), ("latest-tag", "Latest tag"), ("head", "Default tip")):
        d = latest.get((comp, col))
        t = ((d or {}).get("fact") or {}).get("target") or {}
        if t.get("commit"):
            out.append({"key": "matrix-%s" % col, "label": label, "ref": t.get("ref") or "", "commit": t["commit"],
                        "note": "from the matrix cell of %s" % _ago(d["fact"].get("finished_at"))})
    return out


def targets(config: Any, cid: str, comp: str, pins: list[dict], refs: list[dict] | None = None) -> list[dict]:
    """Where `comp` can move (spec 3.4 step 2): [{key, label, ref, commit, note}], no commit twice.
    The default branch tip and the newest tag come from the mirror's refs; then the other configurations' pins of
    the component, the commits that fix a defect citing it, and the matrix's stored targets."""
    refs = views.component_refs(config, comp) if refs is None else refs
    mine = [p for p in pins if (p["fact"].get("child") or {}).get("component") == comp]
    pinned = {p["fact"]["child"]["commit"] for p in mine}
    canon_host, default = _canonical_default(mine)
    out: list[dict] = []
    seen: set[str] = set(pinned)
    candidates = [(t["key"], t["label"], t["ref"], t["commit"], t["note"])
                  for t in _matrix_targets(config, cid, comp)]
    candidates += _ref_targets(refs, default, canon_host, pinned)
    candidates += _pin_targets(config, cid, comp)
    candidates += _fix_targets(config, comp)
    for key, label, ref, commit, note in candidates:
        if commit and commit not in seen:
            seen.add(commit)
            out.append({"key": key, "label": label, "ref": ref, "commit": commit, "note": note})
    return out


def _canonical_default(mine: list[dict]) -> tuple[str | None, str | None]:
    """The canonical host of these pins (else the first host met) and its default branch."""
    canon_host, default = None, None
    for p in mine:
        for host, h in (p["fact"].get("hosts") or {}).items():
            if h.get("role") == "canonical" or canon_host is None:
                canon_host, default = host, h.get("default_branch") or default
    return canon_host, default


def _ref_targets(refs: list[dict], default: str | None, canon_host: str | None, pinned: set[str]) -> list[tuple]:
    """The default branch tip, the latest version tag and the newest tag not pinned, from the mirror's refs."""
    out = _tip_target(refs, default, canon_host)
    tags = [r for r in refs if r["kind"] == "tag"]
    try:                                         # the matrix's latest-tag rule (vMAJOR.MINOR[.PATCH]); pure, no git
        from nanosoc_rm.matrix.versions import DEFAULT_POLICY
        latest = DEFAULT_POLICY.latest(r["name"] for r in tags)
    except ModuleNotFoundError:
        latest = None
    vt = next((r for r in tags if r["name"] == latest), None)
    if vt:
        out.append(("version-tag", "Latest version tag", vt["name"], vt["sha"],
                    "tagged %s on %s" % ((vt["date"] or "")[:10], vt["host"])))
    tag = next((r for r in tags if r["sha"] not in pinned), None)
    if tag:
        out.append(("tag", "Newest tag by date", tag["name"], tag["sha"],
                    "tagged %s on %s" % ((tag["date"] or "")[:10], tag["host"])))
    return out


def _tip_target(refs: list[dict], default: str | None, canon_host: str | None) -> list[tuple]:
    """The default branch's tip, on the canonical host when it has the branch."""
    tip = next((r for r in refs if r["kind"] == "branch" and r["name"] == default and r["host"] == canon_host),
               None) or next((r for r in refs if r["kind"] == "branch" and r["name"] == default), None)
    if not tip:
        return []
    return [("tip", "Default branch tip", "%s %s" % (tip["host"], tip["name"]), tip["sha"],
             "last commit %s" % (tip["date"] or "")[:10])]


def _pin_targets(config: Any, cid: str, comp: str) -> list[tuple]:
    """The commits the other configurations pin `comp` at."""
    out = []
    for d in db.latest("pin_state", "pin_state:", config=config):
        f = d["fact"]
        if f.get("configuration") != cid and (f.get("child") or {}).get("component") == comp:
            out.append(("pin-" + f["configuration"], "Pinned in %s" % f["configuration"], f["tree_paths"][0],
                        f["child"].get("commit"), "the commit %s uses" % f["configuration"]))
    return out


def _fix_targets(config: Any, comp: str) -> list[tuple]:
    """The commits that fix a defect form citing `comp`."""
    out = []
    for dd in views.defects(config):
        rec = dd["fact"].get("record") or {}
        for form in rec.get("forms") or []:
            out += [("fix-%s-%s" % (dd["fact"]["id"], form.get("id")), "Fix for %s" % dd["fact"]["id"],
                     "form %s" % form.get("id"), fx.get("commit"), rec.get("title") or "")
                    for fx in form.get("fixed_in") or [] if fx.get("component") == comp]
    return out


def _job_matching(config: Any, doc: dict | None) -> dict | None:
    """The local job whose result is this stored compat result (same nonce), if any."""
    nonce = ((doc or {}).get("fact") or {}).get("nonce")
    if not nonce:
        return None
    for j in views.safe_jobs(config, limit=200, kind="compat", configuration=doc["fact"]["configuration"]):
        if not j["params"].get("pins") and ((j.get("result") or {}).get("fact") or {}).get("nonce") == nonce:
            return j
    return None


# ---------------------------------------------------------------------------------------------------------
# Header, tabs, rendering
# ---------------------------------------------------------------------------------------------------------
def header(config: Any, ctx: Ctx, request: Request) -> dict:
    """The block every tab shows: chips, the deciding sentence, one primary action, stat tiles."""
    dec = ctx.decision
    v = dec.verdict
    chips = [("NOT CHECKED" if v == V.NO_RESULT else v, shell.tone(v))]
    # the commit is no longer a bare chip: it prints in the line above the chips, "<repo> ⑂ branch main @ sha"
    chips.append(("FROZEN" if ctx.frozen else "NOT FROZEN", "c-ink" if ctx.frozen else "c-neutral"))
    if ctx.silicon_chip:
        chips.append((ctx.silicon_chip, "c-ink"))
    https = V.https_url(config, dec.component) if dec.kind == "ssh-pin" and dec.component else None
    action = V.next_action(dec, ctx.cid, https, ctx.doc)
    f = (ctx.doc or {}).get("fact") or {}
    job = _job_matching(config, ctx.doc)
    tiles = []
    if ctx.doc:
        took = _span(f.get("started_at"), f.get("finished_at"))
        tiles.append({"label": "Last check", "value": _ago(f.get("finished_at")) or "-",
                      "sub": " · ".join(x for x in ("job %d" % job["id"] if job else "", "%s run" % took if took else "")
                                        if x) or "stored result"})
    else:
        tiles.append({"label": "Last check", "value": "never", "sub": "no stored result"})
    red = views.red_pins(ctx.pins, ctx.frozen)
    tiles.append({"label": "Pins", "value": str(len(ctx.pins)), "sub": "%d red on the pinned host" % len(red)
                  if ctx.pins else "no walk stored"})
    comps = {p["fact"]["child"]["component"] for p in ctx.pins if (p["fact"].get("child") or {}).get("component")}
    drifting, _, in_tree = views.drift(views.host_compares(config), comps)
    tiles.append({"label": "Host drift", "value": str(len(drifting)),
                  "sub": "of %d compared copies in the tree" % len(in_tree)})
    open_err = [e for e in ctx.errata if (e["fact"].get("record") or {}).get("status") in views.OPEN_ERRATUM_STATUSES]
    tiles.append({"label": "Errata", "value": str(len(open_err)), "sub": "open of %d" % len(ctx.errata)})
    if ctx.frozen:
        cov = sum(1 for d in ctx.silicon if d["fact"].get("covered"))
        tiles.append({"label": "Silicon protection", "value": "%d of %d" % (cov, len(ctx.silicon)), "sub": "covered"})
        unused = V.unused_waivers(ctx.doc, ctx.record) if ctx.doc else []
        tiles.append({"label": "Waivers", "value": str(len(ctx.waivers)),
                      "sub": "%d cover nothing" % len(unused) if ctx.doc else "no result to check them"})
    return {"chips": chips, "fresh": _freshness(config, ctx), "line": lines.describe(ctx.rec, ctx.root_commit),
            "sentence": dec.sentence if v != V.NO_RESULT else "%s has never been checked." % ctx.cid,
            "action": action, "tiles": tiles, "infra": dec.infra_text}


def _freshness(config: Any, ctx: Ctx) -> dict:
    """The configuration's freshness verdict as Home shows it ({word, href}), so the header names both axes: the
    last check (the chips) and how far its pins lag (this). "not scanned" when no freshness scan is stored."""
    from nanosoc_rm.freshness import view as fview
    root = (ctx.rec or {}).get("component") or ""
    href = "/?" + urllib.parse.urlencode([("v", "1"), ("o", "p:" + root), ("o", "c:" + ctx.cid),
                                          ("sel", "c:" + ctx.cid)])
    try:
        t = fview.tree(ctx.cid, config=config)
    except Exception:                                            # noqa: BLE001 - one unreadable store: no word
        t = None
    if t is None:
        return {"word": "not scanned", "href": href}
    tree = t["tree"]
    return {"word": "Frozen" if ctx.frozen or tree["frozen"] else tree["word"], "href": href}


def _has_matrix(request: Request) -> bool:
    return any(href.rstrip("/") == "/matrix" for _, href, _ in getattr(request.app.state.rm, "nav", ()))


def _tabs(request: Request, ctx: Ctx, current: str, config: Any) -> list[dict]:
    base = _base(ctx.cid)
    counts = {"tree": len(ctx.pins), "silicon": sum(1 for d in ctx.silicon if not d["fact"].get("covered")) or None,
              "history": len(views.safe_jobs(config, limit=200, configuration=ctx.cid)) or None}
    return [{"key": k, "label": label, "href": base + suffix, "count": counts.get(k)}
            for k, label, suffix in TABS if k != "matrix" or _has_matrix(request)]


def _crumbs(ctx: Ctx, tab: str) -> list[tuple[str, str | None]]:
    label = dict((k, label) for k, label, _ in TABS)[tab]
    return shell.crumbs([("Projects", "/"), ("SoC configurations", None), (ctx.cid, _base(ctx.cid)), (label, None)])


def _page(request: Request, ctx: Ctx, tab: str, template: str, problems: list[str] | None = None,
          status: int = 200, page_primary: bool = False, **kw: Any) -> HTMLResponse:
    """`page_primary`: the page body holds its own primary action, so the header's are shown as ghosts."""
    config = request.state.config
    head = header(config, ctx, request)
    # spec 3.1: one primary action per page. The next action (a GET link) wins over Run; the page's own wins over both.
    a = head["action"]
    head["primary"] = None if page_primary else ("action" if a and a.get("method") == "get" else "run")
    return request.app.state.rm.page(
        request, template, ctx.cid, status=status, problems=problems, crumbs=_crumbs(ctx, tab), c=ctx, tab=tab,
        doc_title=ctx.cid if tab == "overview" else "%s · %s" % (dict((k, x) for k, x, _ in TABS)[tab], ctx.cid),
        head=head, tabs_=_tabs(request, ctx, tab, config), base=_base(ctx.cid),
        pending=[j for j in views.safe_jobs(config, states=("queued", "running"), configuration=ctx.cid)
                 if j["kind"] == "compat" and not j["params"].get("pins")]
        + views.pending_runs(views.runs(config, ctx.cid)), **kw)


def _fragment(request: Request, template: str, **kw: Any) -> HTMLResponse:
    p = Page()
    config, ident = request.state.config, request.state.identity
    csrf = request.app.state.rm.csrf_for(ident, config)
    return HTMLResponse(env.get_template(template).render(page=p, fact=p.fact, csrf=csrf, **kw))


def _not_found(request: Request) -> HTMLResponse:
    return request.app.state.rm.page(request, "message.html", "Not found", status=404,
                                     message="No configuration is known here.", next_step=True)


def _job_ctx(config: Any, job: dict | None) -> dict:
    """What `_job.html` needs beside the job (the app's job fragment passes the same)."""
    if job is None:
        return {}
    stored = None
    if job["kind"] == "compat" and job.get("result") and not job["params"].get("pins"):
        hist = db.history(job["result"]["subject"], config=config)
        stored = hist[-1] if hist else None
    return {"job": job, "log": jobs.log_text(config, job["id"]), "position": jobs.queue_position(config, job["id"]),
            "stored": stored}


def _job_arg(config: Any, request: Request) -> dict | None:
    q = request.query_params.get("job") or ""
    return jobs.get(config, n) if (n := jobs.parse_id(q)) is not None else None


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def _gates_table(ctx: Ctx) -> list[dict]:
    rows = list(_gate_rows(ctx.doc).values())
    deciding = set(ctx.decision.same_cause_gates)
    rank = {v: i for i, v in enumerate(GATE_ROW_ORDER)}
    rows = sorted(rows, key=lambda g: (g["id"] not in deciding, rank.get(g.get("verdict"), len(rank))))
    return [dict(g, deciding=g["id"] in deciding, tone=shell.tone(g.get("verdict")), help=V.GATE_HELP.get(g["id"]))
            for g in rows]


def _waived_line(ctx: Ctx) -> str | None:
    """'Waived: g1, g2 by N waivers approved by X on D' for a frozen tree whose result waived gates."""
    gates = [g["id"] for g in _gate_rows(ctx.doc).values() if g.get("verdict") == "WAIVED"]
    if not gates or not ctx.waivers:
        return None
    who = sorted({str(w.get("approved_by")) for w in ctx.waivers if w.get("approved_by")})
    when = sorted({str(w.get("approved_on")) for w in ctx.waivers if w.get("approved_on")})
    return "Waived: %s by %d waivers approved by %s on %s." % (", ".join(gates), len(ctx.waivers),
                                                              ", ".join(who) or "?", ", ".join(when) or "?")


def configuration(request: Request, id: str) -> Response | None:
    """GET /configurations/{id}: the Overview tab. None (the older page answers) for an unknown id."""
    config = request.state.config
    problems = registry_facts.refresh(config)
    ctx = load(config, id)
    if ctx is None:
        return None
    gate_detail = (_gate_rows(ctx.doc).get(ctx.decision.gate) or {}).get("detail") if ctx.decision.gate else None
    return _page(request, ctx, "overview", "cfg_overview.html", problems=problems, gates=_gates_table(ctx),
                 gate_detail=gate_detail, separate=ctx.causes[1:], waived_line=_waived_line(ctx),
                 runs=views.runs(config, ctx.cid), pin_rows=[{"doc": p, "hosts": _hosts(p, ctx.frozen)} for p in ctx.pins],
                 **_job_ctx(config, _job_arg(config, request)))


def compat(request: Request) -> Response | None:
    """GET /compat: `?job=N` of a compat job and `?configuration=X` 303 to the What-if tab (build-first fix 3);
    anything else is the older page (the configuration picker)."""
    config = request.state.config
    q = request.query_params
    if (n := jobs.parse_id(q.get("job"))) is not None:
        job = jobs.get(config, n)
        cid = (job or {}).get("params", {}).get("configuration") if job and job["kind"] == "compat" else None
        if cid and views.is_valid_id(cid):
            return RedirectResponse("%s/whatif?job=%d" % (_base(cid), job["id"]), 303)
        return None
    cid = q.get("configuration") or ""
    if cid and views.is_valid_id(cid):
        return RedirectResponse("%s/whatif" % _base(cid), 303)
    return None


def tree_page(request: Request, id: str) -> Response:
    config = request.state.config
    problems = registry_facts.refresh(config)
    ctx = load(config, id)
    if ctx is None:
        return _not_found(request)
    q = request.query_params
    show = q.get("show") if q.get("show") in SHOW else "all"
    tv = tree_view(ctx, node=q.get("node") or None, show=show, open_=q.get("open") or "")
    return _page(request, ctx, "tree", "cfg_tree.html", problems=problems, t=tv,
                 aside=pin_aside(config, ctx, tv["selected"]))


def tree_node(request: Request, id: str) -> Response:
    """The selected-pin aside alone (htmx target #pin-aside)."""
    config = request.state.config
    ctx = load(config, id)
    if ctx is None:
        return HTMLResponse('<aside id="pin-aside" class="panel" aria-label="Selected pin"><p class="empty">No '
                            'configuration is known here.</p></aside>', 404)
    return _fragment(request, "cfg_pin_aside.html", c=ctx, base=_base(ctx.cid),
                     aside=pin_aside(config, ctx, request.query_params.get("path") or None))


def whatif_page(request: Request, id: str) -> Response:
    config = request.state.config
    problems = registry_facts.refresh(config)
    ctx = load(config, id)
    if ctx is None:
        return _not_found(request)
    q = request.query_params
    job, problems = _whatif_job(config, request, ctx, problems)
    result = _whatif_result(config, ctx, job)
    # the question: which component, which target, which of its paths
    tree_comps = _tree_components(ctx)
    comp, problems = _whatif_component(ctx, q, job, tree_comps, problems)
    comp_rows = _whatif_comp_rows(config, ctx, q, tree_comps, comp)
    refs = views.component_refs(config, comp) if comp else []
    tg = targets(config, ctx.cid, comp, ctx.pins, refs) if comp else []
    target, target_label = _whatif_target(q, tg, refs)
    paths = [p["fact"]["tree_paths"][0] for p in tree_comps.get(comp, [])]
    chosen = q.getlist("path") if q.get("paths_set") else paths
    chosen = [p for p in paths if p in chosen]
    tiers, problems = _whatif_tiers(config, ctx, problems)
    act, steps = _whatif_steps(config, ctx)
    refs_by_kind = {"tag": [r for r in refs if r["kind"] == "tag"], "branch": [r for r in refs if r["kind"] == "branch"]}
    return _page(request, ctx, "whatif", "cfg_whatif.html", problems=problems, result=result,
                 page_primary=bool(comp and target and request.app.state.rm.local_routes),
                 change_tone=CHANGE_TONE, comp=comp, comp_rows=comp_rows, flt=q.get("q") or "", targets=tg,
                 target=target, target_label=target_label, refs=refs_by_kind, n_refs=len(refs), paths=paths,
                 chosen=chosen, tiers=tiers, steps=steps, code=(act or {}).get("code"),
                 tier_blocked=views.tiers_this_host_could_not_run(views.compat_results(config, ctx.cid)[:10]),
                 **_whatif_command(ctx, comp, paths, chosen, target),
                 pin_refs=_whatif_pin_refs(config, ctx) if q.get("all") else None,
                 **(_job_ctx(config, job) if job else {"job": None}))


def _whatif_job(config: Any, request: Request, ctx: Ctx, problems: list[str]) -> tuple[dict | None, list[str]]:
    """The ?job= the page shows, or None with a problem when it is not a compat job of this configuration."""
    job = _job_arg(config, request)
    if job is not None and (job["kind"] != "compat" or job["params"].get("configuration") != ctx.cid):
        problems = problems + ["job %d is not a compat job of %s" % (job["id"], ctx.cid)]
        job = None
    return job, problems


def _whatif_result(config: Any, ctx: Ctx, job: dict | None) -> dict | None:
    """The finished job's result set against the stored one, gate by gate; None while there is none to show."""
    if job is None or not job.get("result") or job["state"] in ("queued", "running"):
        return None
    after = job["result"]
    now = ctx.doc
    rows = diff_gates(now, after)
    changed = sum(1 for r in rows if r["change"] != "same")
    return {"rows": rows, "changed": changed, "unchanged": len(rows) - changed, "now": now, "after": after,
            "now_line": _whatif_now_line(config, now) if now else None,
            "moved": (after.get("fact") or {}).get("overrides") or [],
            "infra": V.deciding(after, ctx.record).infra_text}


def _whatif_now_line(config: Any, now: dict) -> str:
    now_job = _job_matching(config, now)
    return "stored result %s%s, %s" % (now["fact"]["nonce"][:12], ", job %d" % now_job["id"] if now_job else "",
                                       _ago(now["fact"].get("finished_at")))


def _tree_components(ctx: Ctx) -> dict[str, list[dict]]:
    """The stored tree's pins grouped by the component they pin, in pin order."""
    tree_comps: dict[str, list[dict]] = {}
    for p in ctx.pins:
        comp = (p["fact"].get("child") or {}).get("component")
        if comp:
            tree_comps.setdefault(comp, []).append(p)
    return tree_comps


def _whatif_component(ctx: Ctx, q: Any, job: dict | None, tree_comps: dict[str, list[dict]],
                      problems: list[str]) -> tuple[str | None, list[str]]:
    """The component asked about: ?component=, else the deciding ssh pin's, else the job's first override's."""
    comp = q.get("component") or (ctx.decision.component if ctx.decision.kind == "ssh-pin" else None) \
        or ((job or {}).get("params", {}).get("pins") and _comp_of_override(ctx, job)) or None
    if comp not in tree_comps:
        asked = q.get("component")
        if asked:
            problems = problems + ["%s has no component %s in its stored tree: pick one from the list below"
                                   % (ctx.cid, asked[:80])]
        comp = None
    return comp, problems


def _whatif_comp_rows(config: Any, ctx: Ctx, q: Any, tree_comps: dict[str, list[dict]],
                      comp: str | None) -> list[dict]:
    """The component picker: flagged components first, filtered by ?q=."""
    fl = shell.component_flags(config)
    flt = (q.get("q") or "").strip().lower()
    comps = sorted(tree_comps, key=lambda c: (0 if fl.get(c) else 1, c))
    return [{"id": c, "flag": (fl.get(c) or [None])[0], "pin": tree_comps[c][0]["fact"]["child"]["commit"][:7],
             "n": len(tree_comps[c]), "on": c == comp, "href": "%s/whatif?component=%s" % (_base(ctx.cid), _q(c))}
            for c in comps if not flt or flt in c.lower()]


def _whatif_target(q: Any, tg: list[dict], refs: list[dict]) -> tuple[str, str | None]:
    """The target commit (a full sha, else "") and the words that name it."""
    target = q.get("target_other") or q.get("target") or ""
    target = target if len(target) == 40 and all(ch in "0123456789abcdef" for ch in target) else ""
    target_label = next((t["label"] + (" " + t["ref"] if t["ref"] else "") for t in tg if t["commit"] == target),
                        None) or next((r["name"] for r in refs if r["sha"] == target), None) or (target[:7] or None)
    return target, target_label


def _whatif_tiers(config: Any, ctx: Ctx, problems: list[str]) -> tuple[dict, list[str]]:
    tiers = {}
    try:
        from nanosoc_rm.ci import configs as cimod
        tiers = cimod.tiers(ctx.cid, config)
    except Exception as e:                                       # noqa: BLE001 - the form still works without tiers
        problems = problems + ["tiers.yaml could not be read for %s: %s" % (ctx.cid, str(e)[:200])]
    return tiers, problems


def _whatif_steps(config: Any, ctx: Ctx) -> tuple[dict | None, list[dict]]:
    """The next action and the steps after it, in the order the causes decide."""
    https = V.https_url(config, ctx.decision.component) if ctx.decision.kind == "ssh-pin" else None
    act = V.next_action(ctx.decision, ctx.cid, https, ctx.doc)
    steps = []
    if act and ctx.decision.gate:
        steps.append({"lead": act["label"] + ".", "rest": ctx.decision.label})
    for c in ctx.causes[1:]:
        steps.append({"lead": "Then:", "rest": "%s (%s)" % (c.label, ", ".join(c.gates))})
    return act, steps


def _whatif_command(ctx: Ctx, comp: str | None, paths: list[str], chosen: list[str], target: str) -> dict:
    """The --pin values, the CLI line and the expectation for the question as asked."""
    return {"pin_values": ["%s=%s" % (p, target) for p in chosen] if target else [],
            "cli": "nanosoc-rm compat %s%s" % (ctx.cid, "".join(" --pin %s=%s" % (p, target) for p in chosen)
                                               if target else " --pin TREE_PATH=SHA"),
            "expect": expectation(ctx.decision, comp, ctx.cid, paths) if comp else expectation(ctx.decision, None,
                                                                                               ctx.cid)}


def _whatif_pin_refs(config: Any, ctx: Ctx) -> dict[str, list[dict]]:
    return {p["fact"]["tree_paths"][0]: views.component_refs(config, (p["fact"].get("child") or {})
                                                             .get("component") or "")
            for p in ctx.pins}


def _comp_of_override(ctx: Ctx, job: dict) -> str | None:
    first = str((job["params"].get("pins") or [""])[0]).split("=", 1)[0]
    return next(((p["fact"].get("child") or {}).get("component") for p in ctx.pins
                 if p["fact"]["tree_paths"][0] == first), None)


def matrix_page(request: Request, id: str) -> Response:
    config = request.state.config
    ctx = load(config, id)
    if ctx is None:
        return _not_found(request)
    if _has_matrix(request):
        return RedirectResponse("/matrix/%s" % _q(ctx.cid), 303)
    return _page(request, ctx, "matrix", "cfg_matrix.html")


def silicon_page(request: Request, id: str) -> Response:
    config = request.state.config
    problems = registry_facts.refresh(config)
    ctx = load(config, id)
    if ctx is None:
        return _not_found(request)
    rows = sorted(ctx.silicon, key=lambda d: (bool(d["fact"].get("covered")), d["fact"]["component"],
                                               d["fact"]["repo"]["host"]))
    unc = [d for d in rows if not d["fact"].get("covered")]
    seen, cmds, unreadable = set(), [], False
    for d in unc:
        f = d["fact"]
        unreadable = unreadable or f.get("state") == "UNREADABLE"
        if (f["component"], f["commit"]) in seen:
            continue
        seen.add((f["component"], f["commit"]))
        cmds += ["git tag keep/%s/%s %s" % (ctx.cid, f["component"], f["commit"]),
                 "git push origin keep/%s/%s" % (ctx.cid, f["component"])]
    unused = {w.get("tree_path") for w in V.unused_waivers(ctx.doc, ctx.record)} if ctx.doc else set()
    waivers = [dict(w, unused=w.get("tree_path") in unused) for w in ctx.waivers]
    open_err = [e for e in ctx.errata if (e["fact"].get("record") or {}).get("status") in views.OPEN_ERRATUM_STATUSES]
    return _page(request, ctx, "silicon", "cfg_silicon.html", problems=problems, uncovered=unc,
                 taped=_store_panel("taped_out", config, ctx.cid, problems),
                 rows=[{"doc": d, "tone": shell.tone(d["fact"].get("state"))} for d in rows],
                 covered=len(rows) - len(unc), cmds="\n".join(cmds), unreadable=unreadable, waivers=waivers,
                 open_errata=open_err)


def _store_panel(name: str, config: Any, cid: str, problems: list[str]) -> Any:
    """A store-index panel (section 22) from stored facts, or None when the store package is not built here. A
    failure to read it is a page problem, never a broken page."""
    try:
        from nanosoc_rm.store import views as store_views
    except ModuleNotFoundError:
        return None
    try:
        return getattr(store_views, name)(config, cid)
    except Exception as e:                                      # noqa: BLE001 - the panel says why, the page stays
        problems.append("the store index could not be read: %s" % str(e)[:200])
        return None


def history_page(request: Request, id: str) -> Response:
    config = request.state.config
    problems = registry_facts.refresh(config)
    ctx = load(config, id)
    if ctx is None:
        return _not_found(request)
    by_nonce = {d["fact"].get("nonce"): d for d in ctx.docs}
    rows = []
    for j in views.safe_jobs(config, limit=100, configuration=ctx.cid):
        nonce = ((j.get("result") or {}).get("fact") or {}).get("nonce") if j["kind"] == "compat" else None
        rows.append({"job": j, "tier": j["params"].get("tier") or ("resolve check only" if j["kind"] == "compat" else ""),
                     "stored": by_nonce.get(nonce) if nonce and not j["params"].get("pins") else None,
                     "whatif": bool(j["params"].get("pins")),
                     "took": _span(j.get("started_at"), j.get("finished_at")),
                     "when": _ago(j.get("finished_at") or j.get("created_at")),
                     "tone": shell.tone(j.get("verdict") or j["state"]),
                     "href": "%s%s?job=%d" % (_base(ctx.cid), "/whatif" if j["kind"] == "compat" else "", j["id"])})
    ci = not jobs.is_local_ci(config)
    results = [{"doc": d, "verdict": views.run_verdict(d), "tone": shell.tone(views.run_verdict(d))} for d in ctx.docs]
    return _page(request, ctx, "history", "cfg_history.html", problems=problems, rows=rows, ci=ci, results=results,
                 tl=_store_panel("timeline", config, ctx.cid, problems),
                 runs=views.runs(config, ctx.cid) if ci else [])


def router(app: Any = None) -> APIRouter:
    """GET routes only: this module adds no write route in any deployment."""
    r = APIRouter()
    get = ["GET"]
    for path, fn in (("/configurations/{id}/tree", tree_page), ("/configurations/{id}/tree/node", tree_node),
                     ("/configurations/{id}/whatif", whatif_page), ("/configurations/{id}/matrix", matrix_page),
                     ("/configurations/{id}/silicon", silicon_page), ("/configurations/{id}/history", history_page)):
        r.add_api_route(path, fn, methods=get, include_in_schema=False)
    return r
