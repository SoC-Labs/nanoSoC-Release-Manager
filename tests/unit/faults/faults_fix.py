"""Shared helpers for the failure-injection tests (robustness angle: faults).

The fixtures of other unit-test directories are reused read-only by putting those directories on sys.path (the
same way their own tests import them), so a fault test builds exactly the scene the module's own tests build and
then breaks one thing in it."""
from __future__ import annotations

import sys
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1]
for sub in ("web", "analysis", "mirror-walker", "jobs"):
    p = str(UNIT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
