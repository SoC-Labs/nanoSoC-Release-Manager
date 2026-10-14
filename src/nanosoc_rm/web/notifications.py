"""Notifications behind the bell (Wave F, CR-WF-gui; David 2026-10-14: front page approved, Notifications.dc.html).

The inbox stops being the home page: its cards (web.inbox.cards, the same data source) become one line each
here, with one action, mark read, and per-login "notify me about" kinds and projects. One more item comes from
the org scan: "N new repos to characterise" (the current `repo_seen` snapshots), linking to the Catalogue page.

    items(config, login, now=None) -> [Item]          every notification, unread first, each with its read state
    unread_count(config, login) -> int                the bell: unread items of the kinds and projects not muted
    view(config, login, show="unread") -> dict        what notifications.html prints
    page(request)                                      GET /notifications
    router(app)                                        GET /notifications; local-only POSTs /notifications/read,
                                                       /notifications/read-all, /notifications/prefs

Read state is the inbox's done state in gui.sqlite (web.uistate.set_card with the item's fingerprint), so an item
read here is done in the card view too, and it comes back unread when its facts change. Preferences are rows of
uistate's `pref` table ("notify.kind.<kind>" / "notify.project.<id>" = "off"). A GET reads only: it never creates
gui.sqlite, never queues a job and never calls the network.
"""
from __future__ import annotations

import contextvars
import dataclasses
import datetime as dt
import hashlib
import json
import re
import urllib.parse
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import clock, db
from nanosoc_rm.web import uistate, views

__all__ = ["Item", "KINDS", "items", "unread_count", "view", "page", "router", "NEW_REPOS_ID"]

# "Notify me about": the inbox's categories (inbox.CATS without "all"), then new repos from the org scan.
KINDS = (("compat", "A configuration check is red"), ("silicon", "Silicon, errata and red pins"),
         ("branches", "Branches ready to delete"), ("hosts", "Host copies that drifted"),
         ("mirrors", "Mirror and fetch alerts"), ("quality", "Code and documentation quality"),
         ("bumps", "Bump proposals"), ("catalogue", "New repos to characterise"))
KIND_LABEL = dict(KINDS)
NEW_REPOS_ID = "catalogue:new-repos"
_TONE = {"c-fail": "act", "c-warn": "warn", "c-split": "warn"}
_CARD_ID = re.compile(r"^[a-z_]+:[A-Za-z0-9_.@:/+-]{0,240}\Z")
_FP = re.compile(r"^[0-9a-f]{0,64}\Z")


@dataclasses.dataclass
class Item:
    id: str
    kind: str                       # one of KINDS
    text: str
    project: str | None             # the project (a configuration's root component) it is about, if any
    where: str                      # the line under the text: project or configuration, else the kind
    when: str                       # "today", "3 d ago", or "" when the facts carry no time
    action: tuple[str, str]         # (label, href): one GET link
    tone: str                       # act | warn | info: the dot beside the text (decorative; the text says it)
    fingerprint: str
    read: bool = False
    fact: dict | None = None        # the observation it was built from, for the page's data age
    about: frozenset = frozenset()  # every project it concerns; muted only when ALL of them are muted


def _when(at: str | None, now: dt.datetime) -> str:
    if not at:
        return ""
    days = (now.date() - clock.parse(at).date()).days
    return "today" if days <= 0 else "yesterday" if days == 1 else "%d d ago" % days


def _cfg_projects(config) -> dict[str, str]:
    """{configuration id: its project (root component)} from the stored registry records."""
    return {d["fact"]["record"]["id"]: d["fact"]["record"].get("component") for d in views.configurations(config)}


def _card_cfg(card_id: str, cfgs: dict[str, str]) -> str | None:
    rest = card_id.split(":", 1)[1] if ":" in card_id else ""
    head = rest.split(":", 1)[0]
    return head if head in cfgs else None


