"""Every SQLite store the tool creates is mode 0600, and a data directory it creates is 0700, whatever the process
umask (evaluation 2026-10-16: rm, jobs, sessions and bumps were 0644 on the live host, gui was 0600)."""
from __future__ import annotations

import os
import stat

import pytest

from nanosoc_rm import auth, db, jobs
from nanosoc_rm.bumps import store as bumps_store
from nanosoc_rm.config import load_config
from nanosoc_rm.web import uistate


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)                       # the usual login umask: 0644 files unless the tool says otherwise
    yield
    os.umask(old)


def _open_every_store(config) -> None:
    with db.connect(config):
        pass
    with jobs._connect(config):
        pass
    with bumps_store.connect(config):
        pass
    auth._revoked_db(config).close()
    with uistate._connect(config):
        pass


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_every_store_is_created_private(rm_config, loose_umask):
    config = load_config(rm_config)
    _open_every_store(config)
    data = config.data_dir
    assert _mode(data) == 0o700
    names = ["rm.sqlite", "jobs.sqlite", "bumps.sqlite", "sessions.sqlite", "gui.sqlite"]
    assert {n: oct(_mode(data / n)) for n in names} == {n: "0o600" for n in names}
    for side in data.glob("*.sqlite-*"):                     # WAL and shared-memory files follow the main file
        assert _mode(side) & 0o077 == 0, side


def test_control_without_private_store_the_umask_wins(rm_config, loose_umask, monkeypatch):
    monkeypatch.setattr(db, "private_store", lambda p: p)
    config = load_config(rm_config)
    config.data_dir.mkdir()
    with db.connect(config):
        pass
    assert _mode(config.data_dir / "rm.sqlite") == 0o644


def test_an_existing_store_is_left_alone(rm_config, loose_umask):
    config = load_config(rm_config)
    config.data_dir.mkdir()
    p = config.data_dir / "rm.sqlite"
    p.touch()
    os.chmod(p, 0o640)
    with db.connect(config):
        pass
    assert _mode(p) == 0o640
