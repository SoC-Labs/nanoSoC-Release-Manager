"""The phase-gate check (A7; interfaces.md section 17.6, docs/phase-gates.yaml).

A later phase (2 to 5) starts only when a person has checked its written entry gate. This module checks
the tool repo against that rule:

1. each later phase has an entry gate with at least one criterion, and every criterion has a `check`;
2. a phase marked `gate-met` or `started` has `checked.at` and `checked.by`;
3. no implementation commit (one that changes `src/`) names a requirement of a later phase in its
   `Requirements:` trailer before that phase's `checked.at`. Only the trailer is read (CR-R3): a review label or
   an id in prose is not a claim that the commit implements it. Contracts, schemas, docs and tests for later
   phases are gate-2 work and do not count (interfaces.md section 15);
4. no later-phase route or CLI command exists before its gate is met (docs/acceptance/scope_web.md AT-A7).

Build and enable (CR-15, David 2026-10-12). The register's optional `build_allowed: {cr, from, phases}` says
which later phases (`phases`, default all four) may be BUILT before their gate, by commits made at or after
`from` (default: any time). It counts only while docs/CHANGES.md has a row for `cr`, as an exemption does. For
those phases and commits rules 3 and 4 become information (`built`: the commits and surfaces that exist), and
rule 5 holds for every later phase whose gate is not met:

5. nothing ENABLES a later phase's automation before its gate. Three views of the scheduler (`schedule.JOBS`,
   enabled jobs only; a job registered `enabled=False` runs nothing), merged at the Wave B integration:
   a. a job that declares a later `phase`, or whose name holds one of LATER_AUTOMATION[phase] as a word anywhere
      (CR-R8: not only as a prefix; `enabled_automation`, `scheduled_jobs`);
   b. a job whose code REACHES an automation's code (AUTOMATION_CODE: its package, its job kind, its enable flag;
      `automation_reached`), whatever the job is called; the names the gate governs are the register's per-phase
      `automation` list, default GOVERNED_AUTOMATION. Limit: an automation with no code entry is matched by
      name only, and a module name built at run time is not followed;
   c. no outbound write in `WRITE_CALLS` other than the one CI trigger (bot pull requests and tags are Phase 4
      and 5 automation).

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
from typing import Any, Iterable, Mapping, Sequence

from nanosoc_rm import clock, contracts
from nanosoc_rm.gitrun import run_git

__all__ = ["LATER_PHASES", "LATER_SURFACES", "LATER_AUTOMATION", "GOVERNED_AUTOMATION", "AUTOMATION_CODE",
           "LATER_SUBCOMMANDS", "IMPLEMENTATION_PREFIXES", "REGISTER", "REQUIREMENTS", "automation_phase",
           "requirement_phases", "requirement_index", "git_commits", "check", "exemptions", "build_allowed",
           "build_phases", "trailer_text", "gather", "check_repo", "app_routes", "cli_commands", "scheduled_jobs",
           "enabled_automation", "automation_reached", "automation_in_use", "outbound_writes", "find_repo"]

LATER_PHASES = ("Phase 2", "Phase 3", "Phase 4", "Phase 5")
# The surfaces each later phase brings (scope_web.md AT-A7, Then 4). Before its gate they exist only when the
# phase may be built (CR-15).
LATER_SURFACES: dict[str, dict[str, tuple[str, ...]]] = {
    "Phase 2": {"routes": ("/matrix",), "cli": ("matrix",)},
    "Phase 3": {"routes": (), "cli": ("release", "changelog", "version", "regen")},
    "Phase 4": {"routes": ("/bumps",), "cli": ("bump",)},
    "Phase 5": {"routes": ("/releases", "/trains", "/forks"), "cli": ("lock", "forks")},
}
# CR-15 rule 5a: the words that make a scheduler job a later phase's automation, wherever they stand in its name.
LATER_AUTOMATION: dict[str, tuple[str, ...]] = {
    "Phase 2": ("matrix", "flaky", "quarantine"),
    "Phase 3": ("changelog", "classify", "tool_release", "version", "regen"),
    "Phase 4": ("bump", "auto_merge", "revert"),
    "Phase 5": ("release", "lock", "forks", "train"),
}
# CR-15 rule 5b: the automation names each later phase's gate governs (the register's per-phase `automation`
# overrides them), and what each one's code is, so a job that runs it is found under any name.
GOVERNED_AUTOMATION: dict[str, tuple[str, ...]] = {
    "Phase 2": ("nightly_matrix",),
    "Phase 3": ("tool_release",),
    "Phase 4": ("bump_bot",),
    "Phase 5": ("soc_release",),
}
# `modules`: importing one (or a submodule) runs it; `job_kinds`: queuing that local job kind runs it (the job
# runner's own kind table, `kind_table`, names it without running it); `flag`: (module, attribute) whose
# `.enabled` must stay False. Phases 3 to 5 have no code entry yet: their names are matched by 5a only.
AUTOMATION_CODE: dict[str, dict[str, Any]] = {
    "nightly_matrix": {"modules": ("nanosoc_rm.matrix",), "job_kinds": ("matrix-cell",),
                       "kind_table": "nanosoc_rm.jobs", "flag": ("nanosoc_rm.matrix", "NIGHTLY")},
}
# CR-R8: a CLI group may be shared by two phases. A later phase can claim named subcommands of a group ("group sub");
# the bare group name then counts for its own phase only when it has a subcommand no other phase claims.
LATER_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "Phase 5": ("release draft", "release verify", "release list"),
}
# The one outbound write v1 makes (interfaces.md section 17.5); any other `write` entry is bot automation.
CI_TRIGGER = "trigger/pipeline"
_TRAILER = re.compile(r"^Requirements:[ \t]*(.*)$", re.M | re.I)
_EPOCH = "1970-01-01T00:00:00Z"
IMPLEMENTATION_PREFIXES = ("src/",)
REGISTER = Path("docs") / "phase-gates.yaml"
REQUIREMENTS = Path("docs") / "REQUIREMENTS.md"
CHANGES = Path("docs") / "CHANGES.md"
MET = ("gate-met", "started")
_HEADING = re.compile(r"^###\s+([A-Z]+\d+)\s+·\s+(Must|Should)\s+·\s+([^*\n]+?)\s*(?:\*.*)?$", re.M)


def automation_phase(name: str, declared: str | None = None) -> list[str]:
    """The later phases whose automation a scheduler job automates (rule 5a): its declared `phase` when that is a
    later one; else every phase one of whose LATER_AUTOMATION keywords stands as a word of the name (words split
    on anything but letters and digits, case ignored, a plural `s`/`es` allowed), so `nightly_matrix`,
    `auto-bump`, `versions_refresh` and `weekly_release_train` are seen, not only names that start with one."""
    if declared in LATER_PHASES:
        return [declared]
    low = (name or "").lower()
    out = []
    for ph, kws in LATER_AUTOMATION.items():
        for kw in kws:
            words = r"[^a-z0-9]+".join(map(re.escape, re.split(r"[^a-z0-9]+", kw)))
            if re.search(r"(?<![a-z0-9])%s(?:e?s)?(?![a-z0-9])" % words, low):
                out.append(ph)
                break
    return out


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


def trailer_text(message: str) -> str:
    """The values of a commit message's `Requirements:` trailer lines (CR-R3: the only place ids are read)."""
    return "\n".join(m.group(1) for m in _TRAILER.finditer(message or ""))


