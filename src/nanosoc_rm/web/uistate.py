"""Per-user GUI state (Wave C, CR-W2): `<data_dir>/gui.sqlite`.

Not a fact store and not rebuildable, like jobs.sqlite and bumps.sqlite: it holds what one signed-in person did
to their own view, nothing that changes system state. Each row carries the login and the time.

    path(config) -> Path                                          <data_dir>/gui.sqlite (mode 0600)
    card_state(config, login, now=None) -> {card: Row}            the inbox cards this login has hidden now
    set_card(config, login, card, state, fp, until=None)          state "done" (until the fingerprint changes)
                                                                  or "snoozed" (until `until`)
    undo_card(config, login, card)                                show it again
    decisions(config, login, comp, commit) -> {key: Row}          review decisions (accept, edit, skip, dismiss)
    decide(config, login, comp, commit, key, decision, text=None)
    flags(config, login, comp, commit) -> {name: value}           basket flags ("banner", "readme:<row key>")
    set_flag(config, login, comp, commit, name, value)            value None removes the flag
    clear_basket(config, login, comp, commit)
    prefs(config, login) -> {name: value}                         per-login view preferences (Wave F, CR-WF-gui):
                                                                  "timeline" ("on"/"off"), "notify.kind.<cat>" and
                                                                  "notify.project.<id>" ("off" mutes one)
    set_pref(config, login, name, value)                          value None removes the preference

A done card reopens when its facts change: `card_state(..., fingerprints={card: fp})` drops a done row whose
stored fingerprint differs from the card's current one. A snoozed card reopens at `until`.

WAL mode, one table per concern, `PRAGMA user_version = 2` (version 2 adds `pref`, CR-WF-gui; a version-1 file
gains the table the next time it is opened, every other table untouched). `prefs` never creates the file: a GET that reads
preferences of a login with no file answers {} (the defaults). This module imports no HTTP library (AT-A2's local
rule: a sqlite write sits in a file that makes no outbound call).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator, Mapping

from nanosoc_rm import clock

__all__ = ["path", "card_state", "set_card", "undo_card", "decisions", "decide", "flags", "set_flag",
           "clear_basket", "prefs", "set_pref", "PREF_NAME", "CARD_STATES", "DECISIONS", "VERSION"]

VERSION = 2
CARD_STATES = ("done", "snoozed")
DECISIONS = ("accept", "edit", "skip", "dismiss")
_DDL = """
CREATE TABLE IF NOT EXISTS inbox_state (
    login TEXT NOT NULL, card_id TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT NOT NULL,
    until TEXT, at TEXT NOT NULL, PRIMARY KEY (login, card_id));
CREATE TABLE IF NOT EXISTS review_decision (
    login TEXT NOT NULL, component TEXT NOT NULL, commit_sha TEXT NOT NULL, key TEXT NOT NULL,
    decision TEXT NOT NULL, text TEXT, at TEXT NOT NULL, PRIMARY KEY (login, component, commit_sha, key));
CREATE TABLE IF NOT EXISTS basket_flag (
    login TEXT NOT NULL, component TEXT NOT NULL, commit_sha TEXT NOT NULL, name TEXT NOT NULL,
    value TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (login, component, commit_sha, name));
CREATE TABLE IF NOT EXISTS pref (
    login TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (login, name));
