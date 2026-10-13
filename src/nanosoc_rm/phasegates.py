"""The phase-gate check (A7; interfaces.md section 17.6, docs/phase-gates.yaml).

A later phase (2 to 5) starts only when a person has checked its written entry gate. This module checks
the tool repo against that rule:

1. each later phase has an entry gate with at least one criterion, and every criterion has a `check`;
2. a phase marked `gate-met` or `started` has `checked.at` and `checked.by`;
3. no implementation commit (one that changes `src/`) made before CR-15 (BUILD_ALLOWED_FROM) names a requirement
   of a later phase, in its message or its `Requirements:` trailer, before that phase's `checked.at`. Contracts,
   schemas, docs and tests for later phases are gate-2 work and do not count (interfaces.md section 15);
4. no later-phase AUTOMATION is enabled before its gate is met: a name the register lists under the phase's
   `automation` (default LATER_AUTOMATION) must not be a job the scheduler runs (`schedule.JOBS`).

CR-15 (David, 2026-10-12): build and enable are separate. From BUILD_ALLOWED_FROM on, a later phase's capabilities
(code, routes, CLI commands) may be built and used locally; what the entry gate still governs is ENABLING its
automation (the nightly matrix, bot pull requests, published releases). Commits before that date are judged by
the old rule, so the history check is unchanged for them. LATER_SURFACES is kept as the record of which routes and
commands belong to which phase; their existence is no longer a breach.

Exemptions (CR-5): the register's `exemptions` list names commits that named a later-phase id only because the
contract text told them to, before a CR corrected it. An entry `{commit, ids, cr}` takes exactly its `ids` out of
that one commit, and only while docs/CHANGES.md has a row for `cr` that names the commit (its first 7 characters).
History is never rewritten to hide such a commit; the exception stays visible in two places.

`check(...)` is pure; `gather(repo)` reads the register, the requirement-to-phase map and the git log, and
`check_repo(repo)` does both. The CLI front end is `nanosoc-rm phase-gates` (nanosoc_rm.trace).
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from nanosoc_rm import clock, contracts
from nanosoc_rm.gitrun import run_git

__all__ = ["LATER_PHASES", "LATER_SURFACES", "LATER_AUTOMATION", "BUILD_ALLOWED_FROM", "IMPLEMENTATION_PREFIXES", "REGISTER", "REQUIREMENTS",
           "requirement_phases", "requirement_index", "git_commits", "check", "exemptions", "gather", "check_repo",
           "app_routes", "cli_commands", "scheduled_jobs", "find_repo"]

LATER_PHASES = ("Phase 2", "Phase 3", "Phase 4", "Phase 5")
# The surfaces each later phase brings (scope_web.md AT-A7, Then 4). None may exist before its gate.
LATER_SURFACES: dict[str, dict[str, tuple[str, ...]]] = {
    "Phase 2": {"routes": ("/matrix",), "cli": ("matrix",)},
    "Phase 3": {"routes": (), "cli": ("release", "changelog")},
    "Phase 4": {"routes": ("/bumps",), "cli": ("bump",)},
    "Phase 5": {"routes": ("/releases", "/trains", "/forks"), "cli": ("lock", "forks")},
}
# CR-15: what each later phase's entry gate still governs (the register's per-phase `automation` overrides it).
LATER_AUTOMATION: dict[str, tuple[str, ...]] = {
    "Phase 2": ("nightly_matrix",),
    "Phase 3": ("tool_release",),
    "Phase 4": ("bump_bot",),
    "Phase 5": ("soc_release",),
}
BUILD_ALLOWED_FROM = "2026-10-12T00:00:00Z"            # CR-15 agreed: building a later phase is not starting it
IMPLEMENTATION_PREFIXES = ("src/",)
REGISTER = Path("docs") / "phase-gates.yaml"
REQUIREMENTS = Path("docs") / "REQUIREMENTS.md"
CHANGES = Path("docs") / "CHANGES.md"
MET = ("gate-met", "started")
_HEADING = re.compile(r"^###\s+([A-Z]+\d+)\s+·\s+(Must|Should)\s+·\s+([^*\n]+?)\s*(?:\*.*)?$", re.M)


def requirement_index(text: str) -> dict[str, dict[str, str]]:
    """ID -> {priority, phase} from the REQUIREMENTS.md headings (`### A7 · Must · All`)."""
    return {m.group(1): {"priority": m.group(2), "phase": m.group(3).strip()} for m in _HEADING.finditer(text)}


def requirement_phases(text: str) -> dict[str, str]:
    """ID -> phase ('v1', 'All', 'Process', 'Phase 2', ...)."""
    return {k: v["phase"] for k, v in requirement_index(text).items()}


def git_commits(repo: Path) -> list[dict]:
    """Every commit reachable from HEAD: sha, commit time (ISO), message, changed files."""
    r = run_git("log", "--format=%x1e%H%x1f%cI%x1f%B%x1f", "--name-only", cwd=repo, timeout=120).check()
    out = []
    for rec in r.stdout.split("\x1e"):
        if not rec.strip():
            continue
        sha, when, msg, files = rec.split("\x1f", 3)
        out.append({"sha": sha, "at": when, "message": msg, "files": [f for f in files.split("\n") if f.strip()]})
    return out


def _parse_time(s: str) -> dt.datetime:
    return clock.parse(s)


def exemptions(register: dict | None, changes: str | None) -> dict[str, set[str]]:
    """Full commit sha -> the requirement ids its CR-recorded exemption removes (CR-5). An entry counts only when
    `changes` (docs/CHANGES.md) holds a table row that starts with its `cr` and names the commit's short sha."""
    out: dict[str, set[str]] = {}
    rows = [line for line in (changes or "").splitlines() if line.startswith("|")]
    for e in (register or {}).get("exemptions") or []:
        if not isinstance(e, dict):
            continue
        sha, cr, ids = str(e.get("commit") or ""), str(e.get("cr") or ""), e.get("ids") or []
        if len(sha) != 40 or not cr or not ids:
            continue
        if any(r.split("|")[1].strip() == cr and sha[:7] in r for r in rows if r.count("|") > 2):
            out.setdefault(sha, set()).update(map(str, ids))
    return out


