"""docs/cli.md is generated from the click tree (scripts/gen_cli_docs.py) and must match it exactly, so a command
whose help changes, or a new command, fails here until the reference is regenerated."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_cli_docs", ROOT / "scripts" / "gen_cli_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _all_paths(group: click.Group, prefix=()):
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        yield prefix + (name,)
        if isinstance(cmd, click.Group):
            yield from _all_paths(cmd, prefix + (name,))


@pytest.mark.requirement("PR4")
def test_cli_md_is_in_sync_with_the_click_tree():
    current = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert current == _gen().render(), "docs/cli.md is stale: run .venv/bin/python scripts/gen_cli_docs.py"


@pytest.mark.requirement("PR4")
def test_every_command_has_a_section_and_an_index_row():
    from nanosoc_rm.cli import main
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    paths = list(_all_paths(main))
    assert len(paths) > 30
    for path in paths:
        anchor = "nanosoc-rm-" + "-".join(path)
        assert '<a id="%s"></a>' % anchor in text, path
        assert "](#%s) |" % anchor in text, path


def test_render_does_not_depend_on_the_terminal_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    narrow = _gen().render()
    monkeypatch.setenv("COLUMNS", "250")
    assert _gen().render() == narrow


def test_leading_requirement_ids_move_to_the_end_of_the_summary():
    gen = _gen()

    @click.command(help="CR8: every branch of a repo.")
    def a():
        pass

    @click.command(help="B4: which configurations contain SHA.")
    def b():
        pass

    @click.command(help="Plain words (H3).")
    def c():
        pass

    assert gen._summary(a) == "Every branch of a repo. (CR8)"
    assert gen._summary(b) == "Which configurations contain SHA. (B4)"
    assert gen._summary(c) == "Plain words (H3)."
