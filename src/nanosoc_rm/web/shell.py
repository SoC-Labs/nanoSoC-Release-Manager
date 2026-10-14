"""The page shell's data (Wave C spec section 1): the left rail, the command-bar search, breadcrumbs, redaction
and the verdict-to-tone map. Reads stored observations and registry records only; never walks, fetches or
compares inside a request. Templates only loop over what these functions return.

    rail(config, identity, path, nav=()) -> dict          the left rail's blocks
    search(config, q, limit=8, scope=None) -> [hit]       {label, sub, kind, tone, href}
    crumbs(items) -> [(label, href|None)]                 the breadcrumb trail (the last item is the page)
    component_flags(config) -> {component: [(text, tone)]}
    redact(text, config=None) -> str                      URLs become host-name placeholders
    uniform(rows, key) -> bool                            every row shows the same value (a column to hide)
    TONE, tone(verdict)                                   verdict -> chip class
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable, Iterable

from nanosoc_rm import db
from nanosoc_rm.web import routers, verdict as V, views

__all__ = ["TONE", "tone", "rail", "search", "crumbs", "component_flags", "redact", "uniform", "MIN_QUERY",
           "FLAG_ORDER", "RAIL_COMPONENTS"]

# Spec 1.6: verdict to tone. Unknown values are neutral (always shown with their text).
TONE: dict[str, str] = {}
for _tone, _names in (
        ("c-fail", ("FAIL", "UNVERIFIED", "UNREADABLE", "MISSING", "UNCOVERED", "DIVERGED", "fail", "unverified",
                    "failed", "UNRELATED", "LOCAL_URL", "NO_GITLINK", "REWRITTEN", "PR_ONLY", "ORPHAN", "exposed",
                    "affected", "SSH PIN")),
        ("c-warn", ("WARN", "STALE", "BRANCH_ONLY", "probably-exposed", "FLAKY", "warn", "BEHIND",
                    "probably-not-affected")),
        ("c-pass", ("PASS", "MERGED", "TAGGED", "PROTECTED_BY_TAG", "ON_DEFAULT", "SAME", "not-affected", "pass",
                    "done", "covered")),
        ("c-waived", ("WAIVED", "waived")),
        ("c-notrun", ("NOT-RUN", "not-reached", "queued", V.NO_RESULT, "cancelled", "UNKNOWN")),
        ("c-infra", ("INFRA", "ACTIVE", "AHEAD", "infra")),
        ("c-ink", ("PROTECTED", "frozen", "FROZEN")),
        ("c-split", ("PINNED", "SPLIT")),
        ("c-run", ("running",))):
    for _n in _names:
        TONE[_n] = _tone
MIN_QUERY = 2
FLAG_ORDER = ("FAIL", "UNCOVERED", "UNREADABLE", "SSH PIN", "SPLIT")
RAIL_COMPONENTS = 8                      # rows in the rail's "In my configurations" group
_HEX = re.compile(r"^[0-9a-f]{3,40}$")          # spec 1.4 says 4; its test list searches "4de"
_URL = re.compile(r"(?:https?|ssh|git|file)://[^\s'\"<>)]+|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s'\"<>)]+")


def tone(value: Any) -> str:
    return TONE.get(str(value), "c-neutral")


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# ---------------------------------------------------------------------------------------------------------
# Flags and the rail
# ---------------------------------------------------------------------------------------------------------
def _decisions(config) -> dict[str, tuple[dict | None, V.Decision]]:
    out = {}
    for rec in views.configurations(config):
        cid = rec["fact"]["id"]
        docs = views.compat_results(config, cid)
        doc = docs[0] if docs else None
        out[cid] = (doc, V.deciding(doc, rec))
    return out


def component_flags(config, decisions: dict | None = None) -> dict[str, list[tuple[str, str]]]:
    """{component: [(text, tone)]} from stored data only, in FLAG_ORDER (spec 1.3)."""
    found: dict[str, set[str]] = {}

    def add(comp: str | None, flag: str) -> None:
        if comp:
            found.setdefault(comp, set()).add(flag)
    for d in db.latest("pin_state", "pin_state:", config=config):
        f = d["fact"]
        comp = (f.get("child") or {}).get("component")
        if f.get("url_kind") == "ssh":
            add(comp, "SSH PIN")
        if f.get("split"):
            add(comp, "SPLIT")
        _, h = views.pinned_host(d)
        if h is not None and h.get("state") == "UNREADABLE":
            add(comp, "UNREADABLE")
    for d in views.silicon_protection(config):
        if not d["fact"].get("covered"):
            add(d["fact"].get("component"), "UNCOVERED")
    for doc, dec in (decisions if decisions is not None else _decisions(config)).values():
        if doc is None or dec.gate is None:
            continue
        paths = set(dec.tree_paths)
        for p in doc["fact"].get("pins") or []:
            if p.get("tree_path") in paths:
                add(p.get("component"), "FAIL")
    return {c: [(f, TONE.get(f, "c-fail") if f != "SPLIT" else "c-split") for f in FLAG_ORDER if f in fs]
            for c, fs in found.items()}


def _cfg_href(cid: str, tab: str = "") -> str:
    return "/configurations/%s%s" % (_quote(cid), tab)


def rail(config, identity: Any = None, path: str = "", nav: Iterable[tuple] = ()) -> dict:
    """The left rail (spec 1.3). Every block from stored data; a block whose module is not built here is empty."""
    login = getattr(identity, "login", "") or ""
    notes = routers.optional("notifications") or routers.optional("inbox")
    count = getattr(notes, "unread_count", None) or getattr(notes, "open_count", None)
    try:
        open_n = int(count(config, login)) if count is not None else 0
    except Exception:                                            # noqa: BLE001 - the rail never breaks a page
        open_n = 0
    decisions = _decisions(config)
    configs = []
    for rec in views.configurations(config):
        cid = rec["fact"]["id"]
        doc, dec = decisions[cid]
        if doc is None:
            cause = "never checked · %d pins" % len(views.pins(config, cid))
            dot = "d-notrun"
        else:
            cause = dec.label if dec.gate else ("frozen" if views.is_frozen(rec) else "no red gate")
            dot = "d-fail" if dec.verdict in V.RED else "d-pass" if dec.verdict in V.GREEN else "d-notrun"
        href = _cfg_href(cid)
        configs.append({"id": cid, "href": href, "verdict": "NOT CHECKED" if doc is None else dec.verdict,
                        "tone": tone(dec.verdict), "dot": dot, "cause": cause,
                        "current": path == href or path.startswith(href + "/")})
    nav = list(nav)
    has_matrix = any(href.rstrip("/") == "/matrix" for _, href, _ in nav)
    flags = component_flags(config, decisions)
    in_tree: dict[str, int] = {}
    for d in db.latest("pin_state", "pin_state:", config=config):
        comp = (d["fact"].get("child") or {}).get("component")
        if comp:
            in_tree[comp] = in_tree.get(comp, 0) + 1
    order = sorted(in_tree, key=lambda c: (0 if flags.get(c) else 1, c))
    comps = [{"id": c, "href": "/components/%s" % _quote(c), "chips": flags.get(c, [])[:2],
              "sub": "%d pin%s" % (in_tree[c], "" if in_tree[c] == 1 else "s"),
              "current": path == "/components/%s" % _quote(c) or path.startswith("/components/%s/" % _quote(c))}
             for c in order[:RAIL_COMPONENTS]]
    try:
        n_components = len(views.components(config))
    except Exception:                                            # noqa: BLE001
        n_components = 0
    alerts = _alert_lines(config)
    tools = [{"label": label, "href": href,
              "current": any(path == p or (p != "/" and path.startswith(p.rstrip("/") + "/")) for p in prefixes)}
             for label, href, prefixes in nav if href != "/"]
    return {"inbox": {"open": open_n, "href": "/notifications", "current": path == "/notifications"},
            "configurations": configs, "matrix": has_matrix,
            "matrix_current": path.startswith("/matrix"),
            "components": comps, "components_more": max(0, len(in_tree) - len(comps)),
            "n_components": n_components, "alerts": alerts, "tools": tools}


def _alert_lines(config) -> list[dict]:
    """One line per alert cause group (web.alertgroups, unit I). Until it is built: one line for all open
    alerts, so the rail never hides that there are some."""
    groups_mod = routers.optional("alertgroups")
    if groups_mod is not None and hasattr(groups_mod, "groups"):
        try:
            return [{"label": "%d %s" % (g["count"], g["label"]), "sub": g.get("advice") or "",
                     "href": "/alerts#group-%s" % g["key"]} for g in groups_mod.groups(config)]
        except Exception:                                        # noqa: BLE001
            pass
    try:
        n = len(db.alerts(open_only=True, config=config))
    except Exception:                                            # noqa: BLE001
        n = 0
    return [{"label": "%d open alert%s" % (n, "" if n == 1 else "s"), "sub": "", "href": "/alerts"}] if n else []


# ---------------------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------------------
def _has(module: str) -> bool:
    return routers.optional(module) is not None


def search(config, q: str, limit: int = 8, scope: str | None = None) -> list[dict]:
    """Command-bar hits (spec 1.4): configurations, components, commits, branches, errata and defects, then store
    artefacts (section 22: path, name, commit or md5), in that order, cut at `limit`. Case-insensitive substring on the label; a hex query of 4 to 40 characters also
    matches commit prefixes. `scope="component"` searches components only."""
    q = (q or "").strip()
    if len(q) < MIN_QUERY:
        return []
    ql = q.lower()
    hexq = ql if _HEX.match(ql) else None
    out: list[dict] = []
    seen: set[str] = set()
    cfgspace, compspace = _has("cfgspace"), _has("compspace")

    def add(label: str, sub: str, kind: str, tone_: str, href: str) -> bool:
        if href in seen:
            return len(out) < limit
        seen.add(href)
        out.append({"label": label, "sub": sub, "kind": kind, "tone": tone_, "href": href})
        return len(out) < limit


    def configurations() -> bool:
        for rec in views.configurations(config):
            cid = rec["fact"]["id"]
            if ql in cid.lower():
                r = rec["fact"].get("record") or {}
                sub = "configuration · %s" % ("frozen" if views.is_frozen(rec) else r.get("branch") or "")
                if not add(cid, sub.rstrip(" ·"), "config", "c-ink", _cfg_href(cid)):
                    return False
        return True

    def components() -> bool:
        for rec in views.components(config):
            cid = rec["fact"]["id"]
            if ql in cid.lower():
                canon = (rec["fact"].get("record") or {}).get("canonical") or {}
                sub = "component · %s" % canon.get("host", "") if canon else "component"
                if not add(cid, sub, "component", "c-neutral", "/components/%s" % _quote(cid)):
                    return False
        return True

    def commits() -> bool:
        if hexq is None:
            return True
        for d in db.latest("pin_state", "pin_state:", config=config):
            f = d["fact"]
            child = f.get("child") or {}
            if (child.get("commit") or "").startswith(hexq):
                cid, tp = f.get("configuration") or "", f["tree_paths"][0]
                _, h = views.pinned_host(d)
                state = (h or {}).get("state") or f.get("state") or ""
                href = (_cfg_href(cid, "/tree?node=%s" % _quote(tp)) if cfgspace else _cfg_href(cid))
                if not add(child["commit"][:7], "%s in %s · %s" % (child.get("component") or tp, cid, state),
                           "commit", tone(state), href if cfgspace else href + "#pin-" + _quote(tp)):
                    return False
        for rec in views.configurations(config):
            sha = str((rec["fact"].get("record") or {}).get("commit") or "")
            if sha.startswith(hexq):
                if not add(sha[:7], "root of %s" % rec["fact"]["id"], "commit", "c-ink", _cfg_href(rec["fact"]["id"])):
                    return False
        for d in views.silicon_protection(config):
            f = d["fact"]
            if (f.get("commit") or "").startswith(hexq):
                href = (_cfg_href(f["configuration"], "/silicon") if cfgspace
                        else "/silicon-protection?configuration=%s" % _quote(f["configuration"]))
                if not add(f["commit"][:7], "%s silicon commit in %s · %s" % (
                        f["component"], f["configuration"], "covered" if f.get("covered") else "UNCOVERED"),
                        "commit", "c-pass" if f.get("covered") else "c-fail", href):
                    return False
        for d in db.latest("quality_finding", "quality_finding:", config=config):
            parts = d["subject"].split(":")
            if len(parts) >= 3 and parts[2].startswith(hexq):
                target = parts[1]
                href = "/components/%s/quality" % _quote(target) if compspace else "/quality/%s" % _quote(target)
                if not add(parts[2][:7], "quality of %s" % target, "commit", "c-neutral", href):
                    return False
        return True

    def branches() -> bool:
        for d in db.latest("branch_state", "branch_state:", config=config):
            f = d["fact"]
            b = str(f.get("branch") or "")
            if ql in b.lower():
                comp = f.get("component") or ""
                href = ("/components/%s/branches?q=%s" % (_quote(comp), _quote(b)) if compspace
                        else "/branches?component=%s" % _quote(comp))
                if not add(b, "branch of %s · %s" % (comp, f.get("verdict", "")), "branch", tone(f.get("verdict")),
                           href):
                    return False
        return True

    def ids() -> bool:
        for d in views.errata(config):
            eid = d["fact"]["id"]
            if ql in eid.lower():
                st = (d["fact"].get("record") or {}).get("status", "")
                if not add(eid, "erratum · %s" % st, "erratum", "c-warn" if st in ("open", "permanent") else "c-neutral",
                           "/errata#%s" % _quote(eid)):
                    return False
        for d in views.defects(config):
            did = d["fact"]["id"]
            if ql in did.lower():
                if not add(did, "defect", "defect", "c-neutral", "/defects/%s" % _quote(did)):
                    return False
        return True

    def store() -> bool:
        """Section 21: artefacts, builds and manifests of the stored store index, by path, name, commit or md5."""
        try:
            from nanosoc_rm.store import views as store_views
        except ModuleNotFoundError:
            return True
        for h in store_views.search(config, q, limit=limit):
            if not add(h["label"], "store · " + h["sub"], "artefact", h["tone"], h["href"]):
                return False
        return True

    sources = [components] if scope == "component" else [configurations, components, commits, branches, ids, store]
    for src in sources:
        try:
            if not src():
                break
        except Exception:                                        # noqa: BLE001 - one unreadable source: skip it
            continue
    return out[:limit]


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def crumbs(items: Iterable[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """The trail as given, with the last item never a link (it is the page itself)."""
    out = [(str(label), href) for label, href in items if label]
    if out:
        out[-1] = (out[-1][0], None)
    return out


def _host_names(config) -> list[tuple[str, list[str]]]:
    out = []
    try:
        hosts = config.get("hosts", {}) or {}
    except Exception:                                            # noqa: BLE001
        hosts = {}
    for name, table in hosts.items():
        names = []
        for key in ("web", "git", "ssh", "api"):
            v = str((table or {}).get(key) or "")
            h = urllib.parse.urlsplit(v).hostname if "://" in v else v.split("@")[-1].split(":")[0]
            if h:
                names.append(h.lower())
        out.append((name, names))
    return out


def redact(text: Any, config=None) -> str:
    """The text with every URL (and scp-style `user@host:path`) replaced by `<name>`, where name is the service
    config's host key for that host (`github`, `gitlab`), or `<url>` when no configured host matches. Alert
    messages pass through this before display, so no hostname or credential-bearing URL reaches a page."""
    hosts = _host_names(config) if config is not None else []

    def sub(m: re.Match) -> str:
        u = m.group(0)
        host = (urllib.parse.urlsplit(u).hostname or "") if "://" in u else u.split("@", 1)[1].split(":", 1)[0]
        host = host.lower()
        for name, names in hosts:
            if host and host in names:
                return "<%s>" % name
        return "<url>"
    return _URL.sub(sub, str(text or ""))


def uniform(rows: Iterable[Any], key: Callable[[Any], Any] | str) -> bool:
    """True when every row gives the same value (and there is at least one row): a column the board can hide."""
    get = key if callable(key) else (lambda r: r.get(key))
    vals = [get(r) for r in rows]
    return bool(vals) and all(v == vals[0] for v in vals)
