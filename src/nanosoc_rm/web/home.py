"""The projects home at `/` (Wave F, CR-WF-gui; David 2026-10-14: front page approved, Home.dc.html).

One tree of projects -> configurations -> pins, one compact toolbar above it, and a detail panel on the right.
It REVERSES Wave C's inbox at `/` (CR-W1): the inbox is now Notifications behind the bell (web.notifications).

    home_view(config, login, query, today=None, local=False) -> dict    everything home.html prints
    dashboard(request)                                                   GET / (routers.DELEGATES)
    router(app)                                                          POST /home/timeline (local deployments)
    STATE_KEY, WORD, RULE                                                the state words and their CSS keys

Rules this module keeps:
- Stored facts only: the catalogue's characterisation comes from the stored registry records (registry_record,
  record kind component), the trees from `freshness.view` (stored `freshness` snapshots, re-judged today). A GET
  never walks, scans, fetches or queues; it never creates gui.sqlite (`uistate.prefs` reads it only if it exists).
- State is never colour alone: every row prints its state word beside a shape (diamond Outdated, ring Behind,
  dot Current, half-filled amber dot "Current except N not measured", square Frozen, filled square Unpushed,
  dashed ring Not measured).
- The page state (open rows, the selected row, filters, the timeline override) is in the URL, so every row is a
  real link that works without JavaScript; htmx swaps only `#home` when it is there.
- A configuration's verdict is its tree verdict (worst DIRECT pin, freshness.rules; a tree with a direct pin that
  could not be measured is never Current: "Current except N not measured" when nothing worse); a pin row shows the worst
  state at or below it (its rollup); a frozen silicon configuration says Frozen and "not counted", never Outdated.
- The section 13 configuration board (AT-H1: per configuration the T0 result, red pins, errata, host drift,
  runner, data age and the one Run button) stays on this page, closed under the tree (`home.html`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import urllib.parse
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import clock
from nanosoc_rm.web import lines, registry_facts, uistate, views
from nanosoc_rm.web.plugins import login_of

__all__ = ["home_view", "dashboard", "router", "STATE_KEY", "WORD", "RULE", "PILLS", "KRANK", "sibling_rank",
           "parse_state"]

# freshness state -> the CSS key the design uses (Home.dc.html ST table); the word always prints beside it.
STATE_KEY = {"unpushed": "unp", "outdated": "out", "behind": "beh", "current_except": "cxn", "current": "cur",
             "frozen": "frz", "not_measured": "unk"}
WORD = {"unpushed": "Unpushed", "outdated": "Outdated", "behind": "Behind",
        "current_except": "Current except not measured", "current": "Current", "frozen": "Frozen",
        "not_measured": "Not measured"}
# The toolbar pills, in order; Unpushed and Current-except show only when a tree reads so.
PILLS = ("unpushed", "outdated", "behind", "current_except", "current", "frozen", "not_measured")
_SPARSE_PILLS = ("unpushed", "current_except")
RULE = {
    "unpushed": "A direct pin's commit is on no branch or tag of any host.",
    "outdated": "A direct pin is 10+ commits behind, or behind and not bumped for 90+ days.",
    "behind": "Direct pins lag by 1 to 9 commits and were bumped within 90 days.",
    "current_except": ("Every direct pin that could be measured is at its upstream head, but at least one could not "
                       "be counted, so the tree is not Current (David 2026-10-15)."),
    "current": "Every direct pin was measured and is at its upstream default-branch head.",
    "frozen": "A tagged silicon configuration. Drift is expected and is never a to-do.",
    "not_measured": ("No stored scan yet, or a scanned tree none of whose direct pins could be counted. Register "
                     "a configuration and scan it to measure it."),
}
# Sort order of the tree rows (worst first) and of projects.
_ORDER = {"unpushed": 0, "outdated": 1, "behind": 2, "current_except": 3, "not_measured": 4, "current": 5,
          "frozen": 6}
# Sibling order under a configuration or a pin (David: SoC, custom peripherals, subsystems/components, arch tech,
# generator, libraries, toolkits/flows). A direct component (or subsystem, or anything uncatalogued) of a
# project is one of its custom peripherals.
KRANK = {"soc": 0, "custom": 1, "subsystem": 2, "component": 2, "arch": 3, "generator": 4, "lib": 5, "infra": 5,
         "toolkit": 6, "flow": 6}
_LEVELS = (("project", "Project"), ("soc", "SoC"), ("subsystem", "Subsystem"))
_TARGETS = (("chip", "Chip"), ("chiplet", "Chiplet"))
_ROLES = (("implementation", "Implementation"), ("forkable", "Forkable template"), ("example", "Example"))
_TYPE_WORD = {"chip": "Chip", "chiplet": "Chiplet", "soc": "SoC", "subsystem": "Subsystem"}
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TL_AFTER = 6                                   # days of axis after today
_TL_MONTHS = 7                                  # whole months of axis before today's month


def sibling_rank(group: str | None, direct: bool) -> int:
    """Where a pin of catalogue group `group` sits among its siblings (KRANK)."""
    g = group
    if g == "project":
        g = "soc"
    if direct and g in ("component", "subsystem", None, "legacy"):
        g = "custom"
    return KRANK.get(g or "", 2)


def _fmt_date(ts: str | None, today: dt.date) -> str:
    if not ts:
        return "[date]"
    d = dt.date.fromisoformat(ts[:10])
    return "%d %s%s" % (d.day, _MON[d.month - 1], "" if d.year == today.year else " %d" % d.year)


def _age_word(days: int | None) -> str:
    from nanosoc_rm.freshness import rules
    w = rules.age_word(days)
    return rules.AGE_WORDS[w] if w else ""


def _ago(days: int | None) -> str:
    return "[no bump]" if days is None else "%d d ago" % days


def _behind(n: int | None, lower: bool) -> str:
    return "not measured" if n is None else ("%s%d" % ("at least " if lower else "", n))


# ---------------------------------------------------------------------------------------------------------
# URL state
# ---------------------------------------------------------------------------------------------------------
_MULTI = ("lv", "ty", "ro")


def parse_state(q: Any) -> dict:
    """The page state from the query string (a starlette QueryParams or a dict of lists)."""
    def getlist(k):
        if hasattr(q, "getlist"):
            return [v for v in q.getlist(k) if v]
        v = q.get(k) if isinstance(q, dict) else None
        return [x for x in (v if isinstance(v, list) else [v] if v else []) if x]

    def get(k):
        vals = getlist(k)
        return vals[0] if vals else ""
    fresh = get("f")
    return {"explicit": get("v") == "1", "open": set(getlist("o")), "sel": get("sel") or None,
            "fresh": fresh if fresh in WORD else None,
            "lv": {x for x in getlist("lv") if x in dict(_LEVELS)},
            "ty": {x for x in getlist("ty") if x in dict(_TARGETS)},
            "ro": {x for x in getlist("ro") if x in dict(_ROLES)},
            "gap": get("gap") == "1", "filters": get("filters") == "1",
            "tl": get("tl") if get("tl") in ("on", "off") else None}


def _url(st: dict, **change: Any) -> str:
    s = dict(st, **change)
    items: list[tuple[str, str]] = [("v", "1")]
    items += [("o", k) for k in sorted(s["open"])]
    if s.get("sel"):
        items.append(("sel", s["sel"]))
    if s.get("fresh"):
        items.append(("f", s["fresh"]))
    for k in _MULTI:
        items += [(k, x) for x in sorted(s[k])]
    if s.get("gap"):
        items.append(("gap", "1"))
    if s.get("filters"):
        items.append(("filters", "1"))
    if s.get("tl"):
        items.append(("tl", s["tl"]))
    return "/?" + urllib.parse.urlencode(items)


def _row_id(key: str) -> str:
    return "row-" + hashlib.sha1(key.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------------------
def _characterisations(config, problems: list[str]) -> tuple[dict, dict]:
    """({id: Characterisation}, {id: the component's registry_record doc}) from the stored registry records."""
    from nanosoc_rm.catalogue.model import characterise_all
    docs = registry_facts.current(config, "component")

    def index(d):
        p = d["fact"].get("pointer") or ""
        tail = p.rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else 1 << 30
    docs = sorted(docs, key=index)
    try:
        chars = characterise_all([d["fact"]["record"] for d in docs])
    except Exception as e:                                       # noqa: BLE001 - a banner, never a 500
        problems.append("the catalogue characterisation could not be read: %s" % str(e)[:200])
        chars = {}
    return chars, {d["fact"]["id"]: d for d in docs}


class _Data:
    """Everything the page reads, once per request."""

    def __init__(self, config, today: dt.date, problems: list[str]):
        from nanosoc_rm.catalogue.model import gap_unverified, socs_without_forkable_project
        from nanosoc_rm.freshness import view as fview
        self.today = today
        self.chars, self.comp_docs = _characterisations(config, problems)
        self.gaps = set(socs_without_forkable_project(self.chars)) if self.chars else set()
        # SoC references with no stated group default to project, so the gap check cannot see them: the chip says
        # it is unverified rather than "0" (catalogue review F2; `catalogue show --gaps` exits 2 for the same list)
        self.gap_unverified = sorted(gap_unverified(self.chars)) if self.chars else []
        self.cfg_docs = views.configurations(config)
        try:
            self.trees = {t["configuration"]: t for t in fview.trees(today=today, config=config)}
        except Exception as e:                                   # noqa: BLE001
            problems.append("the stored freshness could not be read: %s" % str(e)[:200])
            self.trees = {}
        self._fview, self._config, self._tree_cache = fview, config, {}
        ladder = [c for c in self.chars.values() if c.group in ("project", "soc", "subsystem")]
        self.projects: list[dict] = []
        seen = set()
        for ch in ladder:
            self.projects.append(self._project(ch.id, ch))
            seen.add(ch.id)
        for d in self.cfg_docs:                                  # a configuration root the catalogue does not
            root = d["fact"]["record"].get("component")          # characterise still shows, as a project
            if root and root not in seen:
                self.projects.append(self._project(root, self.chars.get(root)))
                seen.add(root)
        self.by_id = {p["id"]: p for p in self.projects}

    def _project(self, pid: str, ch: Any) -> dict:
        cfgs = []
        for d in self.cfg_docs:
            rec = d["fact"]["record"]
            if rec.get("component") != pid:
                continue
            cfgs.append({"id": rec["id"], "doc": d, "record": rec, "frozen": views.is_frozen(d),
                         "tree": self.trees.get(rec["id"])})
        measured = [c for c in cfgs if c["tree"] is not None]
        measured.sort(key=lambda c: (c["frozen"], c["id"]))
        group = ch.group if ch is not None else "project"
        return {"id": pid, "ch": ch, "group": group, "level": group if group in ("project", "soc", "subsystem")
                else "project", "type": getattr(ch, "type", None), "role": getattr(ch, "role", None),
                "guessed": tuple(getattr(ch, "guessed", ()) or ()) + tuple(getattr(ch, "derived", ()) or ()),
                "configs": cfgs, "measured": measured, "gap": pid in self.gaps}

    def tree(self, cid: str) -> dict | None:
        if cid not in self._tree_cache:
            self._tree_cache[cid] = self._fview.tree(cid, today=self.today, config=self._config)
        return self._tree_cache[cid]

    def capabilities(self, cid: str) -> dict:
        """Wave G: the configuration's capabilities view (capabilities.view.tree_view), once per request."""
        if not hasattr(self, "_caps_cache"):
            self._caps_cache: dict[str, Any] = {}
        if cid not in self._caps_cache:
            from nanosoc_rm.capabilities.view import tree_view
            try:
                self._caps_cache[cid] = tree_view(self._config, cid)
            except Exception as e:                               # noqa: BLE001 - kept, re-raised per caller
                self._caps_cache[cid] = e
        got = self._caps_cache[cid]
        if isinstance(got, Exception):
            raise got
        return got

    def group_of(self, comp: str | None) -> str | None:
        ch = self.chars.get(comp or "")
        return ch.group if ch is not None else None


def _cfg_state(c: dict) -> str:
    t = c["tree"]
    return "frozen" if c["frozen"] or t["frozen"] else t["state"]


def _cfg_word(c: dict) -> str:
    """A configuration row's word: Frozen, or its tree verdict's word ("Current except 2 not measured")."""
    return WORD["frozen"] if _cfg_state(c) == "frozen" else c["tree"]["word"]


def _roll_word(roll: dict) -> str:
    """A project row's word: its worst live tree's, with N summed over its live trees in that state."""
    from nanosoc_rm.freshness.rules import tree_word
    if roll["state"] != "current_except":
        return WORD[roll["state"]]
    return tree_word("current_except", sum(c["tree"].get("direct_not_measured") or 0 for c in roll["live"]
                                           if c["tree"]["state"] == "current_except"))


def _proj_roll(p: dict) -> dict:
    """{state, behind, upd}: the worst live configuration's tree verdict; Frozen with only a frozen tree; Not
    measured with none."""
    live = [c for c in p["measured"] if not (c["frozen"] or c["tree"]["frozen"])]
    if live:
        state = min((c["tree"]["state"] for c in live), key=lambda s: _ORDER.get(s, 9))
        upd = [c["tree"]["updated_days"] for c in live if c["tree"]["updated_days"] is not None]
        return {"state": state, "behind": max((c["tree"]["total_behind"] or 0) for c in live),
                "upd": min(upd) if upd else None, "live": live}
    if p["measured"]:
        return {"state": "frozen", "behind": 0, "upd": None, "live": []}
    return {"state": "not_measured", "behind": 0, "upd": None, "live": []}


# ---------------------------------------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------------------------------------
def home_view(config, login: str | None, query: Any, *, today: dt.date | None = None, local: bool = False,
              problems: list[str] | None = None) -> dict:
    """Everything home.html prints. `query`: the request's query params. `local`: the timeline toggle can be
    saved (POST /home/timeline exists)."""
    today = today or clock.now().date()
    problems = problems if problems is not None else []
    st = parse_state(query)
    data = _Data(config, today, problems)
    prefs = uistate.prefs(config, login) if login else {}
    tl_on = (st["tl"] or prefs.get("timeline") or "off") == "on"

    # ---- filters -------------------------------------------------------------------------------------
    def fresh_pass(p: dict) -> bool:
        f = st["fresh"]
        if not f:
            return True
        if f == "not_measured":                  # no stored scan, or a scanned live tree that reads Not measured
            return not p["measured"] or any(_cfg_state(c) == "not_measured" for c in p["measured"])
        if f == "frozen":
            return any(c["frozen"] or c["tree"]["frozen"] for c in p["measured"])
        return any(_cfg_state(c) == f for c in p["measured"] if not (c["frozen"] or c["tree"]["frozen"]))

    def passes(p: dict, skip: str | None = None) -> bool:
        if skip != "lv" and st["lv"] and p["level"] not in st["lv"]:
            return False
        if skip != "ty" and st["ty"] and p["type"] not in st["ty"]:
            return False
        if skip != "ro" and st["ro"] and p["role"] not in st["ro"]:
            return False
        if skip != "gap" and st["gap"] and not p["gap"]:
            return False
        if skip != "fresh" and not fresh_pass(p):
            return False
        return True

    counts = {k: 0 for k in WORD}
    for p in data.projects:
        for c in p["measured"]:
            counts[_cfg_state(c)] += 1
    # Not measured: the scanned trees that read Not measured (counted above) plus the projects with no scan
    counts["not_measured"] += sum(1 for p in data.projects if not p["measured"])
    pills = []
    for k in PILLS:
        if k in _SPARSE_PILLS and not counts[k] and st["fresh"] != k:
            continue
        on = st["fresh"] == k
        pills.append({"key": k, "css": STATE_KEY[k], "word": WORD[k], "n": counts[k], "on": on, "rule": RULE[k],
                      "href": _url(st, fresh=None if on else k, sel=st["sel"], open=_open_default(st, data))})

    def facet(dim: str, label: str, options: Iterable[tuple[str, str]], key_of) -> dict:
        opts = []
        for k, text in options:
            on = k in st[dim]
            n = sum(1 for p in data.projects if passes(p, dim) and key_of(p) == k)
            nxt = set(st[dim]) - {k} if on else set(st[dim]) | {k}
            opts.append({"label": text, "n": n, "on": on,
                         "href": _url(st, **{dim: nxt}, filters=True, open=_open_default(st, data))})
        return {"label": label, "opts": opts}
    facets = [facet("lv", "Type", _LEVELS, lambda p: p["level"]),
              facet("ty", "Builds a", _TARGETS, lambda p: p["type"]),
              facet("ro", "Role", _ROLES, lambda p: p["role"])]
    n_active = len(st["lv"]) + len(st["ty"]) + len(st["ro"]) + (1 if st["gap"] else 0)

    # ---- the tree ------------------------------------------------------------------------------------
    open_ = _open_default(st, data)
    sel = st["sel"]
    shown = [p for p in data.projects if passes(p)]
    order = {p["id"]: i for i, p in enumerate(data.projects)}
    measured = sorted((p for p in shown if p["measured"]), key=lambda p: (
        _ORDER.get(_proj_roll(p)["state"], 9), -(_proj_roll(p)["behind"] or 0), order[p["id"]]))
    unmeasured = [p for p in shown if not p["measured"]]
    tl = _Timeline(data, today) if tl_on else None
    rows: list[dict] = []

    def link(key: str, branch: bool) -> str:
        o = set(open_)
        if branch and (key not in o or key == sel):
            o ^= {key}
        return _url(st, open=o, sel=key) + "#" + _row_id(key)

    def add(key: str, level: int, branch: bool, state: str, name: str, kind: str, ago: str, aria: str,
            chip: str | None = None, bar: dict | None = None, fact: dict | None = None,
            word: str | None = None, glyph: str | None = None, alias: str = "", again: str | None = None) -> dict:
        # glyph: the row's kind shape (lines.KINDS, an icon; aria-hidden, the words beside it carry the meaning); alias:
        # a muted suffix (a line's configuration id, a pin's component when it differs from its path)
        r = {"key": key, "id": _row_id(key), "level": level, "indent": level * 20, "branch": branch,
             "open": branch and key in open_, "sel": key == sel, "state": state, "css": STATE_KEY[state],
             "word": word or WORD[state], "name": name, "kind": kind, "ago": ago, "aria": aria, "chip": chip,
             "href": link(key, branch), "bar": bar, "fact": fact, "glyph": glyph, "alias": alias, "again": again}
        rows.append(r)
        return r

    def pin_rows(cid: str, frozen: bool, node: dict, nodes: dict, level: int, again: dict) -> None:
        key = "n:%s:%s" % (cid, node["tree_path"])
        kids = _sorted_children(node, nodes, data)
        state = "frozen" if frozen else (node["rollup"] or node["live"])
        days = node["days_since_bump"]
        # a pin is named by its path and commit (two gitlinks of one repo are two rows that read differently);
        # its component only when that differs from the path's last segment
        pn = lines.pin(node["tree_path"], node["parent_path"], node["pinned"], node["component"])
        n_again = again.get(node["component"] or "")
        r = add(key, level, bool(kids), state, pn["text"], "pin", _ago(days),
                "Submodule pin %s%s, %s%s%s, %s" % (
                    pn["text"].replace(" @ ", " at "), (" (%s)" % pn["alias"]) if pn["alias"] else "", WORD[state],
                    ", frozen silicon, not counted" if frozen else "",
                    (", %s pinned at %d commits in this tree" % (node["component"], n_again)) if n_again else "",
                    "bumped %d days ago" % days if days is not None else "bump date unknown"),
                bar=tl.bar(days, tl.frozen_end(cid) if frozen else 0, frozen) if tl else None, fact=node,
                glyph=pn["glyph"], alias=pn["alias"], again=lines.again_text(n_again) if n_again else None)
        if r["open"]:
            for k in kids:
                pin_rows(cid, frozen, nodes[k], nodes, level + 1, again)

    def cfg_rows(c: dict, level: int) -> None:
        key = "c:" + c["id"]
        t = c["tree"]
        frozen = c["frozen"] or t["frozen"]
        state = _cfg_state(c)
        days = t["root"]["days"] if frozen else t["updated_days"]
        tr = data.tree(c["id"])
        nodes = {n["tree_path"]: n for n in (tr["nodes"] if tr else [])}
        tops = _sorted_children(None, nodes, data)
        word = _cfg_word(c)
        # a configuration is a line of its repository: "branch main @ 8e6d043", its id beside it
        ln = lines.line(c["record"], t["root"].get("commit"))
        r = add(key, level, True, state, ln["text"], "cfg", ("frozen %s" % _ago(days)) if frozen else _ago(days),
                "Line %s of %s, configuration %s, %s%s, %s" % (
                    ln["text"].replace(" @ ", " at "), c["record"].get("component") or "?", c["id"], word,
                    ", frozen silicon, not counted" if frozen else "", _ago(days)),
                chip="not counted" if frozen else None,
                bar=(tl.bar(days, days, True) if frozen else tl.bar(days, 0, False)) if tl else None, fact=t,
                word=word, glyph=ln["glyph"], alias=c["id"])
        if r["open"]:
            again = lines.pinned_again(nodes.values())
            for k in tops:
                pin_rows(c["id"], frozen, nodes[k], nodes, level + 1, again)

    def proj_row(p: dict, level: int) -> None:
        key = "p:" + p["id"]
        roll = _proj_roll(p)
        guessed = "type" in p["guessed"] and p["level"] == "project"
        chip = (_TYPE_WORD.get(p["type"] or "", "Project") if p["level"] == "project" else
                _TYPE_WORD.get(p["level"], "")) + (" ?" if guessed else "")
        frz = next((c for c in p["measured"] if c["frozen"] or c["tree"]["frozen"]), None)
        if roll["upd"] is not None:
            ago, bar = _ago(roll["upd"]), (tl.bar(roll["upd"], 0, False) if tl else None)
        elif frz is not None:
            d = frz["tree"]["root"]["days"]
            ago, bar = "frozen " + _ago(d), (tl.bar(d, d, True) if tl else None)
        else:
            ago, bar = "[no scan]", None
        word = _roll_word(roll)
        r = add(key, level, bool(p["measured"]), roll["state"], p["id"], "proj", ago,
                "Repository %s, %s, %s, %s" % (p["id"], chip, word, ago), chip=chip, bar=bar, word=word,
                glyph="repo")
        if r["open"]:
            for c in p["measured"]:
                cfg_rows(c, level + 1)

    for p in measured:
        proj_row(p, 0)
    if unmeasured:
        g = add("grp:unk", 0, True, "not_measured", "Not measured yet (%d)" % len(unmeasured), "group", "[no scan]",
                "%d projects not measured yet" % len(unmeasured))
        if g["open"]:
            for p in unmeasured:
                proj_row(p, 1)

    detail = _detail(sel, data, today, local)
    return {"pills": pills, "facets": facets, "n_active": n_active, "show_filters": st["filters"] or n_active > 0,
            "filters_href": _url(st, filters=not (st["filters"] or n_active > 0), open=open_),
            "clear_href": _url(st, fresh=None, lv=set(), ty=set(), ro=set(), gap=False, open=open_),
            "gap": {"on": st["gap"], "n": sum(1 for p in data.projects if p["gap"] and passes(p, "gap")),
                    "href": _url(st, gap=not st["gap"], filters=True, open=open_),
                    "unverified": data.gap_unverified},
            "timeline": {"on": tl_on, "saved": prefs.get("timeline"), "local": local,
                         "href": _url(st, tl="off" if tl_on else "on", open=open_), "axis": tl.axis() if tl else [],
                         "today": tl.x(0) if tl else None, "marks": tl.marks() if tl else [],
                         "span": tl.span_text() if tl else "",
                         "return_to": _url(st, open=open_, tl=None)},
            "rows": rows, "detail": detail, "state": st, "today": today.isoformat(),
            "scanned": bool(data.trees), "n_projects": len(data.projects)}


def _open_default(st: dict, data: _Data) -> set[str]:
    """The open rows: the URL's, or (no state in the URL) the most outdated live project and its live tree."""
    if st["explicit"]:
        return set(st["open"])
    cands = []
    for i, p in enumerate(data.projects):
        r = _proj_roll(p)
        if r["live"] and r["state"] in ("outdated", "unpushed"):
            cands.append((_ORDER[r["state"]], -(r["behind"] or 0), i, p, r))
    if not cands:
        return set()
    _, _, _, p, r = min(cands, key=lambda x: x[:3])
    return {"p:" + p["id"], "c:" + r["live"][0]["id"]}


def _sorted_children(node: dict | None, nodes: dict, data: _Data) -> list[str]:
    """Child tree paths of `node` (None: the configuration's direct pins), in KRANK order, then path order."""
    if node is None:
        kids = [p for p, n in nodes.items() if not n["parent_path"]]
    else:
        kids = list(node["children"])
    direct = node is None
    idx = {k: i for i, k in enumerate(sorted(kids))}
    return sorted(kids, key=lambda k: (sibling_rank(data.group_of(nodes[k]["component"]), direct), idx[k]))


class _Timeline:
    """The optional bar column: one shared axis from the oldest bump to a few days after today."""

    def __init__(self, data: _Data, today: dt.date):
        self.today = today
        # The axis starts on the 1st of the month _TL_MONTHS before today's (1 Mar for a day in October); an older
        # bump is drawn from the axis start (its bar is clipped), so a 2023 leaf cannot squeeze every other bar.
        m = today.month - _TL_MONTHS
        start = dt.date(today.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        self.start_days = (today - start).days
        self.end_days = -_TL_AFTER
        self.frozen = {cid: t["root"]["days"] for cid, t in data.trees.items() if t["frozen"]}

    def x(self, ago: int) -> float:
        a = max(self.end_days, min(self.start_days, ago))
        return round((self.start_days - a) / (self.start_days - self.end_days) * 100, 2)

    def frozen_end(self, cid: str) -> int:
        return self.frozen.get(cid) or 0

    def bar(self, start: int | None, end: int, frozen: bool) -> dict | None:
        if start is None:
            return None
        a, b = self.x(start), self.x(end)
        return {"left": a, "width": max(1.5, round(b - a, 2)), "frozen": frozen, "clipped": start > self.start_days}

    def axis(self) -> list[dict]:
        out = []
        d = (self.today - dt.timedelta(days=self.start_days))
        while (self.today - d).days > self.end_days:
            if d.month % 2 == 0 or self.start_days < 120:
                out.append({"label": _MON[d.month - 1], "x": self.x((self.today - d).days)})
            d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return out

    def marks(self) -> list[dict]:
        return [{"cid": cid, "x": self.x(days), "date": (self.today - dt.timedelta(days=days)).isoformat()}
                for cid, days in sorted(self.frozen.items()) if days is not None]

    def span_text(self) -> str:
        a = self.today - dt.timedelta(days=self.start_days)
        b = self.today + dt.timedelta(days=-self.end_days)
        return "%d %s %d to %d %s %d" % (a.day, _MON[a.month - 1], a.year, b.day, _MON[b.month - 1], b.year)


# ---------------------------------------------------------------------------------------------------------
# The detail panel
# ---------------------------------------------------------------------------------------------------------
def _fact(label: str, value: str, mono: bool = False, strong: bool = False) -> dict:
    return {"label": label, "value": value, "mono": mono, "strong": strong}


def _detail(sel: str | None, data: _Data, today: dt.date, local: bool) -> dict | None:
    if not sel:
        return None
    if sel.startswith("n:"):
        return _pin_detail(sel, data, today, local)
    if sel.startswith("c:"):
        return _cfg_detail(sel[2:], data, today, local)
    if sel.startswith("p:"):
        return _proj_detail(sel[2:], data, today)
    if sel == "grp:unk":
        n = sum(1 for p in data.projects if not p["measured"])
        return {"kind": "Group", "title": "Not measured yet", "title_cls": "t-big", "state": "not_measured",
                "css": "unk", "word": WORD["not_measured"], "sub": "%d projects" % n, "chips": [],
                "blurb": "These projects have no stored scan of a registered configuration, so their pins are not "
                         "measured. Register a configuration (eligibility.yaml) and scan it to measure one.",
                "facts": [], "note": None, "links": [("The catalogue", "/catalogue/")], "obs": None, "forms": []}
    return None


def _pin_detail(sel: str, data: _Data, today: dt.date, local: bool = False) -> dict | None:
    from nanosoc_rm.catalogue.model import GROUP_LABELS
    _, rest = sel.split(":", 1)
    cid, _, path = rest.partition(":")
    tr = data.tree(cid)
    if tr is None:
        return None
    n = next((x for x in tr["nodes"] if x["tree_path"] == path), None)
    if n is None:
        return None
    pin = data._fview.pin(cid, path, today=today, config=data._config) or dict(n, same_pin_lags_in=[])
    frozen = n["frozen"]
    state = "frozen" if frozen else n["live"]
    if frozen:
        sub = "would be %s if live; not counted" % WORD[n["live"]].lower()
    elif n["rollup"] and n["rollup"] != n["live"]:
        sub = "its own pin; worst beneath it: %s" % WORD[n["rollup"]].lower()
    else:
        sub = ""
    group = data.group_of(n["component"])
    chips = [GROUP_LABELS.get(group, "External") if n["catalogued_as"] != "external" or group else "External"]
    again = lines.pinned_again(tr["nodes"]).get(n["component"] or "")
    if again:
        chips.append(lines.again_text(again))
    up = n["upstream"] or {}
    branch = lines.qualified(n["component"], up.get("default_branch"))      # "tidelink:main", never a bare "main"
    facts = []
    if n["behind"] is None:
        facts.append(_fact("Commits behind", "not measured: %s" % (n["not_measured"] or "no count")))
    else:
        facts.append(_fact("Commits behind", "%s behind %s%s" % (_behind(n["behind"], n["lower_bound"]), branch,
                                                                " (a lower bound)" if n["lower_bound"] else "")))
    b = n["bump"] or {}
    if b:
        facts.append(_fact("Last bump", "%s by %s · %s · %s" % (_fmt_date(b.get("date"), today), b.get("author")
                                                               or "[author]", _ago(n["days_since_bump"]),
                                                               _age_word(n["days_since_bump"]))))
    facts.append(_fact("Pinned commit", (n["pinned"] or "")[:7], mono=True))
    facts.append(_fact("Path", n["tree_path"], mono=True))
    tracks = _tracks(data, cid, path)
    if tracks:
        facts.append(_fact("Tracks", "%s (.gitmodules branch =; the pin is still fixed at its commit)"
                           % lines.qualified(n["component"], tracks), mono=True))
    if again:
        others = sorted({"%s @ %s" % (x["tree_path"], (x["pinned"] or "")[:7]) for x in tr["nodes"]
                         if x["component"] == n["component"] and x["tree_path"] != path})
        facts.append(_fact("Also pinned at", ", ".join(others), mono=True))
    if n["branches"]:
        facts.append(_fact("On branches", ", ".join(n["branches"]) + (" and %d more" % n["branches_more"]
                                                                      if n["branches_more"] else "")))
    if not frozen and (n["behind"] or 0) > 0:
        also = pin.get("same_pin_lags_in") or []
        facts.append(_fact("Same pin also lags in", ", ".join(also) if also else "no other live tree",
                           strong=bool(also)))
    note = None
    if frozen:
        t = data.trees.get(cid) or {}
        note = ("%s is a silicon tag, frozen %s. This pin is old on purpose; changing it means cutting a new "
                "release candidate." % (cid, _fmt_date((t.get("root") or {}).get("date"), today)))
    elif n["unpushed"]:
        note = "No branch or tag on any host holds the pinned commit: push it before anyone else clones this tree."
    elif n["caveat"]:
        note = n["caveat"]
    comp = n["component"]
    links = []
    if comp:
        links += [("See %s across all trees in the matrix" % comp, "/matrix/?comp=%s#m-%s" % (
            urllib.parse.quote(comp, safe=""), urllib.parse.quote(comp, safe=""))),
                  ("Component %s" % comp, "/components/%s" % urllib.parse.quote(comp, safe=""))]
    links.append(("Submodule tree of %s" % cid, "/configurations/%s/tree" % urllib.parse.quote(cid, safe="")))
    caps, forms = _capabilities_section(data, cid, n["tree_path"], None if frozen else up.get("default_branch"),
                                        local)
    links += caps["links"]
    pn = lines.pin(n["tree_path"], None, n["pinned"], comp)
    rec = next((c["record"] for p in data.projects for c in p["configs"] if c["id"] == cid), None)
    ln = lines.line(rec, ((data.trees.get(cid) or {}).get("root") or {}).get("commit"))
    return {"kind": "Submodule pin in %s (%s)" % (cid, ln["ref_text"]), "kind_glyph": "pin",
            "title": pn["text"], "title_cls": "t-mono", "state": state,
            "css": STATE_KEY[state], "word": WORD[state], "sub": sub, "chips": chips, "blurb": None,
            "facts": facts, "note": note, "links": links, "obs": n, "forms": forms, "caps": caps}


def _capabilities_section(data: _Data, cid: str, tree_path: str | None, to: str | None, local: bool
                          ) -> tuple[dict, list[dict]]:
    """Wave G: the detail panel's Capabilities section (what this tree can run, on which host, its last result) and
    the "Test this bump" form of a live pin (local deployments; it only queues a job for an executor). Reads the
    capabilities view once per configuration per page; a problem is a line in the section, never a 500."""
    from nanosoc_rm.capabilities.web import bump_href
    q = urllib.parse.quote(cid, safe="")
    out = {"rows": [], "line": None, "links": [("Capabilities of %s" % cid, "/capabilities/%s" % q)]}
    try:
        view = data.capabilities(cid)
    except Exception as e:                                       # noqa: BLE001 - one line, never a 500
        out["line"] = "Capabilities not read: %s" % str(e)[:160]
        view = None
    if view is not None:
        label = tree_path or "."
        t = next((t for t in view["trees"] if t["tree_path"] == label), None)
        if t is None or t["status"] == "absent":
            out["line"] = "This tree declares no test capability (.nanosoc/capabilities.yaml) at this commit."
        elif t["status"] != "ok":
            out["line"] = "Capabilities file %s: %s" % (t["status"], "; ".join(t["problems"])[:200])
        else:
            for c in t["capabilities"]:
                last = (c.get("last") or {}).get("verdict")
                out["rows"].append({"id": c["id"], "kind": c["kind"], "tier": c["tier"] or "-",
                                    "hosts": ", ".join(c["hosts"]) or "no host", "last": last or "none yet"})
            if not out["rows"]:
                out["line"] = "The capabilities file declares nothing of its own."
    forms = []
    if tree_path and to and local:
        forms.append({"action": "/capabilities/%s/test-bump" % q, "label": "Test this bump (to %s)" % to,
                      "fields": {"path": tree_path, "to": to}})
    if tree_path:
        out["links"].append(("Test a bump of %s" % tree_path, bump_href(cid, tree_path, to)))
    return out, forms


def _cfg_detail(cid: str, data: _Data, today: dt.date, local: bool) -> dict | None:
    c = next((c for p in data.projects for c in p["measured"] if c["id"] == cid), None)
    if c is None:
        return None
    t = c["tree"]
    frozen = c["frozen"] or t["frozen"]
    state = _cfg_state(c)
    proj = c["record"].get("component") or "?"
    would = (t.get("would_be_word") or WORD["not_measured"]).lower()
    sub = ("frozen silicon, not counted; would be %s if live" % would if frozen
           else "judged on its %d direct pins" % t["direct"])
    root = t["root"]
    ln = lines.line(c["record"], root.get("commit"))
    facts = [_fact("Line", "%s (%s) · %s" % (ln["text"], _fmt_date(root.get("date"), today),
                                              "frozen at the tag" if ln["kind"] == "tag" else "follows the branch"),
                   mono=True)]
    nb = t.get("newest_bump") or {}
    if frozen:
        facts.append(_fact("Frozen", _ago(root.get("days"))))
    else:
        facts.append(_fact("Last pin bump", "%s · %s · %s" % (_ago(t["updated_days"]), nb.get("component") or "?",
                                                              _age_word(t["updated_days"]))))
    lag_d = t["would_be_direct_lagging"] if frozen else t["direct_lagging"]
    lag_a = t["would_be_lagging"] if frozen else t["lagging"]
    cn = (t.get("would_be_counts") or t["counts"]) if frozen else t["counts"]     # a frozen tree counts 0 itself
    facts.append(_fact("Direct pins lagging", "%d of %d%s" % (lag_d or 0, t["direct"], " (if live)" if frozen else "")))
    if t.get("direct_not_measured"):
        facts.append(_fact("Direct pins not measured", "%d of %d: the tree is not Current until they are" % (
            t["direct_not_measured"], t["direct"]), strong=not frozen))
    facts.append(_fact("Whole tree lagging", "%d of %d (%d outdated, %d behind, %d unpushed, %d not measured)" % (
        lag_a or 0, t["pins"], cn["outdated"], cn["behind"], cn["unpushed"], cn["not_measured"])))
    w = t.get("would_be_worst_lagging") if frozen else t.get("worst_lagging")
    facts.append(_fact("Most behind", "%s, %s commits" % (w["component"] or w["tree_path"],
                                                           _behind(w.get("behind"), w.get("lower_bound")))
                       if w else "nothing behind"))
    s = t.get("would_be_stalest_lagging") if frozen else t.get("stalest_lagging")
    facts.append(_fact("Waiting longest", "%s, %s" % (s["component"] or s["tree_path"], _ago(s["days"]))
                       if s else "nothing lagging"))
    note = None
    if frozen:
        note = ("%s is the silicon tag, frozen %s. Its pins are old on purpose and never count as outdated."
                % (cid, _fmt_date(root.get("date"), today)))
    elif t.get("not_measured"):
        note = t["not_measured"]
    q = urllib.parse.quote(cid, safe="")
    links = [("Open the configuration", "/configurations/%s" % q), ("Open the matrix", "/matrix/")]
    forms = [{"action": "/freshness/scan", "label": "Scan %s now" % cid, "fields": {"configuration": cid}}] \
        if local else []
    caps, _ = _capabilities_section(data, cid, None, None, local)
    links += caps["links"]
    return {"kind": "Line of %s · %s" % (proj, cid), "kind_glyph": ln["glyph"], "title": ln["text"],
            "title_cls": "t-mono", "state": state,
            "css": STATE_KEY[state], "word": _cfg_word(c), "sub": sub, "chips": ["not counted"] if frozen else [],
            "blurb": None, "facts": facts, "note": note, "links": links, "obs": t, "forms": forms, "caps": caps}


def _proj_detail(pid: str, data: _Data, today: dt.date) -> dict | None:
    from nanosoc_rm.catalogue.model import GROUP_LABELS, ROLE_LABELS, TYPE_LABELS
    p = data.by_id.get(pid)
    if p is None:
        return None
    roll = _proj_roll(p)
    ch = p["ch"]
    unconf = set(p["guessed"])

    def q(f):
        return " ?" if f in unconf else ""
    chips = ["Type: %s%s" % (GROUP_LABELS.get(p["level"], p["level"]), q("group"))]
    if p["level"] == "project" and p["type"]:
        chips.append("Builds a %s%s" % (TYPE_LABELS.get(p["type"], p["type"]), q("type")))
    if p["role"]:
        chips.append("%s%s" % (ROLE_LABELS.get(p["role"], p["role"]), q("role")))
    if p["gap"]:
        chips.append("No forkable project")
    live = roll["live"]
    frz = next((c for c in p["measured"] if c["frozen"] or c["tree"]["frozen"]), None)
    where = "top-level" if p["level"] == "project" else "inside %s" % (
        ", ".join(ch.wrapped_by) if ch is not None and ch.wrapped_by else "[confirm]")
    sub = ("live tree %s" % ", ".join(c["id"] for c in live)) if live else (
        "only a frozen tree" if frz else ("registered, not scanned yet" if p["configs"] else
                                          "no registered configuration"))
    doc = data.comp_docs.get(pid)
    canon = ((doc or {}).get("fact", {}).get("record", {}).get("canonical") or {})
    facts = [_fact("Repository", "%s:%s" % (canon.get("host"), canon.get("path")) if canon else pid, mono=True)]
    if ch is not None and ch.wraps:
        facts.append(_fact("Wraps", ", ".join(ch.wraps) + q("wraps"), mono=True))
    for c in live:
        t = c["tree"]
        facts.append(_fact("Last pin bump", "%s · %s (%s)" % (_ago(t["updated_days"]), _age_word(t["updated_days"]),
                                                              c["id"])))
        w = t.get("worst_lagging")
        facts.append(_fact("Most behind", "%s, %s commits (%s)" % (w["component"] or w["tree_path"],
                                                                    _behind(w.get("behind"), w.get("lower_bound")),
                                                                    c["id"]) if w else "nothing behind"))
    if frz is not None:
        facts.append(_fact("Silicon", "%s, frozen %s (%s). Not counted." % (
            frz["id"], _fmt_date(frz["tree"]["root"].get("date"), today), _ago(frz["tree"]["root"]["days"]))))
    if not p["measured"]:
        pinned = _pinned_in(pid, data)
        if pinned:
            facts.append(_fact("Pinned in", pinned))
    note = None
    if not p["measured"]:
        note = ("Not measured: no registered configuration has this repo as its root." if not p["configs"] else
                "Not measured: %s is registered but has no stored scan yet (the scheduler scans after each poll)."
                % ", ".join(c["id"] for c in p["configs"]))
    elif unconf:
        note = "? = a guessed characterisation still to confirm on the Catalogue page."
    links = [("Component page", "/components/%s" % urllib.parse.quote(pid, safe="")),
             ("Releases", "/releases/")]
    if unconf:
        links.append(("Confirm on the Catalogue page", "/catalogue/"))
    return {"kind": "%s · %s" % (dict(_LEVELS).get(p["level"], "Project"), where), "title": pid, "title_cls": "t-big",
            "state": roll["state"], "css": STATE_KEY[roll["state"]], "word": _roll_word(roll), "sub": sub,
            "chips": chips, "blurb": (ch.question if ch is not None else None), "facts": facts, "note": note,
            "links": links, "obs": doc, "forms": []}


def _tracks(data: _Data, cid: str, path: str) -> str | None:
    """The `.gitmodules` `branch =` of the stored pin_state at `path` in `cid` (walker branch_field), or None."""
    try:
        for d in views.pins(data._config, cid):
            f = d["fact"]
            if path in (f.get("tree_paths") or ()):
                return f.get("branch_field") or None
    except Exception:                                            # noqa: BLE001 - a missing fact, never a 500
        return None
    return None


def _pinned_in(pid: str, data: _Data) -> str | None:
    """Where an unmeasured repo is pinned in a measured live tree: the worst such pin."""
    best = None
    for cid, t in data.trees.items():
        if t["frozen"]:
            continue
        tr = data.tree(cid)
        for n in (tr["nodes"] if tr else []):
            if n["component"] != pid:
                continue
            k = (_ORDER.get(n["live"], 9), n["display_depth"])
            if best is None or k < best[0]:
                best = (k, cid, n)
    if best is None:
        return None
    _, cid, n = best
    return "%s: %s, %s behind, bumped %s" % (cid, WORD[n["live"]].lower(), _behind(n["behind"], n["lower_bound"]),
                                              _ago(n["days_since_bump"]))


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def dashboard(request: Request) -> Response:
    """GET /: the projects home, with the section 13 configuration board closed under the tree."""
    from nanosoc_rm import jobs
    from nanosoc_rm.web import inbox
    config = request.state.config
    state = request.app.state.rm
    problems = list(registry_facts.refresh(config))
    view = home_view(config, login_of(request), request.query_params, local=state.local_routes, problems=problems)
    return state.page(request, "home.html", "Projects", problems=problems, crumbs=[], no_rail=True, bare=True, home=view,
                      board=inbox.board(config, local_ci=jobs.is_local_ci(config)))


async def post_timeline(request: Request) -> Response:
    """POST /home/timeline: save this login's timeline toggle (gui.sqlite), then back to the page it came from."""
    config = request.state.config
    form = await request.form()
    value = str(form.get("timeline") or "")
    if value not in ("on", "off"):
        return PlainTextResponse("400 Bad Request: timeline must be on or off", 400)
    uistate.set_pref(config, login_of(request), "timeline", value)
    back = str(form.get("return") or "/")
    if not back.startswith("/?") and back != "/":
        back = "/"
    return RedirectResponse(back, 303)


def router(app: Any = None):
    """Home adds no GET route (app.py hands `/` over through routers.DELEGATES); its one POST exists only in a
    local deployment (section 13a)."""
    from fastapi import APIRouter
    from nanosoc_rm.web.plugins import local_routes
    r = APIRouter()
    if local_routes(app):
        r.add_api_route("/home/timeline", post_timeline, methods=["POST"], include_in_schema=False)
    return r
