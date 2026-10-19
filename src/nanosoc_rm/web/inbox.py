"""The inbox cards (Wave C spec section 2; CR-W1, CR-W2, CR-W3): ranked cards, one cause and one action each, built
only from stored observations.

CR-WF-gui (David 2026-10-14, chat: front page approved) REVERSES CR-W1's inbox at `/`: `/` is the projects home
(web.home) and the cards are the data source of Notifications behind the bell (web.notifications, one line per
card). The full card view (evidence, snooze) stays reachable at GET /inbox, linked from Notifications; the
configuration board moved under the projects tree.

    cards(config, login=None, now=None) -> [Card]      every card the stored facts give, ranked (2.3, 2.3 Ranking)
    open_count(config, login) -> int                   cards this login has not marked done or snoozed (the rail)
    board(config, d=None, local_ci=False) -> dict      the configuration board (2.5, build-first fix 2)
    dashboard(request)                                 GET /inbox: the card view (its own route since CR-WF-gui)
    alerts(request)                                    GET /alerts, handed over by app.py (routers.DELEGATES)
    router(app)                                        GET /inbox; the local-only POSTs: /inbox/done,
                                                       /inbox/snooze, /inbox/undo, /alerts/clear-group

Rules this module keeps (spec section 0 and 2):
- A card is built from module functions over the store (views, db, verdict, alertgroups, and the Wave B modules
  when they are installed). Nothing here walks, fetches, compares or queues: a GET never submits a job.
- Cards 1 and 2 read a configuration's verdict as the newer of its compat result and its latest CI T0 run (a
  GitHub + CI deployment stores only the second); a configuration with either is not "never checked".
- `inbox.WEIGHTS` is the one ranking table: rank = weight x staleness (0.8 when the card's fact is older than
  7 days). Ties go to frozen configurations, then to the older fact.
- Done, snooze and undo are per login in gui.sqlite (web.uistate); a done card reopens when its fingerprint (a
  hash of the content of the facts it was built from, not of when they were observed) changes. They are not system actions and are not logged in `actions`.
"""
from __future__ import annotations

import contextvars
import dataclasses
import datetime as dt
import hashlib
import importlib
import json
import math
import re
import urllib.parse
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import clock, db, jobs
from nanosoc_rm.quality.findings import is_bulk
from nanosoc_rm.web import alertgroups, routers, shell, uistate, verdict as V, views
from nanosoc_rm.web.plugins import login_of

__all__ = ["Card", "CARD_KINDS", "CATS", "WEIGHTS", "STALE_AFTER", "STALE_FACTOR", "cards", "open_count", "board",
           "dashboard", "alerts", "router", "headline", "inbox_view", "SNOOZE_DAYS"]

# Spec 2.3: kind -> filter tab. The order is the table's.
CARD_KINDS = {
    "compat_fail": "compat", "never_checked": "compat", "silicon_uncovered": "silicon", "red_pin": "silicon",
    "branches_deletable": "branches", "host_drift": "hosts", "alert_group": "mirrors", "quality_top": "quality",
    "waiver_unused": "compat", "errata_exposure": "silicon", "matrix_flaky": "compat", "bump_proposal": "bumps",
    "release_draft": "silicon", "registry_unloaded": "compat",
    "store_mismatch": "silicon", "release_not_archived": "silicon", "tapeout_unregistered": "silicon",   # section 22
}
CATS = (("all", "All"), ("compat", "Compatibility"), ("branches", "Branches"), ("silicon", "Silicon"),
        ("quality", "Quality"), ("hosts", "Host copies"), ("mirrors", "Mirrors"), ("bumps", "Bumps"))
CAT_LABEL = dict(CATS)

# Spec 2.3 Weight column: the one ranking table (each row has a unit test).
WEIGHTS = {
    "compat_fail": {"base": 100, "per_gate": 10, "per_not_reached": 5, "fixable": 20, "infra_only": -40},
    "never_checked": {"base": 60},
    "silicon_uncovered": {"base": 90, "per_row": 5},
    "red_pin": {"frozen": 70, "other": 40},
    "branches_deletable": {"base": 20, "cap": 40, "shown": 3},
    "host_drift": {"diverged": 50, "branch_mismatch": 10, "summary": 15},
    "alert_group": {"base": 30, "per_log2": 5},
    "quality_top": {"base": 10, "cap_k": 5},
    "waiver_unused": {"base": 35},
    "errata_exposure": {"exposed": 80, "affected": 80, "probably-exposed": 45, "probably-not-affected": 0,
                        "not-affected": 0},
    "matrix_flaky": {"base": 40, "expiring": 55, "expiring_days": 3},
    "bump_proposal": {"base": 25, "blocked": 10},
    "release_draft": {"base": 60},
    "store_mismatch": {"base": 85},                 # the store does not hold what a manifest names
    "release_not_archived": {"base": 45},           # a silicon configuration with no manifest in the store
    "tapeout_unregistered": {"base": 55},           # shipped streams no registered silicon configuration covers
    "registry_unloaded": {"base": 200},
}
STALE_AFTER = dt.timedelta(days=7)
STALE_FACTOR = 0.8
EVIDENCE_MAX = 5
SNOOZE_DAYS = (1, 7, 30)
_CARD_ID = re.compile(r"^[a-z_]+:[A-Za-z0-9_.@:/+-]{0,240}\Z")
_FP = re.compile(r"^[0-9a-f]{0,64}\Z")


