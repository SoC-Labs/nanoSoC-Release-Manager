"""Versions and change classes (C1-C12), fixed-in from trailers (F6), tool releases (G1, G5, G6).

Phase 3 capabilities, BUILT and usable locally; nothing here is scheduled or writes to a repository,
a host or the registry. Every write a person makes is handed over as a file, a patch or a command.

    changelog_lint(target, ...)  -> changelog_lint observation            `nanosoc-rm changelog lint`   (C2, C5)
    plan(target, ...)            -> version_plan observation              `nanosoc-rm version next`     (C1, C3, C6-C9, C12)
    tag_check(target, ...)       -> tag_check observation                 `nanosoc-rm version tags`     (C10)
    manifest_init(target, ...)   -> (manifest_proposal observation, patch) `nanosoc-rm manifest init`   (C11)
    draft(target, out_dir, ...)  -> tool_release_draft observation + folder    `nanosoc-rm version draft`    (G1, G5)
    approve(draft_dir, step, login)                                       `nanosoc-rm version approve`  (G5)
    yank(target, version, ...)   -> (yank_proposal observation, patch)    `nanosoc-rm version yank`     (G6)
    defect_trailers(target, ...) -> (defect_trailers observation, patch)  `nanosoc-rm version fixed-in` (F6)
    is_yanked / allowed          the interface the bump engine (E1) codes against (G6)
    Regenerator / NoGenerator    the pluggable regeneration step of the C3 classifier (classify.py)
    changelog_entries            the classed entries between two commits, for release notes (releases)
    classify_bump                the C3 class of one pin move, for the bump engine (interfaces.md section 19.6)

Contract: docs/contracts/interfaces.md section 14b; schemas/tool-release.schema.json.
"""
from __future__ import annotations

from nanosoc_rm.versioning.classify import NoGenerator, RegenerationResult, Regenerator
from nanosoc_rm.versioning.consumers import changelog_entries, classify_bump
from nanosoc_rm.versioning.core import (KINDS, changelog_lint, manifest_init, observe, overview, plan, stored,
                                        tag_check, verdict_of_plan, yanked_versions)
from nanosoc_rm.versioning.release import APPROVAL_MATRIX, allowed, approve, draft, is_yanked, yank
from nanosoc_rm.versioning.repo import VersioningError, VersioningRefused, VersioningUsage
from nanosoc_rm.versioning.trailers import defect_trailers

__all__ = ["KINDS", "changelog_lint", "plan", "tag_check", "manifest_init", "draft", "approve", "yank", "is_yanked",
           "allowed", "defect_trailers", "overview", "stored", "observe", "verdict_of_plan", "yanked_versions",
           "Regenerator", "NoGenerator", "RegenerationResult", "APPROVAL_MATRIX", "VersioningError", "VersioningUsage",
           "VersioningRefused", "changelog_entries", "classify_bump"]
