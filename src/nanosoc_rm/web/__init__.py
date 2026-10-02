"""The web front end (interfaces.md sections 13 and 14): pages and a JSON API over the same functions as the
CLI, behind GitHub sign-in limited to active SoC-Labs members.

    from nanosoc_rm.web import create_app
    app = create_app(config_path=None, auth=None)     # auth=FakeMembership(login=...) in tests

The app module (FastAPI, Jinja2) is imported on the first call, so the CLI, which imports every plug-in's
commands, does not pay for it.
"""
from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def create_app(config_path: Any = None, auth: Any = None):
    """The FastAPI app. See nanosoc_rm.web.app.create_app."""
    from nanosoc_rm.web.app import create_app as _create_app
    return _create_app(config_path, auth=auth)
