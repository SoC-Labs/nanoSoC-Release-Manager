"""CLI front ends of the analysis functions (interfaces.md sections 5, 6, 6a, 7). No logic of their own.

    nanosoc-rm hosts [COMPONENT] [--json]                          1 any DIVERGED; 2 any UNKNOWN and none DIVERGED
    nanosoc-rm contains SHA [--component ID] [--config ID]... [--json]   0 answered; 2 any unknown or any configuration
                                                                   never walked; 3 no / several commits
    nanosoc-rm silicon-protection [CONFIG] [--uncovered] [--json]  1 any row uncovered; else 2 any pin not judged
    nanosoc-rm detect DEFECT DETECTOR --input PATH [--json]        0 clean; 1 fired; 2 could not run

Every command: 3 for a usage problem (unknown component, configuration, defect or detector), 2 when the data
to answer is missing (no stored walk, no mirror, an invalid registry, a detector that could not run). When only
part of the data is missing, the rows that could be answered are printed and each gap is named on stderr.
"""
from __future__ import annotations

from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, emit, exit_code, json_option


def _fail(e: Exception) -> int:
    click.echo("nanosoc-rm: %s" % e, err=True)
    return getattr(e, "exit_code", EXIT_UNVERIFIED)


def _unanswered(docs) -> bool:
    """Name on stderr each part of the question the data could not answer; True when there was one."""
    gaps = list(getattr(docs, "unanswered", []))
    for gap in gaps:
        click.echo("nanosoc-rm: could not answer: %s" % gap, err=True)
    return bool(gaps)


def _repo(side: dict) -> str:
    return "%s:%s@%s" % (side["repo"]["host"], side["repo"]["path"], side["default_branch"] or "?")


def _hosts_row(d: dict) -> dict:
    f = d["fact"]
    return {"component": f["component"], "canonical": _repo(f["canonical"]), "copy": _repo(f["copy"]),
            "relation": f["relation"], "canonical_only": f["canonical_only"], "copy_only": f["copy_only"],
            "branch_names_differ": "yes" if f["default_branch_mismatch"] else ""}


def _contains_row(d: dict) -> dict:
    f = d["fact"]
    pins = ", ".join("%s %s=%s" % (p["tree_path"], p["commit"][:7], {True: "yes", False: "no", None: "?"}[p["contains"]])
                     for p in f["pins"])
    return {"configuration": f["configuration"], "host": f["host"], "answer": f["answer"], "pins": pins}


def _protect_row(d: dict) -> dict:
    f = d["fact"]
    role = "+".join(r for r in ("pinned", "canonical") if f[r])
    return {"configuration": f["configuration"], "component": f["component"], "commit": f["commit"][:12],
            "host": f["repo"]["host"], "role": role, "state": f["state"], "covered": "yes" if f["covered"] else "NO",
            "covering_tags": " ".join(f["covering_tags"]), "tree_paths": " ".join(f["tree_paths"])}


def register(main: click.Group) -> None:
    from nanosoc_rm.analysis import AnalysisError

    @main.command("hosts")
    @click.argument("component", required=False)
    @json_option
    def hosts_cmd(component: str | None, as_json: bool) -> int:
        """B6: compare each component's copies with its canonical repo (rev-list in one mirror). Exit 1 if any
        relation is DIVERGED; 2 if any is UNKNOWN and none DIVERGED."""
        from nanosoc_rm.analysis.hosts import compare_hosts, components_with_copies
        try:
            ids = [component] if component else components_with_copies()
            docs = [d for cid in ids for d in compare_hosts(cid)]
        except AnalysisError as e:
            return _fail(e)
        emit(docs, as_json, ("component", "canonical", "copy", "relation", "canonical_only", "copy_only",
                             "branch_names_differ"), _hosts_row)
        relations = {d["fact"]["relation"] for d in docs}
        return exit_code(findings="DIVERGED" in relations, unverified="UNKNOWN" in relations)

    @main.command("contains")
    @click.argument("sha")
    @click.option("--component", help="Component whose mirror holds the commit (default: search every mirror).")
    @click.option("--config", "configurations", multiple=True, help="Configuration id (repeatable; default: all).")
    @json_option
    def contains_cmd(sha: str, component: str | None, configurations: tuple[str, ...], as_json: bool) -> int:
        """B4: which configurations contain commit SHA, per host. Exit 0 answered (including `no`), 2 if any
        answer is unknown, 3 if SHA resolves to no commit or to several."""
        from nanosoc_rm.analysis.contains import contains
        try:
            docs = contains(sha, component=component, configurations=list(configurations) or None)
        except AnalysisError as e:
            return _fail(e)
        emit(docs, as_json, ("configuration", "host", "answer", "pins"), _contains_row)
        gaps = _unanswered(docs)
        return exit_code(unverified=gaps or any(d["fact"]["answer"] == "unknown" for d in docs))

    @main.command("silicon-protection")
    @click.argument("configuration", required=False)
    @click.option("--uncovered", is_flag=True, help="Print only rows no tag on their host covers.")
    @json_option
    def protect_cmd(configuration: str | None, uncovered: bool, as_json: bool) -> int:
        """B5: commits of frozen or silicon trees that their default branch cannot reach, on the pinned host and
        the canonical host, and whether a tag there covers each. Exit 1 if any row is uncovered."""
        from nanosoc_rm.analysis.protect import silicon_protection
        try:
            docs = silicon_protection(configuration)
        except AnalysisError as e:
            return _fail(e)
        shown = [d for d in docs if not d["fact"]["covered"]] if uncovered else docs
        emit(shown, as_json, ("configuration", "component", "commit", "host", "role", "state", "covered",
                              "covering_tags", "tree_paths"), _protect_row)
        gaps = _unanswered(docs)
        return exit_code(findings=any(not d["fact"]["covered"] for d in docs), unverified=gaps)

    @main.command("detect")
    @click.argument("defect")
    @click.argument("detector")
    @click.option("--input", "input_path", required=True, type=click.Path(path_type=Path),
                  help="Built output: a generated-RTL archive or directory, or a netlist file.")
    @json_option
    def detect_cmd(defect: str, detector: str, input_path: Path, as_json: bool) -> int:
        """F2: run DETECTOR of DEFECT on built output and print the run for the erratum's exposure.run.
        Exit 0 clean, 1 fired, 2 could not run. Never run by the service (A4)."""
        from nanosoc_rm.analysis.detect import run
        try:
            result = run(defect, detector, input_path)
        except AnalysisError as e:
            return _fail(e)
        if result.log:
            click.echo(result.log, err=True)
        doc = result.as_doc()
        emit([doc], as_json, ("result", "forms", "md5", "detector_source", "command"),
             lambda d: {"result": d["result"], "forms": ",".join(d["forms"]), "md5": d["input"]["md5"],
                        "detector_source": ("%s@%s" % (d["detector_source"]["component"],
                                                       d["detector_source"]["commit"][:12])
                                            if d["detector_source"] else "pattern in the record"),
                        "command": d["command"]})
        return EXIT_FINDING if doc["result"] == "fired" else EXIT_OK
