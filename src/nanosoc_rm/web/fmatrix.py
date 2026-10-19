"""The freshness matrix, the default tab of /matrix/ (design: Matrix.dc.html): one row per repository,
one column per stored configuration tree, each cell the worst pin of that repository in that tree (state word,
commits behind, days since bump). The compatibility matrix (D3) stays the second tab, unchanged.

    matrix_view(config, query, today=None) -> dict      what _fmatrix.html prints

Stored facts only (freshness.view.grid / pin / tree and the stored registry records); a GET never scans.
Frozen silicon columns are greyed and say "not counted": their cells never make a repository "lagging".
A column's verdict is its tree verdict (freshness.view.trees: "Current except N not measured" when a direct pin could
not be measured and none is worse), and a repository with a live cell nobody could measure is
never called current: it says "current except N not measured" and stays listed under Lagging only.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any

from nanosoc_rm import clock
from nanosoc_rm.web import home as H
from nanosoc_rm.web import lines, registry_facts, views

__all__ = ["matrix_view", "TABS", "GROUP_ORDER", "kind_of"]

TABS = (("project", "Projects"), ("toolkit", "Toolkits"), ("flow", "Flows"), ("component", "Components"),
        ("generator", "Generators"), ("arch", "Arch tech"), ("lib", "Libraries"), ("infra", "Boards"),
        ("legacy", "Legacy"), ("ext", "External"))
TAB_LABEL = dict(TABS)
GROUP_ORDER = ("project", "component", "arch", "generator", "lib", "infra", "legacy", "ext", "toolkit", "flow")
KIND_TONE = {"project": "#123A8C", "toolkit": "#7A3E00", "flow": "#5B2E91", "component": "#16603F",
             "generator": "#94204F", "arch": "#0A5566", "lib": "#454953", "infra": "#6A4E00", "legacy": "#4A4E58",
             "ext": "#6B6F79"}
SORTS = (("behind", "Most behind"), ("age", "Oldest bump"))
_LAG = ("unpushed", "outdated", "behind")


def kind_of(group: str | None) -> str:
    """The matrix's row kind of a catalogue group: the ladder is Projects; uncatalogued repos are External."""
    if group in ("project", "soc", "subsystem"):
        return "project"
    return group if group in TAB_LABEL else "ext"


def _q(**params: Any) -> str:
    items = [(k, v) for k, v in params.items() if v not in (None, "", False)]
    return "/matrix/" + ("?" + urllib.parse.urlencode(items) if items else "")


def matrix_view(config, query: Any, *, today: dt.date | None = None) -> dict:
    today = today or clock.now().date()
    problems: list[str] = []
    group, lag_only, sort, sel_comp, sel_cfg = _matrix_query(query)
    m = _Matrix(config, today, problems, sel_comp, sel_cfg)
    comps = list(m.g["components"])
    of_group = [c for c in comps if group == "all" or m.kind(c) == group]
    shown = [c for c in of_group if not lag_only or m.n_lag(c) or m.n_unmeasured(c) or c == m.sel_comp]
    key = _matrix_sort_key(m, sort)
    m.state = {"group": None if group == "all" else group, "rows": None if lag_only else "all",
               "sort": None if sort == "behind" else sort}
    rows = _matrix_rows(m, group, shown, key)
    head, repos = _matrix_head(m)
    state = m.state
    return {"problems": problems, "cols": head, "repos": repos, "rows": rows, "n_shown": len(shown), "n_group": len(of_group),
            "hidden": len(of_group) - len(shown), "lag_only": lag_only, "tabs": _matrix_tabs(m, comps, group),
            "segs": [{"label": "Lagging only", "on": lag_only, "href": _q(**dict(state, rows=None))},
                     {"label": "Show all", "on": not lag_only, "href": _q(**dict(state, rows="all"))}],
            "sorts": [{"label": label, "on": sort == k, "href": _q(**dict(state, sort=None if k == "behind" else k))}
                      for k, label in SORTS],
            "show_all": _q(**dict(state, rows="all")), "unpinned": _matrix_unpinned(m, comps, group),
            "scanned": bool(m.cols)}


