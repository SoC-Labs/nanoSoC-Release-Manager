"""Releases in local mode (G2, G3, G4, G7, F4, I1, I2): drafts, re-verification, errata documents, forks.

Built for local use now, while the Phase 5 entry gate still governs enabling (published releases,
tags made by a tool). Nothing in this package tags, pushes, uploads or writes to any host: every such action is a
person step the draft lists with its command.

    draft(configuration, version, ...) -> release_draft observation     the draft folder (nanosoc_rm.releases.builder)
    verify(ref, ...) -> release_verify observation                      re-check a draft against the mirrors now
    drafts(config, configuration=None) -> [{dir, configuration, version, doc, problems}]
    forks(component=None, ...) -> [fork_status observation]             I1, I2 read-only (nanosoc_rm.releases.forkscan)
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm.releases.core import (PUBLISH_NOTE, ReleaseError, ReleaseUsage, draft_path, exit_for, ladder,
                                      releases_dir)
from nanosoc_rm.releases.builder import draft, drafts, find_draft, parse_waiver, read_draft, verify
from nanosoc_rm.releases.forkscan import forks

__all__ = ["draft", "verify", "drafts", "find_draft", "read_draft", "forks", "parse_waiver", "ReleaseError",
           "ReleaseUsage", "exit_for", "ladder", "releases_dir", "draft_path", "draft_path_of", "PUBLISH_NOTE"]


def draft_path_of(doc: dict, out: str | None = None) -> Path:
    """The folder a release_draft document was written to."""
    if out:
        return Path(out)
    from nanosoc_rm.config import load_config
    f = doc["fact"]
    return draft_path(load_config(), f["configuration"], f["version"])