def check(register: dict | None, phase_of: dict[str, str], commits: Iterable[dict], routes: Sequence[str],
          commands: Sequence[str], changes: str | None = None, automation: Sequence[str] = ()) -> list[dict]:
    """One result per later phase: {phase, status, met, checked_at, problems}. [] problems everywhere = A7 holds.
    `changes` is docs/CHANGES.md: without it no exemption counts. `automation`: the names of the jobs the
    scheduler runs (CR-15: an enabled later-phase automation is the breach; routes and commands are not)."""
    built_from = _parse_time(BUILD_ALLOWED_FROM)
    commits = [c for c in commits if any(f.startswith(IMPLEMENTATION_PREFIXES) for f in c.get("files", []))
               and _parse_time(c["at"]) < built_from]
    exempt = exemptions(register, changes)
    phases = {g.get("phase"): g for g in (register or {}).get("phases", []) if isinstance(g, dict)}
    ids = sorted(phase_of, key=len, reverse=True)
    # A requirement id stands alone: `E1`, `(E1)`, `E1,`. A review label such as `CI-E1` or `MW-R1` contains an id
    # after a hyphen and names a review finding, not the requirement (found at integration: build/ci's message).
    ids_rx = re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(map(re.escape, ids))) if ids else None
    out = []
    for ph in LATER_PHASES:
        problems: list[str] = []
        g = phases.get(ph)
        met, checked_at = False, None
        if not g:
            problems.append("%s: no entry gate written down" % ph)
        else:
            crit = g.get("entry_gate") or []
            if not crit:
                problems.append("%s: the entry gate has no criteria" % ph)
            for c in crit:
                if not isinstance(c, dict) or not c.get("criterion") or not c.get("check"):
                    problems.append("%s: criterion %r has no check" % (ph, (c or {}).get("criterion")))
            met = g.get("status") in MET
            checked = g.get("checked") or {}
            checked_at = checked.get("at")
            if met and not (checked_at and checked.get("by")):
                problems.append("%s: marked %s without checked.at and checked.by" % (ph, g.get("status")))
        ph_ids = {i for i, p in phase_of.items() if p == ph}
        if ids_rx is not None and ph_ids:
            named = lambda c: set(ids_rx.findall(c["message"])) - exempt.get(c["sha"], set())
            naming = [c for c in commits if named(c) & ph_ids]
            if naming:
                first = min(naming, key=lambda c: _parse_time(c["at"]))
                try:
                    early = not met or not checked_at or _parse_time(first["at"]) < _parse_time(str(checked_at))
                except ValueError:
                    early = True
                if early:
                    problems.append("%s: implementation commit %s (%s) names %s before the entry gate was checked"
                                    % (ph, first["sha"][:7], first["at"],
                                       sorted(named(first) & ph_ids)))
        if not met:
            governed = (g or {}).get("automation")
            for name in (governed if isinstance(governed, list) else LATER_AUTOMATION[ph]):
                if name in automation:
                    problems.append("%s: automation `%s` is enabled before its entry gate (CR-15: build, do not "
                                    "enable)" % (ph, name))
        out.append({"phase": ph, "status": (g or {}).get("status"), "met": met, "checked_at": checked_at,
                    "problems": problems})
    return out


def app_routes() -> list[str]:
    """Every path in the web app's own route table."""
    from nanosoc_rm.web import create_app
    return [getattr(r, "path", "") for r in create_app(None).routes if getattr(r, "path", None)]


def cli_commands() -> list[str]:
    """The top-level `nanosoc-rm` commands."""
    from nanosoc_rm.cli import main
    return sorted(main.commands)


def scheduled_jobs() -> list[str]:
    """The names of every job the scheduler can run (`schedule.JOBS`): what is enabled."""
    from nanosoc_rm.schedule import JOBS
    return [j.name for j in JOBS]


def find_repo(start: Path | None = None) -> Path | None:
    """The tool repo: `start` if given (and None if it is not one); else the checkout this package runs from
    (editable install), else the current directory: the first that holds docs/phase-gates.yaml and a .git."""
    here = Path(__file__).resolve()
    for cand in [start] if start else [here.parents[2], Path.cwd()]:
        if cand and (Path(cand) / REGISTER).is_file() and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def gather(repo: Path) -> dict[str, Any]:
    reg = repo / REGISTER
    register = contracts.load_yaml(reg.read_text()) if reg.is_file() else None
    phase_of = requirement_phases((repo / REQUIREMENTS).read_text())
    changes = (repo / CHANGES).read_text() if (repo / CHANGES).is_file() else None
    return {"register": register, "phase_of": phase_of, "commits": git_commits(repo), "changes": changes}


def check_repo(repo: Path, routes: Sequence[str] | None = None, commands: Sequence[str] | None = None,
               automation: Sequence[str] | None = None) -> list[dict]:
    g = gather(repo)
    return check(g["register"], g["phase_of"], g["commits"], app_routes() if routes is None else routes,
                 cli_commands() if commands is None else commands, changes=g["changes"],
                 automation=scheduled_jobs() if automation is None else automation)
