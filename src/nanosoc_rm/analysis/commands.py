"""CLI front ends of the analysis functions (interfaces.md sections 5, 6, 6a, 7). No logic of their own.

    nanosoc-rm hosts [COMPONENT] [--json]                          1 any DIVERGED; 2 any UNKNOWN and none DIVERGED
    nanosoc-rm contains SHA [--component ID] [--config ID]... [--json]   0 answered; 2 any unknown or any configuration
                                                                   never walked; 3 no / several commits
    nanosoc-rm silicon-protection [CONFIG] [--uncovered] [--json]  1 any row uncovered; else 2 any pin not judged
    nanosoc-rm detect DEFECT DETECTOR --input PATH [--json]        0 clean; 1 fired; 2 could not run
    nanosoc-rm branches TARGET [--all] [--delete-list] [--stale-days N] [--sync] [--json]   (CR8)
                                                                   0 no deletable branch; 1 some; 2 no mirror
    nanosoc-rm compat CONFIG [--pin TREE_PATH=SHA]... [--tier t1|t2 [--force-tier]] [--keep-work DIR] [--store] [--json]
                                                                                  (CR-11; --tier: CR-12)
                                                                   0 PASS/WAIVED/WARN; 1 FAIL; 2 UNVERIFIED/INFRA

Every command: 3 for a usage problem (unknown component, configuration, defect or detector), 2 when the data
to answer is missing (no stored walk, no mirror, an invalid registry, a detector that could not run). When only
part of the data is missing, the rows that could be answered are printed and each gap is named on stderr.
"""
from __future__ import annotations

from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, exit_code, json_option


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


def _branch_row(d: dict) -> dict:
    f = d["fact"]
    return {"branch": f["branch"], "verdict": f["verdict"], "tip": f["tip"][:12], "ahead": f["ahead"], "behind": f["behind"],
            "last_commit": f["last_commit_at"][:10], "tags": " ".join(f["tags"]),
            "pins": " ".join(sorted({"%s:%s" % (p["configuration"], p["tree_path"]) for p in f["pins"]})),
            "cited": " ".join(sorted({"%s#%s" % (c["file"], c["pointer"]) for c in f["cited"]})),
            "deletable": "yes" if f["deletable"] else ""}


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

    @main.command("branches")
    @click.argument("target")
    @click.option("--all", "all_repos", is_flag=True, help="Every repo of the component (canonical and copies), not only the canonical.")
    @click.option("--delete-list", is_flag=True,
                  help="Print only the deletable branches (MERGED or STALE, no pin, no citation, no tag) as "
                       "`git push origin --delete <branch>` lines for a person to review. Never run by the tool.")
    @click.option("--stale-days", type=click.IntRange(0), default=None, show_default="90",
                  help="An unmerged, unheld branch whose last commit is older than this is STALE.")
    @click.option("--sync", "sync", is_flag=True, help="Fetch the component's mirror first (mirror sync --component).")
    @json_option
    def branches_cmd(target: str, all_repos: bool, delete_list: bool, stale_days: int | None, sync: bool,
                     as_json: bool) -> int:
        """CR8: every branch of a repo (a component id, or host:path) from its local mirror: ahead/behind the
        default branch, merged, tags on the tip, registered pins and registry citations reaching into it, and a
        verdict MERGED | PROTECTED | PINNED | STALE | ACTIVE. Exit 0 when nothing is deletable, 1 when
        something is, 2 when there is no mirror (use --sync) or a repo could not be judged."""
        from nanosoc_rm.analysis.branches import DEFAULT_STALE_DAYS, branches, delete_commands
        from nanosoc_rm.analysis.branches import delete_list as deletable_rows
        try:
            docs = branches(target, all_repos=all_repos, sync=sync,
                            stale_days=DEFAULT_STALE_DAYS if stale_days is None else stale_days)
        except AnalysisError as e:
            return _fail(e)
        dele = deletable_rows(docs)
        if delete_list:
            if as_json:
                emit(dele, True)
            else:
                for line in delete_commands(dele) or ["# nothing to delete: no MERGED or STALE branch without a pin, citation or tag"]:
                    click.echo(line)
        else:
            emit(docs, as_json, ("branch", "verdict", "tip", "ahead", "behind", "last_commit", "tags", "pins", "cited",
                                 "deletable"), _branch_row)
        gaps = _unanswered(docs)
        return exit_code(findings=bool(dele), unverified=gaps)

    @main.command("compat")
    @click.argument("configuration")
    @click.option("--pin", "pins", multiple=True, metavar="TREE_PATH=SHA",
                  help="Replace the gitlink at TREE_PATH with SHA (full 40 hex); the tree below it is walked from "
                       "SHA's own .gitmodules. Repeatable. A what-if: never stored.")
    @click.option("--keep-work", type=click.Path(path_type=Path), default=None,
                  help="Keep the local run's work directory (anonymous fetches, empty HOME/TMPDIR) here; it must "
                       "be new or empty. Default: a temporary directory, deleted afterwards.")
    @click.option("--store", is_flag=True, help="Store the walk and the compat_result (real runs only: not with --pin).")
    @click.option("--tier", type=click.Choice(["t1", "t2"]), default=None,
                  help="CR-12: after T0, run the registry tiers.yaml command of this tier in a sandbox checkout of "
                       "the resolved tree (budget enforced, killed at 1.5 x). Skipped when T0 is FAIL.")
    @click.option("--force-tier", is_flag=True,
                  help="Run --tier even when T0 is FAIL: a person's override, recorded; the result is UNVERIFIED "
                       "for release.")
    @json_option
    def compat_cmd(configuration: str, pins: tuple[str, ...], keep_work: Path | None, store: bool,
                   tier: str | None, force_tier: bool, as_json: bool) -> int:
        """CR-11: are these pins compatible? Resolve CONFIGURATION's tree (with each --pin override), then run the
        configuration T0 gates LOCALLY with their negative controls (anonymous; t0.runner.hardened NOT-RUN) and
        roll up with the configuration's T0 waivers. One line per pin and the run verdict. With --tier (CR-12) the
        tier command then runs in a sandbox checkout and joins the run verdict. Exit 0 PASS, WAIVED or WARN; 1 FAIL;
        2 UNVERIFIED or INFRA; 3 usage (unknown configuration, bad --pin, no pin at a tree path, no tier command,
        a tier that regenerates without NANOSOC_GEN_OK=1)."""
        from nanosoc_rm.analysis.compat import CompatUsage, compat, exit_for, parse_pins
        from nanosoc_rm.analysis.tierrun import TierUsage
        from nanosoc_rm.ci.configs import RegistryUnreadable
        from nanosoc_rm.ci.configs import UnknownConfiguration as UnknownCfg
        from nanosoc_rm.ci.trigger import TriggerError
        from nanosoc_rm.walker import OverrideError, UnknownConfiguration, WalkError
        argv = " ".join(["nanosoc-rm compat", configuration] + ["--pin %s" % p for p in pins]
                        + (["--tier", tier] if tier else []) + (["--force-tier"] if force_tier else []))
        try:
            doc = compat(configuration, parse_pins(pins), store=store, keep_work=keep_work, argv=argv, tier=tier,
                         force_tier=force_tier)
        except (CompatUsage, TierUsage, OverrideError, UnknownConfiguration, UnknownCfg) as e:
            click.echo("nanosoc-rm: %s" % (e.args[0] if e.args else e), err=True)
            return EXIT_USAGE
        except (WalkError, TriggerError, RegistryUnreadable) as e:
            click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
            return EXIT_UNVERIFIED
        if as_json:
            emit([doc], True)
        else:
            _print_compat(doc)
        f = doc["fact"]
        return exit_for(f.get("run_verdict") or f["verdict"])


