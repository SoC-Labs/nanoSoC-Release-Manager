"""Traceability (PR7) and the phase-gate check (A7): two read-only commands over the tool repo itself.

    nanosoc-rm trace [--repo PATH] [--requirement ID] [--json]
        requirement -> acceptance tests -> commits. Tests come from the `requirement("<ID>")` markers in tests/
        (read statically, nothing is imported or run); commits from the `Requirements:` trailer lines of the
        git log (PR7). Exit 1 when a v1 or All-phase Must requirement has no acceptance test, or a trailer names
        an ID that REQUIREMENTS.md does not have; else 0. With --requirement, only that requirement's row and
        findings count (exit 1 only when it has no acceptance test), and an ID REQUIREMENTS.md does not have is
        a usage error (exit 3).

    nanosoc-rm phase-gates [--repo PATH] [--json]
        The A7 check of nanosoc_rm.phasegates over docs/phase-gates.yaml, the git log, the web app's routes and
        this CLI's commands. Exit 1 when any later phase breaks its gate; else 0.

Both need a checkout of the tool repo (docs/, tests/ and .git). Without one they exit 3.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import click

from nanosoc_rm import phasegates
from nanosoc_rm.cliutil import EXIT_USAGE, emit, exit_code, json_option

__all__ = ["requirement_markers", "commit_trailers", "trace", "UnknownRequirement", "register"]

_TRAILER = re.compile(r"^Requirements:\s*(.+?)\s*$", re.M | re.I)
_ID_TOKEN = re.compile(r"[A-Z]+[0-9]+")


def requirement_markers(tests_dir: Path) -> dict[str, list[str]]:
    """ID -> test node ids ("<file>::<function>") carrying `@pytest.mark.requirement("<ID>")`."""
    out: dict[str, list[str]] = {}
    for py in sorted(tests_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError:
            continue
        rel = py.relative_to(tests_dir.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "requirement" \
                        and dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                    out.setdefault(dec.args[0].value, []).append("%s::%s" % (rel, node.name))
    return out


def commit_trailers(commits: list[dict]) -> list[dict]:
    """[{sha, at, subject, ids}] for every commit with a `Requirements:` trailer line."""
    out = []
    for c in commits:
        ids: list[str] = []
        for m in _TRAILER.finditer(c["message"]):
            ids += _ID_TOKEN.findall(m.group(1))
        if ids:
            out.append({"sha": c["sha"], "at": c["at"], "subject": c["message"].splitlines()[0] if c["message"] else "",
                        "ids": sorted(set(ids))})
    return out


class UnknownRequirement(LookupError):
    """`trace(only=...)` names an ID that REQUIREMENTS.md does not have."""


def trace(repo: Path, only: str | None = None) -> tuple[list[dict], list[str]]:
    """(one row per requirement {requirement, priority, phase, tests, commits}, problems). With `only`, the
    one requirement's row and its own problems; UnknownRequirement if REQUIREMENTS.md lacks it."""
    index = phasegates.requirement_index((repo / phasegates.REQUIREMENTS).read_text())
    if only is not None and only not in index:
        raise UnknownRequirement("%s is not a requirement in %s" % (only, phasegates.REQUIREMENTS))
    markers = requirement_markers(repo / "tests")
    trailers = commit_trailers(phasegates.git_commits(repo))
    rows, problems = [], []
    for rid, meta in index.items():
        if only is not None and rid != only:
            continue
        commits = [{"sha": t["sha"], "at": t["at"], "subject": t["subject"]} for t in trailers if rid in t["ids"]]
        rows.append({"requirement": rid, "priority": meta["priority"], "phase": meta["phase"],
                     "tests": sorted(markers.get(rid, [])), "commits": commits})
        if meta["priority"] == "Must" and meta["phase"] in ("v1", "All") and not markers.get(rid):
            problems.append("%s (Must, %s) has no acceptance test" % (rid, meta["phase"]))
    if only is not None:
        return rows, problems                    # the other findings concern other IDs
    for t in trailers:
        unknown = [i for i in t["ids"] if i not in index]
        if unknown:
            problems.append("commit %s names unknown requirement %s" % (t["sha"][:7], ", ".join(unknown)))
    for rid in sorted(set(markers) - set(index)):
        problems.append("tests mark unknown requirement %s" % rid)
    return rows, problems


def _repo(path: str | None) -> Path | None:
    return phasegates.find_repo(Path(path) if path else None)


def register(main: click.Group) -> None:
    @main.command("trace")
    @click.option("--repo", "repo_path", default=None, help="The tool repo checkout (default: this package's).")
    @click.option("--requirement", "only", default=None, help="Show one requirement.")
    @json_option
    def trace_cmd(repo_path: str | None, only: str | None, as_json: bool) -> int:
        """Requirement -> tests -> commits (PR7), from test markers and `Requirements:` trailers."""
        repo = _repo(repo_path)
        if repo is None:
            click.echo("nanosoc-rm: no tool repo checkout found (docs/phase-gates.yaml and .git); use --repo", err=True)
            return EXIT_USAGE
        try:
            rows, problems = trace(repo, only)
        except UnknownRequirement as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE

        def row(d: dict) -> dict[str, Any]:
            return {"requirement": d["requirement"], "priority": d["priority"], "phase": d["phase"],
                    "tests": len(d["tests"]), "commits": " ".join(c["sha"][:7] for c in d["commits"])}
        emit(rows, as_json, ("requirement", "priority", "phase", "tests", "commits"), row)
        for p in problems:
            click.echo("finding: %s" % p, err=True)
        return exit_code(findings=bool(problems))

    @main.command("phase-gates")
    @click.option("--repo", "repo_path", default=None, help="The tool repo checkout (default: this package's).")
    @json_option
    def phase_gates_cmd(repo_path: str | None, as_json: bool) -> int:
        """A7: every later phase has a checked entry gate before its code, routes or commands appear."""
        repo = _repo(repo_path)
        if repo is None:
            click.echo("nanosoc-rm: no tool repo checkout found (docs/phase-gates.yaml and .git); use --repo", err=True)
            return EXIT_USAGE
        results = phasegates.check_repo(repo)

        def row(d: dict) -> dict[str, Any]:
            return {"phase": d["phase"], "status": d["status"], "checked_at": d["checked_at"],
                    "problems": "; ".join(d["problems"]) or "none"}
        emit(results, as_json, ("phase", "status", "checked_at", "problems"), row)
        return exit_code(findings=any(d["problems"] for d in results))
