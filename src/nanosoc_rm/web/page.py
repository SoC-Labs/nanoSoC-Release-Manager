"""Rendering with the page hooks of interfaces.md section 13 (A3).

Every fact a page shows goes through `Page.fact(obs)`, which prints it with `data-subject` and
`data-observed-at` and a `data-source-for` link to `/facts/<subject>`, and records it. The page's one
data-age element (`data-age-seconds`, `data-oldest-observed-at`) is computed AFTER the body is rendered, from
exactly the facts the body printed, and measured from the OLDEST of them (README section 4), so the age a page
shows can never be younger than its oldest fact. The header says both clocks: when the newest fact on the page was
observed ("Updated 4 min ago") and the age of the oldest, which links to that fact.
"""
from __future__ import annotations

import datetime as dt
import copy
import html
import shlex
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from nanosoc_rm import clock
from nanosoc_rm.web import lines as _lines, terms as _terms

__all__ = ["Page", "env", "render", "fact_href", "short", "human_age", "STATE_CLASS", "EXPOSURE_CLASS",
           "VERDICT_CLASS", "ITEM_CLASS", "BRANCH_CLASS", "RELATION_CLASS", "JOB_CLASS", "NAV", "TOP_NAV", "top_nav", "src_name",
           "MORE_GROUPS", "more_groups"]

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Colours (README section 5 states; section 13 F2 exposure; verdicts).
STATE_CLASS = {"TAGGED": "state-green", "ON_DEFAULT": "state-green", "PROTECTED_BY_TAG": "state-green",
               "BRANCH_ONLY": "state-amber"}
EXPOSURE_CLASS = {"exposed": "state-red", "affected": "state-red", "probably-exposed": "state-amber",
                  "probably-not-affected": "state-amber", "not-affected": "state-green"}
VERDICT_CLASS = {"PASS": "state-green", "WAIVED": "state-amber", "WARN": "state-amber", "FAIL": "state-red",
                 "UNVERIFIED": "state-red",
                 "INFRA": "state-amber", "NOT-RUN": "state-grey"}
# A gate's per-item result (compat_item) and a branch verdict (CR8): text is always shown beside the colour.
ITEM_CLASS = {"pass": "state-green", "warn": "state-amber", "waived": "state-amber", "fail": "state-red",
              "unverified": "state-red", "not-reached": "state-grey"}
BRANCH_CLASS = {"MERGED": "state-green", "STALE": "state-amber", "PINNED": "state-info", "PROTECTED": "state-info",
                "ACTIVE": "state-grey"}
RELATION_CLASS = {"SAME": "state-green", "AHEAD": "state-amber", "BEHIND": "state-amber", "DIVERGED": "state-red",
                  "UNRELATED": "state-red", "UNKNOWN": "state-grey"}
JOB_CLASS = {"queued": "state-grey", "running": "state-info", "done": "", "failed": "state-red",
             "cancelled": "state-grey", "infra": "state-amber"}


def _with_tone(old: dict[str, str]) -> dict[str, str]:
    """Spec 1.6: each older class keeps its name (tests and plug-ins read it) and gains the shell's tone
    class from shell.TONE, which decides the colour."""
    from nanosoc_rm.web.shell import tone
    return {k: ("%s %s" % (v, tone(k))).strip() for k, v in old.items()}


STATE_CLASS, EXPOSURE_CLASS, VERDICT_CLASS, ITEM_CLASS, BRANCH_CLASS, RELATION_CLASS, JOB_CLASS = (
    _with_tone(m) for m in (STATE_CLASS, EXPOSURE_CLASS, VERDICT_CLASS, ITEM_CLASS, BRANCH_CLASS, RELATION_CLASS,
                            JOB_CLASS))


def fact_href(subject: str) -> str:
    return "/facts/" + urllib.parse.quote(subject, safe="")


