#!/usr/bin/env python3
"""Write docs/cli.md from the `nanosoc-rm` click tree: every command and subcommand, its --help verbatim.

    .venv/bin/python scripts/gen_cli_docs.py           # rewrite docs/cli.md
    .venv/bin/python scripts/gen_cli_docs.py --check   # exit 1 when docs/cli.md is out of date

tests/unit/test_cli_docs.py runs the --check comparison, so a command whose help changes without a regenerated
docs/cli.md fails the suite. The help is rendered at a fixed width (100 columns), independent of the terminal.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "cli.md"
WIDTH = 100

# A requirement id at the start of a summary line ("CR8: ...", "CR-11: ...", "B4: ...") is moved to the end of
# the line in the index table, so the table reads as plain words; the full help below keeps the text verbatim.
_LEADING_ID = re.compile(r"^((?:CR-?[\w-]+|[A-Z]{1,2}\d+(?:-[A-Z]?\d+)?)(?:\s*,\s*[A-Z]{1,2}\d+)*):\s+")

HEADER = """# Command-line reference

Generated from `nanosoc-rm --help` by `scripts/gen_cli_docs.py`; do not edit by hand. After changing a command's
help, run `.venv/bin/python scripts/gen_cli_docs.py` and commit the result (`tests/unit/test_cli_docs.py` fails
until you do).

Every command follows the same exit codes: 0 answered (PASS), 1 finding or FAIL, 2 could not answer (missing data,
unreadable host, UNVERIFIED), 3 usage or config error. Most take `--json` (one JSON document per line). The service
config is named by `$NANOSOC_RM_CONFIG`. Words such as tier, configuration and freshness are defined in
[concepts.md](concepts.md).
"""


def _summary(cmd: click.Command) -> str:
    text = cmd.get_short_help_str(limit=200)
    m = _LEADING_ID.match(text)
    if m:
        rest = text[m.end():]
        text = "%s (%s)" % (rest[:1].upper() + rest[1:], m.group(1))
    return text.replace("|", "\\|")


def _context(cmd: click.Command, name: str, parent: click.Context | None) -> click.Context:
    """The context `nanosoc-rm ... --help` would build (the command's own context_settings, `-h` inherited from the
    root), at the fixed width, so the text is the same whatever ran before in this process."""
    settings = dict(cmd.context_settings or {})
    settings.update(terminal_width=WIDTH, max_content_width=WIDTH)
    return click.Context(cmd, info_name=name, parent=parent, **settings)


def _walk(cmd: click.Command, ctx: click.Context, path: list[str]):
    yield path, cmd, ctx
    if isinstance(cmd, click.Group):
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is None or getattr(sub, "hidden", False):
                continue
            sub_ctx = _context(sub, name, ctx)
            yield from _walk(sub, sub_ctx, path + [name])


def _anchor(path: list[str]) -> str:
    return "nanosoc-rm-" + "-".join(path) if path else "nanosoc-rm"


def render() -> str:
    from nanosoc_rm.cli import main
    root_ctx = _context(main, "nanosoc-rm", None)
    entries = list(_walk(main, root_ctx, []))
    out = [HEADER, "## Commands\n", "| Command | What it does |", "|---|---|"]
    for path, cmd, _ in entries:
        if not path:
            continue
        name = "nanosoc-rm " + " ".join(path)
        indent = "&nbsp;&nbsp;" * (len(path) - 1)
        out.append("| %s[`%s`](#%s) | %s |" % (indent, name, _anchor(path), _summary(cmd)))
    out.append("")
    for path, cmd, ctx in entries:
        name = " ".join(["nanosoc-rm"] + path)
        out.append('<a id="%s"></a>\n' % _anchor(path))
        out.append("%s `%s`\n" % ("#" * min(2 + len(path), 4), name))
        out.append("```")
        out.append(cmd.get_help(ctx).rstrip())
        out.append("```\n")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/cli.md is out of date: run .venv/bin/python scripts/gen_cli_docs.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print("wrote %s (%d lines)" % (OUT.relative_to(ROOT), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
