"""The tool's own CHANGELOG.md (C2): it passes the tool's own `changelog lint`, and every change request it cites
is a row in docs/CHANGES.md, so a reader can trace each entry back to what was agreed (PR7)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]


def _changes_ids() -> set[str]:
    ids = set()
    for line in (ROOT / "docs" / "CHANGES.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(CR-?[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\s*\|", line)
        if m:
            ids.add(m.group(1))
    return ids


@pytest.mark.requirement("C2")
def test_the_tools_changelog_passes_its_own_lint(monkeypatch):
    from nanosoc_rm.cli import main
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    result = CliRunner().invoke(main, ["changelog", "lint", "--path", str(ROOT)])
    assert result.exit_code == 0, result.output
    assert "PASS (0 error, 0 warning)" in result.output, result.output


def _entries(text: str) -> list[str]:
    """Each list item of the changelog as one line (continuation lines joined)."""
    return [e for e in re.sub(r"\n[ \t]+", " ", text).splitlines() if e.startswith("- ")]


@pytest.mark.requirement("PR7")
def test_every_entry_cites_what_it_came_from_and_each_cr_has_a_changes_row():
    entries = _entries((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert len(entries) > 20
    cited, uncited = set(), []
    for e in entries:
        m = re.search(r"\(([^()]+)\)\s*$", e)
        if not m:
            uncited.append(e[:60])
            continue
        cited.update(t.strip() for t in m.group(1).split(",") if re.fullmatch(r"CR-?[A-Za-z0-9-]+", t.strip()))
    assert not uncited, "entries that cite no change request: %s" % uncited
    missing = sorted(cited - _changes_ids())
    assert not missing, "CHANGELOG.md cites change requests with no docs/CHANGES.md row: %s" % missing