@dataclasses.dataclass
class Card:
    id: str
    kind: str
    title: str
    why: str
    chip: tuple[str, str]
    weight: float
    fact_at: str | None = None                 # the observed_at the card is measured by (staleness, ties)
    meta: str = ""
    evidence: list[dict] = dataclasses.field(default_factory=list)   # {tag, tone, what, say}
    code: str | None = None
    primary: dict | None = None                # {label, href, method: get|post, fields: {}, local: bool}
    dest: str = ""
    frozen: bool = False
    facts: list[dict] = dataclasses.field(default_factory=list)       # the observations it was built from
    forms: list[dict] = dataclasses.field(default_factory=list)       # extra POST forms (never_checked)
    note: str = ""
    basis: Any = None                          # extra values the fingerprint covers
    rank: float = 0.0
    number: int = 0
    fingerprint: str = ""

    @property
    def cat(self) -> str:
        return CARD_KINDS[self.kind]

    @property
    def cat_label(self) -> str:
        return CAT_LABEL[self.cat]

    @property
    def shown_evidence(self) -> list[dict]:
        return self.evidence[:EVIDENCE_MAX]

    @property
    def more(self) -> int:
        return max(0, len(self.evidence) - EVIDENCE_MAX)

    @property
    def fact(self) -> dict | None:
        return self.facts[0] if self.facts else None

    @property
    def code_id(self) -> str:
        return "code-" + hashlib.sha256(self.id.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def _q(s: Any) -> str:
    return urllib.parse.quote(str(s), safe="")


def _has(name: str) -> bool:
    return routers.optional(name) is not None


def _optional(modname: str) -> Any:
    """A Wave B module when it is installed here, else None (a missing plug-in means no card, never an error)."""
    try:
        return importlib.import_module(modname)
    except ImportError:
        return None


def _fingerprint(card: Card) -> str:
    """A hash of what the card says and of the CONTENT of its facts: not their observed_at or snapshot, which a
    producer run renews even when nothing changed (I-R1). A new run of a set kind with the same facts keeps a done
    card done; a changed fact, or a new event (a new subject, e.g. a new compat nonce), reopens it."""
    basis = [(d.get("subject"), d.get("fact")) for d in card.facts if isinstance(d, dict)]
    raw = json.dumps([card.kind, card.title, basis, card.basis], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _age(at: str | None, now: dt.datetime) -> str:
    if not at:
        return ""
    try:
        from nanosoc_rm.web.page import human_age
        return human_age((now - clock.parse(at)).total_seconds()) + " ago"
    except (TypeError, ValueError):
        return ""


def _cfg_href(cid: str, tab: str = "") -> str:
    return "/configurations/%s%s" % (_q(cid), tab)


def _tree_href(cid: str, path: str) -> str:
    if _has("cfgspace"):
        return _cfg_href(cid, "/tree?node=%s" % _q(path))
    return _cfg_href(cid) + "#pin-" + _q(path)


def _silicon_href(cid: str) -> str:
    return _cfg_href(cid, "/silicon") if _has("cfgspace") else "/silicon-protection?configuration=%s" % _q(cid)


def _evidence(tag: str, tone: str, what: str, say: str = "") -> dict:
    return {"tag": tag, "tone": tone, "what": what, "say": say}


# ---------------------------------------------------------------------------------------------------------
# Card builders, one per row of spec 2.3. Each takes a shared context and returns cards.
# ---------------------------------------------------------------------------------------------------------
def _newer(compat: dict | None, t0: dict | None) -> dict | None:
    """The verdict a configuration stands at (I-R2): its compat result or its latest T0 run, the newer one. A T0
    run still pending acceptance has no verdict yet."""
    if t0 is not None and t0["fact"].get("acceptance") == "pending":
        t0 = None
    if compat is None or t0 is None:
        return compat or t0
    return t0 if clock.parse(t0["observed_at"]) > clock.parse(compat["observed_at"]) else compat


def _is_t0(doc: dict | None) -> bool:
    return bool(doc) and doc.get("kind") == "t0_result"


class _Ctx:
    def __init__(self, config, now: dt.datetime, problems: list[str] | None = None):
        self.config, self.now = config, now
        self.problems = list(problems or [])                    # the page's banner so far (registry problems)
        self.records = views.configurations(config)
        self.frozen = {r["fact"]["id"]: views.is_frozen(r) for r in self.records}
        # One read per kind for every configuration (each store read opens a connection).
        newest: dict[str, dict] = {}
        for d in views.compat_results(config):                   # newest first
            newest.setdefault(d["fact"]["configuration"], d)
        self.pins: dict[str, list[dict]] = {}
        for d in db.latest("pin_state", "pin_state:", config=config):
            self.pins.setdefault(d["fact"].get("configuration") or "", []).append(d)
        for ps in self.pins.values():
            ps.sort(key=lambda d: d["fact"]["tree_paths"][0])
        runs: dict[str, list[dict]] = {}
        for d in views.runs(config):                              # the CI T0 results, one read
            runs.setdefault(d["fact"]["configuration"], []).append(d)
        self.local_ci = jobs.is_local_ci(config)
        self.compat: dict[str, dict | None] = {}
        self.t0: dict[str, dict | None] = {}                      # the board's latest T0 run (may be pending)
        self.result: dict[str, dict | None] = {}                  # the newer verdict: compat result or T0 run
        self.decisions: dict[str, V.Decision] = {}
        for rec in self.records:
            cid = rec["fact"]["id"]
            self.compat[cid] = newest.get(cid)
            self.t0[cid] = views.latest_run(runs.get(cid, []))
            self.result[cid] = _newer(self.compat[cid], self.t0[cid])
            self.decisions[cid] = V.deciding(self.result[cid], rec)
        self.claimed: dict[str, set[str]] = {}          # cid -> tree paths a compat or silicon card already names

    def record(self, cid: str) -> dict | None:
        return next((r for r in self.records if r["fact"]["id"] == cid), None)

    def claim(self, cid: str, paths) -> None:
        self.claimed.setdefault(cid, set()).update(p for p in paths if p)


def _compat_fail(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["compat_fail"]
    out = []
    for rec in ctx.records:
        cid = rec["fact"]["id"]
        doc = ctx.result[cid]
        if doc is None or V.verdict_of(doc) not in V.RED:
            continue
        if _is_t0(doc):
            out.append(_t0_card(ctx, rec, doc))
            continue
        d = ctx.decisions[cid]
        cs = V.causes(doc, rec)
        f = doc["fact"]
        gates = f.get("gates") or []
        waived = [g for g in gates if g.get("verdict") == "WAIVED"]
        top = cs[0] if cs else None
        n_gates = len(top.gates) if top else 0
        infra_only = not cs and d.infra
        weight = (w["base"] + w["per_gate"] * n_gates + w["per_not_reached"] * (top.not_reached if top else 0)
                  + (w["fixable"] if not ctx.frozen[cid] else 0) + (w["infra_only"] if infra_only else 0))
        if top and (n_gates > 1 or top.not_reached):
            why = "That one cause fails %d %s and hides %d %s from the vendor scan." % (
                n_gates, _plural(n_gates, "gate"), top.not_reached, _plural(top.not_reached, "tree"))
        elif top:
            why = "It is the one red gate that decides this configuration."
        else:
            why = "Nothing in the design is red: this host could not run a tier."
        if waived:
            why += " The other %d red %s %s waived." % (len(waived), _plural(len(waived), "gate"),
                                                         "is" if len(waived) == 1 else "are")
        ev = []
        for c in cs:
            ev.append(_evidence(c.verdict, shell.tone(c.verdict), ", ".join(c.gates), c.label + (
                "; %d trees below never reached" % c.not_reached if c.not_reached else "")))
        if d.infra and d.infra_text:
            ev.append(_evidence("INFRA", "c-infra", (f.get("tier") or {}).get("tier") or "tier", d.infra_text))
        for g in waived:
            ev.append(_evidence("WAIVED", "c-waived", g["id"], "raw %s; waived for this frozen tree"
                                % (g.get("raw_verdict") or "FAIL")))
        url = V.https_url(ctx.config, d.component) if d.kind == "ssh-pin" and d.component else None
        act = V.next_action(d, cid, https_url=url, doc=doc) or {}
        primary = None
        if act.get("label"):
            primary = {"label": act["label"], "href": act["href"], "method": act.get("method", "get"), "fields": {},
                       "local": False}
        ctx.claim(cid, d.tree_paths)
        out.append(Card(
            id="compat_fail:%s" % cid, kind="compat_fail", chip=(d.verdict, shell.tone(d.verdict)),
            title="%s is %s: %s." % (cid, d.verdict, d.label), why=why, weight=weight,
            fact_at=doc.get("observed_at"), frozen=ctx.frozen[cid], facts=[doc],
            meta="stored result %s · %s" % (str(f.get("nonce") or "")[:12], _age(f.get("finished_at"), ctx.now)),
            evidence=ev, code=act.get("code"), primary=primary,
            dest="opens the %s" % ("What-if tab" if d.kind == "ssh-pin" else "submodule tree" if d.kind == "vendor"
                                   else "configuration"),
            basis=[d.gate, d.label]))
    return out


def _t0_card(ctx: _Ctx, rec: dict, doc: dict) -> Card:
    """Card 1 when the configuration's newest verdict is a CI T0 run (a GitHub + CI deployment, I-R2)."""
    w = WEIGHTS["compat_fail"]
    cid, f = rec["fact"]["id"], doc["fact"]
    d = ctx.decisions[cid]
    nonce = str((f.get("run") or {}).get("nonce") or "")
    cs = V.causes(doc, rec)
    top = cs[0] if cs else None
    if f.get("acceptance") == "rejected":
        label = "the run was rejected"
        why = "A rejected run proves nothing about this configuration: %s." % str(f.get("reason") or "no reason "
                                                                                  "was stored")[:200]
    elif top:
        label = d.label
        why = "It is the red gate that decides this configuration in its latest CI run."
    else:
        label = d.label.lower() if d.label != d.verdict else "no gate result"
        why = "The latest CI run has no gate that explains its verdict%s." % (
            ": %s" % str(f.get("reason"))[:200] if f.get("reason") else "")
    weight = (w["base"] + w["per_gate"] * (len(top.gates) if top else 0)
              + w["per_not_reached"] * (top.not_reached if top else 0) + (w["fixable"] if not ctx.frozen[cid] else 0))
    act = V.next_action(d, cid, doc=doc) if top else None
    primary = ({"label": act["label"], "href": act["href"], "method": act.get("method", "get"), "fields": {},
                "local": False} if act and act.get("label") and act.get("method", "get") == "get" else
               {"label": "Open the run", "href": "/runs/%s" % _q(nonce), "method": "get", "fields": {}})
    run = f.get("run") or {}
    ctx.claim(cid, d.tree_paths)
    return Card(
        id="compat_fail:%s" % cid, kind="compat_fail", chip=(d.verdict, shell.tone(d.verdict)),
        title="%s is %s in its latest T0 run: %s." % (cid, d.verdict, label), why=why, weight=weight,
        fact_at=doc.get("observed_at"), frozen=ctx.frozen[cid], facts=[doc],
        meta="T0 run %s · %s" % (nonce[:12], _age(run.get("triggered_at"), ctx.now)),
        evidence=[_evidence(c.verdict, shell.tone(c.verdict), ", ".join(c.gates), c.label) for c in cs]
        + [_evidence(str(f.get("acceptance") or "?").upper(), "c-warn" if f.get("acceptance") == "rejected"
                     else "c-neutral", "run %s" % nonce[:12], "%s by %s" % (run.get("trigger") or "?",
                                                                       run.get("triggered_by") or "?"))],
        primary=primary, dest="opens the %s" % ("run" if primary["href"].startswith("/runs/") else "configuration"),
        basis=[d.gate, label])


def _never_checked(ctx: _Ctx) -> list[Card]:
    cids = [r["fact"]["id"] for r in ctx.records
            if ctx.compat[r["fact"]["id"]] is None and ctx.t0[r["fact"]["id"]] is None]
    if not cids:
        return []
    names = ", ".join(cids)
    forms = [{"label": "Run %s" % c, "action": "/configurations/%s/runs" % _q(c), "cid": c} for c in cids]
    recs = [r for r in ctx.records if r["fact"]["id"] in cids]
    return [Card(
        id="never_checked:all", kind="never_checked", chip=("NOT CHECKED", "c-notrun"),
        title="%d %s never been checked: %s." % (len(cids), "configuration has" if len(cids) == 1
                                                 else "configurations have", names),
        why="Without a first verdict there is no baseline to compare a bump against.",
        weight=WEIGHTS["never_checked"]["base"], fact_at=min((r["observed_at"] for r in recs), key=clock.parse),
        facts=recs[:1], meta="registry records",
        evidence=[_evidence("NOT CHECKED", "c-notrun", c, "%d pins in the stored walk" % len(ctx.pins.get(c, [])))
                  for c in cids],
        forms=forms, primary={"label": "Run first check: %s" % cids[0], "href": forms[0]["action"], "method": "post",
                              "fields": {}, "local": False},
        dest=("queues one check per press (nanosoc-rm compat CONFIG --store)" if ctx.local_ci
              else "triggers one CI T0 run per press"), basis=cids)]


def _registry_unloaded(ctx: _Ctx) -> list[Card]:
    """No configuration record is stored: the registry has never loaded (a registry that loads has at least one
    configuration). Every other card is built from registry records, so an empty inbox here would mean "nothing
    known", not "nothing needs you" (ROB-faults-12)."""
    if ctx.records:
        return []
    said = [p for p in ctx.problems if "registry" in p.lower()] or ["no registry records are stored"]
    return [Card(
        id="registry_unloaded:all", kind="registry_unloaded", chip=("NO REGISTRY", "c-fail"),
        title="The registry has never loaded, so nothing here can be checked.",
        why="Every configuration and component comes from the registry. Until it loads, an empty inbox means "
            "nothing is known, not that nothing needs you.",
        weight=WEIGHTS["registry_unloaded"]["base"], meta="registry checkout ([registry] path)",
        evidence=[_evidence("PROBLEM", "c-fail", p[:300]) for p in said],
        dest="fix the registry checkout; the next page load reads it")]


def _silicon_uncovered(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["silicon_uncovered"]
    out = []
    for rec in ctx.records:
        cid = rec["fact"]["id"]
        if not ctx.frozen[cid]:
            continue
        rows = [d for d in views.silicon_protection(ctx.config, cid) if not d["fact"].get("covered")]
        if not rows:
            continue
        names = ", ".join("%s %s" % (d["fact"]["component"], d["fact"]["commit"][:7]) for d in rows[:3])
        code = []
        for d in rows:
            f = d["fact"]
            code += ["git tag keep/%s/%s %s" % (cid, f["component"], f["commit"]),
                     "git push origin keep/%s/%s" % (cid, f["component"])]
        unreadable = any(d["fact"].get("state") == "UNREADABLE" for d in rows)
        for d in rows:
            ctx.claim(cid, d["fact"].get("tree_paths") or [])
        out.append(Card(
            id="silicon_uncovered:%s" % cid, kind="silicon_uncovered", chip=("UNCOVERED", "c-fail"),
            title="%d %s silicon %s no keep tag: %s." % (len(rows), cid, "commit has" if len(rows) == 1
                                                          else "commits have", names),
            why="An untagged commit is lost if its branch is deleted.",
            weight=w["base"] + w["per_row"] * len(rows), fact_at=min((d["observed_at"] for d in rows), key=clock.parse),
            frozen=True, facts=rows, meta="silicon protection · %s" % _age(rows[0]["observed_at"], ctx.now),
            evidence=[_evidence(d["fact"].get("state") or "UNCOVERED", shell.tone(d["fact"].get("state") or "UNCOVERED"),
                                "%s %s" % (d["fact"]["component"], d["fact"]["commit"][:7]),
                                "%s (%s)" % (", ".join(d["fact"].get("tree_paths") or []), d["fact"]["repo"]["host"]))
                      for d in rows],
            code="\n".join(code), note=("UNREADABLE: run from a checkout that can read it." if unreadable else ""),
            primary={"label": "Open Silicon", "href": _silicon_href(cid), "method": "get", "fields": {}},
            dest="opens the silicon protection table", basis=[d["subject"] for d in rows]))
    return out


def _red_pins(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["red_pin"]
    out = []
    for rec in ctx.records:
        cid = rec["fact"]["id"]
        frozen = ctx.frozen[cid]
        for p in views.red_pins(ctx.pins.get(cid, []), frozen):
            f = p["fact"]
            path = f["tree_paths"][0]
            if path in ctx.claimed.get(cid, set()):
                continue
            host, h = views.pinned_host(p)
            state = f.get("state") if f.get("state") in views.PIN_LEVEL_RED else (h or {}).get("state") or "red"
            out.append(Card(
                id="red_pin:%s:%s" % (cid, path), kind="red_pin", chip=(state, shell.tone(state)),
                title="%s: %s is %s." % (cid, path, state),
                why="%s on the pinned host%s." % (state, " (%s)" % host if host else ""),
                weight=w["frozen"] if frozen else w["other"], fact_at=p["observed_at"], frozen=frozen, facts=[p],
                meta="pin %s · %s" % ((f.get("child") or {}).get("commit", "")[:7], _age(p["observed_at"], ctx.now)),
                evidence=[_evidence(hh.get("state") or "?", shell.tone(hh.get("state")), "%s%s" % (n, "*" if hh.get("pinned") else ""),
                                    "lag %s" % hh.get("lag") if hh.get("lag") is not None else "")
                          for n, hh in sorted((f.get("hosts") or {}).items())],
                primary={"label": "Show the pin", "href": _tree_href(cid, path), "method": "get", "fields": {}},
                dest="opens the submodule tree at this pin", basis=state))
    return out


def _branches(ctx: _Ctx) -> list[Card]:
    from nanosoc_rm.analysis.branches import delete_list
    w = WEIGHTS["branches_deletable"]
    by_comp: dict[str, list[dict]] = {}
    for d in db.latest("branch_state", "branch_state:", config=ctx.config):    # stored rows only; never refreshed
        comp = d["fact"].get("component")
        if comp:
            by_comp.setdefault(comp, []).append(d)
    found = []
    for comp, rows in by_comp.items():
        dele = delete_list(rows)
        if not dele:
            continue
        merged = sum(1 for d in rows if d["fact"]["verdict"] == "MERGED")
        stale = sum(1 for d in rows if d["fact"]["verdict"] == "STALE")
        n = len(dele)
        href = "/components/%s/branches" % _q(comp) if _has("compspace") else "/branches?component=%s" % _q(comp)
        found.append(Card(
            id="branches_deletable:%s" % comp, kind="branches_deletable", chip=("%d DELETABLE" % n, "c-warn"),
            title="%d %s %s can be deleted." % (n, comp, _plural(n, "branch", "branches")),
            why="%d MERGED plus %d STALE, minus %d kept by a tag, pin or citation. Fewer branches, fewer wrong pins."
                % (merged, stale, merged + stale - n),
            weight=min(w["cap"], w["base"] + n), fact_at=min((d["observed_at"] for d in rows), key=clock.parse),
            facts=dele[:1],
            meta="%d branches on every copy · %s" % (len(rows), _age(rows[0]["observed_at"], ctx.now)),
            evidence=[_evidence(d["fact"]["verdict"], shell.tone(d["fact"]["verdict"]), d["fact"]["branch"],
                                "%s:%s, %d ahead / %d behind, last commit %s" % (
                                    d["fact"]["repo"]["host"], d["fact"]["repo"]["path"], d["fact"].get("ahead") or 0,
                                    d["fact"].get("behind") or 0, str(d["fact"].get("last_commit_at") or "")[:10]))
                      for d in dele],
            primary={"label": "Review the delete list", "href": href, "method": "get", "fields": {}},
            dest="opens Branches with the delete list first", basis=sorted(d["subject"] for d in dele)))
    found.sort(key=lambda c: (-c.weight, c.id))
    shown = found[:w["shown"]]
    if len(found) > len(shown) and shown:
        rest = found[len(shown):]
        shown[-1].evidence.append(_evidence("+%d" % len(rest), "c-neutral", "more components",
                                            ", ".join(c.id.split(":", 1)[1] for c in rest)))
    return shown


def _host_drift(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["host_drift"]
    out, summary = [], []
    for d in views.host_compares(ctx.config):
        f = d["fact"]
        rel = f.get("relation")
        comp = f["component"]
        if rel == "DIVERGED":
            a, b = f.get("canonical_only"), f.get("copy_only")
            mism = bool(f.get("default_branch_mismatch"))
            out.append(Card(
                id="host_drift:%s:%s:%s" % (comp, f["copy"]["repo"]["host"], f["copy"]["repo"]["path"]),
                kind="host_drift", chip=("DIVERGED", "c-fail"),
                title="%s copies have diverged (%s / %s)." % (comp, a, b),
                why="DIVERGED needs a person; AHEAD can be pushed canonical to copy; BEHIND: check before overwriting.",
                weight=w["diverged"] + (w["branch_mismatch"] if mism else 0), fact_at=d["observed_at"], facts=[d],
                meta="host compare · %s" % _age(d["observed_at"], ctx.now),
                evidence=[_evidence("canonical", "c-neutral", "%s:%s" % (f["canonical"]["repo"]["host"], f["canonical"]["repo"]["path"]),
                                    "%s commits only here, default %s" % (a, f["canonical"].get("default_branch"))),
                          _evidence("copy", "c-neutral", "%s:%s" % (f["copy"]["repo"]["host"], f["copy"]["repo"]["path"]),
                                    "%s commits only here, default %s" % (b, f["copy"].get("default_branch")))]
                + ([_evidence("MISMATCH", "c-warn", "default branch", "the two copies name different default branches")]
                   if mism else []),
                primary={"label": "Open %s" % comp, "href": "/components/%s" % _q(comp), "method": "get", "fields": {}},
                dest="opens the component's host block", basis=[a, b, mism]))
        elif rel in ("AHEAD", "BEHIND"):
            summary.append(d)
    if summary:
        first = summary[0]["fact"]
        n = first.get("canonical_only") if first["relation"] == "AHEAD" else first.get("copy_only")
        if len(summary) == 1:
            title = ("The %s copy of %s lacks %s commits." % (first["copy"]["repo"]["host"], first["component"], n)
                     if first["relation"] == "AHEAD" else
                     "The %s copy of %s has %s commits the canonical repo lacks." % (
                         first["copy"]["repo"]["host"], first["component"], n))
        else:
            ah = sum(1 for d in summary if d["fact"]["relation"] == "AHEAD")
            title = "%d host copies lag or lead their canonical repo (%d AHEAD, %d BEHIND)." % (
                len(summary), ah, len(summary) - ah)
        out.append(Card(
            id="host_drift:summary", kind="host_drift", chip=("AHEAD/BEHIND", "c-infra"), title=title,
            why="DIVERGED needs a person; AHEAD can be pushed canonical to copy; BEHIND: check before overwriting.",
            weight=w["summary"], fact_at=min((d["observed_at"] for d in summary), key=clock.parse), facts=summary[:1],
            meta="host compare · %d components" % len(summary),
            evidence=[_evidence(d["fact"]["relation"], shell.tone(d["fact"]["relation"]), d["fact"]["component"],
                                "canonical only %s, copy only %s (%s copy)" % (
                                    d["fact"].get("canonical_only"), d["fact"].get("copy_only"),
                                    d["fact"]["copy"]["repo"]["host"])) for d in summary],
            primary={"label": "Open %s" % first["component"], "href": "/components/%s" % _q(first["component"]),
                     "method": "get", "fields": {}},
            dest="opens the component's host block", basis=sorted(d["subject"] for d in summary)))
    return out


def _alert_groups(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["alert_group"]
    out = []
    for g in alertgroups.groups(ctx.config):
        if g["kind"] == "waiver_unused":
            continue                                       # card 9 owns these
        n = g["count"]
        out.append(Card(
            id="alert_group:%s" % g["key"], kind="alert_group", chip=("%d ALERTS" % n if n != 1 else "1 ALERT", "c-fail"),
            title="%d %s one cause: %s." % (n, "alert has" if n == 1 else "alerts have", g["label"]),
            why="These mirrors cannot sync, so their branch and drift facts go stale."
                if g["kind"] == "mirror_failed" else "Each one is open until a person clears it.",
            weight=w["base"] + math.log2(max(n, 1)) * w["per_log2"], fact_at=g["newest_at"], facts=g["sample"][:1],
            meta="%s%s" % (g["kind"], " · %s" % g["hosts_text"] if g["hosts_text"] else ""),
            evidence=[_evidence(a["fact"]["alert_kind"], "c-fail", str(a["fact"].get("about") or ""),
                                shell.redact(a["fact"].get("message"), ctx.config)) for a in g["alerts"]],
            primary={"label": "Clear all %d after fixing" % n, "href": "/alerts/clear-group", "method": "post",
                     "fields": {"group": g["key"], "seen": g["fp"], "return": "inbox"}, "local": True,
                     "fallback": "/alerts#group-%s" % g["key"]},
            dest=g["advice"], basis=sorted(g["subjects"])))
    return out


_SEVERITY = {"stale_todo": 0, "unknown_identifier": 1, "commented_out_code": 2, "restates_code": 3,
             "boilerplate": 4}


def _rank_findings(findings: list[dict]) -> list[dict]:
    """web.review.rank (unit K) when it is built; otherwise the kinds in a fixed order, then path and line."""
    review = routers.optional("review")
    rank: Callable | None = getattr(review, "rank", None) if review is not None else None
    if rank is not None:
        try:
            return list(rank(findings))
        except Exception:                                      # noqa: BLE001 - fall back to the fixed order
            pass
    return sorted(findings, key=lambda x: (_SEVERITY.get(x.get("kind"), 9), x.get("path") or "", x.get("line") or 0))


def _quality(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["quality_top"]
    newest: dict[str, dict] = {}
    # The newest document per target (subject minus its commit part), read without decoding the older commits'
    # documents, which can be megabytes each (ROB-load-2).
    for d in db.newest_per("quality_finding", lambda s: s.rsplit(":", 1)[0], "quality_finding:", config=ctx.config):
        t = d["fact"].get("target")
        if t and (t not in newest or clock.parse(d["observed_at"]) > clock.parse(newest[t]["observed_at"])):
            newest[t] = d
    out = []
    for t, d in sorted(newest.items()):
        f = d["fact"]
        findings = [x for x in f.get("findings") or [] if not is_bulk(x)]       # CR-WD-Q1: one decoration rule
        if not findings:
            continue
        ranked = _rank_findings(findings)
        k = len(findings)
        banner = sum(1 for x in f.get("findings") or [] if is_bulk(x))
        first = ranked[0]
        href = "/components/%s/review" % _q(t) if _has("compspace") else "/quality/%s/comments" % _q(t)
        out.append(Card(
            id="quality_top:%s" % t, kind="quality_top", chip=("%d WORTH A LOOK" % k, "c-warn"),
            title="%s has %d comment findings; %d are decoration (one rule). Review the %d that matter first." % (
                t, f.get("total") or 0, banner, k),
            why="Top one: %s:%s %s." % (first.get("path"), first.get("line"), first.get("kind")),
            weight=w["base"] + min(k, w["cap_k"]), fact_at=d["observed_at"], facts=[d],
            meta="commit %s · %s" % (str(f.get("commit") or "")[:7], _age(d["observed_at"], ctx.now)),
            evidence=[_evidence(x.get("kind") or "?", "c-warn", "%s:%s" % (x.get("path"), x.get("line")),
                                str(x.get("suggestion") or "")) for x in ranked],
            primary={"label": "Review one at a time", "href": href, "method": "get", "fields": {}},
            dest="opens the review queue", basis=[f.get("commit"), k, banner]))
    return out


def _waivers(ctx: _Ctx) -> list[Card]:
    out = []
    alerts_by_cid: dict[str, list[dict]] = {}
    for a in db.alerts(open_only=True, config=ctx.config):
        if a["fact"].get("alert_kind") != "waiver_unused":
            continue
        parts = str(a["fact"].get("about") or "").split(":")
        if len(parts) >= 2:
            alerts_by_cid.setdefault(parts[1], []).append(a)
    for rec in ctx.records:
        cid = rec["fact"]["id"]
        unused = V.unused_waivers(ctx.compat[cid], rec) if ctx.compat[cid] is not None else []
        raised = alerts_by_cid.get(cid, [])
        if not unused and not raised:
            continue
        n = len(unused) or len(raised)
        lines = ["# eligibility.yaml, configuration %s: t0_waivers entries to delete" % cid]
        lines += ["- {gate: %s, tree_path: %s}" % (w.get("gate"), w.get("tree_path")) for w in unused]
        facts = ([ctx.compat[cid]] if unused else []) + raised
        out.append(Card(
            id="waiver_unused:%s" % cid, kind="waiver_unused", chip=("UNUSED", "c-warn"),
            title="%d %s on %s %s nothing." % (n, _plural(n, "waiver"), cid, "covers" if n == 1 else "cover"),
            why="An unused waiver can hide the next real failure at that path.",
            weight=WEIGHTS["waiver_unused"]["base"], fact_at=facts[0]["observed_at"], frozen=ctx.frozen[cid],
            facts=facts, meta="registry waivers against the stored result",
            evidence=[_evidence("WAIVER", "c-waived", "%s at %s" % (w.get("gate"), w.get("tree_path")),
                                "approved by %s" % w.get("approved_by") if w.get("approved_by") else "")
                      for w in unused] + [_evidence("ALERT", "c-warn", a["subject"],
                                                    shell.redact(a["fact"].get("message"), ctx.config)) for a in raised],
            code="\n".join(lines) if unused else None,
            primary={"label": "Open the waivers", "href": _cfg_href(cid) + "#waivers", "method": "get", "fields": {}},
            dest="opens the configuration's waivers", basis=[w.get("tree_path") for w in unused]))
    return out


def _errata(ctx: _Ctx) -> list[Card]:
    w = WEIGHTS["errata_exposure"]
    order = ("exposed", "affected", "probably-exposed", "probably-not-affected", "not-affected")
    by_defect: dict[str, list[dict]] = {}
    for e in views.errata(ctx.config):
        r = e["fact"]["record"]
        if r.get("status") in views.OPEN_ERRATUM_STATUSES and r.get("defect"):
            by_defect.setdefault(r["defect"], []).append(e)
    titles = {d["fact"]["id"]: (d["fact"]["record"] or {}).get("title") or "" for d in views.defects(ctx.config)}
    out = []
    for defect, es in sorted(by_defect.items()):
        levels = [((e["fact"]["record"].get("exposure") or {}).get("level") or "probably-exposed") for e in es]
        worst = min(levels, key=lambda lv: order.index(lv) if lv in order else 2)
        weight = w.get(worst, w["probably-exposed"])
        if weight <= 0:
            continue                                         # probably-not: shown only as evidence, never a card
        cids = sorted({e["fact"]["record"].get("configuration") for e in es})
        frozen = any(ctx.frozen.get(c) for c in cids)
        out.append(Card(
            id="errata_exposure:%s" % defect, kind="errata_exposure", chip=(worst, shell.tone(worst)),
            title="Defect %s is %s in %s." % (defect, worst, ", ".join(cids)),
            why="%s. It is %s." % (titles.get(defect) or defect, ", ".join(
                "%s %s" % (e["fact"]["id"], e["fact"]["record"].get("status")) for e in es)),
            weight=weight, fact_at=min((e["observed_at"] for e in es), key=clock.parse), frozen=frozen, facts=es,
            meta="registry errata",
            evidence=[_evidence(lv, shell.tone(lv), e["fact"]["id"], "%s by %s" % (
                e["fact"]["record"].get("configuration"),
                (e["fact"]["record"].get("exposure") or {}).get("method") or "?")) for e, lv in zip(es, levels)],
            primary={"label": "Open %s silicon" % cids[0], "href": _silicon_href(cids[0]), "method": "get",
                     "fields": {}},
            dest="opens the errata of %s" % cids[0], basis=levels))
    return out


def _matrix(ctx: _Ctx) -> list[Card]:
    cells = _optional("nanosoc_rm.matrix.cells")
    if cells is None:
        return []
    w = WEIGHTS["matrix_flaky"]
    out = []
    # cells.latest_cells and cells.quarantine per configuration, from one read of each kind
    latest: dict[str, dict] = {}
    for d in sorted(db.latest("matrix_cell", "matrix_cell:", config=ctx.config),
                    key=lambda d: clock.parse(d["fact"]["finished_at"])):
        # keyed by tier too: a tier cell (CR-WD-matrix-t1) neither replaces nor poses as the T0 cell
        latest.setdefault(d["fact"]["configuration"], {})[
            (d["fact"]["component"], d["fact"]["column"], d["fact"].get("tier") or None)] = d
    quarantined: dict[str, list[dict]] = {}
    for q in db.latest("matrix_quarantine", "matrix_quarantine:", config=ctx.config):
        quarantined.setdefault(q["fact"]["configuration"], []).append(q)
    for rec in ctx.records:
        cid = rec["fact"]["id"]
        for (comp, column, tier), d in sorted(latest.get(cid, {}).items(), key=lambda kv: str(kv[0])):
            if d["fact"].get("verdict") != "FLAKY":
                continue
            out.append(Card(
                id="matrix_flaky:%s:%s:%s%s" % (cid, comp or "-", column, ":" + tier if tier else ""),
                kind="matrix_flaky", chip=("FLAKY", "c-warn"),
                title="%s: %s at %s%s is FLAKY." % (cid, comp or "the pinned tree", column,
                                                    " (%s)" % tier if tier else ""),
                why="FLAKY blocks auto-merge; a quarantined gate does not count until it expires.",
                weight=w["base"], fact_at=d["observed_at"], frozen=ctx.frozen[cid], facts=[d],
                meta="matrix cell %s · %s" % (str(d["fact"].get("nonce") or "")[:12], _age(d["observed_at"], ctx.now)),
                evidence=[_evidence("FLAKY", "c-warn", g, "") for g in d["fact"].get("flaky_gates") or []],
                primary={"label": "Open the cell", "method": "get", "fields": {},
                         "href": "/matrix/%s/cell?component=%s&column=%s%s" % (
                             _q(cid), _q(comp or ""), _q(column), "&tier=%s" % _q(tier) if tier else "")},
                dest="opens the matrix cell", basis=d["fact"].get("nonce")))
        for test, q in sorted(cells.active_quarantine(quarantined.get(cid, []), ctx.now).items()):
            f = q["fact"]
            soon = clock.parse(f["expires_at"]) - ctx.now <= dt.timedelta(days=w["expiring_days"])
            out.append(Card(
                id="matrix_flaky:%s:quarantine:%s" % (cid, test), kind="matrix_flaky", chip=("QUARANTINED", "c-warn"),
                title="%s: %s is quarantined until %s (owner %s)." % (cid, test, f["expires_at"][:10], f.get("owner")),
                why="FLAKY blocks auto-merge; a quarantined gate does not count until it expires.",
                weight=w["expiring"] if soon else w["base"], fact_at=q["observed_at"], frozen=ctx.frozen[cid],
                facts=[q], meta="quarantine since %s" % str(f.get("since") or "")[:10],
                evidence=[_evidence("FLAKY", "c-warn", s, "") for s in f.get("flaky_cells") or []],
                primary={"label": "Open the matrix", "href": "/matrix/%s" % _q(cid), "method": "get", "fields": {}},
                dest="opens the matrix of %s" % cid, basis=f["expires_at"]))
    return out


def _bumps(ctx: _Ctx) -> list[Card]:
    bumps = _optional("nanosoc_rm.bumps")
    if bumps is None or not hasattr(bumps, "stored"):
        return []
    w = WEIGHTS["bump_proposal"]
    decided = {m.get("proposal") for m in bumps.marks(config=ctx.config)}
    seen: dict[str, dict] = {}
    for d in bumps.stored(config=ctx.config):
        f = d["fact"]
        if f.get("id") in decided or f.get("id") in seen or f.get("state") not in ("proposed", "blocked"):
            continue
        seen[f["id"]] = d

    def line(f: dict) -> str:
        return "%s %s %s to %s" % ((f.get("consumer") or {}).get("component"), (f.get("dependency") or {}).get("component"),
                                   (f.get("from") or {}).get("tag") or (f.get("from") or {}).get("commit", "")[:7],
                                   (f.get("to") or {}).get("tag") or (f.get("to") or {}).get("commit", "")[:7])
    out = []
    for state, label in (("proposed", "waiting"), ("blocked", "blocked")):
        docs = [d for d in seen.values() if d["fact"]["state"] == state]
        if not docs:
            continue
        f0 = docs[0]["fact"]
        out.append(Card(
            id="bump_proposal:%s" % state, kind="bump_proposal",
            chip=("%d %s" % (len(docs), state.upper()), "c-infra" if state == "proposed" else "c-warn"),
            title="%d bump %s %s: %s." % (len(docs), _plural(len(docs), "proposal"),
                                          "is " + label if len(docs) == 1 else "are " + label, line(f0)),
            why=("blocked: %s" % f0.get("reason")) if state == "blocked" else
                "Shadow mode: a person applies the patch; the ledger compares what people did.",
            weight=w["base"] if state == "proposed" else w["blocked"],
            fact_at=min((d["observed_at"] for d in docs), key=clock.parse), facts=docs[:1],
            meta="bump queue · %d %s" % (len(docs), _plural(len(docs), "proposal")),
            evidence=[_evidence(d["fact"]["state"], "c-infra" if state == "proposed" else "c-warn", d["fact"]["id"],
                                "%s in %s" % (line(d["fact"]), d["fact"].get("configuration"))) for d in docs],
            primary={"label": "Open %s" % f0["id"], "href": "/bumps/p/%s" % _q(f0["id"]), "method": "get",
                     "fields": {}},
            dest="opens the proposal and its patch", basis=sorted(seen)))
    return out


def _releases(ctx: _Ctx) -> list[Card]:
    if _optional("nanosoc_rm.releases") is None:
        return []
    out = []
    for d in db.latest("release_verify", "release_verify:", config=ctx.config):
        f = d["fact"]
        if f.get("verdict") == "PASS":
            continue
        cid, ver = f.get("configuration"), f.get("version")
        bad = [c for c in f.get("checks") or [] if c.get("verdict") != "PASS"]
        out.append(Card(
            id="release_draft:%s:%s" % (cid, ver), kind="release_draft", chip=(f.get("verdict") or "?", shell.tone(f.get("verdict"))),
            title="Release %s %s no longer verifies." % (cid, ver),
            why="A draft that no longer verifies is not what was drafted: re-draft or explain the drift.",
            weight=WEIGHTS["release_draft"]["base"], fact_at=d["observed_at"], frozen=ctx.frozen.get(cid, False),
            facts=[d], meta="release verify · %s" % _age(d["observed_at"], ctx.now),
            evidence=[_evidence(c.get("verdict") or "?", shell.tone(c.get("verdict")), c.get("id") or "",
                                str(c.get("detail") or "")) for c in bad],
            primary={"label": "Open the draft", "href": "/releases/%s/%s" % (_q(cid), _q(ver)), "method": "get",
                     "fields": {}},
            dest="opens the release draft", basis=[c.get("id") for c in bad]))
    return out


def _store(ctx: _Ctx) -> list[Card]:
    """Section 21: the store index's findings (store.views.findings), one card each, from the stored index only."""
    mod = _optional("nanosoc_rm.store.views")
    if mod is None:
        return []
    out = []
    for f in mod.findings(ctx.config):
        kind, doc = f["kind"], f["doc"]
        about = f["about"]
        if kind == "release_not_archived":
            primary = {"label": "Open %s Silicon" % about, "href": _silicon_href(about), "method": "get", "fields": {}}
            dest, why = "opens the configuration's Taped out panel", (
                "A silicon release with no manifest in the store cannot be traced from the store to its commit (D12).")
            code = "nanosoc-rm store findings"
        elif kind == "store_mismatch":
            primary = {"label": "Open the manifest in Store", "href": "/store/?q=%s" % _q(about.rsplit("/", 1)[-1]),
                       "method": "get", "fields": {}}
            dest, why, code = "opens the store index at that manifest", (
                "The manifest and the store disagree about the bytes: fix the manifest or the upload."), None
        else:
            primary = {"label": "Find %s in Store" % about, "href": "/store/?q=%s" % _q(about), "method": "get",
                       "fields": {}}
            dest, why, code = "lists that project's streams", (
                "The store marks these streams shipped, but no registered silicon configuration covers them: "
                "register the configuration, then publish its tapeout manifest."), None
        out.append(Card(
            id="%s:%s" % (kind, re.sub(r"[^A-Za-z0-9_.@:/+-]", "_", about)[:200]), kind=kind, chip=({"store_mismatch": "MISMATCH",
                                                           "release_not_archived": "NOT ARCHIVED",
                                                           "tapeout_unregistered": "UNREGISTERED"}[kind],
                                                          "c-fail" if kind == "store_mismatch" else "c-warn"),
            title=f["title"], why=why, weight=WEIGHTS[kind]["base"], fact_at=doc["observed_at"],
            frozen=ctx.frozen.get(about, False), facts=[doc], meta="store index · %s" % _age(doc["observed_at"], ctx.now),
            evidence=[_evidence("store", "c-neutral", e) for e in f["evidence"]], code=code, primary=primary,
            dest=dest, basis=f["evidence"]))
    return out


# Builder order matters: compat and silicon claim tree paths before the red-pin card reads them.
BUILDERS = (_registry_unloaded, _compat_fail, _never_checked, _silicon_uncovered, _red_pins, _branches, _host_drift,
            _alert_groups, _quality, _waivers, _errata, _matrix, _bumps, _releases, _store)


def _rank(cards_: list[Card], now: dt.datetime) -> list[Card]:
    for c in cards_:
        stale = c.fact_at is not None and now - clock.parse(c.fact_at) > STALE_AFTER
        c.rank = round(c.weight * (STALE_FACTOR if stale else 1.0), 3)
        c.fingerprint = _fingerprint(c)
    far = "9999-12-31T00:00:00Z"
    return sorted(cards_, key=lambda c: (-c.rank, 0 if c.frozen else 1, clock.parse(c.fact_at or far), c.id))


def cards(config, login: str | None = None, now: dt.datetime | None = None,
          problems: list[str] | None = None) -> list[Card]:
    """Every card the stored facts give, ranked. A builder that cannot read its source adds a problem (when a list
    is passed) and gives no card; it never takes the inbox down."""
    now = now or clock.now()
    ctx = _Ctx(config, now, problems)
    out: list[Card] = []
    for build in BUILDERS:
        try:
            out += build(ctx)
        except Exception as e:                                   # noqa: BLE001 - one source down: the rest still show
            if problems is not None:
                problems.append("the inbox could not read %s: %s" % (build.__name__.strip("_"), str(e)[:200]
                                                                       or type(e).__name__))
    return _rank(out, now)


def _hidden(config, login: str | None, now: dt.datetime, fps: dict[str, str]) -> dict[str, dict]:
    if not login or not uistate.path(config).exists():          # a GET never creates gui.sqlite
        return {}
    return uistate.card_state(config, login, now=now, fingerprints=fps)


# The open count the inbox page just computed, for the rail of the same request (one card build per page, not
# two). Keyed on the config object of the request and the login; a context variable, so never shared between
# requests.
_LAST_COUNT: contextvars.ContextVar[tuple[int, str, int] | None] = contextvars.ContextVar("inbox_open", default=None)


def open_count(config, login: str | None, now: dt.datetime | None = None) -> int:
    last = _LAST_COUNT.get()
    if last is not None and last[0] == id(config) and last[1] == (login or ""):
        return last[2]
    now = now or clock.now()
    cs = cards(config, login, now)
    hidden = _hidden(config, login, now, {c.id: c.fingerprint for c in cs})
    return sum(1 for c in cs if c.id not in hidden)


def headline(open_cards: list[Card]) -> str:
    return ("Next: " + open_cards[0].title) if open_cards else "Inbox clear. Nothing needs you right now."


def inbox_view(config, login: str | None, cat: str = "all", now: dt.datetime | None = None,
               problems: list[str] | None = None) -> dict:
    """Everything `_inbox.html` prints: the open cards of the filter, the counts, done today and snoozed."""
    now = now or clock.now()
    cat = cat if cat in CAT_LABEL else "all"
    cs = cards(config, login, now, problems)
    hidden = _hidden(config, login, now, {c.id: c.fingerprint for c in cs})
    by_id = {c.id: c for c in cs}
    open_ = [c for c in cs if c.id not in hidden]
    for i, c in enumerate(open_, start=1):
        c.number = i
    today = now.date()
    done = [dict(r, card=by_id.get(r["card_id"])) for r in hidden.values()
            if r["state"] == "done" and clock.parse(r["at"]).date() == today]
    snoozed = [dict(r, card=by_id.get(r["card_id"])) for r in hidden.values() if r["state"] == "snoozed"]
    done.sort(key=lambda r: r["at"], reverse=True)
    snoozed.sort(key=lambda r: r["until"] or "")
    _LAST_COUNT.set((id(config), login or "", len(open_)))
    counts = {k: (len(open_) if k == "all" else sum(1 for c in open_ if c.cat == k)) for k, _ in CATS}
    shown = open_ if cat == "all" else [c for c in open_ if c.cat == cat]
    total = len(done) + len(open_)
    return {"cards": shown, "open": len(open_), "headline": headline(open_), "cat": cat, "cat_label": CAT_LABEL[cat],
            "filters": [{"key": k, "label": label, "count": counts[k], "on": k == cat} for k, label in CATS],
            "done": done, "snoozed": snoozed, "done_n": len(done), "total": total,
            "progress": int(round(100 * len(done) / total)) if total else 0, "snooze_days": SNOOZE_DAYS,
            "login": login}


# ---------------------------------------------------------------------------------------------------------
# The board under the inbox (spec 2.5, build-first fix 2)
# ---------------------------------------------------------------------------------------------------------
def board(config, d: dict | None = None, local_ci: bool = False) -> dict:
    """views.dashboard plus, per configuration, the deciding gate and its next action; the T0 and Runner columns
    hidden (but present, for the section 13 hooks) when every row reads the same."""
    d = d if d is not None else views.dashboard(config)
    extra = {}
    for r in d["rows"]:
        cid, doc = r["id"], r["compat"]
        dec = V.deciding(doc, r["record"])
        act = V.next_action(dec, cid, doc=doc) if doc is not None else None
        extra[cid] = {"decision": dec, "tone": shell.tone(dec.verdict), "action": act,
                      "t0_text": None if r["last"] else ("no CI here" if local_ci else "no run yet"),
                      "runner_key": (tuple(s["subject"] for s in d["runners"]), tuple(r["pending_age_s"]))}
    rows = list(extra.values())
    hide_t0 = bool(rows) and all(x["t0_text"] == "no CI here" for x in rows)
    hide_runner = shell.uniform(rows, lambda x: x["runner_key"]) and not any(x["runner_key"][1] for x in rows)
    try:
        groups = alertgroups.groups(config)
    except Exception:                                            # noqa: BLE001 - the board still renders
        groups = []
    return {"d": d, "x": extra, "hide_t0": hide_t0, "hide_runner": hide_runner, "alert_groups": groups,
            "redacted": {a["subject"]: shell.redact(a["fact"].get("message"), config)
                         for g in groups for a in g["sample"]}}


# ---------------------------------------------------------------------------------------------------------
# Pages (handed over by app.py through routers.DELEGATES) and the local-only POSTs
# ---------------------------------------------------------------------------------------------------------
def _ctx(request: Request) -> tuple[Any, Any]:
    return request.state.config, request.state.identity


def _cat(value: Any) -> str:
    v = str(value or "all")
    return v if v in CAT_LABEL else "all"


def dashboard(request: Request) -> Response:
    """GET /inbox: the inbox cards with their evidence, snooze and undo (the board is on the projects home)."""
    from nanosoc_rm.web import registry_facts
    config, _ = _ctx(request)
    problems = list(registry_facts.refresh(config))
    cat = _cat(request.query_params.get("cat"))
    view = inbox_view(config, login_of(request), cat, problems=problems)
    trail = [("Projects", "/"), ("Notifications", "/notifications")]
    crumbs = trail + ([("Cards", None)] if cat == "all" else [("Cards", "/inbox"), (CAT_LABEL[cat], None)])
    return request.app.state.rm.page(request, "inbox.html", "Notification cards", problems=problems, crumbs=crumbs,
                                     inbox=view, board=None)


def alerts(request: Request) -> Response:
    """GET /alerts: one row per cause group, each with its alerts in a <details>; the cleared ones flat."""
    config, _ = _ctx(request)
    open_, cleared = views.all_alerts(config)
    groups = alertgroups.groups(config, open_)
    redacted = {a["subject"]: shell.redact(a["fact"].get("message"), config) for a in open_ + cleared[:50]}
    return request.app.state.rm.page(request, "alerts.html", "Alerts", groups=groups, open_n=len(open_),
                                     cleared=cleared[:50], redacted=redacted)


def _inbox_fragment(request: Request, cat: str) -> HTMLResponse:
    from nanosoc_rm.web.page import Page, env
    config, ident = _ctx(request)
    state = request.app.state.rm
    p = Page()
    view = inbox_view(config, login_of(request), cat)
    html = env.get_template("_inbox.html").render(page=p, fact=p.fact, csrf=state.csrf_for(ident, config),
                                                  identity=ident, actions=state.local_routes, inbox=view)
    return HTMLResponse(html)


def _back(request: Request, cat: str) -> Response:
    if request.headers.get("hx-request"):
        return _inbox_fragment(request, cat)
    return RedirectResponse("/inbox" if cat == "all" else "/inbox?cat=%s" % cat, 303)


def _card_field(form: Any) -> str | None:
    card = str(form.get("card") or "")
    return card if _CARD_ID.match(card) else None


async def post_done(request: Request) -> Response:
    config, _ = _ctx(request)
    form = await request.form()
    card, fp = _card_field(form), str(form.get("fp") or "")
    if card is None or not _FP.match(fp):
        return PlainTextResponse("400 Bad Request: card and fp are needed", 400)
    uistate.set_card(config, login_of(request), card, "done", fp)
    return _back(request, _cat(form.get("cat")))


async def post_snooze(request: Request) -> Response:
    config, _ = _ctx(request)
    form = await request.form()
    card = _card_field(form)
    days = str(form.get("days") or "")
    if card is None or not days.isdigit() or int(days) not in SNOOZE_DAYS:
        return PlainTextResponse("400 Bad Request: card and days (1, 7 or 30) are needed", 400)
    until = clock.now() + dt.timedelta(days=int(days))
    uistate.set_card(config, login_of(request), card, "snoozed", str(form.get("fp") or "")[:64], until=until)
    return _back(request, _cat(form.get("cat")))


async def post_undo(request: Request) -> Response:
    config, _ = _ctx(request)
    form = await request.form()
    card = _card_field(form)
    if card is None:
        return PlainTextResponse("400 Bad Request: card is needed", 400)
    uistate.undo_card(config, login_of(request), card)
    return _back(request, _cat(form.get("cat")))


async def post_clear_group(request: Request) -> Response:
    """CR-W3: clear every open alert of one cause group; each clear is logged as `clear_alert` with the login. The
    form carries `seen`, the fingerprint of the alerts the page showed: a group that changed since answers 409."""
    from starlette.concurrency import run_in_threadpool
    config, _ = _ctx(request)
    form = await request.form()
    key, seen = str(form.get("group") or ""), str(form.get("seen") or "")
    if not seen:
        return PlainTextResponse("400 Bad Request: group and seen (the alerts the page showed) are needed", 400)
    g = await run_in_threadpool(alertgroups.group, config, key)
    if g is None:
        return PlainTextResponse("404 Not Found: no open alert group %s" % key[:60], 404)
    if g["fp"] != seen:                                          # I-R3: clear only what the person saw
        return PlainTextResponse("409 Conflict: the group %s changed since the page was drawn (%d open now); "
                                 "reload and check it again" % (key[:60], g["count"]), 409)
    login = login_of(request)

    def clear() -> None:
        for s in g["subjects"]:
            try:
                db.clear_alert(s, login, config=config)
            except KeyError:
                pass                                             # cleared meanwhile: nothing to do
    await run_in_threadpool(clear)
    if str(form.get("return") or "") == "inbox":
        return _back(request, _cat(form.get("cat")))
    return RedirectResponse("/alerts", 303)


def router(app: Any = None):
    """GET /inbox (the card view, CR-WF-gui); its POSTs exist only in a local deployment (section 13a), so a
    GitHub + CI deployment keeps exactly the two Run routes (D5)."""
    from fastapi import APIRouter
    from nanosoc_rm.web.plugins import local_routes
    r = APIRouter()
    r.add_api_route("/inbox", dashboard, methods=["GET"], include_in_schema=False)
    if local_routes(app):
        for path, fn in (("/inbox/done", post_done), ("/inbox/snooze", post_snooze), ("/inbox/undo", post_undo),
                         ("/alerts/clear-group", post_clear_group)):
            r.add_api_route(path, fn, methods=["POST"], include_in_schema=False)
    return r