def _print_compat(doc: dict) -> None:
    from nanosoc_rm.analysis.compat import GATE_COLUMNS
    from nanosoc_rm.cliutil import table
    f = doc["fact"]
    click.echo("compat %s @ %s  (local run, nonce %s)" % (f["configuration"], f["commit"][:12], f["nonce"][:8]))
    for o in f["overrides"]:
        click.echo("override %s: %s -> %s" % (o["tree_path"], (o["replaces"] or "?")[:12], o["commit"][:12]))
    cols = ("tree_path", "component", "commit", "override", "url", "state", "B7") + tuple(GATE_COLUMNS.values())
    rows = []
    for p in f["pins"]:
        r = {"tree_path": p["tree_path"] + ("" if len(p["tree_paths"]) == 1 else " (+%d)" % (len(p["tree_paths"]) - 1)),
             "component": p["component"] or "?", "commit": (p["commit"] or "-")[:12],
             "override": ("yes (was %s)" % (p["replaces"] or "?")[:7]) if p["overridden"] else "",
             "url": p["url_kind"], "state": p["state"] or "unjudged", "B7": "split" if p["split"] else ""}
        r.update({c: p["items"].get(c, "-") for c in GATE_COLUMNS.values()})
        rows.append(r)
    click.echo(table(rows, cols))
    for s in f["splits"]:
        click.echo("B7 split: %s pinned at %d commits: %s" % (s["component"], len({p["commit"] for p in s["pins"]}),
                                                              ", ".join("%s@%s" % (p["tree_path"], (p["commit"] or "-")[:7])
                                                                        for p in s["pins"])))
    click.echo(table([{"gate": g["id"], "verdict": g["verdict"] + (" (raw %s)" % g["raw_verdict"] if g.get("raw_verdict") else ""),
                       "control": g["control"], "detail": g["detail"][:110]} for g in f["gates"]],
                     ("gate", "verdict", "control", "detail")))
    secs = int((clock_parse(f["finished_at"]) - clock_parse(f["started_at"])).total_seconds())
    click.echo("%s: %s  (%d NOT-RUN, %d unused waiver(s), %d s%s)%s" % (
        "T0 verdict" if f.get("tier") else "run verdict", f["verdict"], f["not_run"], f["unused_waivers"], secs, ", over the T0 budget" if f["over_budget"] else "",
        ("  -- " + f["reason"]) if f.get("reason") else ""))
    t = f.get("tier")
    if t:
        click.echo("tier %s: %s  (%s s of %d s budget%s%s)  command: %s  [cwd %s]" % (
            t["tier"], t["verdict"], "-" if t["seconds"] is None else "%.1f" % t["seconds"], t["budget_s"],
            ", OVER BUDGET" if t["over_budget"] else "", ", KILLED" if t["killed"] else "", t["command"], t["cwd"]))
        if t["rows"]:
            click.echo(table([{"gate": r["gate"], "status": r["status"], "detail": r["detail"][:110]} for r in t["rows"]],
                             ("gate", "status", "detail")))
        if t["reason"]:
            click.echo("tier reason: %s" % t["reason"])
        if t["verdict"] not in ("PASS", "NOT-RUN") and t["log_tail"]:
            click.echo("log tail:\n  " + "\n  ".join(t["log_tail"][-8:]))
        click.echo("run verdict (T0 + %s): %s" % (t["tier"], f["run_verdict"]))
    if f.get("work_dir"):
        click.echo("work dir kept: %s" % f["work_dir"])


def clock_parse(t: str):
    from nanosoc_rm import clock
    return clock.parse(t)