def _card_projects(card: Any, cfgs: dict[str, str]) -> frozenset:
    """Every project a card concerns: the configuration or project its id names (`compat_fail:<cfg>`,
    `branches_deletable:<project>`), and the configurations its facts and evidence name (an erratum's
    exposures, `never_checked:all`)."""
    projects = set(cfgs.values())
    rest = card.id.split(":", 1)[1] if ":" in card.id else ""
    head = rest.split(":", 1)[0]
    cids = {head} if head in cfgs else set()
    out = {head} if head in projects else set()
    for d in card.facts or []:
        f = (d or {}).get("fact") or {}
        rec = f.get("record") if isinstance(f.get("record"), dict) else {}
        cids |= {x for x in (f.get("configuration"), rec.get("configuration"), f.get("id")) if x in cfgs}
    cids |= {e.get("what") for e in card.evidence or [] if e.get("what") in cfgs}
    out |= {cfgs[c] for c in cids if cfgs.get(c)}
    return frozenset(out)


def _new_repos(config, now: dt.datetime) -> Item | None:
    try:
        docs = db.latest("repo_seen", config=config)
    except Exception:                                            # noqa: BLE001 - no scan stored, no item
        return None
    if not docs:
        return None
    repos = sorted("%s:%s" % (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) for d in docs)
    fp = hashlib.sha256(json.dumps(repos).encode()).hexdigest()[:32]
    newest = max(docs, key=lambda d: d["observed_at"])
    n = len(repos)
    return Item(id=NEW_REPOS_ID, kind="catalogue", text="%d new repo%s to characterise" % (n, "" if n == 1 else "s"),
                project=None, where="Catalogue", when=_when(newest["observed_at"], now),
                action=("Characterise", "/catalogue/#new"), tone="act", fingerprint=fp, fact=newest)


def _all(config, now: dt.datetime) -> list[Item]:
    from nanosoc_rm.web import inbox
    cfgs = _cfg_projects(config)
    out = []
    for c in inbox.cards(config, None, now):
        cid = _card_cfg(c.id, cfgs)
        prim = c.primary or {}
        if prim.get("href") and (prim.get("method") or "get") == "get":
            action = (prim.get("label") or "Open", prim["href"])
        else:                                    # a POST action stays on the card view, behind its own button
            action = ("Open the card", "/inbox?cat=%s" % c.cat)
        out.append(Item(id=c.id, kind=c.cat, text=c.title, project=cfgs.get(cid) if cid else None,
                        where=cid or inbox.CAT_LABEL.get(c.cat, c.cat), when=_when(c.fact_at, now), action=action,
                        tone=_TONE.get(c.chip[1], "info"), fingerprint=c.fingerprint, fact=c.fact,
                        about=_card_projects(c, cfgs)))
    extra = _new_repos(config, now)
    if extra is not None:
        out.append(extra)
    return out


def _muted(prefs: dict[str, str]) -> tuple[set[str], set[str]]:
    kinds = {k.split(".", 2)[2] for k, v in prefs.items() if k.startswith("notify.kind.") and v == "off"}
    projects = {k.split(".", 2)[2] for k, v in prefs.items() if k.startswith("notify.project.") and v == "off"}
    return kinds, projects


def items(config, login: str | None, now: dt.datetime | None = None) -> list[Item]:
    """Every notification for this login, its read state set; muted kinds and projects left out."""
    now = now or clock.now()
    all_ = _all(config, now)
    if login and uistate.path(config).exists():                  # a GET never creates gui.sqlite
        hidden = uistate.card_state(config, login, now=now, fingerprints={i.id: i.fingerprint for i in all_})
        kinds, projects = _muted(uistate.prefs(config, login))
    else:
        hidden, kinds, projects = {}, set(), set()
    out = []
    for i in all_:
        if i.kind in kinds or (i.about and i.about <= projects):
            continue
        i.read = i.id in hidden
        out.append(i)
    return out


# The count the page just computed, for the bell of the same request (one card build per page, not two).
_LAST: contextvars.ContextVar[tuple[int, str, int] | None] = contextvars.ContextVar("notify_unread", default=None)


def unread_count(config, login: str | None) -> int:
    last = _LAST.get()
    if last is not None and last[0] == id(config) and last[1] == (login or ""):
        return last[2]
    n = sum(1 for i in items(config, login) if not i.read)
    _LAST.set((id(config), login or "", n))
    return n


