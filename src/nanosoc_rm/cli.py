"""`nanosoc-rm`: the command-line front end (interfaces.md section 0; PR4).

Conventions every command keeps (section 0.1, 0.2):

- It calls one package function that returns contract documents; the command holds no logic of its own.
- It takes `--json` (`@json_option`) and then prints one JSON document per line (`emit`); without it, a table.
- It returns its exit code: 0 answered (no finding, PASS), 1 finding or FAIL, 2 could not answer (missing
  data, unreadable host, UNVERIFIED), 3 usage or config error. `main` maps click's usage errors and every
  ConfigError to 3, a registry that does not load (RegistryError) to 2 with its problems, and any other
  exception to 2 with its traceback on stderr: a crash is never read as a
  finding (1) or an answer (0).

The helpers (`json_option`, `emit`, `table`, `exit_code`, `subgroup`, the EXIT_* codes) live in
nanosoc_rm.cliutil, which plug-ins import; this module re-exports them.

Plug-in seam. cli.py never changes when a module adds commands. For each name in PLUGIN_MODULES, `main`
imports `nanosoc_rm.<name>.commands` and calls its `register(main)`; a plain module `nanosoc_rm/<name>.py`
may instead define `register(main)` itself. A module that does not exist yet is skipped. A module that exists
but fails to import is an error, never skipped. Two modules that share a command group (`t0`: the runner's
`t0 run`, and ci's `t0 trigger|fetch|runs|items|coverage`) both call `subgroup(main, "t0")`.

Rules for plug-ins: import heavy or CI-only code inside the command function, not at module level (the
service process imports every plug-in, and must never import `nanosoc_rm.t0.runner`, AT-A4); never register
a later phase's command before its entry gate (AT-A7: matrix, release, changelog, bump, lock, forks).
"""
from __future__ import annotations

import importlib
import sys
import traceback
from typing import Any, Iterable, Sequence

import click

from nanosoc_rm import __version__
from nanosoc_rm.cliutil import (EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, exit_code, json_option,
                                subgroup, table)
from nanosoc_rm.config import ConfigError

__all__ = ["main", "EXIT_OK", "EXIT_FINDING", "EXIT_UNVERIFIED", "EXIT_USAGE", "PLUGIN_MODULES", "json_option",
           "emit", "table", "exit_code", "subgroup", "load_plugins", "RMGroup"]

# interfaces.md section 0.5 modules that own commands, in --help order of registration. `db` is the
# foundation's own (`alerts`, section 8). Phase 5's `lock` is deliberately absent (A7).
PLUGIN_MODULES = ("db", "registry", "mirror", "walker", "analysis", "t0", "ci", "scan", "credentials", "web",
                  "schedule", "trace", "quality", "matrix")


class RMGroup(click.Group):
    """The top-level group: section 0.2 exit codes for every way a command can end."""

    def main(self, args: Sequence[str] | None = None, prog_name: str | None = None, complete_var: str | None = None,
             standalone_mode: bool = True, **extra: Any) -> Any:
        try:
            rv = super().main(args=args, prog_name=prog_name or "nanosoc-rm", complete_var=complete_var,
                              standalone_mode=False, **extra)
        except click.exceptions.UsageError as e:          # bad option, missing argument, unknown command
            e.show()
            code = EXIT_USAGE
        except ConfigError as e:
            click.echo("nanosoc-rm: config error: %s" % e, err=True)
            code = EXIT_USAGE
        except click.exceptions.Abort:
            click.echo("nanosoc-rm: aborted", err=True)
            code = EXIT_UNVERIFIED
        except click.ClickException as e:                 # e.g. an unreadable file argument
            e.show()
            code = EXIT_USAGE
        except Exception as e:
            from nanosoc_rm.registry import RegistryError
            if isinstance(e, RegistryError):              # the registry checkout does not load: missing data, not a bug
                click.echo("nanosoc-rm: the registry does not load (%d problem(s)); see `nanosoc-rm registry "
                           "validate`:" % len(e.problems), err=True)
                for p in e.problems:
                    click.echo("  %s" % p, err=True)
            else:                                         # a bug: say so, and do not pretend to an answer
                traceback.print_exc()
                click.echo("nanosoc-rm: internal error: the command could not answer", err=True)
            code = EXIT_UNVERIFIED
        else:
            code = rv if isinstance(rv, int) and not isinstance(rv, bool) else EXIT_OK
        if standalone_mode:
            sys.exit(code)
        return code


def load_plugins(group: click.Group, modules: Iterable[str] = PLUGIN_MODULES, package: str = "nanosoc_rm") -> list[str]:
    """Register the commands of every module in `modules` that exists (the seam in the module docstring).
    Returns the names registered."""
    loaded = []
    for name in modules:
        pkg, modname = "%s.%s" % (package, name), "%s.%s.commands" % (package, name)
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            if e.name == pkg:
                continue                                  # the module is not built yet
            if e.name != modname:
                raise                                     # a built module that cannot import: never hidden
            mod = importlib.import_module(pkg)            # a plain module, or a package without commands.py
            if not callable(getattr(mod, "register", None)):
                continue                                  # it has no commands
        register = getattr(mod, "register", None)
        if not callable(register):
            raise RuntimeError("%s has no register(main) function" % mod.__name__)
        register(group)
        loaded.append(name)
    return loaded


@click.group(cls=RMGroup, context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.version_option(__version__, prog_name="nanosoc-rm")
def main() -> None:
    """nanoSoC Release Manager. Service config: $NANOSOC_RM_CONFIG. Exit codes: 0 answered, 1 finding or
    FAIL, 2 could not answer, 3 usage or config error."""


load_plugins(main)
