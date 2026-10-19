"""The plug-in helpers live once, in web/plugins.py: no plug-in or workspace module defines its own copy.

Before they moved, `_config` had 11 copies with 6 bodies and `_login` 9 with 4; each new plug-in copied whichever
variant it started from. Negative control (PR6): the same scan finds a copy planted in a module's source.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from nanosoc_rm.web import plugins, routers

SHARED = {"_config": "config_of", "_login": "login_of", "_local": "is_local", "_job_html": "job_card"}


def copies(source: str) -> set[str]:
    """The top-level functions in `source` that duplicate a shared helper."""
    return {n.name for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in SHARED}


def _modules() -> list[str]:
    return [m for m, _ in plugins.PLUGINS] + list(routers.ROUTERS) + list(routers.CORE)


def test_no_plugin_or_workspace_module_copies_a_shared_helper():
    found = {}
    for name in _modules():
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, name
        hit = copies(Path(spec.origin).read_text())
        if hit:
            found[name] = sorted(hit)
    assert not found, "use nanosoc_rm.web.plugins (%s) instead: %s" % (SHARED, found)


def test_negative_control_a_planted_copy_is_found():
    source = Path(importlib.util.find_spec("nanosoc_rm.store.web").origin).read_text()
    planted = source + "\n\ndef _config(request):\n    return request.state.config\n"
    assert copies(source) == set()
    assert copies(planted) == {"_config"}


def test_every_module_scanned_exists():
    assert len(_modules()) >= 15
    for name in _modules():
        assert importlib.util.find_spec(name) is not None, name