def _build_entry(register: dict | None, changes: str | None) -> dict | None:
    entry = (register or {}).get("build_allowed")
    if not isinstance(entry, dict):
        return None
    cr = str(entry.get("cr") or "")
    rows = [line for line in (changes or "").splitlines() if line.startswith("|") and line.count("|") > 2]
    if not cr or not any(r.split("|")[1].strip() == cr for r in rows):
        return None
    return entry


def build_allowed(register: dict | None, changes: str | None) -> str | None:
    """CR-15: the time from which later-phase capabilities may be built (`build_allowed.from`; the epoch when the
    entry names no time), or None: no entry, no CHANGES.md row whose CR column is its `cr`, or a bad time."""
    entry = _build_entry(register, changes)
    if entry is None:
        return None
    frm = str(entry.get("from") or _EPOCH)
    try:
        _parse_time(frm)
    except ValueError:
        return None
    return frm


def build_phases(register: dict | None, changes: str | None) -> set[str]:
    """CR-15: the later phases the register lets be BUILT before their gate (`build_allowed.phases`, default all
    four), while build_allowed() counts."""
    if build_allowed(register, changes) is None:
        return set()
    phases = _build_entry(register, changes).get("phases")
    return set(LATER_PHASES) if phases is None else {str(p) for p in phases or [] if p in LATER_PHASES}


