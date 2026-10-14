"""Helpers every command shares (interfaces.md section 0.1, 0.2). Plug-ins import these from here, never from
nanosoc_rm.cli, which imports the plug-ins."""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Sequence

import click

__all__ = ["EXIT_OK", "EXIT_FINDING", "EXIT_UNVERIFIED", "EXIT_USAGE", "json_option", "emit", "table", "exit_code",
           "subgroup", "unregistered_configuration"]

EXIT_OK, EXIT_FINDING, EXIT_UNVERIFIED, EXIT_USAGE = 0, 1, 2, 3


def json_option(f: Callable) -> Callable:
    """The `--json` flag every command takes (section 0.1). The callback receives it as `as_json`."""
    return click.option("--json", "as_json", is_flag=True, help="Print one JSON document per line.")(f)


def _as_doc(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    return obj


def table(rows: Iterable[dict], columns: Sequence[str]) -> str:
    """A plain fixed-width text table of `rows` (dicts) in `columns` order."""
    rows = [{c: "" if r.get(c) is None else str(r.get(c)) for c in columns} for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in columns}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in columns).rstrip()]
    lines += ["  ".join(r[c].ljust(widths[c]) for c in columns).rstrip() for r in rows]
    return "\n".join(lines)


def _default_row(doc: dict) -> dict:
    return {"subject": doc.get("subject"), "observed_at": doc.get("observed_at")}


def emit(docs: Iterable[Any], as_json: bool, columns: Sequence[str] = ("subject", "observed_at"),
         row: Callable[[dict], dict] = _default_row) -> list[dict]:
    """Print documents: one compact JSON object per line with --json, else a table of row(doc) in `columns`.
    Returns the documents as dicts."""
    docs = [_as_doc(d) for d in docs]
    if as_json:
        for d in docs:
            click.echo(json.dumps(d, separators=(",", ":")))
    else:
        click.echo(table([row(d) for d in docs], columns))
    return docs


def exit_code(*, findings: bool = False, unverified: bool = False) -> int:
    """Section 0.2: a finding wins over could-not-answer, which wins over a clean answer."""
    return EXIT_FINDING if findings else EXIT_UNVERIFIED if unverified else EXIT_OK


def subgroup(parent: click.Group, name: str, help: str | None = None, **kwargs: Any) -> click.Group:
    """The command group `name` under `parent`, created on first use, so two modules can add to one group."""
    cmd = parent.commands.get(name)
    if cmd is None:
        cmd = click.Group(name=name, help=help, **kwargs)
        parent.add_command(cmd)
    elif not isinstance(cmd, click.Group):
        raise RuntimeError("%s is already a command, not a group" % name)
    elif help and not cmd.help:
        cmd.help = help
    return cmd


def unregistered_configuration(configuration: str | None, config: Any = None) -> str | None:
    """The usage message for a CONFIGURATION argument the registry does not know, else None (section 0.2: an
    unknown configuration is exit 3). None too when no name was given or the registry cannot be read: a listing
    command still answers from what is stored then. Callers ask only when their listing came back empty, so a
    retired configuration whose records are still stored keeps listing them."""
    if not configuration:
        return None
    from nanosoc_rm.ci import configs
    try:
        configs.configuration(configuration, config)
    except configs.UnknownConfiguration:
        return "no registered configuration %r" % configuration
    except Exception:                                             # noqa: BLE001 - registry unreadable: cannot say
        return None
    return None
