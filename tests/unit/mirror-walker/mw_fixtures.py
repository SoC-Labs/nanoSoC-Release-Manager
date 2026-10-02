"""Loaders shared by the mirror and walker unit tests.

The acceptance test files hold the hermetic fixtures (LocalHosts, build_state_table, local_estate, FakeHost, ...)
and the checkers. These unit tests reuse them read-only by importing the files as modules, so a unit test and its
AT share one fixture definition (PR9); nothing in tests/acceptance is changed. (A conftest.py here would shadow
tests/unit/conftest.py, which the foundation's tests import by name, so each test module declares its own small
fixtures over these loaders.)
"""
from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


@functools.lru_cache(maxsize=None)
def load_at(name: str):
    """tests/acceptance/<name>.py as a module."""
    spec = importlib.util.spec_from_file_location("mw_at_%s" % name, REPO / "tests" / "acceptance" / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