"""
# A preference name: the timeline toggle, or one muted notification kind or project (CR-WF-gui).
PREF_NAME = re.compile(r"^(timeline|notify\.kind\.[a-z_]{1,40}|notify\.project\.[A-Za-z0-9_.-]{1,100})\Z")


def path(config) -> Path:
    return Path(config.data_dir) / "gui.sqlite"


@contextlib.contextmanager
def _connect(config) -> Iterator[sqlite3.Connection]:
    p = path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600)     # created 0600, never world-readable
        os.close(fd)
    con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        if con.execute("PRAGMA user_version").fetchone()[0] < VERSION:
            con.executescript(_DDL)                                  # every table IF NOT EXISTS: an upgrade adds
            con.execute("PRAGMA user_version = %d" % VERSION)
        yield con
    finally:
        con.close()


def _need(login: str) -> None:
    if not login:
        raise ValueError("GUI state needs the signed-in login")


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or clock.now()


def card_state(config, login: str, now: dt.datetime | None = None,
               fingerprints: Mapping[str, str] | None = None) -> dict[str, dict]:
    """{card id: row} for the cards this login has hidden NOW: done rows whose fingerprint still matches (when
    `fingerprints` gives the cards' current ones), snoozed rows whose `until` is in the future."""
    _need(login)
    t = _now(now)
    out = {}
    with _connect(config) as con:
        for r in con.execute("SELECT * FROM inbox_state WHERE login = ?", (login,)):
            d = dict(r)
            if d["state"] == "snoozed" and (not d["until"] or clock.parse(d["until"]) <= t):
                continue
            if d["state"] == "done" and fingerprints is not None and d["card_id"] in fingerprints \
                    and fingerprints[d["card_id"]] != d["fingerprint"]:
                continue
            out[d["card_id"]] = d
    return out


def set_card(config, login: str, card: str, state: str, fp: str, until: dt.datetime | str | None = None,
             now: dt.datetime | None = None) -> None:
    _need(login)
    if state not in CARD_STATES:
        raise ValueError("card state must be one of %s" % ", ".join(CARD_STATES))
    if not card:
        raise ValueError("no card id")
    if state == "snoozed" and until is None:
        raise ValueError("a snoozed card needs `until`")
    u = clock.iso(until) if isinstance(until, dt.datetime) else until
    with _connect(config) as con:
        con.execute("INSERT OR REPLACE INTO inbox_state (login, card_id, state, fingerprint, until, at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (login, card, state, fp or "", u, clock.iso(_now(now))))


def undo_card(config, login: str, card: str) -> bool:
    _need(login)
    with _connect(config) as con:
        return con.execute("DELETE FROM inbox_state WHERE login = ? AND card_id = ?", (login, card)).rowcount > 0


def decisions(config, login: str, comp: str, commit: str) -> dict[str, dict]:
    _need(login)
    with _connect(config) as con:
        rows = con.execute("SELECT * FROM review_decision WHERE login = ? AND component = ? AND commit_sha = ?",
                           (login, comp, commit))
        return {r["key"]: dict(r) for r in rows}


def decide(config, login: str, comp: str, commit: str, key: str, decision: str, text: str | None = None,
           now: dt.datetime | None = None) -> None:
    _need(login)
    if decision not in DECISIONS:
        raise ValueError("decision must be one of %s" % ", ".join(DECISIONS))
    if decision != "edit":
        text = None
    with _connect(config) as con:
        con.execute("INSERT OR REPLACE INTO review_decision (login, component, commit_sha, key, decision, text, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (login, comp, commit, key, decision, text, clock.iso(_now(now))))


def flags(config, login: str, comp: str, commit: str) -> dict[str, str]:
    _need(login)
    with _connect(config) as con:
        rows = con.execute("SELECT name, value FROM basket_flag WHERE login = ? AND component = ? AND commit_sha = ?",
                           (login, comp, commit))
        return {r["name"]: r["value"] for r in rows}


def set_flag(config, login: str, comp: str, commit: str, name: str, value: str | None,
             now: dt.datetime | None = None) -> None:
    _need(login)
    with _connect(config) as con:
        if value is None:
            con.execute("DELETE FROM basket_flag WHERE login = ? AND component = ? AND commit_sha = ? AND name = ?",
                        (login, comp, commit, name))
        else:
            con.execute("INSERT OR REPLACE INTO basket_flag (login, component, commit_sha, name, value, at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (login, comp, commit, name, str(value), clock.iso(_now(now))))


def clear_basket(config, login: str, comp: str, commit: str) -> None:
    """Forget this login's decisions and flags for one component at one analysed commit."""
    _need(login)
    with _connect(config) as con:
        for table in ("review_decision", "basket_flag"):
            con.execute("DELETE FROM %s WHERE login = ? AND component = ? AND commit_sha = ?" % table,
                        (login, comp, commit))


def prefs(config, login: str) -> dict[str, str]:
    """{name: value} of this login's view preferences; {} when gui.sqlite does not exist (a GET never creates
    it) or was written before preferences existed."""
    _need(login)
    if not path(config).exists():
        return {}
    with _connect(config) as con:
        return {r["name"]: r["value"] for r in con.execute("SELECT name, value FROM pref WHERE login = ?", (login,))}


def set_pref(config, login: str, name: str, value: str | None, now: dt.datetime | None = None) -> None:
    _need(login)
    if not PREF_NAME.match(name or ""):
        raise ValueError("not a preference name: %r" % (name or "")[:60])
    with _connect(config) as con:
        if value is None:
            con.execute("DELETE FROM pref WHERE login = ? AND name = ?", (login, name))
        else:
            con.execute("INSERT OR REPLACE INTO pref (login, name, value, at) VALUES (?, ?, ?, ?)",
                        (login, name, str(value)[:40], clock.iso(_now(now))))
