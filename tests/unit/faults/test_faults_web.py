"""Failure injection on the web GUI (local mode, interfaces.md sections 13 and 13a): a corrupt or unwritable fact
store, a corrupt session store, a registry that does not load. The rules under test: no page ever shows a Python
traceback; a page whose data cannot be read says which store failed (not a bare "Internal Server Error"); a broken
registry is never summarised as "nothing needs you".

Robustness angle: faults. A test that exposes a defect is a strict xfail named ROB-faults-<n>."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat

import pytest
from fastapi.testclient import TestClient

import faults_fix  # sys.path for the other unit-test directories

assert faults_fix.UNIT.is_dir()
__all__ = ["seeded_db"]           # the fixture, used by name
from nanosoc_rm.web import create_app
from test_web_local import local_config, sign_in
from webfix import _git, make_registry, plant_problem, seeded_db  # noqa: F401

# Pages that read rm.sqlite (the fact store) for their content.
STORE_PAGES = ["/", "/mirrors", "/alerts", "/compat", "/quality/", "/configurations/eth-rc4"]


@pytest.fixture
def gui(tmp_path, monkeypatch, seeded_db):
    reg = make_registry(tmp_path / "registry")
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(seeded_db, data / "rm.sqlite")
    cfg = local_config(tmp_path / "rm.toml", data, reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    assert sign_in(client).status_code == 303
    return {"client": client, "data": data, "registry": reg, "cfg": cfg}


def corrupt(path):
    path.write_bytes(b"this is not an sqlite database\n" * 64)
    for ext in ("-wal", "-shm"):
        path.with_name(path.name + ext).unlink(missing_ok=True)


def bare_500(r) -> bool:
    return r.status_code == 500 and r.text.strip() == "Internal Server Error"


def no_traceback(r) -> bool:
    return "Traceback (most recent call last)" not in r.text and 'File "' not in r.text


def test_every_store_page_renders_before_the_fault(gui):
    """The control for the tests below: the same pages on the same data, nothing broken."""
    for p in STORE_PAGES:
        r = gui["client"].get(p)
        assert r.status_code == 200 and not bare_500(r) and no_traceback(r), (p, r.status_code)


# ----- the fact store is corrupt ------------------------------------------------------------------------------------
def test_a_corrupt_store_never_shows_a_traceback(gui):
    corrupt(gui["data"] / "rm.sqlite")
    for p in STORE_PAGES:
        r = gui["client"].get(p)
        assert no_traceback(r), p
        assert r.status_code != 200 or "could not be read" in r.text or "Problem" in r.text, p


@pytest.mark.xfail(strict=True, reason="ROB-faults-11: with rm.sqlite unreadable (corrupt, locked past the timeout, "
                                       "read-only) most pages answer a bare 500 'Internal Server Error' with no word "
                                       "of which store failed; there is no app-wide exception handler")
@pytest.mark.parametrize("page", STORE_PAGES)
def test_a_corrupt_store_page_names_the_store_not_a_bare_500(gui, page):
    corrupt(gui["data"] / "rm.sqlite")
    r = gui["client"].get(page)
    assert not bare_500(r), (page, r.status_code, r.text[:200])


@pytest.mark.xfail(strict=True, reason="ROB-faults-11: a corrupt sessions.sqlite turns every signed-in page and the "
                                       "sign-in itself into a bare 500 'Internal Server Error'")
def test_a_corrupt_session_store_is_not_a_bare_500(gui):
    corrupt(gui["data"] / "sessions.sqlite")
    fresh = TestClient(gui["client"].app, raise_server_exceptions=False)
    answers = [gui["client"].get("/"), sign_in(fresh)]
    assert not any(bare_500(r) for r in answers), [(r.status_code, r.text[:80]) for r in answers]


def test_a_corrupt_session_store_never_shows_a_traceback_and_never_signs_in(gui):
    corrupt(gui["data"] / "sessions.sqlite")
    r = gui["client"].get("/")
    assert no_traceback(r) and r.status_code != 200            # a session that cannot be checked is not accepted


def test_a_read_only_data_dir_never_shows_a_traceback(gui):
    data = gui["data"]
    mode = data.stat().st_mode
    os.chmod(data, stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(data, os.W_OK):
            pytest.skip("running as a user that ignores directory modes")
        for p in STORE_PAGES:
            assert no_traceback(gui["client"].get(p)), p
    finally:
        os.chmod(data, mode)


# ----- the registry does not load ------------------------------------------------------------------------------------
def test_a_registry_broken_after_a_good_load_keeps_the_last_good_records_and_says_so(gui):
    plant_problem(gui["registry"])
    r = gui["client"].get("/")
    assert r.status_code == 200 and no_traceback(r)
    assert "registry problem" in r.text and "last good" in r.text


def _empty_registry(reg):
    for f, key in (("catalogue.yaml", "components"), ("eligibility.yaml", "configurations")):
        d = json.loads((reg / f).read_text())
        d[key] = []
        (reg / f).write_text(json.dumps(d))
    _git("add", "-A", ".", cwd=reg)
    _git("commit", "-qm", "empty catalogue", cwd=reg)


@pytest.mark.xfail(strict=True, reason="ROB-faults-12: when the registry has never loaded (here: an empty catalogue, "
                                       "refused by the schema) the inbox still says 'Inbox clear. Nothing needs you "
                                       "right now.' and 'showing the last good records' when there are none")
def test_a_registry_that_never_loaded_is_not_an_inbox_clear(tmp_path, monkeypatch):
    reg = make_registry(tmp_path / "registry")
    _empty_registry(reg)
    (tmp_path / "data").mkdir()
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    sign_in(client)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/").text))
    assert "registry problem" in text                               # the banner is there either way
    assert "Nothing needs you" not in text, text[:600]