def _enabled(automation: Any) -> list[tuple[str, str | None, str | None]]:
    """(name, declared phase or None, why or None) from `automation`: {name: why}, [name], [(name, phase)] or
    [(name, phase, why)]."""
    out = []
    for item in automation or ():
        if isinstance(item, (tuple, list)):
            out.append((str(item[0]), str(item[1]) if len(item) > 1 and item[1] else None,
                        str(item[2]) if len(item) > 2 and item[2] else None))
        else:
            why = automation.get(item) if isinstance(automation, Mapping) else None
            out.append((str(item), None, why))
    return out


def check(register: dict | None, phase_of: dict[str, str], commits: Iterable[dict], routes: Sequence[str],
          commands: Sequence[str], changes: str | None = None, automation: Any = None,
          scheduled: Sequence[str] = (), writes: Sequence[dict] = ()) -> list[dict]:
    """One result per later phase: {phase, status, met, checked_at, built, problems}. [] problems everywhere = A7
    holds. `changes` is docs/CHANGES.md: without it no exemption and no build_allowed entry counts.
    Rule 5 inputs: `automation`, what the scheduler enables ({name: why} from automation_in_use, [name], or
    [(name, phase)] from enabled_automation); `scheduled`, the enabled scheduler job names; `writes`, the
    WRITE_CALLS entries."""
    commits = [c for c in commits if any(f.startswith(IMPLEMENTATION_PREFIXES) for f in c.get("files", []))]
    exempt = exemptions(register, changes)
    built_from = build_allowed(register, changes)
    buildable = build_phases(register, changes)
    enabled = _enabled(automation)
    bot_writes = [w for w in writes if w.get("effect") == "write" and CI_TRIGGER not in str(w.get("target", ""))]
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
        built: list[str] = []
        building = ph in buildable and not met
        if ids_rx is not None and ph_ids:
            named = lambda c: set(ids_rx.findall(trailer_text(c["message"]))) - exempt.get(c["sha"], set())
            naming = [c for c in commits if named(c) & ph_ids]
            if building:
                late = [c for c in naming if _parse_time(c["at"]) >= _parse_time(built_from)]
                built += ["commit %s names %s" % (c["sha"][:7], sorted(named(c) & ph_ids)) for c in late]
                naming = [c for c in naming if c not in late]
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
            surfaces = []
            for r in LATER_SURFACES[ph]["routes"]:
                if any(x == r or x.startswith(r + "/") for x in routes):
                    surfaces.append("route %s" % r)
            claimed = {sc for p2, subs in LATER_SUBCOMMANDS.items() if p2 != ph for sc in subs}
            for c in LATER_SURFACES[ph]["cli"] + LATER_SUBCOMMANDS.get(ph, ()):
                if c not in commands:
                    continue
                subs = [x for x in commands if x.startswith(c + " ")]
                if " " not in c and subs and all(x in claimed for x in subs):
                    continue                          # the group holds only another phase's subcommands
                surfaces.append("command `%s`" % c)
            if building:
                built += ["%s built (CR-15)" % s for s in surfaces]
            else:
                problems += ["%s: %s exists before its entry gate" % (ph, s) for s in surfaces]
            # rule 5: enabling stays gated whether or not the phase may be built
            governed = (g or {}).get("automation")
            governed = governed if isinstance(governed, list) else GOVERNED_AUTOMATION[ph]
            for name, declared, why in enabled:
                if declared == ph or (declared is None and name in governed):
                    problems.append("%s: automation `%s` is enabled before its entry gate (CR-15: build, do not "
                                    "enable)%s" % (ph, name, ": %s" % why if why else ""))
            for j in scheduled:
                if ph in automation_phase(j):
                    problems.append("%s: scheduled job %s enables automation before its entry gate" % (ph, j))
            if ph in ("Phase 4", "Phase 5") and bot_writes:
                problems.append("%s: outbound write(s) %s enable bot automation before its entry gate" % (
                    ph, ", ".join("%s %s" % (w.get("method"), w.get("target")) for w in bot_writes)))
        out.append({"phase": ph, "status": (g or {}).get("status"), "met": met, "checked_at": checked_at,
                    "built": built, "problems": problems})
    return out


def app_routes() -> list[str]:
    """Every path in the web app's own route table, including the plug-in routers' routes (FastAPI keeps an
    included router as one entry without a path; its routes carry their full paths)."""
    from nanosoc_rm.web import create_app

    def walk(routes) -> list[str]:
        out = []
        for r in routes:
            if getattr(r, "path", None):
                out.append(r.path)
            sub = getattr(r, "original_router", None)
            if sub is not None:
                out += walk(sub.routes)
        return out
    return walk(create_app(None).routes)


