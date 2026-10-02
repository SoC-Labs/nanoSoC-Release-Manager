"""Fixtures for the foundation's unit tests (config, contracts, gitrun, db, cli)."""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "schemas" / "examples"


@pytest.fixture
def rm_config(tmp_path, monkeypatch):
    """A minimal service config ([service] data_dir only), named by $NANOSOC_RM_CONFIG for this test."""
    path = tmp_path / "rm.toml"
    path.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(path))
    return path