def view(config, login: str | None, show: str = "unread", now: dt.datetime | None = None) -> dict:
    all_items = items(config, login, now)
    unread = [i for i in all_items if not i.read]
    _LAST.set((id(config), login or "", len(unread)))
    shown = unread if show == "unread" else sorted(all_items, key=lambda i: i.read)
    prefs = uistate.prefs(config, login) if login else {}
    kinds_off, projects_off = _muted(prefs)
    projects = sorted({p for p in _cfg_projects(config).values() if p})
    return {"show": "unread" if show == "unread" else "all", "items": shown, "unread": len(unread),
            "total": len(all_items),
            "kinds": [{"key": k, "label": label, "on": k not in kinds_off} for k, label in KINDS],
            "projects": [{"key": p, "label": p, "on": p not in projects_off} for p in projects]}


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def _login(request: Request) -> str:
    return str(getattr(request.state.identity, "login", "") or "")


def page(request: Request) -> Response:
    """GET /notifications[?show=all]."""
    config = request.state.config
    show = "all" if request.query_params.get("show") == "all" else "unread"
    v = view(config, _login(request), show)
    return request.app.state.rm.page(request, "notifications.html", "Notifications", no_rail=True, bare=True,
                                     crumbs=[("Projects", "/"), ("Notifications", None)], n=v)


def _back(form: Any) -> Response:
    return RedirectResponse("/notifications?show=all" if str(form.get("show") or "") == "all" else "/notifications",
                            303)


async def post_read(request: Request) -> Response:
    """Mark one item read (`read=1`, with the fingerprint the page showed) or unread again (`read=0`)."""
    config = request.state.config
    form = await request.form()
    item, fp = str(form.get("item") or ""), str(form.get("fp") or "")
    if not _CARD_ID.match(item) or not _FP.match(fp):
        return PlainTextResponse("400 Bad Request: item and fp are needed", 400)
    if str(form.get("read") or "1") == "0":
        uistate.undo_card(config, _login(request), item)
    else:
        uistate.set_card(config, _login(request), item, "done", fp)
    return _back(form)


async def post_read_all(request: Request) -> Response:
    """Mark read exactly the items the page showed (their ids and fingerprints come with the form), so an item
    that arrived after the page was drawn stays unread."""
    config = request.state.config
    form = await request.form()
    ids, fps = form.getlist("item"), form.getlist("fp")
    if not ids or len(ids) != len(fps):
        return PlainTextResponse("400 Bad Request: the items the page showed are needed", 400)
    pairs = [(str(i), str(f)) for i, f in zip(ids, fps)]
    if not all(_CARD_ID.match(i) and _FP.match(f) for i, f in pairs):
        return PlainTextResponse("400 Bad Request: not an item", 400)
    for i, f in pairs[:500]:
        uistate.set_card(config, _login(request), i, "done", f)
    return _back(form)


async def post_prefs(request: Request) -> Response:
    """Save "notify me about": every kind and project the form lists (`listed`), on when its box is ticked."""
    config = request.state.config
    form = await request.form()
    listed = [str(x) for x in form.getlist("listed")]
    ticked = {str(x) for x in form.getlist("on")}
    names = []
    for name in listed:
        if not uistate.PREF_NAME.match(name) or not name.startswith("notify."):
            return PlainTextResponse("400 Bad Request: not a notification preference: %s" % name[:60], 400)
        names.append(name)
    if name_kind_unknown(names):
        return PlainTextResponse("400 Bad Request: unknown notification kind", 400)
    login = _login(request)
    for name in names:
        uistate.set_pref(config, login, name, None if name in ticked else "off")
    return _back(form)


def name_kind_unknown(names: list[str]) -> bool:
    return any(n.startswith("notify.kind.") and n.split(".", 2)[2] not in KIND_LABEL for n in names)


def router(app: Any = None):
    """GET /notifications always; the POSTs only in a local deployment (section 13a)."""
    from fastapi import APIRouter
    from nanosoc_rm.web.plugins import local_routes
    r = APIRouter()
    r.add_api_route("/notifications", page, methods=["GET"], include_in_schema=False)
    if local_routes(app):
        for path, fn in (("/notifications/read", post_read), ("/notifications/read-all", post_read_all),
                         ("/notifications/prefs", post_prefs)):
            r.add_api_route(path, fn, methods=["POST"], include_in_schema=False)
    return r


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")
