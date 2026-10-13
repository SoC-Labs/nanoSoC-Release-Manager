"""Wave B integration: what the releases module and the bump engine read from versioning."""
from __future__ import annotations

import pytest

from nanosoc_rm import versioning
from nanosoc_rm.config import load_config
import vfix


@pytest.mark.requirement("C2")
@pytest.mark.requirement("G2")
def test_changelog_entries_are_what_the_range_brings_and_release_notes_use_them(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."]})
    got = versioning.changelog_entries("nanosoc_arch_tech", s["base"], s["head"], config=s["cfg"])
    assert got == [{"class": "fix", "summary": "Fix the flist order.", "commit": s["head"]}], got
    every = versioning.changelog_entries("nanosoc_arch_tech", None, s["head"], config=s["cfg"])
    assert {(e["class"], e["summary"]) for e in every} == {("fix", "Fix the flist order."),
                                                          ("feature", "First release of the flist layout.")}
    with pytest.raises(LookupError):
        versioning.changelog_entries("no_such_component", None, s["head"], config=s["cfg"])
    dropped = vfix.commit(s["gen"], {"CHANGELOG.md": None}, "2026-09-20T10:00:00Z", "drop the changelog")
    vfix.mirror_from(s["data"], "nanosoc_gen", {vfix.GEN: s["gen"]})
    assert versioning.changelog_entries("nanosoc_gen", s["gen_a"], s["gen_b"], config=s["cfg"]) == [
        {"class": "fix", "summary": "Generator fix.", "commit": s["gen_b"]}]
    with pytest.raises(LookupError):                       # a commit without CHANGELOG.md: the caller falls back
        versioning.changelog_entries("nanosoc_gen", s["gen_b"], dropped, config=s["cfg"])
    from nanosoc_rm.releases import notes
    entries, source = notes.entries(load_config(s["cfg"]), "nanosoc_arch_tech", s["base"], s["head"])
    assert source.startswith("nanosoc_rm.versioning.changelog_entries(")
    assert [(e["class"], e["summary"], e["how"]) for e in entries] == [("fix", "Fix the flist order.", "declared")]


def test_an_entry_released_in_the_range_still_counts():
    from nanosoc_rm.versioning import changelog as cl
    from nanosoc_rm.versioning.consumers import _added

    class View:
        def __init__(self, texts):
            self.texts = texts

        def files_at(self, commit, paths):
            return {cl.FILE: self.texts.get(commit)}
    before = vfix.unreleased({"feature": ["Add the DMA window."]})
    after = before.replace("## [Unreleased]\n### feature\n- Add the DMA window.\n",
                           "## [Unreleased]\n\n## [0.2.0] - 2026-09-20\n### feature\n- Add the DMA window.\n")
    assert _added(View({"a": before, "b": after}), "a", "b") == [("feature", "Add the DMA window.")]
    assert _added(View({"a": after, "b": after}), "a", "b") == []


@pytest.mark.requirement("C3")
@pytest.mark.requirement("E1")
def test_classify_bump_gives_the_c3_class_of_a_move(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."]},
                       api_after="# API\n\n- flist layout v1\n")                 # a port line removed: breaking
    got = versioning.classify_bump("nanosoc_arch_tech", s["base"], s["head"], config=s["cfg"])
    assert got["class"] == "breaking" and "api breaking (measured)" in got["detail"], got
    assert "consumers - (unmeasured)" in got["detail"] and got["detail"].startswith("C3 partial")
    assert got["sources"], "the mirror reads are named"
    plain = vfix.arch_site(tmp_path / "plain", monkeypatch, entries={"fix": ["Fix the flist order."]})
    got = versioning.classify_bump("nanosoc_arch_tech", plain["base"], plain["head"], config=plain["cfg"])
    assert got["class"] == "fix", got                                           # API.md unchanged: none; declared fix
    assert versioning.classify_bump("no_such_component", plain["base"], plain["head"], config=plain["cfg"]) is None
