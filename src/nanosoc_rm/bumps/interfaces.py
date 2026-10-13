"""The two things the bump engine needs from other Wave B modules, as small interfaces (interfaces.md section 19.6).

Both are looked up when a proposal is computed. A module that is not built (or not installed) here is not an
error: the engine falls back to what it can read itself and says which source it used.

1. **Change class (C2, C3; the versioning module).** `nanosoc_rm.versioning.classify_bump`:

       classify_bump(component: str, from_commit: str, to_commit: str, *, config) -> dict | None
           {"class": "breaking" | "feature" | "fix" | "none", "detail": str, "sources": [source, ...]}

   Fallback: the class the changelog's own entries declare (C2's author signal only, `class_source: changelog`),
   else `unknown`. The fallback never infers a class from a version number (C2).

2. **Matrix cell (D3; the matrix module).** `nanosoc_rm.matrix.bump_cell`:

       bump_cell(configuration: str, overrides: dict[str, str], *, config) -> dict | None
           {"verdict": <gate verdict>, "observed_at": utc, "href": "/matrix/...", "detail": str}

   for the stored result of exactly that configuration with exactly those pins moved, or None. Fallback: a
   finished local compat job (CR-17) whose pins are exactly those overrides (`source: compat-job`), else no result
   and a Run button that queues that compat job.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable

__all__ = ["CLASSIFIER", "MATRIX", "provider"]

CLASSIFIER = ("nanosoc_rm.versioning", "classify_bump")
MATRIX = ("nanosoc_rm.matrix", "bump_cell")


def provider(spec: tuple[str, str]) -> Callable[..., Any] | None:
    """The function `spec` names, or None when its module is not built here. A module that exists but fails to
    import is an error, never hidden (as the CLI and web plug-in seams do)."""
    modname, attr = spec
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        parts = modname.split(".")
        if e.name in {".".join(parts[:i]) for i in range(1, len(parts) + 1)}:
            return None
        raise
    fn = getattr(mod, attr, None)
    return fn if callable(fn) else None
