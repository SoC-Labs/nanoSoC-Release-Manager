"""Rendering with the page hooks of interfaces.md section 13 (A3).

Every fact a page shows goes through `Page.fact(obs)`, which prints it with `data-subject` and
`data-observed-at` and a `data-source-for` link to `/facts/<subject>`, and records it. The page's one
data-age element (`data-age-seconds`, `data-oldest-observed-at`) is computed AFTER the body is rendered, from
exactly the facts the body printed, and measured from the OLDEST of them (README section 4), so the age a page
shows can never be younger than its oldest fact.
"""
from __future__ import annotations

import datetime as dt
import copy
import html
import shlex
import json
import urllib.parse
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from nanosoc_rm import clock

__all__ = ["Page", "env", "render", "fact_href", "short", "human_age", "STATE_CLASS", "EXPOSURE_CLASS",
           "VERDICT_CLASS"]

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Colours (README section 5 states; section 13 F2 exposure; verdicts).
STATE_CLASS = {"TAGGED": "state-green", "ON_DEFAULT": "state-green", "PROTECTED_BY_TAG": "state-green",
               "BRANCH_ONLY": "state-amber"}
EXPOSURE_CLASS = {"exposed": "state-red", "affected": "state-red", "probably-exposed": "state-amber",
                  "probably-not-affected": "state-amber", "not-affected": "state-green"}
VERDICT_CLASS = {"PASS": "state-green", "WAIVED": "state-amber", "WARN": "state-amber", "FAIL": "state-red",
                 "UNVERIFIED": "state-red",
                 "INFRA": "state-amber", "NOT-RUN": "state-grey"}


def fact_href(subject: str) -> str:
    return "/facts/" + urllib.parse.quote(subject, safe="")


def short(sha: str | None, n: int = 7) -> str:
    return (sha or "")[:n]


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "no facts shown"
    s = int(seconds)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%d h" % (s // 3600)
    return "%d days" % (s // 86400)


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
                      ' <a class="src" data-source-for="%s" href="%s" title="observed %s; how it was found">src</a>'
                      % (tag, escape(cls), escape(s), escape(obs["observed_at"]), escape(text), tag,
                         escape(s), escape(fact_href(s)), escape(obs["observed_at"])))

    def oldest(self) -> str | None:
        return min((o["observed_at"] for o in self.facts), key=clock.parse) if self.facts else None

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
    """A source's command as a page shows it (K2 option a, David 2026-10-10): a registry read's
    `git -C <checkout path> show ...` shows the literal `<registry>` in place of the service's checkout path, so no
    page carries the VM's filesystem layout. The stored fact keeps the real path (`/facts` JSON, the CLI, the DB).
    Every other command is shown exactly as stored (A3)."""
    return verbatim(_page_command(source if isinstance(source, dict) else {}))


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)


env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True)
env.globals.update(short=short, fact_href=fact_href, human_age=human_age, STATE_CLASS=STATE_CLASS,
                   EXPOSURE_CLASS=EXPOSURE_CLASS, VERDICT_CLASS=VERDICT_CLASS, to_json=_json, verbatim=verbatim,
                   shown_command=shown_command, page_doc=page_doc,
                   quote=lambda s: urllib.parse.quote(s, safe=""))


def render(template: str, *, title: str, identity: Any = None, problems: list[str] | None = None,
           now: dt.datetime | None = None, csrf: str = "", **ctx: Any) -> str:
    """The body template rendered first (its facts recorded), then the layout with the page's data age."""
    page = Page(now)
    body = env.get_template(template).render(page=page, fact=page.fact, csrf=csrf, identity=identity, **ctx)
    return env.get_template("layout.html").render(
        body=Markup(body), title=title, identity=identity, problems=problems or [],
        age_seconds=page.age_seconds(), oldest=page.oldest(), human_age=human_age, now=clock.iso(page.now))