def _matrix_query(query: Any) -> tuple[str, bool, str, str | None, str | None]:
    """(group, lag_only, sort, selected component, selected configuration) from the query, defaults for the rest."""
    get = (lambda k: query.get(k) or "") if hasattr(query, "get") else (lambda k: "")
    group = get("group") if get("group") in TAB_LABEL or get("group") == "all" else "all"
    lag_only = get("rows") != "all"
    sort = get("sort") if get("sort") in dict(SORTS) else "behind"
    return group, lag_only, sort, get("comp") or None, get("cfg") or None


def _matrix_sort_key(m: "_Matrix", sort: str) -> Any:
    if sort == "age":
        return lambda c: (-m.max_age(c), -m.max_behind(c), c)
    return lambda c: (-m.n_lag(c), -m.max_behind(c), c)


class _Matrix:
    """The stored facts one matrix page reads (grid, trees, characterisation, configuration records) and the
    per-repository measures and cell views over them. A read that fails is a page problem, never a 500."""

    def __init__(self, config: Any, today: dt.date, problems: list[str], sel_comp: str | None,
                 sel_cfg: str | None) -> None:
        from nanosoc_rm.catalogue.model import characterise_all
        from nanosoc_rm.freshness import rules, view as fview
        self.config, self.today, self.sel_comp, self.sel_cfg = config, today, sel_comp, sel_cfg
        self.rules, self.fview = rules, fview
        self.state: dict = {}
        try:
            self.g = fview.grid(today=today, config=config)
            self.trees = {t["configuration"]: t for t in fview.trees(today=today, config=config)}
        except Exception as e:                                       # noqa: BLE001 - a banner, never a 500
            problems.append("the stored freshness could not be read: %s" % str(e)[:200])
            self.g, self.trees = {"components": [], "configurations": [], "cells": {}}, {}
        try:
            recs = [d["fact"]["record"] for d in registry_facts.current(config, "component")]
            self.chars = characterise_all(recs)
        except Exception as e:                                       # noqa: BLE001
            problems.append("the catalogue characterisation could not be read: %s" % str(e)[:200])
            self.chars = {}
        self.cfg_rec = {d["fact"]["record"]["id"]: d["fact"]["record"] for d in views.configurations(config)}
        self.cfg_root = {cid: r.get("component") for cid, r in self.cfg_rec.items()}
        # the lines of one repository sit side by side (eth: branch main | tag v1.0.0), live before frozen within it
        g, cfg_root = self.g, self.cfg_root
        order = {c: i for i, c in enumerate(g["configurations"])}
        self.cols = sorted(g["configurations"], key=lambda c: (min(order[x] for x in g["configurations"]
                                                                   if cfg_root.get(x) == cfg_root.get(c)),
                                                               cfg_root.get(c) or "", order[c]))
        self.live = [c for c in self.cols if not self.trees.get(c, {}).get("frozen")]
        self.cells = g["cells"]

    def kind(self, comp: str) -> str:
        ch = self.chars.get(comp)
        return kind_of(ch.group if ch is not None else None)

    def _live_cells(self, comp: str) -> list[dict]:
        return [self.cells[comp].get(c) or {} for c in self.live]

    def n_lag(self, comp: str) -> int:
        return sum(1 for x in self._live_cells(comp) if x.get("live") in _LAG)

    def n_unmeasured(self, comp: str) -> int:
        return sum(1 for x in self._live_cells(comp) if x.get("live") == "not_measured")

    def max_behind(self, comp: str) -> int:
        return max([x.get("behind") or 0 for x in self._live_cells(comp)] or [0])

    def max_age(self, comp: str) -> int:
        return max([x.get("days_since_bump") or 0 for x in self._live_cells(comp) if x.get("live") != "current"]
                   or [0])

    def cell_view(self, comp: str, cid: str) -> dict | None:
        x = self.cells[comp].get(cid)
        if x is None:
            return None
        words = self.rules.WORDS
        frozen = not x["counted"]
        st = "frozen" if frozen else x["live"]
        on = comp == self.sel_comp and cid == self.sel_cfg
        num = "" if x["live"] == "current" else ("" if x["behind"] is None else
                                                      ("≥%d" % x["behind"] if x["lower_bound"] else str(x["behind"])))
        days = x["days_since_bump"]
        aria = "%s in %s: %s, %s commits behind, %s. Show details" % (
            comp, cid, ("frozen, not counted, would be %s" % words[x["live"]].lower()) if frozen
            else words[x["live"]], "unknown" if x["behind"] is None else x["behind"],
            "bumped %d days ago" % days if days is not None else "bump date unknown")
        href = _q(**self.state, comp=None if on else comp, cfg=None if on else cid) + "#m-" + urllib.parse.quote(comp, safe="")
        return {"css": H.STATE_KEY[st], "word": H.WORD[st], "num": num, "age": "" if frozen or days is None else "%d d" % days,
                "aria": aria, "href": href, "on": on, "frozen": frozen, "obs": x}

    def root_cell(self, comp: str, cid: str) -> dict | None:
        """The column's own repository: "root @ sha", not "not used"."""
        if self.cfg_root.get(cid) != comp:
            return None
        return {"root": "root @ %s" % lines.line(self.cfg_rec.get(cid),
                                                 (self.trees.get(cid) or {}).get("root", {}).get("commit"))["sha"]}

    def spread(self, comp: str) -> str:
        used = [c for c in self.live if c in self.cells[comp]]
        nl, nu = self.n_lag(comp), self.n_unmeasured(comp)
        if not used:
            return "frozen only"
        if nl:
            return "lags in %d of %d" % (nl, len(used))
        if nu == len(used):
            return "not measured"
        if nu:
            return "current except %d not measured" % nu
        return "current"

    def row(self, comp: str) -> dict:
        return {"comp": comp, "kind": self.kind(comp), "tone": KIND_TONE[self.kind(comp)], "sel": comp == self.sel_comp,
                "spread": self.spread(comp),
                "cells": [self.cell_view(comp, c) or self.root_cell(comp, c) for c in self.cols],
                "anchor": "m-" + urllib.parse.quote(comp, safe="")}


