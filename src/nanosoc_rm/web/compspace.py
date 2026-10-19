"""The component workspace (spec section 4): `/components/{id}` and its tabs.

    GET  /components/{id}                     Overview (web.routers.DELEGATES hands the app's page over)
    GET  /components/{id}/branches            Branches: every copy by default, the delete list first (fix 4)
    GET  /components/{id}/quality             Quality: stored facts only, banners as one rule
    GET  /components/{id}/versions            303 to the Versions plug-in, or a placeholder
    GET  /components/{id}/review              Review queue: one finding at a time, with the patch basket
    GET  /components/{id}/review/patch.diff   the combined patch (text/x-diff), or 404 with the reason
    POST /components/{id}/review/decide       local only: accept, edit, skip or dismiss one finding
    POST /components/{id}/review/basket       local only: remove, banner-on/off, readme-add/remove, clear
    GET  /branches                            the picker (?component=X answers 303 to the Branches tab)

Every page reads stored observations, registry records and the person's own state in gui.sqlite. A request never
walks, fetches, analyses or compares: a missing result shows the job button or the CLI command. Blobs for code
excerpts and the patch are read only when they are already local (quality.review_text).

Pure helpers (unit-tested): `sentence`, `branch_why`, `branch_view`, `pin_meaning`, `quality_view`, `tabs`.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from fastapi import APIRouter
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import db
from nanosoc_rm.quality.findings import BULK_KINDS, is_bulk
from nanosoc_rm.web import lines, plugins, registry_facts, review, shell, uistate, verdict as V, views
from nanosoc_rm.web.page import Page, env
from nanosoc_rm.web.plugins import login_of

__all__ = ["router", "component", "branches", "sentence", "branch_why", "branch_view", "pin_meaning",
           "quality_view", "tabs", "header", "BRANCH_FILTERS"]

BRANCH_FILTERS = ("deletable", "kept", "MERGED", "STALE", "PROTECTED", "ACTIVE", "PINNED")
TAB_LABEL = {"overview": "Overview", "branches": "Branches", "quality": "Quality", "versions": "Versions",
             "review": "Review"}
MAX_FLAGS = 3


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _href(comp: str, tab: str = "", **params: Any) -> str:
    base = "/components/%s%s" % (_q(comp), ("/" + tab) if tab and tab != "overview" else "")
    items = [(k, v) for k, v in params.items() if v not in (None, "", False, [])]
    flat = []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            flat += [(k, x) for x in v]
        else:
            flat.append((k, v))
    return base + ("?" + urllib.parse.urlencode(flat) if flat else "")


# ---------------------------------------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------------------------------------
def _bkey(f: dict) -> str:
    return "%s:%s:%s" % (f["repo"]["host"], f["repo"]["path"], f["branch"])


def _kept(f: dict) -> bool:
    return bool(f.get("tags") or f.get("pins") or f.get("cited"))


def branch_why(f: dict) -> str:
    """One line: why this branch has its verdict, from the stored row only."""
    default = f.get("default_branch") or "the default branch"
    if f.get("tags"):
        return "Kept: tag %s sits on its tip." % f["tags"][0]
    if f.get("pins"):
        p = f["pins"][0]
        return "Kept: pinned by %s:%s." % (p.get("configuration", "?"), p.get("tree_path", "?"))
    if f.get("cited"):
        return "Kept: cited by %s." % f["cited"][0].get("file", "the registry")
    v = f.get("verdict")
    if v == "MERGED":
        return "Every commit is on %s (%d ahead)." % (default, int(f.get("ahead") or 0))
    if v == "STALE":
        return "No commit since %s and %d behind %s." % ((f.get("last_commit_at") or "")[:10],
                                                         int(f.get("behind") or 0), default)
    if v == "ACTIVE":
        return "Last commit %s, within %s days: still in use." % ((f.get("last_commit_at") or "")[:10],
                                                                    f.get("stale_days") or 90)
    return v or ""


def _stored_branches(config: Any, comp: str) -> list[dict]:
    rows = [d for d in db.latest("branch_state", "branch_state:", config=config) if d["fact"]["component"] == comp]
    return sorted(rows, key=lambda d: (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"],
                                       not d["fact"].get("deletable"), d["fact"]["branch"]))


def _canonical(config: Any, comp: str) -> tuple[str, str] | None:
    rec = next((d for d in views.components(config) if d["fact"]["id"] == comp), None)
    canon = ((rec or {}).get("fact", {}).get("record") or {}).get("canonical") or {}
    return (canon["host"], canon["path"]) if canon.get("host") and canon.get("path") else None


def branch_view(rows: list[dict], *, canonical: tuple[str, str] | None = None, only_canonical: bool = False,
                verdict: str = "", q: str = "", picked: bool = False, selected: list[str] | tuple = ()) -> dict:
    """The Branches tab model. `rows`: every stored branch_state of the component. Every copy is counted unless
    `only_canonical`. The first load (`picked` false) ticks every deletable row; afterwards `selected` holds the
    ticked keys ("{host}:{path}:{branch}"). The delete list is built from the ticked rows only."""
    from nanosoc_rm.analysis.branches import delete_commands, delete_list
    every = list(rows)
    if only_canonical and canonical is not None:
        rows = [d for d in rows if (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) == canonical]
    deletable = delete_list(rows)
    del_keys = [_bkey(d["fact"]) for d in deletable]
    chosen = set(del_keys) if not picked else set(selected) & set(del_keys)
    sel_docs = [d for d in deletable if _bkey(d["fact"]) in chosen]
    # ticked rows outside the current scope (canonical-only hides the copies) ride along unchanged, so a scope
    # or filter link never re-ticks or drops them (review K-R1)
    every_del = {_bkey(d["fact"]) for d in delete_list(every)}
    carry = sorted(chosen | ((set(selected) & every_del) if picked else set()))
    counts: dict[str, int] = {}
    for d in rows:
        counts[d["fact"]["verdict"]] = counts.get(d["fact"]["verdict"], 0) + 1
    kept = [d for d in rows if _kept(d["fact"])]
    if verdict not in BRANCH_FILTERS:
        verdict = ""
    ql = q.strip().lower()

    def keep(d: dict) -> bool:
        f = d["fact"]
        if ql and ql not in f["branch"].lower():
            return False
        if verdict == "deletable":
            return _bkey(f) in del_keys
        if verdict == "kept":
            return _kept(f)
        return not verdict or f["verdict"] == verdict
    shown = []
    for d in rows:
        if not keep(d):
            continue
        f = d["fact"]
        k = _bkey(f)
        shown.append({"doc": d, "key": k, "deletable": k in del_keys, "checked": k in chosen,
                      "why": branch_why(f), "tone": shell.tone(f["verdict"])})
    shown_keys = {r["key"] for r in shown}
    warnings = ["%s has %d commit%s not on %s; %s go with it." % (
        d["fact"]["branch"], d["fact"]["ahead"], "" if d["fact"]["ahead"] == 1 else "s",
        d["fact"].get("default_branch") or "the default branch", "it" if d["fact"]["ahead"] == 1 else "they")
        for d in sel_docs if d["fact"]["verdict"] == "STALE" and int(d["fact"].get("ahead") or 0) > 0]
    repos = sorted({(d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) for d in every})
    filters = [{"key": "", "label": "All", "count": len(rows)},
               {"key": "deletable", "label": "Deletable", "count": len(deletable)},
               {"key": "kept", "label": "Kept", "count": len(kept)}] + \
              [{"key": v, "label": v, "count": counts.get(v, 0)} for v in BRANCH_FILTERS[2:]]
    return {"rows": shown, "total": len(rows), "every": len(every), "deletable": len(deletable),
            "selected": len(sel_docs), "counts": counts, "kept": len(kept), "verdict": verdict, "q": q,
            "delete_text": "\n".join(delete_commands(sel_docs)) if sel_docs else "",
            "warnings": warnings, "hidden": [k for k in carry if k not in shown_keys], "carry": carry if picked else [], "filters": filters,
            "only_canonical": only_canonical, "canonical": canonical, "repos": repos,
            "canonical_empty": bool(only_canonical and not rows and every), "picked": picked,
            "all_keys": del_keys}


# ---------------------------------------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------------------------------------
def quality_view(config: Any, comp: str, ref: str | None = None, commit: str | None = None) -> dict:
    """The Quality tab model from the stored facts (comments, density, readme, docs) of one analysed commit."""
    comments = review.stored_fact(config, comp, "comments", ref=ref, commit=commit)
    at = (comments or {}).get("fact", {}).get("commit") if comments else commit
    density = review.stored_fact(config, comp, "density", ref=None if at else ref, commit=at)
    readme = review.stored_fact(config, comp, "readme", ref=None if at else ref, commit=at)
    docs = review.stored_fact(config, comp, "docs", ref=None if at else ref, commit=at)
    out: dict[str, Any] = {"comments": comments, "density": density, "readme": readme, "docs": docs,
                           "analysed": any((comments, density, readme, docs))}
    findings = (comments or {}).get("fact", {}).get("findings") or []
    banner = [f for f in findings if is_bulk(f)]             # the decoration rule: banner + boilerplate
    rest = [f for f in findings if not is_bulk(f)]
    by_kind: dict[str, int] = {}
    for f in rest:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    top = max(by_kind.values()) if by_kind else 0
    out.update(total=len(findings), banner=len(banner), worth=len(rest),
               banner_kinds={k: sum(1 for f in banner if f.get("kind") == k) for k in BULK_KINDS},
               banner_files=len({f.get("path") for f in banner}),
               banner_share=round(100 * len(banner) / len(findings)) if findings else 0,
               kinds=[{"kind": k, "count": n, "width": round(100 * n / top) if top else 0}
                      for k, n in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))],
               worst=review.queue(comments)[:5] if comments else [])
    if density:
        d = density["fact"]
        over = [l for l in d.get("languages") or [] if l.get("over")]
        worst = max(over, key=lambda l: l.get("ratio") or 0) if over else None
        ratio = (d.get("totals") or {}).get("ratio")
        out.update(ratio=ratio, ratio_text="%.1f%%" % (100 * ratio) if ratio is not None else "-",
                   code_files=(d.get("totals") or {}).get("files"),
                   outliers=len(d.get("outliers") or []), worst_language=worst,
                   worst_text=("%s %.1f%% comments (threshold %.0f%%), %d outlier files" % (
                       worst["language"], 100 * (worst.get("ratio") or 0), 100 * (worst.get("max_ratio") or 0),
                       worst.get("outliers") or 0)) if worst else None)
    rows = review.readme_rows(readme)
    groups: list[dict] = []
    for r in rows:
        if not groups or groups[-1]["readme"] != r["readme"]:
            groups.append({"readme": r["readme"], "rows": []})
        groups[-1]["rows"].append(r)
    out.update(readme_rows=rows, readme_groups=groups,
               readme_count=(readme or {}).get("fact", {}).get("mismatch_count", len(rows)),
               readme_files=len({r["readme"] for r in rows}))
    if docs:
        files = [f for f in docs["fact"].get("files") or [] if f.get("words")]
        out.update(doc_score=docs["fact"].get("score") if docs["fact"].get("words") else None,
                   weakest=sorted(files, key=lambda f: (f.get("score") or 0, f["path"]))[:2])
    try:
        from nanosoc_rm import quality as qmod
        out["trend_points"] = len(qmod.trend("comments", comp, config=config)) if comments else 0
    except Exception:                                            # noqa: BLE001
        out["trend_points"] = 0
    return out


# ---------------------------------------------------------------------------------------------------------
# Overview and header
# ---------------------------------------------------------------------------------------------------------
def _decisions(config: Any) -> dict[str, tuple[dict | None, Any, dict]]:
    out = {}
    for rec in views.configurations(config):
        cid = rec["fact"]["id"]
        docs = views.compat_results(config, cid)
        doc = docs[0] if docs else None
        out[cid] = (doc, V.deciding(doc, rec), rec)
    return out


def pin_meaning(pin: dict, *, decision: Any = None, frozen: bool = False, uncovered: bool = False,
                split_n: int = 1, errata: list[str] = ()) -> dict:
    """What one pin means, first rule that matches (the order of the tree's pin headline): the deciding gate,
    uncovered silicon, a red state, a split, an erratum, else fine. {text, tone, gate: bool}."""
    f = pin["fact"]
    tp = f["tree_paths"][0]
    cid = f.get("configuration") or ""
    if decision is not None and getattr(decision, "gate", None) and tp in (decision.tree_paths or ()):
        return {"text": "Holds the deciding cause of the %s %s: %s." % (cid, decision.verdict, decision.label),
                "tone": "c-fail", "gate": True, "chip": "DECIDING GATE"}
    if uncovered:
        return {"text": "Silicon commit with no keep tag: lost if its branch is deleted.", "tone": "c-fail",
                "gate": False, "chip": "UNCOVERED"}
    host, h = views.pinned_host(pin)
    if views.pin_is_red(pin, frozen):
        st = f.get("state") or (h or {}).get("state") or "red"
        return {"text": "%s on the pinned host%s." % (st, (" " + host) if host else ""), "tone": "c-fail",
                "gate": False, "chip": st}
    if f.get("url_kind") == "ssh":
        return {"text": "Pinned by ssh URL%s." % (": waived in this frozen tree" if frozen else ""),
                "tone": "c-warn", "gate": False, "chip": "SSH URL"}
    if split_n > 1:
        return {"text": "Pinned %d times in %s at different commits." % (split_n, cid), "tone": "c-split",
                "gate": False, "chip": lines.again_text(split_n)}
    if errata:
        return {"text": "Erratum %s cites this component." % ", ".join(errata), "tone": "c-warn", "gate": False,
                "chip": "ERRATUM"}
    st = (h or {}).get("state") or f.get("state") or "no host state"
    return {"text": "Fine. %s." % st, "tone": shell.tone(st) if st in shell.TONE else "c-pass", "gate": False,
            "chip": "OK"}


def _pins_of(config: Any, comp: str) -> tuple[list[dict], list[str]]:
    """(rows of "Where {comp} is pinned", configurations never walked)."""
    decisions = _decisions(config)
    rows, never = [], []
    for cid, (doc, dec, rec) in decisions.items():
        pins = views.pins(config, cid)
        if not pins:
            never.append(cid)
            continue
        frozen = views.is_frozen(rec)
        mine = [p for p in pins if (p["fact"].get("child") or {}).get("component") == comp]
        if not mine:
            continue
        uncovered = {(d["fact"].get("commit") or "") for d in views.silicon_protection(config, cid)
                     if d["fact"].get("component") == comp and not d["fact"].get("covered")}
        errata = []
        for e in views.open_errata(config, cid):
            did = (e["fact"].get("record") or {}).get("defect")
            dfx = next((d for d in views.defects(config) if d["fact"]["id"] == did), None)
            if dfx is not None and views._cites_component(dfx["fact"]["record"], comp):
                errata.append(e["fact"]["id"])
        for p in sorted(mine, key=lambda p: p["fact"]["tree_paths"][0]):
            f = p["fact"]
            commit = (f.get("child") or {}).get("commit") or ""
            hosts = [{"text": "%s%s %s" % (name, "*" if h.get("pinned") else "", h.get("state") or "?"),
                      "tone": shell.tone(h.get("state")), "lag": h.get("lag")}
                     for name, h in sorted((f.get("hosts") or {}).items())]
            _, ph = views.pinned_host(p)
            m = pin_meaning(p, decision=dec, frozen=frozen, uncovered=commit in uncovered, split_n=len(mine),
                            errata=errata)
            rows.append({"doc": p, "configuration": cid, "line": lines.line(rec["fact"].get("record")),
                         "tree_path": f["tree_paths"][0], "commit": commit,
                         "hosts": hosts, "lag": (ph or {}).get("lag"), "meaning": m, "frozen": frozen,
                         "href": "/configurations/%s" % _q(cid)})
    return rows, never


def lines_of(config: Any, comp: str) -> list[dict]:
    """The registered lines (configurations) whose root is `comp`: the repository's own branches and tags with
    a page, each {cid, line (lines.line), frozen, verdict, tone, href} (evaluation 2026-10-16 section 7: a
    repo's own lines had no page)."""
    out = []
    decisions = _decisions(config)
    for rec in views.configurations(config):
        r = rec["fact"].get("record") or {}
        if r.get("component") != comp:
            continue
        cid = rec["fact"]["id"]
        doc, dec, _ = decisions.get(cid, (None, None, None))
        verdict = "NOT CHECKED" if doc is None or dec is None else dec.verdict
        commit = r.get("commit") or ((doc or {}).get("fact") or {}).get("commit")
        out.append({"cid": cid, "line": lines.line(r, commit), "frozen": views.is_frozen(rec), "verdict": verdict,
                    "tone": shell.tone(verdict), "href": "/configurations/%s" % _q(cid), "doc": rec})
    return out


def sentence(comp: str, s: dict) -> str:
    """One sentence for the header from the counts the tabs show (no estimate, no invented number)."""
    parts = []
    if s.get("branches_total"):
        parts.append("%d of %d branches can be deleted" % (s.get("deletable", 0), s["branches_total"]))
    if s.get("analysed"):
        extra = " (%d more are decoration, one rule)" % s["banner"] if s.get("banner") else ""
        parts.append("%d comment findings are worth a look%s" % (s.get("worth", 0), extra))
    for h in s.get("hosts") or []:
        if h["relation"] == "AHEAD" and h.get("canonical_only"):
            parts.append("the %s copy lacks %d commits that %s has" % (h["copy_host"], h["canonical_only"], h["canonical"]))
        elif h["relation"] == "BEHIND" and h.get("copy_only"):
            parts.append("the %s copy has %d commits that %s lacks" % (h["copy_host"], h["copy_only"], h["canonical"]))
        elif h["relation"] == "DIVERGED":
            parts.append("the %s and %s copies have diverged (%s / %s)" % (
                h["canonical"], h["copy_host"], h.get("canonical_only"), h.get("copy_only")))
    if not parts:
        return "Nothing is stored for %s yet: no branch inventory, quality result or host comparison." % comp
    if len(parts) == 1:
        body = parts[0]
    else:
        body = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return body[0].upper() + body[1:] + "."


def header(config: Any, comp: str, *, branch_rows: list[dict] | None = None, qv: dict | None = None) -> dict:
    """Title block of every tab: canonical repo and default branch, up to 3 flag chips, the sentence and the one
    primary action."""
    branch_rows = _stored_branches(config, comp) if branch_rows is None else branch_rows
    qv = quality_view(config, comp) if qv is None else qv
    canon = _canonical(config, comp)
    compares = views.host_compares(config, comp)
    default = next((d["fact"].get("default_branch") for d in branch_rows
                    if canon and (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) == canon), None)
    if default is None and compares:
        default = compares[0]["fact"]["canonical"].get("default_branch")
    hosts = []
    for d in compares:
        f = d["fact"]
        hosts.append({"relation": f.get("relation"), "canonical": f["canonical"]["repo"]["host"],
                      "copy_host": f["copy"]["repo"]["host"], "canonical_only": f.get("canonical_only"),
                      "copy_only": f.get("copy_only"), "doc": d, "tone": shell.tone(f.get("relation")),
                      "chip": "%s%s" % (f.get("relation"), (" %s" % f["canonical_only"]) if f.get("relation") == "AHEAD"
                                        and f.get("canonical_only") else ""),
                      "mismatch": bool(f.get("default_branch_mismatch"))})
    try:
        flags = list(shell.component_flags(config).get(comp, []))
    except Exception:                                            # noqa: BLE001 - the header never breaks a page
        flags = []
    for h in hosts:
        if h["relation"] not in (None, "SAME"):
            n = h["canonical_only"] if h["relation"] == "AHEAD" else h["copy_only"] if h["relation"] == "BEHIND" else None
            # "HOSTS DIFFER": the copies on two hosts are not the same (never "SPLIT", which read like a pin)
            flags.append(("HOSTS DIFFER: %s %s%s" % (h["canonical"], h["relation"].lower(), (" %s" % n) if n else ""),
                          shell.tone(h["relation"])))
    from nanosoc_rm.analysis.branches import delete_list
    deletable = len(delete_list(branch_rows))
    s = {"branches_total": len(branch_rows), "deletable": deletable, "analysed": bool(qv.get("comments")),
         "worth": qv.get("worth", 0), "banner": qv.get("banner", 0), "hosts": hosts}
    if deletable:
        primary = {"label": "Review delete list", "href": _href(comp, "branches")}
    elif qv.get("worth"):
        primary = {"label": "Start review", "href": _href(comp, "review")}
    elif not qv.get("analysed"):
        primary = {"label": "Analyse quality", "href": "/quality/%s" % _q(comp)}
    else:
        primary = None
    return {"comp": comp, "canonical": canon, "default": default, "flags": flags[:MAX_FLAGS],
            "sentence": sentence(comp, s), "primary": primary, "summary": s, "hosts": hosts}


def tabs(comp: str, *, branches_n: int | None, worth: int | None, left: int | None) -> list[dict]:
    return [{"key": "overview", "label": "Overview", "href": _href(comp), "count": None},
            {"key": "branches", "label": "Branches", "href": _href(comp, "branches"), "count": branches_n},
            {"key": "quality", "label": "Quality", "href": _href(comp, "quality"), "count": worth},
            {"key": "versions", "label": "Versions", "href": _href(comp, "versions"), "count": None},
            {"key": "review", "label": "Review", "href": _href(comp, "review"),
             "count": None if left is None else "%d left" % left}]


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def _ctx(request: Request) -> tuple[Any, Any]:
    return request.state.config, request.state.identity


def _known(config: Any, comp: str) -> bool:
    if any(d["fact"]["id"] == comp for d in views.components(config)):
        return True
    if views.component_page(config, comp) is not None:
        return True
    return bool(_stored_branches(config, comp) or review.stored_fact(config, comp, "comments")
                or review.stored_fact(config, comp, "density"))


def _crumbs(comp: str, tab: str) -> list[tuple[str, str | None]]:
    return shell.crumbs([("Projects", "/"), ("Components", "/components"), (comp, _href(comp)),
                         (TAB_LABEL[tab], None)])


def _left(config: Any, login: str, comp: str, qv: dict) -> int | None:
    obs = qv.get("comments")
    if obs is None or not login:
        return None
    items = review.queue(obs)
    try:
        dec = uistate.decisions(config, login, comp, review.commit_of(obs))
    except Exception:                                            # noqa: BLE001
        dec = {}
    return sum(1 for it in items if it["key"] not in dec)


def _workspace(request: Request, comp: str, tab: str, template: str, *, problems: list[str] | None = None,
               status: int = 200, qv: dict | None = None, branch_rows: list[dict] | None = None,
               **ctx: Any) -> HTMLResponse:
    config, _ = _ctx(request)
    branch_rows = _stored_branches(config, comp) if branch_rows is None else branch_rows
    qv = quality_view(config, comp) if qv is None else qv
    hdr = header(config, comp, branch_rows=branch_rows, qv=qv)
    t = tabs(comp, branches_n=len(branch_rows), worth=qv.get("worth") if qv.get("analysed") else None,
             left=_left(config, login_of(request), comp, qv))
    return request.app.state.rm.page(
        request, template, comp, status=status, problems=problems, crumbs=_crumbs(comp, tab), hdr=hdr,
        ws_tabs=t, ws_tab=tab, comp=comp, qv=qv, href=_href,
        doc_title=comp if tab == "overview" else "%s · %s" % (TAB_LABEL.get(tab, tab), comp), **ctx)


def _bad(request: Request, comp: str) -> Response | None:
    config, _ = _ctx(request)
    if not views.is_valid_id(comp) or not _known(config, comp):
        return request.app.state.rm.page(request, "message.html", "Not found", status=404,
                                         message="No component is known here.", next_step=True)
    return None


def component(request: Request, id: str) -> Response | None:
    """GET /components/{id}: the Overview tab (app.py delegates here). None for an invalid id (the app 404s)."""
    if not views.is_valid_id(id):
        return None
    config, _ = _ctx(request)
    problems = registry_facts.refresh(config)
    bad = _bad(request, id)
    if bad is not None:
        return bad
    rows = _stored_branches(config, id)
    qv = quality_view(config, id)
    pins, never = _pins_of(config, id)
    from nanosoc_rm.analysis.branches import delete_list
    counts: dict[str, int] = {}
    for d in rows:
        counts[d["fact"]["verdict"]] = counts.get(d["fact"]["verdict"], 0) + 1
    c = views.component_page(config, id) or {}
    return _workspace(request, id, "overview", "comp_overview.html", problems=problems, qv=qv, branch_rows=rows,
                      branch_counts=counts, deletable=len(delete_list(rows)),
                      branch_oldest=min(rows, key=lambda d: d["observed_at"]) if rows else None, pins=pins,
                      never=never, own_lines=lines_of(config, id), record=c.get("record"), mirrors=c.get("mirrors") or [],
                      defects=c.get("defects") or [], repos=sorted({(d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) for d in rows}))


def _branches_page(request: Request, id: str) -> Response:
    comp = id
    bad = _bad(request, comp)
    if bad is not None:
        return bad
    config, _ = _ctx(request)
    q = request.query_params
    rows = _stored_branches(config, comp)
    view = branch_view(rows, canonical=_canonical(config, comp), only_canonical=q.get("canonical") == "1",
                       verdict=q.get("verdict") or "", q=(q.get("q") or "")[:80], picked=q.get("picked") == "1",
                       selected=q.getlist("del"))
    if request.headers.get("hx-request") and q.get("part") == "list":
        state = request.app.state.rm
        _, ident = _ctx(request)
        p = Page()
        return HTMLResponse(env.get_template("comp_delete_list.html").render(
            v=view, comp=comp, href=_href, page=p, fact=p.fact, csrf=state.csrf_for(ident, config)))
    has_mirror = any(d["fact"].get("component") == comp for d in views.mirrors(config))
    return _workspace(request, comp, "branches", "comp_branches.html", branch_rows=rows, v=view,
                      has_mirror=has_mirror)


def _quality_page(request: Request, id: str) -> Response:
    comp = id
    bad = _bad(request, comp)
    if bad is not None:
        return bad
    config, ident = _ctx(request)
    q = request.query_params
    qv = quality_view(config, comp, ref=q.get("ref") or None, commit=q.get("commit") or None)
    b = None
    if qv.get("comments") is not None and login_of(request):
        b = review.basket(config, login_of(request), comp, qv["comments"])
    return _workspace(request, comp, "quality", "comp_quality.html", qv=qv, basket=b,
                      trend=_trend_svg(config, comp) if qv.get("trend_points", 0) >= 2 else None)


def _trend_svg(config: Any, comp: str) -> Markup | None:
    try:
        from nanosoc_rm import quality as qmod
        from nanosoc_rm.quality.web import sparkline
        return sparkline([p["value"] for p in qmod.trend("comments", comp, config=config)])
    except Exception:                                            # noqa: BLE001
        return None


def _versions_page(request: Request, id: str) -> Response:
    comp = id
    bad = _bad(request, comp)
    if bad is not None:
        return bad
    nav = getattr(request.app.state.rm, "nav", ()) or ()
    if any(href.rstrip("/") == "/versions" for _, href, _ in nav):
        return RedirectResponse("/versions/%s" % _q(comp), 303)
    return _workspace(request, comp, "versions", "comp_versions.html")


def _review_model(request: Request, comp: str, i: int | None = None, commit: str | None = None
                  ) -> tuple[dict | None, dict | None, dict | None]:
    config, _ = _ctx(request)
    obs = review.stored_fact(config, comp, "comments", ref=request.query_params.get("ref") or None, commit=commit)
    if obs is None:
        return None, None, None
    login = login_of(request)
    st = review.state(config, login, comp, obs, i)
    readme_obs = review.stored_fact(config, comp, "readme", commit=obs["fact"].get("commit"))
    text, problems = review.build_patch(config, login, comp, obs, readme_obs)
    st["patch_preview"] = review.preview(text) if text else []
    st["patch_lines"] = text.count("\n")
    st["patch_problems"] = problems
    st["download"] = _href(comp, "review/patch.diff", commit=st["commit"])
    return obs, st, readme_obs


def _review_page(request: Request, id: str) -> Response:
    comp = id
    bad = _bad(request, comp)
    if bad is not None:
        return bad
    q = request.query_params
    i = int(q["i"]) if re.fullmatch(r"[0-9]{1,9}", q.get("i") or "") else None   # ASCII digits only
    obs, st, _ = _review_model(request, comp, i, q.get("commit") or None)
    return _workspace(request, comp, "review", "comp_review.html", obs=obs, st=st)


def _review_fragment(request: Request, comp: str, obs: dict, st: dict) -> HTMLResponse:
    config, ident = _ctx(request)
    state = request.app.state.rm
    p = Page()
    body = env.get_template("review_main.html").render(
        comp=comp, obs=obs, st=st, href=_href, page=p, fact=p.fact, csrf=state.csrf_for(ident, config),
        actions=state.local_routes)
    # the tabs sit outside #review: send them out of band so "N left" follows the decision (review K-R5)
    qv = quality_view(config, comp)
    t = tabs(comp, branches_n=len(_stored_branches(config, comp)),
             worth=qv.get("worth") if qv.get("analysed") else None, left=st.get("left"))
    return HTMLResponse(body + env.get_template("_comp_tabs.html").render(ws_tabs=t, ws_tab="review", oob=True))


def _patch_diff(request: Request, id: str) -> Response:
    comp = id
    config, _ = _ctx(request)
    if not views.is_valid_id(comp):
        return PlainTextResponse("404 Not Found: not a component id\n", 404)
    obs = review.stored_fact(config, comp, "comments", commit=request.query_params.get("commit") or None)
    if obs is None:
        return PlainTextResponse("404 Not Found: no stored comment findings for %s at this commit; analyse it on "
                                 "/quality/%s first\n" % (comp, _q(comp)), 404)
    login = login_of(request)
    readme_obs = review.stored_fact(config, comp, "readme", commit=obs["fact"].get("commit"))
    text, problems = review.build_patch(config, login, comp, obs, readme_obs)
    if not text.strip():
        why = "; ".join(problems) if problems else "the basket is empty: accept or edit a finding first"
        return PlainTextResponse("404 Not Found: nothing to patch for %s: %s\n" % (comp, why), 404)
    name = review.basket(config, login, comp, obs)["file_name"]
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return Response(text, media_type="text/x-diff", headers={"content-disposition": 'attachment; filename="%s"' % safe})


def _back(request: Request, comp: str, form: Any, i: int | None, commit: str) -> str:
    ret = str(form.get("return") or "")
    if ret.startswith("/components/%s/" % _q(comp)) and "//" not in ret and "\\" not in ret:
        return ret
    return _href(comp, "review", commit=commit, i=i)


async def _decide(request: Request, id: str) -> Response:
    config, _ = _ctx(request)
    form = await request.form()
    if not views.is_valid_id(id):
        return PlainTextResponse("400 Bad Request: not a component id", 400)
    obs = review.stored_fact(config, id, "comments", commit=str(form.get("commit") or "") or None)
    if obs is None:
        return PlainTextResponse("404 Not Found: no stored comment findings to review", 404)
    items = review.queue(obs)
    key, decision = str(form.get("key") or ""), str(form.get("decision") or "")
    idx = next((it["n"] for it in items if it["key"] == key), None)
    if idx is None:
        return PlainTextResponse("404 Not Found: no such finding in this review", 404)
    if decision not in uistate.DECISIONS:
        return PlainTextResponse("400 Bad Request: decision must be accept, edit, skip or dismiss", 400)
    if decision == "accept" and not items[idx]["finding"].get("edit"):
        return PlainTextResponse("400 Bad Request: this finding has no suggested edit; use Edit", 400)
    c = review.commit_of(obs)
    text = str(form.get("text") or "") if decision == "edit" else None
    await run_in_threadpool(uistate.decide, config, login_of(request), id, c, key, decision, text)
    nxt = review.next_index(items, uistate.decisions(config, login_of(request), id, c), after=idx)
    if request.headers.get("hx-request"):
        obs2, st, _ = _review_model(request, id, nxt, c)
        return _review_fragment(request, id, obs2, st)
    return RedirectResponse(_href(id, "review", commit=c, i=nxt), 303)


BASKET_OPS = ("remove", "banner-on", "banner-off", "readme-add", "readme-remove", "clear")


async def _basket(request: Request, id: str) -> Response:
    config, _ = _ctx(request)
    form = await request.form()
    if not views.is_valid_id(id):
        return PlainTextResponse("400 Bad Request: not a component id", 400)
    obs = review.stored_fact(config, id, "comments", commit=str(form.get("commit") or "") or None)
    if obs is None:
        return PlainTextResponse("404 Not Found: no stored comment findings to review", 404)
    op, key = str(form.get("op") or ""), str(form.get("key") or "")
    if op not in BASKET_OPS:
        return PlainTextResponse("400 Bad Request: op must be one of %s" % ", ".join(BASKET_OPS), 400)
    login, c = login_of(request), review.commit_of(obs)
    i = None
    if op == "remove":
        items = review.queue(obs)
        hit = next((it for it in items if it["key"] == key), None)
        if hit is None:
            return PlainTextResponse("404 Not Found: no such finding in this review", 404)
        i = hit["n"]
        await run_in_threadpool(uistate.decide, config, login, id, c, key, "skip")
    elif op in ("banner-on", "banner-off"):
        await run_in_threadpool(uistate.set_flag, config, login, id, c, review.BANNER_FLAG,
                                "1" if op == "banner-on" else None)
    elif op in ("readme-add", "readme-remove"):
        readme_obs = review.stored_fact(config, id, "readme", commit=obs["fact"].get("commit"))
        row = next((r for r in review.readme_rows(readme_obs) if r["key"] == key), None)
        if row is None:
            return PlainTextResponse("404 Not Found: no such README row", 404)
        if op == "readme-add" and not row["mechanical"]:
            return PlainTextResponse("400 Bad Request: this README row has no mechanical edit; fix it by hand", 400)
        await run_in_threadpool(uistate.set_flag, config, login, id, c, review.README_FLAG + key,
                                "1" if op == "readme-add" else None)
    else:
        await run_in_threadpool(uistate.clear_basket, config, login, id, c)
    if request.headers.get("hx-request") and not form.get("return"):
        obs2, st, _ = _review_model(request, id, i, c)
        return _review_fragment(request, id, obs2, st)
    return RedirectResponse(_back(request, id, form, i, c), 303)


def branches(request: Request) -> Response | None:
    """GET /branches (app.py delegates here): ?component=X answers 303 to the Branches tab; otherwise the picker
    (components in my configurations first, then every mirrored component)."""
    q = request.query_params
    comp = q.get("component") or ""
    if comp:
        if not views.is_valid_id(comp):
            return None
        extra = {"verdict": q.get("verdict") or None}
        if q.get("all") not in (None, "", "1", "on", "true"):
            extra["canonical"] = "1"
        return RedirectResponse(_href(comp, "branches", **extra), 303)
    config, _ = _ctx(request)
    problems = registry_facts.refresh(config)
    mirrored = {d["fact"].get("component") for d in views.mirrors(config)}
    stored: dict[str, int] = {}
    for d in db.latest("branch_state", "branch_state:", config=config):
        stored[d["fact"]["component"]] = stored.get(d["fact"]["component"], 0) + 1
    in_tree = sorted({(d["fact"].get("child") or {}).get("component")
                      for d in db.latest("pin_state", "pin_state:", config=config)} - {None})
    comps = {d["fact"]["id"]: d for d in views.components(config)}
    mine = [{"id": c, "rec": comps.get(c), "stored": stored.get(c, 0), "mirrored": c in mirrored} for c in in_tree]
    others = [{"id": c, "rec": comps.get(c), "stored": stored.get(c, 0), "mirrored": c in mirrored}
              for c in sorted((mirrored | set(stored)) - set(in_tree) - {None})]
    return request.app.state.rm.page(request, "branches.html", "Branches", problems=problems, mine=mine,
                                     others=others, component="", choices=sorted((mirrored | set(stored) | set(in_tree)) - {None}),
                                     crumbs=shell.crumbs([("Projects", "/"), ("Branches", None)]))


def router(app: Any) -> APIRouter:
    """GET pages always; the two review POSTs only in a local deployment (section 13a, D5)."""
    r = APIRouter()
    get = ["GET"]
    r.add_api_route("/components/{id}/branches", _branches_page, methods=get, include_in_schema=False)
    r.add_api_route("/components/{id}/quality", _quality_page, methods=get, include_in_schema=False)
    r.add_api_route("/components/{id}/versions", _versions_page, methods=get, include_in_schema=False)
    r.add_api_route("/components/{id}/review", _review_page, methods=get, include_in_schema=False)
    r.add_api_route("/components/{id}/review/patch.diff", _patch_diff, methods=get, include_in_schema=False)
    if plugins.local_routes(app):
        r.add_api_route("/components/{id}/review/decide", _decide, methods=["POST"], include_in_schema=False)
        r.add_api_route("/components/{id}/review/basket", _basket, methods=["POST"], include_in_schema=False)
    return r
