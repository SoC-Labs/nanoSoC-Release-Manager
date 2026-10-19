"""Tier cells of the matrix (D3 with T1, interfaces.md section 18 item 8).

    TIERS = ("t1",)                                     the tiers a matrix cell may add to its T0 run
    parse_tiers(text) -> [tier]                         MatrixUsage-style ValueError for anything else
    preflight(configuration, tier, *, config, environ=None) -> Ready

A matrix column set may include tier t1 (lint): the cell then runs `analysis.compat(..., tier="t1")`, the local T0
run followed by the registry's tiers.yaml t1 command in a sandbox checkout of the same tree (section 9a "Tiers").
Before anything runs, `preflight` reads the tier's spec and this host:

- no t1 command for the configuration in tiers.yaml, or one the tool refuses to run (`tierrun.check_spec`: a
  set_env.sh, `regenerates` without NANOSOC_GEN_OK=1): NOT-RUN with the reason. That is a fact about the
  registry, not about the host, so it is not INFRA and it counts neither way;
- a tool the spec names that this host does not have: INFRA, reason "tool not on this host: ...", and nothing
  is run (no T0 either). A tool is an absolute path in the tier's own `env` (`tierrun.missing_tools`, the rule the
  tier runner applies itself) or the command's program (argv[0], or a shell string's first word) when it is not
  found on the tier's clean PATH;
- otherwise ready.

The view runs the same preflight (it reads one YAML file and stats a few paths: no work, no job), so a cell that
cannot run on this host says so before anyone presses Run, and Run does not queue it.
"""
from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

__all__ = ["TIERS", "parse_tiers", "preflight", "Ready", "NO_TOOL", "NO_COMMAND", "spec_of"]

TIERS = ("t1",)
NO_TOOL = "tool not on this host"
NO_COMMAND = "no tier command"


@dataclass(frozen=True)
class Ready:
    tier: str
    ready: bool
    verdict: str | None          # None when ready; NOT-RUN (no or refused command) or INFRA (tool not on host)
    reason: str | None
    command: str | None
    budget_s: int | None = None

    def as_dict(self) -> dict:
        return {"tier": self.tier, "ready": self.ready, "verdict": self.verdict, "reason": self.reason,
                "command": self.command, "budget_s": self.budget_s}


def parse_tiers(text: str | Sequence[str] | None) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in (text.split(",") if isinstance(text, str) else text) if p and p.strip()]
    bad = [p for p in parts if p not in TIERS]
    if bad:
        raise ValueError("unknown tier(s) %s (a matrix cell may add: %s; T0 always runs)" % (
            ", ".join(bad), ", ".join(TIERS)))
    return [t for t in TIERS if t in parts]


def spec_of(configuration: str, tier: str, config) -> dict | None:
    from nanosoc_rm.ci import configs
    return configs.tiers(configuration, config).get(tier)


def _program(command) -> str | None:
    """The program a command starts: argv[0], or a shell string's first word when it is a plain word."""
    if isinstance(command, (list, tuple)):
        return command[0] if command else None
    try:
        words = shlex.split(command or "")
    except ValueError:
        return None
    if not words:
        return None
    w = words[0]
    if "=" in w or any(c in w for c in "$`(){}<>|;&*?[]") or w in _SHELL_WORDS:
        return None
    return w


# bash builtins and keywords a shell-string tier may start with: not programs, never "missing"
_SHELL_WORDS = frozenset(": . ! [ [[ { source alias builtin case cd command declare echo eval exec exit export false "
                         "for function if let local popd printf pushd read set shift test time trap true type ulimit "
                         "umask unset until wait while".split())


def preflight(configuration: str, tier: str, *, config, environ: Mapping[str, str] | None = None) -> Ready:
    """Can `tier` run for `configuration` on this host? (module docstring)."""
    from nanosoc_rm.analysis import tierrun
    if tier not in TIERS:
        raise ValueError("unknown tier %r (a matrix cell may add: %s)" % (tier, ", ".join(TIERS)))
    spec = spec_of(configuration, tier, config)
    if not spec:
        return Ready(tier, False, "NOT-RUN", "%s: the registry's tiers.yaml declares no %s command for %s"
                     % (NO_COMMAND, tier, configuration), None)
    cmd = tierrun.command_text(spec.get("command") or "?")
    budget = int(spec.get("budget_s") or 0) or None
    try:
        spec = tierrun.check_spec(spec, tier, configuration, environ)
    except tierrun.TierUsage as e:
        return Ready(tier, False, "NOT-RUN", "the %s command is refused: %s" % (tier, e), cmd, budget)
    with tempfile.TemporaryDirectory(prefix="nanosoc-rm-preflight-") as d:
        tree, work = Path(d) / "tree", Path(d) / "work"
        tree.mkdir()
        work.mkdir()
        try:
            env = tierrun._env(spec, tier, tree, work)
        except tierrun.TierUsage as e:
            return Ready(tier, False, "NOT-RUN", "the %s command is refused: %s" % (tier, e), cmd, budget)
        absent = tierrun.missing_tools(spec, env, tree, work)
        prog = _program(spec.get("command"))
        if prog and "/" not in prog and shutil.which(prog, path=env.get("PATH", os.defpath)) is None:
            absent.append("%s is not on the tier's PATH (%s)" % (prog, env.get("PATH", "")))
        elif prog and prog.startswith("/") and not os.access(prog, os.X_OK):
            absent.append("%s does not exist or is not executable" % prog)
    if absent:
        return Ready(tier, False, "INFRA", "%s: %s; the %s tier was not run" % (NO_TOOL, "; ".join(absent), tier),
                     cmd, budget)
    return Ready(tier, True, None, None, cmd, budget)