def _matrix_rows(m: _Matrix, group: str, shown: list[str], key: Any) -> list[dict]:
    """The rows in group order (a group header before each group under All), the selected cell's detail row
    inserted after its repository's row."""
    rows: list[dict] = []
    if group == "all":
        for gk in GROUP_ORDER:
            ids = sorted((c for c in shown if m.kind(c) == gk), key=key)
            if not ids:
                continue
            rows.append({"group": gk, "label": TAB_LABEL[gk], "n": len(ids), "tone": KIND_TONE[gk]})
            rows += [m.row(c) for c in ids]
    else:
        rows += [m.row(c) for c in sorted(shown, key=key)]
    det = _matrix_detail(m)
    if det is not None:
        for i, r in enumerate(rows):
            if r.get("comp") == det["comp"]:
                rows.insert(i + 1, {"detail": det})
                break
    return rows


def _matrix_detail(m: _Matrix) -> dict | None:
    """The selected cell's detail: what it is, the same pin elsewhere, and the Home link that opens it."""
    sel_comp, sel_cfg, cells, today = m.sel_comp, m.sel_cfg, m.cells, m.today
    if not (sel_comp and sel_cfg and sel_comp in cells and sel_cfg in cells[sel_comp]):
        return None
    x = cells[sel_comp][sel_cfg]
    n = m.fview.pin(sel_cfg, x["tree_path"], today=today, config=m.config)
    if n is None:
        return None
    frozen = n["frozen"]
    body = _detail_body(m, sel_comp, n)
    also = n.get("same_pin_lags_in") or []
    also_text = ""
    if not frozen and (n["behind"] or 0) > 0:
        also_text = ("Same pin also lags in %s." % ", ".join(also)) if also else "No other live tree pins this commit."
    extra = "pinned %d times in %s; this is the worst" % (x["pins"], sel_cfg) if x.get("pins", 1) > 1 else ""
    return {"head": "%s in %s @ %s:" % (sel_comp, sel_cfg, (n.get("pinned") or "")[:7]), "body": body,
            "also": also_text, "also_strong": bool(also) and not frozen, "extra": extra,
            "home": _detail_home(m, sel_cfg, n), "obs": n, "comp": sel_comp}


