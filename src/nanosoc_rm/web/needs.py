"""The "What needs me?" strip on Home: at most three one-line actions, each with one link, from stored facts.

    items(config, chars=None, limit=3) -> [{key, text, label, href}]

Each source reads the store or the registry records only (a GET never walks, fetches or queues), and a source that
cannot be read gives no line rather than a broken page. The strip is hidden when the list is empty. Sources, in
the order they are shown:

    unreleased   a silicon configuration with no published release (release_publication facts)
    held         bump proposals the bump gate holds (state blocked, not yet decided by a person)
    private      private repos the mirrors cannot read (the alert groups whose cause is a private repo)
    catalogue    repos that need a group: new repos from the org scan, and catalogue entries with a question
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Callable

__all__ = ["items", "SOURCES", "LIMIT"]

LIMIT = 3


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _names(ids: list[str], n: int = 2) -> str:
    return ", ".join(ids[:n]) + (" and %d more" % (len(ids) - n) if len(ids) > n else "")


def _unreleased(config: Any, chars: dict | None) -> dict | None:
    from nanosoc_rm.publish.service import publications
    from nanosoc_rm.web import views
    silicon = sorted(d["fact"]["record"]["id"] for d in views.configurations(config)
                     if isinstance(d["fact"]["record"].get("silicon"), dict))
    if not silicon:
        return None
    published = {r["fact"].get("configuration") for r in publications(config) if r["fact"].get("kind") == "release"}
    missing = [cid for cid in silicon if cid not in published]
    if not missing:
        return None
    n = len(missing)
    return {"key": "unreleased",
            "text": "%s %s taped out with no published release." % (_names(missing), _plural(n, "is", "are")),
            "label": "Publish a release", "href": "/publish/"}


def _held(config: Any, chars: dict | None) -> dict | None:
    from nanosoc_rm import bumps
    decided = {m.get("proposal") for m in bumps.marks(config=config)}
    held: dict[str, dict] = {}
    for d in bumps.stored(config=config):
        f = d["fact"]
        if f.get("state") == "blocked" and f.get("id") not in decided:
            held.setdefault(f["id"], f)
    if not held:
        return None
    n = len(held)
    first = held[sorted(held)[0]]
    what = "%s in %s" % ((first.get("dependency") or {}).get("component") or "a pin",
                         (first.get("consumer") or {}).get("component") or first.get("configuration") or "a tree")
    return {"key": "held",
            "text": "%d bump %s held (%s%s)." % (
                n, _plural(n, "proposal is", "proposals are"), what, " and more" if n > 1 else ""),
            "label": "Open the held bumps", "href": "/bumps/"}


def _private(config: Any, chars: dict | None) -> dict | None:
    from nanosoc_rm.web import alertgroups
    groups = [g for g in alertgroups.groups(config) if g["key"].startswith("private-")]
    if not groups:
        return None
    n = sum(g["count"] for g in groups)
    g = groups[0]                                                # biggest first: the link opens it
    return {"key": "private",
            "text": "%d private %s cannot be read by the mirrors." % (n, _plural(n, "repo", "repos")),
            "label": "See why and the fix", "href": "/alerts#group-%s" % urllib.parse.quote(g["key"], safe="")}


def _catalogue(config: Any, chars: dict | None) -> dict | None:
    from nanosoc_rm import db
    try:
        new = len(db.latest("repo_seen", config=config))
    except Exception:                                            # noqa: BLE001 - no org scan stored yet
        new = 0
    ask = sum(1 for ch in (chars or {}).values() if getattr(ch, "question", None))
    n = new + ask
    if not n:
        return None
    return {"key": "catalogue",
            "text": "%d %s a group in the catalogue." % (n, _plural(n, "repo needs", "repos need")),
            "label": "Characterise", "href": "/catalogue/"}


SOURCES: tuple[tuple[str, Callable[[Any, dict | None], dict | None]], ...] = (
    ("unreleased", _unreleased), ("held", _held), ("private", _private), ("catalogue", _catalogue))


def items(config: Any, chars: dict | None = None, limit: int = LIMIT) -> list[dict]:
    """The strip's lines, at most `limit`, in SOURCES order."""
    out: list[dict] = []
    for _, source in SOURCES:
        if len(out) >= limit:
            break
        try:
            got = source(config, chars)
        except Exception:                                        # noqa: BLE001 - one unreadable source: no line
            got = None
        if got:
            out.append(got)
    return out
