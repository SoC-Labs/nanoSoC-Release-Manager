"""What the pages show about freshness, from stored facts only (interfaces.md section 24.3).

No function here runs git, starts a job or touches the network: each reads the latest stored `freshness` snapshot
of each configuration (`db.latest`) and re-judges it with `rules` on `today` (default: the UTC date now), because
"not bumped for 90 days" moves with the calendar. A configuration never scanned is simply absent.

    trees(today=None, config=None)                    -> [tree summary]             one per scanned configuration
    tree(configuration, today=None, config=None)      -> {"tree", "nodes"} | None    the tree in display order
    pin(configuration, tree_path, today=None, ...)    -> node | None                one row's detail panel
    grid(today=None, config=None, live_only=False)    -> {"components", "configurations", "cells"}
    stored(config=None)                               -> {configuration: [docs]}    the raw current documents

Every summary, node and cell carries `subject` and `observed_at` of the observation it came from, so a page can
print the A3 hooks (`data-subject`, `data-observed-at`, a link to `/facts/<subject>`) and its data age.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config
from nanosoc_rm.freshness import rules
from nanosoc_rm.freshness.scan import KIND

__all__ = ["stored", "trees", "tree", "pin", "grid", "same_pin_lags"]


def _today(today: dt.date | None) -> dt.date:
    return today or clock.now().date()


def _registered(config: Config) -> dict[str, tuple[bool, bool]] | None:
    """{configuration id: (frozen, silicon)} from the stored registry records (`registry_record`, kept current by
    the pages' registry refresh), frozen as scan.py decides it (frozen or silicon). None when no configuration
    registry record is stored at all (the registry was never read): then the stored scans are shown as they are."""
    docs = db.latest("registry_record", config=config)
    if not docs:
        return None
    recs = [d["fact"]["record"] for d in docs if d["fact"].get("record_kind") == "configuration"]
    return {c["id"]: (bool(c.get("frozen") or c.get("silicon")), bool(c.get("silicon"))) for c in recs}


def stored(*, config: Config | str | Path | None = None) -> dict[str, list[dict]]:
    """The current freshness documents per configuration: the tree document first, then its pins by tree path.

    Only configurations in the stored registry records are listed (a configuration dropped from eligibility.yaml
    leaves the pages once the registry is re-read, though its scan snapshot stays), and `frozen` / `silicon` come
    from those records, so marking a configuration frozen shows on the next page draw, not after the next scan.
    With no stored registry record at all, every stored scan is listed as stored."""
    registered = _registered(config)
    out: dict[str, list[dict]] = {}
    for d in db.latest(KIND, config=config):
        cid = d["fact"]["configuration"]
        if registered is not None and cid not in registered:
            continue
        frozen, silicon = registered[cid] if registered is not None else (d["fact"]["frozen"], None)
        if d["fact"].get("frozen") != frozen or (d["fact"]["level"] == "tree" and silicon is not None
                                                 and d["fact"].get("silicon") != silicon):
            fact = dict(d["fact"], frozen=frozen)
            if fact["level"] == "tree" and silicon is not None:
                fact["silicon"] = silicon
            d = dict(d, fact=fact)
        out.setdefault(cid, []).append(d)
    for cid, docs in out.items():
        docs.sort(key=lambda d: (d["fact"]["level"] != "tree", d["fact"].get("tree_path") or ""))
    return out


def _summary(docs: list[dict], today: dt.date) -> dict:
    tree_doc = docs[0]
    t = tree_doc["fact"]
    pins = [d["fact"] for d in docs[1:]]
    j = rules.judge_tree(t["frozen"], pins, today)
    root = dict(t["root"], days=rules.days_since(t["root"].get("date"), today))
    return {"configuration": t["configuration"], "frozen": t["frozen"], "silicon": t.get("silicon", False),
            "root": root, "walk": t["walk"], "not_measured": t.get("not_measured"), "scanned_on": t["judged_on"],
            "subject": tree_doc["subject"], "observed_at": tree_doc["observed_at"],
            "word": rules.WORDS[j["state"]], "would_be_word": rules.WORDS[j["would_be"]] if j["would_be"] else None,
            "age_word": rules.AGE_WORDS.get(j["age"]) if j["age"] else None, **j}


def trees(*, today: dt.date | None = None, config: Config | str | Path | None = None) -> list[dict]:
    """One summary per scanned configuration, live ones first, then by id. Each is rules.judge_tree on `today`
    plus: configuration, frozen, silicon, root {component, commit, date, days}, walk {snapshot, observed_at},
    not_measured (why a tree has no pins), scanned_on, word / would_be_word / age_word (display words), subject,
    observed_at."""
    today = _today(today)
    out = [_summary(docs, today) for docs in stored(config=config).values() if docs[0]["fact"]["level"] == "tree"]
    return sorted(out, key=lambda s: (s["frozen"], s["configuration"]))


def _parent_path(path: str, paths: set[str]) -> str | None:
    """The nearest other tree path that is a directory prefix of `path` (the gitlink holding it)."""
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        cand = "/".join(parts[:i])
        if cand in paths:
            return cand
    return None


def _nodes(docs: list[dict], today: dt.date) -> list[dict]:
    """One node per tree path (a pin reached at two paths shows at both), in depth-first path order, each with
    its own judgement and the worst state beneath it (itself included)."""
    rows = []
    for d in docs[1:]:
        f = d["fact"]
        j = rules.judge_pin(f, today)
        for tp in f["tree_paths"]:
            rows.append({"tree_path": tp, "name": tp.rsplit("/", 1)[-1], "first_path": tp == f["tree_path"],
                         "component": f["component"], "catalogued_as": f["catalogued_as"], "pinned": f["pinned"],
                         "pin_date": f["pin_date"], "behind": f["behind"], "ahead": f["ahead"],
                         "lower_bound": f["lower_bound"], "on_default": f["on_default"],
                         "diverged": f["on_default"] is False and bool(f["behind"]),
                         "unmerged": f["on_default"] is False and f["behind"] == 0,
                         "reachable": f["reachable"], "unpushed": f["unpushed"], "branches": f["branches"],
                         "branches_more": f["branches_more"], "upstream": f["upstream"], "bump": f["bump"],
                         "not_measured": f["not_measured"], "caveat": f.get("caveat"),
                         "parent_component": f["parent"]["component"], "parent_commit": f["parent"]["commit"],
                         "frozen": f["frozen"], "subject": d["subject"], "observed_at": d["observed_at"],
                         "word": rules.WORDS[j["state"]],
                         "would_be_word": rules.WORDS[j["would_be"]] if j["would_be"] else None, **j})
    paths = {r["tree_path"] for r in rows}
    by = {r["tree_path"]: r for r in rows}
    for r in rows:
        r["parent_path"] = _parent_path(r["tree_path"], paths)
        r["children"] = []
    for r in sorted(rows, key=lambda r: r["tree_path"]):
        if r["parent_path"]:
            by[r["parent_path"]]["children"].append(r["tree_path"])

    def depth(r):
        return 1 if not r["parent_path"] else 1 + depth(by[r["parent_path"]])

    def roll(r):
        if "_live_rollup" in r:
            return r["_live_rollup"], r["rollup_behind"]
        states, most = [r["live"]], r["behind"] or 0
        for c in r["children"]:
            s, b = roll(by[c])
            states.append(s)
            most = max(most, b)
        r["_live_rollup"], r["rollup_behind"] = rules.worst(states), most
        return r["_live_rollup"], most

    for r in rows:
        r["display_depth"] = depth(r)
        roll(r)
    for r in rows:                     # a frozen tree's nodes roll up Frozen; the live answer is would_be_rollup
        live = r.pop("_live_rollup")
        r["rollup"], r["would_be_rollup"] = ("frozen", live) if r["frozen"] else (live, None)
        r["rollup_word"] = rules.WORDS[r["rollup"]]
    order: list[dict] = []

    def visit(r):
        order.append(r)
        for c in r["children"]:
            visit(by[c])

    for r in sorted((r for r in rows if not r["parent_path"]), key=lambda r: r["tree_path"]):
        visit(r)
    return order


def tree(configuration: str, *, today: dt.date | None = None,
         config: Config | str | Path | None = None) -> dict | None:
    """{"tree": the trees() summary, "nodes": every tree path in depth-first order} or None when never scanned.
    A node: tree_path, name, display_depth, parent_path, children, component, catalogued_as, pinned, pin_date,
    behind, ahead, lower_bound, on_default, diverged, unmerged, reachable, unpushed, branches, branches_more,
    upstream {repo, default_branch, head, head_date}, bump {commit, date, author, subject, total}, not_measured,
    caveat, state / would_be / live / lagging / days_since_bump / pin_age_days (rules.judge_pin), word,
    would_be_word, rollup (worst live state at or under it; `frozen` on every node of a frozen tree, which carries
    the live answer as would_be_rollup, else None) and rollup_word, rollup_behind, subject, observed_at."""
    docs = stored(config=config).get(configuration)
    if not docs or docs[0]["fact"]["level"] != "tree":
        return None
    today = _today(today)
    return {"tree": _summary(docs, today), "nodes": _nodes(docs, today)}


def same_pin_lags(component: str | None, commit: str | None, exclude: str, all_docs: dict[str, list[dict]],
                  today: dt.date) -> list[str]:
    """Other LIVE configurations whose tree pins the same component at the same commit and lag on it."""
    if not component or not commit:
        return []
    out = []
    for cid, docs in sorted(all_docs.items()):
        if cid == exclude or not docs or docs[0]["fact"]["frozen"]:
            continue
        if any(d["fact"]["component"] == component and d["fact"]["pinned"] == commit
               and rules.judge_pin(d["fact"], today)["live"] in rules.LAGGING for d in docs[1:]):
            out.append(cid)
    return out


def pin(configuration: str, tree_path: str, *, today: dt.date | None = None,
        config: Config | str | Path | None = None) -> dict | None:
    """One node of tree() (any of its tree paths) plus `same_pin_lags_in`: the other live configurations that
    pin the same commit and lag on it. None when that configuration or path has no stored scan."""
    today = _today(today)
    all_docs = stored(config=config)
    docs = all_docs.get(configuration)
    if not docs or docs[0]["fact"]["level"] != "tree":
        return None
    node = next((n for n in _nodes(docs, today) if n["tree_path"] == tree_path), None)
    if node is None:
        return None
    return dict(node, configuration=configuration,
                same_pin_lags_in=same_pin_lags(node["component"], node["pinned"], configuration, all_docs, today))


def grid(*, today: dt.date | None = None, config: Config | str | Path | None = None,
         live_only: bool = False) -> dict:
    """Component by configuration. A cell is the worst pin of that component in that tree (by severity, then
    commits behind): {state, live, would_be, word, behind, lower_bound, days_since_bump, tree_path, pins (how many
    pins of the component the tree has), subject, observed_at}. A frozen configuration's cells say `frozen` with
    the live answer in `would_be`, and are never counted (`counted: false`). `components` lists every component
    with a cell, most lagging live cells first; `configurations` the column order (live first)."""
    today = _today(today)
    all_docs = stored(config=config)
    cols = sorted((cid for cid, docs in all_docs.items() if docs and docs[0]["fact"]["level"] == "tree"),
                  key=lambda c: (all_docs[c][0]["fact"]["frozen"], c))
    if live_only:
        cols = [c for c in cols if not all_docs[c][0]["fact"]["frozen"]]
    cells: dict[str, dict[str, dict]] = {}
    for cid in cols:
        for d in all_docs[cid][1:]:
            f = d["fact"]
            comp = f["component"] or "(uncatalogued)"
            j = rules.judge_pin(f, today)
            cur = cells.setdefault(comp, {}).get(cid)
            key = (rules.SEVERITY[j["live"]], f["behind"] or 0)
            cell = {"state": j["state"], "live": j["live"], "would_be": j["would_be"], "word": rules.WORDS[j["state"]],
                    "behind": f["behind"], "lower_bound": f["lower_bound"], "days_since_bump": j["days_since_bump"],
                    "tree_path": f["tree_path"], "counted": not f["frozen"], "subject": d["subject"],
                    "observed_at": d["observed_at"], "_key": key}
            if cur is None or key > cur["_key"]:
                cell["pins"] = 1 + (cur["pins"] if cur else 0)
                cells[comp][cid] = cell
            else:
                cur["pins"] += 1
    for row in cells.values():
        for c in row.values():
            c.pop("_key", None)

    def rank(comp):
        live = [c for c in cells[comp].values() if c["counted"]]
        lag = sum(1 for c in live if c["live"] in rules.LAGGING)
        return (-lag, -max([c["behind"] or 0 for c in live] or [0]), comp)

    return {"components": sorted(cells, key=rank), "configurations": cols, "cells": cells}