def _detail_body(m: _Matrix, sel_comp: str, n: dict) -> str:
    """The detail sentence: live state, commits behind, the bump; or what a frozen pin would be."""
    today, words = m.today, m.rules.WORDS
    behind = "not measured" if n["behind"] is None else ("at least %d" % n["behind"] if n["lower_bound"]
                                                         else str(n["behind"]))
    if n["frozen"]:
        return ("frozen silicon, not counted. It would be %s if live (%s behind); pinned %s."
                % (words[n["live"]].lower(), behind, H._fmt_date(n.get("pin_date"), today)))
    up = lines.qualified(sel_comp, (n.get("upstream") or {}).get("default_branch"))   # "tidelink:main"
    b = n.get("bump") or {}
    bdate = H._fmt_date(b.get("date"), today) if b else "[date]"
    return "%s, %s behind %s, bumped %s by %s (%s, %s)." % (
        words[n["live"]], behind, up, bdate, b.get("author") or "[author]",
        H._ago(n["days_since_bump"]).replace(" ago", ""), H._age_word(n["days_since_bump"]))


def _detail_home(m: _Matrix, sel_cfg: str, n: dict) -> str:
    """The Home link: opens the project, the configuration and every pin above this one, and selects it."""
    tr = m.fview.tree(sel_cfg, today=m.today, config=m.config)
    by = {t["tree_path"]: t for t in (tr["nodes"] if tr else [])}
    opens, p = [], by.get(n["tree_path"], {}).get("parent_path")
    while p:
        opens.append("n:%s:%s" % (sel_cfg, p))
        p = by.get(p, {}).get("parent_path")
    root = m.cfg_root.get(sel_cfg)
    params = [("v", "1")] + ([("o", "p:" + root)] if root else []) + [("o", "c:" + sel_cfg)] + \
        [("o", o) for o in opens] + [("sel", "n:%s:%s" % (sel_cfg, n["tree_path"]))]
    return "/?" + urllib.parse.urlencode(params) + "#" + H._row_id("n:%s:%s" % (sel_cfg, n["tree_path"]))


def _matrix_head(m: _Matrix) -> tuple[list[dict], list[dict]]:
    """The column heads and one spanning header per repository over its lines."""
    head = []
    repos: list[dict] = []
    for c in m.cols:
        t = m.trees.get(c) or {}
        frozen = bool(t.get("frozen"))
        ln = lines.line(m.cfg_rec.get(c), (t.get("root") or {}).get("commit"))
        if repos and repos[-1]["repo"] == m.cfg_root.get(c):
            repos[-1]["span"] += 1
        else:
            repos.append({"repo": m.cfg_root.get(c), "span": 1, "glyph": "repo"})
        head.append({"id": c, "frozen": frozen, "line": ln["text"], "glyph": ln["glyph"],
                     "sub": "frozen silicon · not counted" if frozen else "%s · bumped %s" % (
                         t.get("word") or "?", H._ago(t.get("updated_days")))})
    return head, repos


def _matrix_tabs(m: _Matrix, comps: list[str], group: str) -> list[dict]:
    tabs = [{"key": "all", "label": "All", "n": len(comps), "on": group == "all", "tone": None,
             "href": _q(**dict(m.state, group=None))}]
    for k, label in TABS:
        n = sum(1 for c in comps if m.kind(c) == k)
        if n or k not in ("legacy",):
            tabs.append({"key": k, "label": label, "n": n, "on": group == k, "tone": KIND_TONE[k],
                         "href": _q(**dict(m.state, group=k))})
    return tabs


def _matrix_unpinned(m: _Matrix, comps: list[str], group: str) -> list[str]:
    in_group = set()
    if group not in ("all", "ext"):
        in_group = {cid for cid, ch in m.chars.items() if kind_of(ch.group) == group}
    return sorted(in_group - set(comps))
