"""CLI: `nanosoc-rm test-bump` and `nanosoc-rm capabilities show|validate` (interfaces.md section 26)."""
from __future__ import annotations

from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup, table

__all__ = ["register"]

_EXIT = {"PASS": 0, "WARN": 0, "FAIL": 1, "UNVERIFIED": 2}


def _print(doc: dict) -> None:
    f = doc["fact"]
    click.echo("test-bump %s: %s %s -> %s (%s)  root %s  host %s" % (
        f["configuration"], f["path"], (f["from"] or "?")[:12], f["to"]["commit"][:12], f["to"]["resolved_as"],
        f["root_commit"][:12], f["host"] or "(none)"))
    if len(f["moved"]) > 1:
        click.echo("moves every path of %s together: %s" % (f["component"], ", ".join(f["moved"])))
    for m in f["manifests"]:
        st = sorted({(m.get(s) or {}).get("status") for s in ("now", "after")} - {None})
        if st != ["absent"]:
            click.echo("manifest %-30s %s" % (m["tree_path"], "/".join(st)))
    for p in f["problems"]:
        click.echo("problem: %s" % p)
    if f["plan_only"] or not f["rows"]:
        click.echo(table([{"capability": r["key"], "kind": r["kind"], "tier": r.get("tier") or "-", "gate": r["gate"],
                           "runtime": r["runtime"], "why": r["why"][:70]} for r in f["plan"]],
                         ("capability", "kind", "tier", "gate", "runtime", "why")) if f["plan"] else
                   "no capability covers this move")
    else:
        click.echo(table([{"capability": r["key"], "gate": r["gate"],
                           "now": (r["now"] or {}).get("verdict") or "-", "after": (r["after"] or {}).get("verdict") or "-",
                           "change": r["change"],
                           "detail": ((r["after"] or {}).get("reason") or "")[:80]} for r in f["rows"]],
                         ("capability", "gate", "now", "after", "change", "detail")))
    h = f.get("hygiene") or {}
    if h:
        click.echo("hygiene (T0, reported, never hides the rows): now %s, after %s" % (
            (h.get("now") or {}).get("verdict"), (h.get("after") or {}).get("verdict")))
    click.echo("functional verdict: %s%s" % (f["verdict"], "  -- " + f["reason"] if f.get("reason") else ""))


def register(main: click.Group) -> None:
    @main.command("test-bump")
    @click.argument("configuration")
    @click.argument("path")
    @click.argument("to")
    @click.option("--host", default=None, help="The executor host id whose site data (registry tiers.yaml hosts) "
                                               "builds the environment. Default: $NANOSOC_RM_EXECUTOR_HOST, then "
                                               "[executor] host.")
    @click.option("--plan", "plan_only", is_flag=True, help="Show what would run, run nothing.")
    @click.option("--queue", is_flag=True, help="Queue it as a job for an executor instead of running it here "
                                                "(what the GUI's Test this bump does).")
    @json_option
    def test_bump_cmd(configuration: str, path: str, to: str, host: str | None, plan_only: bool, queue: bool,
                      as_json: bool) -> int:
        """Test moving the pin at PATH of CONFIGURATION to TO (a tag, branch or commit of that component), Now
        against After, with the capabilities the trees declare in .nanosoc/capabilities.yaml. Every tree path of
        that component moves together. T0 runs on both sides and is reported as hygiene. Exit 0 PASS or WARN; 1
        FAIL (a gate went green to red); 2 UNVERIFIED (nothing covers the move, or something did not run); 3 usage."""
        from nanosoc_rm.capabilities.run import CannotTest, TestBumpUsage, test_bump
        from nanosoc_rm.config import load_config
        config = load_config()
        if queue:
            from nanosoc_rm import jobs
            import getpass
            try:
                job = jobs.submit(config, "test-bump", {"configuration": configuration, "path": path, "to": to},
                                  by=getpass.getuser() or "cli")
            except jobs.JobUsage as e:
                click.echo("nanosoc-rm: %s" % e, err=True)
                return EXIT_USAGE
            if as_json:
                emit([{"job": job["id"], "state": job["state"], "command": job["command"]}], True)
            else:
                click.echo("queued job %d for an executor: %s" % (job["id"], job["command"]))
            return EXIT_OK
        argv = "nanosoc-rm test-bump %s %s %s%s%s" % (configuration, path, to, " --host %s" % host if host else "",
                                                     " --plan" if plan_only else "")
        try:
            doc = test_bump(configuration, path, to, config=config, host=host, plan_only=plan_only, argv=argv)
        except TestBumpUsage as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE
        except CannotTest as e:
            click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
            return EXIT_UNVERIFIED
        if as_json:
            emit([doc], True)
        else:
            _print(doc)
        if plan_only:
            return EXIT_OK
        return _EXIT.get(doc["fact"]["verdict"], EXIT_UNVERIFIED)

    caps = subgroup(main, "capabilities", help="What each tree can test (.nanosoc/capabilities.yaml).")

    @caps.command("show")
    @click.argument("configuration")
    @json_option
    def show_cmd(configuration: str, as_json: bool) -> int:
        """Per tree of CONFIGURATION's latest stored walk: its capabilities file at the pinned commit, each
        capability, the hosts that can run it, and its last test-bump result. Reads only."""
        from nanosoc_rm.analysis._inputs import CannotAnswer
        from nanosoc_rm.capabilities.view import tree_view
        from nanosoc_rm.ci.configs import UnknownConfiguration, configuration as get_cfg
        from nanosoc_rm.config import load_config
        config = load_config()
        try:
            get_cfg(configuration, config)
            v = tree_view(config, configuration, fetch=True)
        except UnknownConfiguration as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE
        except CannotAnswer as e:
            click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
            return EXIT_UNVERIFIED
        if as_json:
            emit([v], True)
            return EXIT_OK
        click.echo("%s @ %s: %d of %d trees declare capabilities; hosts: %s" % (
            configuration, (v["root_commit"] or "?")[:12], v["declared"], v["total"], ", ".join(v["hosts"]) or "none"))
        rows = []
        for t in v["trees"]:
            if t["status"] != "ok":
                if t["status"] != "absent":
                    rows.append({"tree": t["tree_path"], "capability": "(%s)" % t["status"], "kind": "", "tier": "",
                                 "runs on": "", "last": "; ".join(t["problems"])[:60]})
                continue
            for c in t["capabilities"]:
                rows.append({"tree": t["tree_path"], "capability": c["id"], "kind": c["kind"], "tier": c["tier"] or "-",
                             "runs on": ", ".join(c["hosts"]) or "no host",
                             "last": (c["last"] or {}).get("verdict") or "-"})
        click.echo(table(rows, ("tree", "capability", "kind", "tier", "runs on", "last")) if rows else
                   "no tree of %s declares a capability yet" % configuration)
        for p in v["problems"]:
            click.echo("problem: %s" % p)
        return EXIT_OK

    @caps.command("validate")
    @click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def validate_cmd(file: Path) -> int:
        """Check a capabilities file while you write it (exit 1 with every problem). The release manager itself
        only ever reads the file at a commit, from its mirror."""
        from nanosoc_rm.capabilities.manifest import ManifestError, parse
        try:
            doc = parse(file.read_text(encoding="utf-8"), where=str(file))
        except ManifestError as e:
            for p in e.problems:
                click.echo(p)
            return 1
        click.echo("%s: valid; %d capabilit%s, %d composed path(s)" % (
            file, len(doc["capabilities"]), "y" if len(doc["capabilities"]) == 1 else "ies", len(doc["compose"])))
        return EXIT_OK