def cli_commands() -> list[str]:
    """The top-level `nanosoc-rm` commands, and `group sub` for each subcommand of a group (CR-R8)."""
    from nanosoc_rm.cli import main
    out = list(main.commands)
    for name, cmd in main.commands.items():
        out += ["%s %s" % (name, sub) for sub in getattr(cmd, "commands", {}) or {}]
    return sorted(out)


def _jobs(jobs: Sequence[Any] | None, enabled_only: bool = True) -> list[Any]:
    if jobs is None:
        from nanosoc_rm.schedule import JOBS as jobs
    return [j for j in jobs if getattr(j, "enabled", True) or not enabled_only]


def scheduled_jobs(enabled_only: bool = True) -> list[str]:
    """The names of the jobs the scheduler runs (rule 5a). A job registered disabled (CR-15) is never run, so it
    enables nothing; `enabled_only=False` lists every registered job."""
    return [j.name for j in _jobs(None, enabled_only)]


def enabled_automation() -> list[tuple[str, str]]:
    """(name, phase) of every enabled job in the real scheduler (schedule.JOBS) that automates a later phase, by
    its declared phase or by its name (automation_phase). A job that ships disabled (`Job(..., enabled=False)`)
    names its gate and is not counted."""
    return [(j.name, ph) for j in _jobs(None) for ph in automation_phase(j.name, getattr(j, "phase", None))]


def outbound_writes() -> list[dict]:
    """The declared outbound calls (WRITE_CALLS, interfaces.md section 17.5)."""
    return list(contracts.WRITE_CALLS)


def _module_file(name: str) -> str | None:
    import importlib.util
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    return spec.origin if spec and spec.origin and spec.origin.endswith(".py") else None


def _read_module(path: str, name: str) -> tuple[set[str], set[str], set[str]]:
    """(the nanosoc_rm modules a source file imports, anywhere in it, with their parent packages; its string
    constants; the other top-level module names it imports). Reads `import x`, `from x import y` (y may be a module), relative imports, and `__import__("x")`
    / `importlib.import_module("x")` with a literal name."""
    return _read_source(Path(path).read_text(), path, name)


def _read_source(text: str, path: str, name: str) -> tuple[set[str], set[str], set[str]]:
    import ast
    tree = ast.parse(text, filename=path)
    pkg = name if path.endswith("__init__.py") else name.rpartition(".")[0]
    found: set[str] = set()
    strings: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".") if pkg else []
                parts = parts[:len(parts) - n.level + 1] if n.level > 1 else parts
                base = ".".join(parts + ([n.module] if n.module else []))
            found.add(base)
            found.update("%s.%s" % (base, a.name) for a in n.names)
        elif isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if fname in ("__import__", "import_module"):
                found.add(n.args[0].value)
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            strings.add(n.value)
    mods, other = set(), set()
    for m in found:
        if m == "nanosoc_rm" or m.startswith("nanosoc_rm."):
            parts = m.split(".")
            mods.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif m and "." not in m:
            other.add(m)                                           # followed only when it sits beside the job
    return mods, strings, other


