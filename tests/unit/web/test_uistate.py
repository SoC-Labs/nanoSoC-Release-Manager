"""web.uistate: per-user GUI state in <data_dir>/gui.sqlite (Wave C, CR-W2)."""
from __future__ import annotations

import ast
import datetime as dt
import stat
import types
from pathlib import Path

import pytest

from nanosoc_rm.web import uistate

T0 = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path / "data")


def test_the_file_is_created_0600_with_its_schema_version(cfg):
    uistate.set_card(cfg, "alice", "compat_fail:eth-main", "done", "fp1", now=T0)
    p = uistate.path(cfg)
    assert p.name == "gui.sqlite" and p.parent == cfg.data_dir
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    import sqlite3
    con = sqlite3.connect(str(p))
    assert con.execute("PRAGMA user_version").fetchone()[0] == uistate.VERSION == 1
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
        "inbox_state", "review_decision", "basket_flag"}


def test_each_login_sees_only_its_own_state(cfg):
    uistate.set_card(cfg, "alice", "c1", "done", "fp", now=T0)
    uistate.decide(cfg, "alice", "tidelink", "abc", "k1", "accept", now=T0)
    uistate.set_flag(cfg, "alice", "tidelink", "abc", "banner", "on", now=T0)
    assert set(uistate.card_state(cfg, "alice", now=T0)) == {"c1"}
    assert uistate.card_state(cfg, "carol", now=T0) == {}
    assert uistate.decisions(cfg, "carol", "tidelink", "abc") == {} and uistate.flags(cfg, "carol", "tidelink", "abc") == {}
    row = uistate.card_state(cfg, "alice", now=T0)["c1"]
    assert row["login"] == "alice" and row["at"] == "2026-10-13T09:00:00Z"


def test_a_done_card_reopens_when_its_fingerprint_changes(cfg):
    uistate.set_card(cfg, "alice", "c1", "done", "fp-old", now=T0)
    assert "c1" in uistate.card_state(cfg, "alice", now=T0, fingerprints={"c1": "fp-old"})
    assert "c1" not in uistate.card_state(cfg, "alice", now=T0, fingerprints={"c1": "fp-new"})


def test_a_snoozed_card_hides_until_its_date_then_returns(cfg):
    uistate.set_card(cfg, "alice", "c2", "snoozed", "fp", until=T0 + dt.timedelta(days=7), now=T0)
    assert "c2" in uistate.card_state(cfg, "alice", now=T0 + dt.timedelta(days=6, hours=23))
    assert "c2" not in uistate.card_state(cfg, "alice", now=T0 + dt.timedelta(days=7))
    with pytest.raises(ValueError):
        uistate.set_card(cfg, "alice", "c3", "snoozed", "fp", now=T0)            # a snooze needs its date


def test_undo_restores_a_card(cfg):
    uistate.set_card(cfg, "alice", "c1", "done", "fp", now=T0)
    assert uistate.undo_card(cfg, "alice", "c1") is True
    assert uistate.card_state(cfg, "alice", now=T0) == {}
    assert uistate.undo_card(cfg, "alice", "c1") is False


def test_review_decisions_and_basket_are_scoped_to_login_component_and_commit(cfg):
    uistate.decide(cfg, "alice", "tidelink", "c1", "k", "edit", text="new line", now=T0)
    uistate.decide(cfg, "alice", "tidelink", "c1", "k2", "skip", text="ignored", now=T0)
    d = uistate.decisions(cfg, "alice", "tidelink", "c1")
    assert (d["k"]["decision"], d["k"]["text"]) == ("edit", "new line") and d["k2"]["text"] is None
    assert uistate.decisions(cfg, "alice", "tidelink", "c2") == {}                    # a new commit: a new basket
    uistate.set_flag(cfg, "alice", "tidelink", "c1", "readme:README.md#3", "1")
    uistate.set_flag(cfg, "alice", "tidelink", "c1", "banner", "on")
    uistate.set_flag(cfg, "alice", "tidelink", "c1", "banner", None)
    assert uistate.flags(cfg, "alice", "tidelink", "c1") == {"readme:README.md#3": "1"}
    uistate.clear_basket(cfg, "alice", "tidelink", "c1")
    assert uistate.decisions(cfg, "alice", "tidelink", "c1") == {} and uistate.flags(cfg, "alice", "tidelink", "c1") == {}
    with pytest.raises(ValueError):
        uistate.decide(cfg, "alice", "tidelink", "c1", "k", "approve")
    with pytest.raises(ValueError):
        uistate.set_card(cfg, "", "c1", "done", "fp")                                   # no login, no row


def test_uistate_imports_no_http_library():
    """AT-A2's local rule: a module that writes sqlite makes no outbound call."""
    src = Path(uistate.__file__).read_text()
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    assert not names & {"httpx", "requests", "urllib3", "http", "socket", "aiohttp", "subprocess"}, names
