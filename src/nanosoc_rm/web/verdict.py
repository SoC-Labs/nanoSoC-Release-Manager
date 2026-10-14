"""The deciding gate of a stored compat result (Wave C spec section 2.2), shared by the inbox, the board, the rail
and the configuration Overview. Pure functions over one `compat_result` document: no store, no git, no request.

    deciding(doc, record=None) -> Decision        the one cause that makes the configuration red, and its label
    causes(doc, record=None) -> [Cause]           every red cause, the deciding one first
    next_action(decision, cid, https_url=None)    {label, href, code, method}: the single primary action
    unused_waivers(doc, record) -> [waiver]       registry waivers that cover no failing item
    https_url(config, component) -> str | None    the canonical repo's https clone URL (the only impure helper)

Rules (spec 2.2):
1. `run_verdict or verdict`. PASS, WARN or WAIVED: no gate, the label is the verdict alone.
2. Candidates: rows with verdict FAIL, then UNVERIFIED (never WAIVED), in `t0.gates.GATES` order. The tier row
   `tier.<t>` counts only when no T0 gate is red.
3. Each candidate's failing tree paths: pins whose `items[GATE_ITEM[gate]]` is fail or unverified.
4. Same cause: gates whose failing paths are the same set and include a pin with url_kind ssh collapse into one
   cause, "{path} pinned by ssh URL" (fetchable + no-ssh is the known pair). Its not-reached count is the pins with
   a `not-reached` item below that path.
5. The deciding cause has the most gates; a tie goes to the most not-reached pins, then to GATES order.
6. Labels from GATE_LABEL; an unknown gate falls back to its id.
7. `infra`: the tier row or a gate is INFRA ("This host cannot run {tier}. Nothing to fix in the design.").
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

__all__ = ["Decision", "Cause", "GATE_ITEM", "GATE_LABEL", "GATE_ORDER", "deciding", "causes", "next_action",
           "unused_waivers", "https_url", "verdict_of", "NO_RESULT"]

# t0.gates.GATES order (configuration gates; registry gates never appear in a configuration's compat result).
GATE_ORDER = ("t0.infra.probe", "t0.runner.hardened", "t0.pins.fetchable", "t0.pins.reachable",
              "t0.urls.no-ssh-public", "t0.manifest.valid", "t0.clone.upgrade-path", "t0.vendor.no-arm-ip",
              "t0.registry.schema", "t0.registry.refs-resolve")
# A gate's per-pin item column (analysis.compat.GATE_COLUMNS).
GATE_ITEM = {"t0.pins.fetchable": "fetchable", "t0.pins.reachable": "reachable", "t0.urls.no-ssh-public": "no_ssh",
             "t0.manifest.valid": "manifest", "t0.vendor.no-arm-ip": "vendor"}
GATE_LABEL = {
    "t0.vendor.no-arm-ip": "vendor scan on {paths}",
    "ssh-pin": "{path} pinned by ssh URL",
    "t0.clone.upgrade-path": "upgrade path from {upgrade_from} does not clone anonymously",
    "t0.pins.reachable": "{n} pins not reachable",
    "t0.manifest.valid": "manifest invalid in {paths}",
    "t0.infra.probe": "anonymous git does not work on this host",
}
RED = ("FAIL", "UNVERIFIED")
GREEN = ("PASS", "WARN", "WAIVED")
NO_RESULT = "NOT-CHECKED"
_PATHS_SHOWN = 3
_UPGRADE = re.compile(r"upgrade_from\s+([0-9a-f]{7,40})")


@dataclasses.dataclass(frozen=True)
class Cause:
    gates: tuple[str, ...]              # gate ids, GATES order
    kind: str                           # ssh-pin | vendor | upgrade-path | reachable | manifest | probe | tier | gate
    label: str
    tree_paths: tuple[str, ...]
    not_reached: int = 0
    component: str | None = None        # ssh-pin: the pinned component
    verdict: str = "FAIL"


@dataclasses.dataclass(frozen=True)
class Decision:
    verdict: str                        # the run verdict, or NO_RESULT
    gate: str | None                    # the deciding cause's first gate id (None when nothing is red)
    label: str
    subject: str | None                 # what the cause is about: a tree path, or None
    tree_paths: tuple[str, ...]
    same_cause_gates: tuple[str, ...]
    separate_gates: tuple[str, ...]
    infra: bool
    infra_text: str | None = None
    kind: str = "none"
    not_reached: int = 0
    component: str | None = None

    @property
    def sentence(self) -> str:
        """The one line the header shows: "FAIL: tidelink pinned by ssh URL"."""
        return self.verdict if self.label == self.verdict else "%s: %s" % (self.verdict, self.label)


def verdict_of(doc: dict | None) -> str:
    if not doc:
        return NO_RESULT
    f = doc.get("fact") or {}
    return f.get("run_verdict") or f.get("verdict") or NO_RESULT


def _order(gid: str) -> int:
    return GATE_ORDER.index(gid) if gid in GATE_ORDER else len(GATE_ORDER)


def _join(paths: list[str]) -> str:
    if len(paths) <= _PATHS_SHOWN:
        return ", ".join(paths)
    return "%s and %d more" % (", ".join(paths[:_PATHS_SHOWN]), len(paths) - _PATHS_SHOWN)


def _failing(pins: list[dict], gid: str) -> list[str]:
    col = GATE_ITEM.get(gid)
    if not col:
        return []
    return [p["tree_path"] for p in pins if (p.get("items") or {}).get(col) in ("fail", "unverified")]


def _not_reached_below(pins: list[dict], paths: tuple[str, ...]) -> int:
    return sum(1 for p in pins if "not-reached" in (p.get("items") or {}).values()
               and any(p["tree_path"].startswith(t + "/") for t in paths))


def _tier_row(f: dict) -> dict | None:
    t = f.get("tier")
    if not isinstance(t, dict) or not t.get("tier"):
        return None
    return {"id": "tier.%s" % t["tier"], "verdict": t.get("verdict"), "detail": t.get("reason") or ""}


def _upgrade_from(f: dict, record: dict | None) -> str | None:
    rec = ((record or {}).get("fact") or {}).get("record") or {}
    uf = rec.get("upgrade_from")
    if isinstance(uf, dict):                       # eligibility.yaml: {tag: v1.0.0} or {commit: <sha>}
        named = uf.get("tag") or str(uf.get("commit") or "")[:7]
        if named:
            return str(named)
    elif uf:
        return str(uf)[:7]
    for g in f.get("gates") or []:
        if g.get("id") == "t0.clone.upgrade-path":
            m = _UPGRADE.search(g.get("detail") or "")
            if m:
                return m.group(1)[:7]
    return None


def _label(gid: str, paths: list[str], f: dict, record: dict | None) -> tuple[str, str]:
    """(kind, label) of a single-gate cause."""
    if gid.startswith("tier."):
        return "tier", "%s is %s" % (gid.split(".", 1)[1].upper(), "red")
    if gid == "t0.vendor.no-arm-ip":
        return "vendor", GATE_LABEL[gid].format(paths=_join(paths) or "the tree")
    if gid == "t0.clone.upgrade-path":
        frm = _upgrade_from(f, record)
        return "upgrade-path", (GATE_LABEL[gid].format(upgrade_from=frm) if frm
                                else "upgrade path does not clone anonymously")
    if gid == "t0.pins.reachable":
        return "reachable", GATE_LABEL[gid].format(n=len(paths))
    if gid == "t0.manifest.valid":
        return "manifest", GATE_LABEL[gid].format(paths=_join(paths) or "the tree")
    if gid == "t0.infra.probe":
        return "probe", GATE_LABEL[gid]
    return "gate", gid


def causes(doc: dict | None, record: dict | None = None) -> list[Cause]:
    """Every red cause of the result, the deciding one first (empty when nothing is red)."""
    if not doc:
        return []
    f = doc.get("fact") or {}
    pins = list(f.get("pins") or [])
    rows = [g for g in f.get("gates") or [] if g.get("verdict") in RED]
    rows.sort(key=lambda g: (RED.index(g["verdict"]), _order(g["id"])))
    if not rows:
        tier = _tier_row(f)
        if tier and tier["verdict"] in RED:
            rows = [tier]
    ssh_paths = {p["tree_path"] for p in pins if p.get("url_kind") == "ssh"}
    groups: dict[frozenset, list[dict]] = {}
    singles: list[tuple[dict, list[str]]] = []
    for g in rows:
        paths = _failing(pins, g["id"])
        key = frozenset(paths)
        if key and key & ssh_paths:
            groups.setdefault(key, []).append(g)
        else:
            singles.append((g, paths))
    out: list[Cause] = []
    for key, gs in groups.items():
        if len(gs) == 1:
            singles.append((gs[0], sorted(key)))
            continue
        ssh = sorted(key & ssh_paths)
        comp = next((p.get("component") for p in pins if p["tree_path"] == ssh[0]), None)
        tps = tuple(sorted(key))
        out.append(Cause(gates=tuple(sorted((g["id"] for g in gs), key=_order)), kind="ssh-pin",
                         label=GATE_LABEL["ssh-pin"].format(path=_join(ssh)), tree_paths=tps,
                         not_reached=_not_reached_below(pins, tps), component=comp,
                         verdict="FAIL" if any(g["verdict"] == "FAIL" for g in gs) else "UNVERIFIED"))
    for g, paths in singles:
        kind, label = _label(g["id"], paths, f, record)
        tps = tuple(paths)
        out.append(Cause(gates=(g["id"],), kind=kind, label=label, tree_paths=tps,
                         not_reached=_not_reached_below(pins, tps), verdict=g["verdict"]))
    out.sort(key=lambda c: (-len(c.gates), -c.not_reached, RED.index(c.verdict), min(_order(x) for x in c.gates)))
    return out


def _infra(f: dict) -> tuple[bool, str | None]:
    tier = _tier_row(f)
    if tier and tier["verdict"] == "INFRA":
        return True, "This host cannot run %s. Nothing to fix in the design. %s" % (
            tier["id"].split(".", 1)[1].upper(), tier["detail"])
    g = next((g for g in f.get("gates") or [] if g.get("verdict") == "INFRA"), None)
    if g is not None:
        return True, "This host cannot run %s. Nothing to fix in the design. %s" % (g["id"], g.get("detail") or "")
    return False, None


def deciding(doc: dict | None, record: dict | None = None) -> Decision:
    """The deciding gate (module docstring). With no result: verdict NO_RESULT and the label "never checked"."""
    if not doc:
        return Decision(NO_RESULT, None, "never checked", None, (), (), (), False, None, "no-result")
    f = doc.get("fact") or {}
    v = verdict_of(doc)
    infra, infra_text = _infra(f)
    cs = causes(doc, record) if v not in GREEN else []
    if not cs:
        if v == "INFRA":
            tier = _tier_row(f)
            gid = tier["id"] if tier and tier["verdict"] == "INFRA" else next(
                (g["id"] for g in f.get("gates") or [] if g.get("verdict") == "INFRA"), None)
            return Decision(v, gid, "this host cannot run it; nothing to fix in the design", None, (), (), (),
                            True, infra_text, "infra")
        return Decision(v, None, v, None, (), (), (), infra, infra_text, "none")
    top, rest = cs[0], cs[1:]
    subject = top.tree_paths[0] if top.kind == "ssh-pin" and top.tree_paths else (
        ", ".join(top.tree_paths) if top.tree_paths else None)
    return Decision(v, top.gates[0], top.label, subject, top.tree_paths, top.gates,
                    tuple(g for c in rest for g in c.gates), infra, infra_text, top.kind, top.not_reached,
                    top.component)


def _submodule_name(path: str, doc: dict | None) -> str:
    """The pin's path in its parent's .gitmodules: the tree path minus the nearest pinned ancestor."""
    pins = [p["tree_path"] for p in ((doc or {}).get("fact") or {}).get("pins") or []]
    parents = [p for p in pins if path.startswith(p + "/")]
    return path[len(max(parents, key=len)) + 1:] if parents else path


def next_action(decision: Decision | None, cid: str, https_url: str | None = None,
                doc: dict | None = None) -> dict | None:
    """The single primary action for a decision: {label, href, code, method}. None when nothing is red."""
    if decision is None or decision.verdict == NO_RESULT:
        return {"label": "Run first check", "href": "/configurations/%s/runs" % cid, "code": None, "method": "post"}
    if decision.gate is None:
        return None
    if decision.kind == "ssh-pin":
        name = _submodule_name(decision.subject or "", doc)
        url = https_url or "<https URL of %s>" % (decision.component or name)
        code = "git config -f .gitmodules submodule.%s.url %s\ngit submodule sync %s" % (name, url, name)
        return {"label": "Try the fix in What-if", "code": code, "method": "get",
                "href": "/configurations/%s/whatif?component=%s" % (cid, decision.component or "")}
    if decision.kind == "vendor":
        node = decision.tree_paths[0] if decision.tree_paths else ""
        return {"label": "Show that tree", "href": "/configurations/%s/tree?node=%s" % (cid, node), "code": None,
                "method": "get"}
    if decision.kind == "upgrade-path":
        return {"label": "Open the gate", "href": "/configurations/%s#gate-t0.clone.upgrade-path" % cid, "code": None,
                "method": "get"}
    return {"label": "Open the gate", "href": "/configurations/%s#gate-%s" % (cid, decision.gate), "code": None,
            "method": "get"}


def unused_waivers(doc: dict | None, record: dict | None) -> list[dict]:
    """Registry waivers (a frozen record's t0_waivers) whose tree_path no gate's `waived[].covered_by` names."""
    rec = ((record or {}).get("fact") or {}).get("record") or {}
    waivers = list(rec.get("t0_waivers") or []) if rec.get("frozen") else []
    used = {w.get("covered_by") for g in ((doc or {}).get("fact") or {}).get("gates") or []
            for w in g.get("waived") or []}
    return [w for w in waivers if w.get("tree_path") not in used]


def https_url(config: Any, component: str) -> str | None:
    """The https clone URL of the component's canonical repo, from the registry record and the service config's
    host table (`hosts.<key>.web`). None when either is missing."""
    from nanosoc_rm.web import views
    rec = next((c for c in views.components(config) if c["fact"]["id"] == component), None)
    canon = (((rec or {}).get("fact") or {}).get("record") or {}).get("canonical") or {}
    host, path = canon.get("host"), canon.get("path")
    if not host or not path:
        return None
    try:
        web = str(config.host(host).get("web") or "")
    except Exception:                                            # noqa: BLE001 - an unknown host: no URL
        return None
    return "%s/%s.git" % (web.rstrip("/"), path) if web.startswith("https://") else None