def src_name(label: Any, subject: str) -> str:
    """What a src link's accessible name calls its fact: the label shown beside it, or the subject when the label
    is empty, so two src links on one page never read the same to a screen reader unless their facts do."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(label or ""))).strip()
    return text if text and text != subject else subject


def short(sha: str | None, n: int = 7) -> str:
    return (sha or "")[:n]


def _span(s: int) -> str:
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%d h" % (s // 3600)
    return "%d days" % (s // 86400)


def human_age(seconds: float | None) -> str:
    """An age for a page. A negative age (a fact stamped after now: clock skew between hosts, or a clock that
    stepped back) is shown as clock skew, never as a fresh-looking "-3600 s"."""
    if seconds is None:
        return "no facts shown"
    s = int(seconds)
    if s < 0:
        return "clock skew: %s in the future" % _span(-s)
    return _span(s)


class Page:
    """Collects the facts one page prints."""

    def __init__(self, now: dt.datetime | None = None):
        self.now = now or clock.now()
        self.facts: list[dict] = []

    def fact(self, obs: dict, label: Any = None, *, tag: str = "span", cls: str = "") -> Markup:
        """One fact: its label marked with the subject and time, and the link to its sources."""
        self.facts.append(obs)
        s = obs["subject"]
        text = obs["subject"] if label is None else label
        return Markup('<%s class="fact %s" data-subject="%s" data-observed-at="%s">%s</%s>'
                      ' <a class="src" data-source-for="%s" href="%s" title="observed %s; how it was found"'
                      ' aria-label="How %s was found">src</a>'
                      % (tag, escape(cls), escape(s), escape(obs["observed_at"]), escape(text), tag,
                         escape(s), escape(fact_href(s)), escape(obs["observed_at"]), escape(src_name(text, s))))

    def mark(self, obs: dict) -> Markup:
        """A fact shown as a whole element (a tree row, a matrix cell) rather than as a label: its A3 attributes
        (`data-subject`, `data-observed-at`) for that element, recorded for the page's data age. The element's
        detail view prints the same fact with `fact()` and its source link."""
        self.facts.append(obs)
        return Markup('data-subject="%s" data-observed-at="%s"' % (escape(obs["subject"]), escape(obs["observed_at"])))

    def oldest(self) -> str | None:
        return min((o["observed_at"] for o in self.facts), key=clock.parse) if self.facts else None

    def oldest_subject(self) -> str | None:
        """The subject of the oldest fact on the page (the header links to it)."""
        return min(self.facts, key=lambda o: clock.parse(o["observed_at"]))["subject"] if self.facts else None

    def newest_age_seconds(self) -> int | None:
        """How long ago the newest fact on the page was observed: when the page was last brought up to date."""
        if not self.facts:
            return None
        newest = max((o["observed_at"] for o in self.facts), key=clock.parse)
        return int((self.now - clock.parse(newest)).total_seconds())

    def age_seconds(self) -> int | None:
        o = self.oldest()
        return None if o is None else int((self.now - clock.parse(o)).total_seconds())

    def age_of(self, at: str) -> str:
        return human_age((self.now - clock.parse(at)).total_seconds())


def verbatim(s: Any) -> Markup:
    """Text exactly as stored, escaped for HTML text only (&, <, >): a command keeps its quotes, so the
    page shows the very string `sources[].command` holds (A3)."""
    return Markup(html.escape(str(s), quote=False))


REGISTRY_PLACEHOLDER = "<registry>"


def _page_command(src: dict) -> str:
    cmd = str(src.get("command", ""))
    if src.get("kind") == "registry":
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = []
        if len(argv) > 3 and argv[:2] == ["git", "-C"]:
            cmd = " ".join(["git", "-C", REGISTRY_PLACEHOLDER] + [shlex.quote(a) for a in argv[3:]])
    return cmd


def page_doc(obs: Any) -> Any:
    """A copy of an observation as a page prints it: registry source commands as `shown_command` shows them."""
    if not isinstance(obs, dict):
        return obs
    doc = copy.deepcopy(obs)
    for src in doc.get("sources") or []:
        if isinstance(src, dict) and "command" in src:
            src["command"] = _page_command(src)
    return doc


def shown_command(source: Any) -> Markup:
    """A source's command as a page shows it: a registry read's
    `git -C <checkout path> show ...` shows the literal `<registry>` in place of the service's checkout path, so no
    page carries the VM's filesystem layout. The stored fact keeps the real path (`/facts` JSON, the CLI, the DB).
    Every other command is shown exactly as stored (A3)."""
    return verbatim(_page_command(source if isinstance(source, dict) else {}))


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)


env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True)
# Every page in the shell's navigation: (label, href, the path prefixes that mark it current). Each label is the
# page's own title, so a link reads the same as the page it opens. Plug-ins add entries per app.
NAV = (("Projects", "/", ("/", "/configurations", "/runs")), ("Compatibility check", "/compat", ("/compat",)),
       ("Branches", "/branches", ("/branches",)), ("Host copies", "/hosts", ("/hosts",)),
       ("Contains commit", "/contains", ("/contains",)),
       ("Silicon protection", "/silicon-protection", ("/silicon-protection",)),
       ("Components", "/components", ("/components", "/defects", "/errata")), ("Mirrors", "/mirrors", ("/mirrors",)),
       ("Alerts", "/alerts", ("/alerts",)))


# The header's main navigation: four places plus the bell. An entry whose page is not built here (a plug-in not
# installed) is left out; every other page is under More, in MORE_GROUPS.
TOP_NAV = (("Projects", "/", ("/",)), ("Matrix", "/matrix/", ("/matrix",)), ("Releases", "/releases/", ("/releases",)),
           ("Catalogue", "/catalogue/", ("/catalogue",)))
_CORE_TOP = ("/",)
# More, in three groups: what checks a tree, what reads the repositories, what makes a release. A plug-in page not
# listed here goes under Other.
MORE_GROUPS = (("Check", ("/compat", "/silicon-protection", "/quality/", "/alerts")),
               ("Repos", ("/components", "/branches", "/hosts", "/mirrors", "/contains")),
               ("Release", ("/versions/", "/bumps/", "/store/", "/publish/")))


def top_nav(nav: tuple, path: str) -> tuple[list[dict], list[dict]]:
    """(main entries, the rest under More) for the header, each {label, href, current}; a More entry also
    carries its `group`, and the More list is in MORE_GROUPS order."""
    def cur(prefixes) -> bool:
        return any(path == p or (p != "/" and path.startswith(p.rstrip("/") + "/")) or
                   (p != "/" and path == p.rstrip("/")) for p in prefixes)
    hrefs = {href for _, href, _ in nav}
    main = [{"label": label, "href": href, "current": cur(prefixes)} for label, href, prefixes in TOP_NAV
            if href in _CORE_TOP or href in hrefs]
    taken = {m["href"] for m in main}
    group_of = {href: g for g, hrefs_ in MORE_GROUPS for href in hrefs_}
    order = {g: i for i, (g, _) in enumerate(MORE_GROUPS)}
    more = [{"label": label, "href": href, "current": cur(prefixes), "group": group_of.get(href, "Other")}
            for label, href, prefixes in nav if href not in taken]
    more.sort(key=lambda m: order.get(m["group"], len(order)))
    return main, more


def more_groups(more: list[dict]) -> list[dict]:
    """The More entries as [{label, entries}] in MORE_GROUPS order (Other last), leaving out empty groups."""
    out: list[dict] = []
    for m in more:
        if not out or out[-1]["label"] != m["group"]:
            out.append({"label": m["group"], "entries": []})
        out[-1]["entries"].append(m)
    return out


def _age_text(at: str | None) -> str:
    if not at:
        return ""
    try:
        return human_age((clock.now() - clock.parse(at)).total_seconds())
    except (TypeError, ValueError):
        return ""


env.globals.update(short=short, fact_href=fact_href, human_age=human_age, STATE_CLASS=STATE_CLASS,
                   EXPOSURE_CLASS=EXPOSURE_CLASS, VERDICT_CLASS=VERDICT_CLASS, ITEM_CLASS=ITEM_CLASS,
                   BRANCH_CLASS=BRANCH_CLASS, RELATION_CLASS=RELATION_CLASS, JOB_CLASS=JOB_CLASS, age_text=_age_text,
                   to_json=_json, verbatim=verbatim,
                   shown_command=shown_command, page_doc=page_doc,
                   quote=lambda s: urllib.parse.quote(s, safe=""), LINE_LEGEND=_lines.LEGEND, line_of=_lines.line)
env.filters["plain"] = _terms.plain


def render(template: str, *, title: str, identity: Any = None, problems: list[str] | None = None,
           now: dt.datetime | None = None, csrf: str = "", environment: Environment | None = None,
           nav: tuple = NAV, path: str = "", auth_mode: str = "github", rail: dict | None = None,
           crumbs: list | None = None, tabs: list | None = None, tab_current: str | None = None,
           bell: int | None = None, bare: bool = False, doc_title: str | None = None, **ctx: Any) -> str:
    """The body template rendered first (its facts recorded), then the layout with the page's data age.
    `environment`: a plug-in's own Jinja environment for the body (the layout is always this package's).
    Shell: `rail` (shell.rail), `crumbs` [(label, href)], `tabs` [{key, label, href, count}] and
    `tab_current` (a tab's key or href) go to the layout only. Header: `bell` (unread notifications, None
    when unknown) and `bare` (the page prints its own title: no title bar). `doc_title`: the browser tab's title
    when it should say more than the heading (a workspace tab: "History · eth-rc4")."""
    page = Page(now)
    body_env = environment or env
    body_env.globals.update({k: v for k, v in env.globals.items() if k not in body_env.globals})
    body_env.filters.setdefault("plain", _terms.plain)
    body = body_env.get_template(template).render(page=page, fact=page.fact, csrf=csrf, identity=identity, **ctx)
    current = next((n for n in nav if any(path == p or (p != "/" and path.startswith(p + "/")) for p in n[2])), None)
    main_nav, more_nav = top_nav(tuple(nav), path)
    newest = page.newest_age_seconds()
    oldest_subject = page.oldest_subject()
    oldest_href = None
    if oldest_subject:
        # the oldest fact's own source link on this page (a plug-in may serve its facts elsewhere than /facts/)
        m = re.search(r'data-source-for="%s" href="([^"]+)"' % re.escape(str(escape(oldest_subject))), body)
        oldest_href = html.unescape(m.group(1)) if m else fact_href(oldest_subject)
    return env.get_template("layout.html").render(
        body=Markup(body), title=title, doc_title=doc_title or title, main_nav=main_nav, more_nav=more_nav,
        more_groups=more_groups(more_nav), bell=bell, bare=bare,
        path_is_notifications=path == "/notifications" or path.startswith("/notifications/"), identity=identity,
        problems=problems or [], csrf=csrf, nav=nav, current=current[1] if current else None, auth_mode=auth_mode,
        rail=rail, crumbs=crumbs or [], tabs=tabs or [], tab_current=tab_current,
        age_seconds=page.age_seconds(), oldest=page.oldest(), oldest_href=oldest_href,
        updated=None if newest is None else ("Updated %s ago" % human_age(newest) if newest >= 0
                                             else human_age(newest)),
        human_age=human_age, now=clock.iso(page.now))
