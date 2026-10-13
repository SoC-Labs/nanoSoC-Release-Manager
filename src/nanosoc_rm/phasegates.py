"""The phase-gate check (A7; interfaces.md section 17.6, docs/phase-gates.yaml).

A later phase (2 to 5) starts only when a person has checked its written entry gate. This module checks
the tool repo against that rule:

1. each later phase has an entry gate with at least one criterion, and every criterion has a `check`;
2. a phase marked `gate-met` or `started` has `checked.at` and `checked.by`;
3. no implementation commit (one that changes `src/`) names a requirement of a later phase, in its message or
   its `Requirements:` trailer, before that phase's `checked.at`. Contracts, schemas, docs and tests for later
   phases are gate-2 work and do not count (interfaces.md section 15);
4. no later-phase route or CLI command exists before its gate is met (docs/acceptance/scope_web.md AT-A7).

Build and enable (CR-15). From the register's `build_allowed.from` on, a later phase's capabilities may be BUILT
and used locally: an implementation commit may name its ids (rule 3) and its routes and commands may exist
(rule 4). What a phase's gate still governs is ENABLING automation (nightly CI matrix, bot pull requests,
published releases): rule 5, no automation of a later phase is enabled before its gate (`automation`: the
scheduled jobs that declare a later `phase` and are enabled). `build_allowed` counts only while docs/CHANGES.md
has a row for its `cr`, as an exemption does; without it rules 3 and 4 apply as written.

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

__all__ = ["LATER_PHASES", "LATER_SURFACES", "IMPLEMENTATION_PREFIXES", "REGISTER", "REQUIREMENTS",
           "requirement_phases", "requirement_index", "git_commits", "check", "exemptions", "build_allowed", "gather",
           "check_repo", "app_routes", "cli_commands", "enabled_automation", "automation_phase", "find_repo"]

LATER_PHASES = ("Phase 2", "Phase 3", "Phase 4", "Phase 5")
# The surfaces each later phase brings (scope_web.md AT-A7, Then 4). None may exist before its gate.
LATER_SURFACES: dict[str, dict[str, tuple[str, ...]]] = {
    "Phase 2": {"routes": ("/matrix",), "cli": ("matrix",)},
    "Phase 3": {"routes": (), "cli": ("release", "changelog", "version")},
    "Phase 4": {"routes": ("/bumps",), "cli": ("bump",)},
    "Phase 5": {"routes": ("/releases", "/trains", "/forks"), "cli": ("lock", "forks")},
}
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


def build_allowed(register: dict | None, changes: str | None) -> str | None:
    """CR-15: the time from which later-phase capabilities may be built (`build_allowed.from`), or None. Counts
    only while `changes` (docs/CHANGES.md) holds a table row whose CR column is `build_allowed.cr`."""
    b = (register or {}).get("build_allowed") or {}
    if not isinstance(b, dict):
        return None
    cr, frm = str(b.get("cr") or ""), b.get("from")
    if not cr or not frm:
        return None
    rows = [line for line in (changes or "").splitlines() if line.startswith("|") and line.count("|") > 2]
    if not any(r.split("|")[1].strip() == cr for r in rows):
        return None
    try:
        _parse_time(str(frm))
    except ValueError:
        return None
    return str(frm)


def check(register: dict | None, phase_of: dict[str, str], commits: Iterable[dict], routes: Sequence[str],
          commands: Sequence[str], changes: str | None = None,
          automation: Sequence[tuple[str, str]] | None = None) -> list[dict]:
    """One result per later phase: {phase, status, met, checked_at, problems}. [] problems everywhere = A7 holds.
    `changes` is docs/CHANGES.md: without it no exemption counts, and neither does CR-15's `build_allowed`.
    `automation`: (name, phase) of every enabled automation (rule 5)."""
    commits = [c for c in commits if any(f.startswith(IMPLEMENTATION_PREFIXES) for f in c.get("files", []))]
    exempt = exemptions(register, changes)
    built_from = build_allowed(register, changes)
    if built_from:                                   # CR-15: building a later phase is allowed from this time on
        commits = [c for c in commits if _parse_time(c["at"]) < _parse_time(built_from)]
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
            for name, phase in automation or ():
                if phase == ph:
                    problems.append("%s: automation %s is enabled before its entry gate" % (ph, name))
        if not met and not built_from:
            for r in LATER_SURFACES[ph]["routes"]:
                if any(x == r or x.startswith(r + "/") for x in routes):
                    problems.append("%s: route %s exists before its entry gate" % (ph, r))
            for c in LATER_SURFACES[ph]["cli"]:
                if c in commands:
                    problems.append("%s: command `%s` exists before its entry gate" % (ph, c))
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


def automation_phase(name: str, declared: str | None) -> str | None:
    """The later phase a scheduled job automates: its declared `phase` when that is a later one, else the phase
    whose CLI surface its name names (`bump_prs` -> Phase 4, `nightly_matrix` -> Phase 2), else None. A job that
    forgets to declare its phase is still caught by its name (review item 5)."""
    if declared in LATER_PHASES:
        return declared
    words = {w[:-1] if w.endswith("s") else w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}
    for phase in LATER_PHASES:
        if words & set(LATER_SURFACES.get(phase, {}).get("cli", ())):
            return phase
    return None


def enabled_automation() -> list[tuple[str, str]]:
    """(name, phase) of every job in the real scheduler (schedule.JOBS) that automates a later phase and is
    enabled (CR-15 rule 5). A job that ships disabled (`Job(..., phase=..., enabled=False)`) names its gate and is
    not counted; a job is a later-phase job by its declared phase or by its name (automation_phase)."""
    from nanosoc_rm import schedule
    out = []
    for j in getattr(schedule, "JOBS", ()):
        phase = automation_phase(j.name, getattr(j, "phase", None))
        if phase and getattr(j, "enabled", True):
            out.append((j.name, phase))
    return out


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


def check_repo(repo: Path, routes: Sequence[str] | None = None, commands: Sequence[str] | None = None) -> list[dict]:
    g = gather(repo)
    return check(g["register"], g["phase_of"], g["commits"], app_routes() if routes is None else routes,
                 cli_commands() if commands is None else commands, changes=g["changes"],
                 automation=enabled_automation())