def automation_reached(jobs: Sequence[Any] | None = None) -> dict[str, str]:
    """{automation name: why} for every AUTOMATION_CODE entry an enabled scheduler job reaches (default
    `schedule.JOBS`),
    whatever the job is called, or whose enable flag is on. A job reaches an automation when its defining module,
    or any module imported from there at any depth (nanosoc_rm modules, and helper modules in the job's own
    directory), imports one of the automation's modules or names one of its job kinds (outside the job runner's
    kind table). Read statically: nothing is run."""
    import functools
    import inspect
    jobs = _jobs(jobs)
    out: dict[str, str] = {}
    cache: dict[str, tuple[set[str], set[str], set[str]]] = {}

    def read(path: str, name: str) -> tuple[set[str], set[str], set[str]]:
        if path not in cache:
            cache[path] = _read_module(path, name)
        return cache[path]

    for job in jobs:
        fn = job.run
        while isinstance(fn, functools.partial):
            fn = fn.func
        fn = inspect.unwrap(fn)
        start = inspect.getsourcefile(fn)
        if not start:
            continue
        start_name = getattr(fn, "__module__", "") or ""
        home = Path(start).resolve().parent                        # a job's own helper modules sit beside it
        seen: dict[str, str] = {}                                  # module -> the module that imported it
        todo = [(start, start_name, None)]
        reached_strings: list[tuple[str, set[str]]] = []
        while todo:
            path, name, via = todo.pop()
            if name in seen and name != start_name:
                continue
            seen[name] = via or ""
            mods, strings, local = read(path, name)
            reached_strings.append((name, strings))
            for m in sorted(mods | {x for x in local if x not in seen}):
                if m not in seen:
                    f = _module_file(m)
                    if f and (m == "nanosoc_rm" or m.startswith("nanosoc_rm.") or Path(f).resolve().parent == home):
                        todo.append((f, m, name))
        for auto, code in AUTOMATION_CODE.items():
            if out.get(auto, "").startswith("scheduler job"):      # a job named outright; a module blame may
                continue                                           # still give way to the job that does it
            hit = [m for m in seen if any(m == p or m.startswith(p + ".") for p in code.get("modules", ()))]
            if hit:
                chain, cur = [hit[0]], hit[0]
                while seen.get(cur) and seen[cur] not in chain:
                    cur = seen[cur]
                    chain.append(cur)
                out[auto] = "scheduler %s reaches %s (%s)" % (_who(job, fn, start, start_name, code), hit[0],
                                                              " <- ".join(chain))
                continue
            own = tuple(code.get("modules", ())) + ((code["kind_table"],) if code.get("kind_table") else ())
            for mod, strings in reached_strings:
                kinds = sorted(set(code.get("job_kinds", ())) & strings)
                if kinds and not any(mod == p or mod.startswith(p + ".") for p in own):
                    out[auto] = "scheduler %s queues job kind %s (in %s)" % (
                        _who(job, fn, start, start_name, code), kinds[0], mod or "?")
                    break
    for auto, code in AUTOMATION_CODE.items():
        flag = code.get("flag")
        if flag and auto not in out and _module_file(flag[0]):
            import importlib
            obj = getattr(importlib.import_module(flag[0]), flag[1], None)
            if getattr(obj, "enabled", False):
                out[auto] = "%s.%s.enabled is True" % flag
    return out


def _who(job: Any, fn: Any, start: str, start_name: str, code: Mapping) -> str:
    """Which code to blame: the job itself when its own source names the automation, else its whole module (a
    helper defined beside it does; the module-level walk cannot tell which job calls the helper)."""
    import inspect
    import textwrap
    try:
        mods, strings, _ = _read_source(textwrap.dedent(inspect.getsource(fn)).strip().rstrip(","), start,
                                        start_name)
    except (OSError, TypeError, SyntaxError):
        mods, strings = set(), set()
    if any(m == p or m.startswith(p + ".") for m in mods for p in code.get("modules", ())) \
            or set(code.get("job_kinds", ())) & strings:
        return "job `%s`" % job.name
    return "module %s (which defines job `%s` and others)" % (start_name or start, job.name)


def automation_in_use(jobs: Sequence[Any] | None = None) -> dict[str, str]:
    """What the scheduler enables: {job name: "scheduler job"} for every job, plus every automation it reaches
    (automation_reached). The `automation` argument of `check`."""
    jobs = _jobs(jobs)
    out = {j.name: "scheduler job `%s`" % j.name for j in jobs}
    out.update(automation_reached(jobs))
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


def check_repo(repo: Path, routes: Sequence[str] | None = None, commands: Sequence[str] | None = None,
               automation: Any = None, scheduled: Sequence[str] | None = None,
               writes: Sequence[dict] | None = None) -> list[dict]:
    """check() over the repo, with every rule-5 view of the real scheduler unless given: what its enabled jobs
    reach (automation_in_use) and declare (5b), their names (scheduled_jobs, 5a), and WRITE_CALLS (5c)."""
    g = gather(repo)
    if automation is None:                       # 5b by reach and governed name, plus each declared later phase
        automation = [(n, None, why) for n, why in automation_in_use().items()] + [
            (j.name, j.phase, None) for j in _jobs(None) if getattr(j, "phase", None) in LATER_PHASES]
    return check(g["register"], g["phase_of"], g["commits"], app_routes() if routes is None else routes,
                 cli_commands() if commands is None else commands, changes=g["changes"], automation=automation,
                 scheduled=scheduled_jobs() if scheduled is None else scheduled,
                 writes=outbound_writes() if writes is None else writes)
