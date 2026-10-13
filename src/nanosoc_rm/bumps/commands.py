"""`nanosoc-rm bumps`: bump proposals in shadow mode (E1-E7; interfaces.md section 18). No logic of its own.

    nanosoc-rm bumps [CONFIG] [--store] [--json]       scan now (every registered configuration without CONFIG)
    nanosoc-rm bumps queue [CONFIG] [--json]           the kept proposals (from the last `--store`)
    nanosoc-rm bumps show ID [--json]                  one kept proposal
    nanosoc-rm bumps patch ID [--out FILE]             its patch, for a person to `git apply --index`
    nanosoc-rm bumps mark ID done|declined --by LOGIN [--note TEXT]
    nanosoc-rm bumps ledger [--json]                   E2: agreement with what people did
    nanosoc-rm bumps schedule [--json]                 the schedule as shipped: disabled, naming the Phase 4 gate

The tool never opens a pull request, pushes or writes a branch: the patch and the commands it prints are for a
person. Exit: 0 answered (proposals are work for a person, not a failure), 1 a problem finding (a bump writer
credential is configured, or auto_merge is not empty), 2 could not answer (no stored walk, a registry that does
not load, an unknown proposal store), 3 usage (unknown configuration or proposal, an existing --out file).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, json_option, table

SHADOW = ("SHADOW MODE (E2): nothing is pushed, no branch is written, no pull request is opened. Each proposal is a "
          "patch and commands for a person.")


class _BumpsGroup(click.Group):
    """`bumps CONFIG` is `bumps scan CONFIG`: a first argument that is not a subcommand selects the scan."""

    def parse_args(self, ctx, args):
        if not args or (args[0] not in self.commands and args[0] not in ("-h", "--help")):
            args = ["scan"] + list(args)
        return super().parse_args(ctx, args)


def _guard(fn):
    from nanosoc_rm import bumps
    from nanosoc_rm.analysis._inputs import AnalysisError
    try:
        return fn(), None
    except bumps.BumpUsage as e:
        click.echo("nanosoc-rm: %s" % e, err=True)
        return None, EXIT_USAGE
    except AnalysisError as e:
        click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
        return None, getattr(e, "exit_code", EXIT_UNVERIFIED)


def _from(f: dict) -> str:
    r = f["from"]
    if not r["tag"]:
        return "no release"
    return r["tag"] if r.get("exact") else "contains " + r["tag"]


def _prop_row(d: dict) -> dict:
    f = d["fact"]
    m = f["matrix"]
    return {"order": f["order"], "id": f["id"],
            "consumer": "%s %s:%s@%s" % (f["consumer"]["component"], f["consumer"]["repo"]["host"],
                                         f["consumer"]["repo"]["path"], f["consumer"]["branch"]),
            "path": f["path"], "from": f["from"]["commit"][:12] + " (" + _from(f) + ")",
            "to": f["to"]["tag"], "class": f["change"]["class"],
            "fixes": ",".join(x["id"] for x in f["defects"]["fixed"]) or "-",
            "introduces": ",".join(x["id"] for x in f["defects"]["introduced"]) or "-",
            "matrix": m["verdict"] or "not run", "state": f["state"]}


PROP_COLS = ("order", "id", "consumer", "path", "from", "to", "class", "fixes", "introduces", "matrix", "state")


def _print_scan(s: dict, props: list[dict]) -> None:
    f = s["fact"]
    if not f["eligible"]:
        click.echo("%s: REFUSED: %s" % (f["configuration"], f["refused"]))
        return
    counts = ", ".join("%s %d" % kv for kv in sorted(f["counts"].items()))
    click.echo("%s: %d pins (walk %s): %s" % (f["configuration"], f["walk"]["pins"], f["walk"]["observed_at"], counts))
    others = [r for r in f["rows"] if r["status"] not in ("proposal", "duplicate")]
    if others:
        click.echo(table(others, ("status", "tree_path", "component", "tag", "note")))
    mine = [p for p in props if p["fact"]["configuration"] == f["configuration"]]
    if mine:
        click.echo("\nproposals, deepest first:")
        click.echo(table([_prop_row(p) for p in mine], PROP_COLS))
    else:
        click.echo("\nno proposals for %s" % f["configuration"])
    for p in f["problems"]:
        click.echo("PROBLEM: %s" % p)
    click.echo("")


def register(main: click.Group) -> None:
    @main.group("bumps", cls=_BumpsGroup, invoke_without_command=False,
                help="Bump proposals in SHADOW MODE (Phase 4, E1-E7; built per CR-15). `bumps [CONFIG]` scans now. "
                     "Never pushes, never writes a branch, never opens a pull request: patches and commands for a "
                     "person.")
    def grp() -> None:
        pass

    @grp.command("scan")
    @click.argument("configuration", required=False)
    @click.option("--store", is_flag=True, help="Keep the proposals for the queue page and the shadow ledger.")
    @json_option
    def scan_cmd(configuration, store, as_json):
        """Scan one configuration (or all) for releases newer than their pins (the default subcommand)."""
        from nanosoc_rm import bumps
        res, err = _guard(lambda: bumps.proposals(configuration, store=store))
        if err is not None:
            return err
        scans = [d for d in res if d["kind"] == "bump_scan"]
        props = [d for d in res if d["kind"] == "bump_proposal"]
        if as_json:
            for d in res:
                click.echo(json.dumps(d, separators=(",", ":")))
        else:
            click.echo(SHADOW + "\n")
            for s in scans:
                _print_scan(s, props)
            sched = bumps.schedule_status()
            click.echo("schedule: DISABLED (gate %s: %s)" % (sched["gate"], sched["gate_status"]))
            if store:
                click.echo("kept %d proposal(s) in the queue (bumps.sqlite)" % len(props))
        for u in getattr(res, "unanswered", []):
            click.echo("nanosoc-rm: could not answer: %s" % u, err=True)
        if any(s["fact"]["problems"] for s in scans):
            return EXIT_FINDING
        return EXIT_UNVERIFIED if getattr(res, "unanswered", []) else EXIT_OK

    @grp.command("queue")
    @click.argument("configuration", required=False)
    @json_option
    def queue_cmd(configuration, as_json):
        """The kept proposals (oldest first), as the /bumps page shows them."""
        from nanosoc_rm import bumps
        docs, err = _guard(lambda: bumps.stored(configuration))
        if err is not None:
            return err
        if as_json:
            for d in docs:
                click.echo(json.dumps(d, separators=(",", ":")))
        else:
            click.echo(table([_prop_row(d) for d in docs], PROP_COLS) if docs else
                       "no kept proposals: run `nanosoc-rm bumps --store`")
        return EXIT_OK

    @grp.command("show")
    @click.argument("proposal_id")
    @json_option
    def show_cmd(proposal_id, as_json):
        """One kept proposal: the move, changelog, defects, matrix, blockers."""
        from nanosoc_rm import bumps
        d, err = _guard(lambda: bumps.get(proposal_id))
        if err is not None:
            return err
        if as_json:
            click.echo(json.dumps(d, separators=(",", ":")))
            return EXIT_OK
        f = d["fact"]
        click.echo(SHADOW)
        click.echo("%s  %s  (%s)" % (f["id"], f["state"], f["configuration"]))
        click.echo("consumer   %s %s:%s branch %s at %s" % (f["consumer"]["component"], f["consumer"]["repo"]["host"],
                                                          f["consumer"]["repo"]["path"], f["consumer"]["branch"],
                                                          f["consumer"]["base"][:12]))
        click.echo("gitlink    %s: %s (%s) -> %s (%s)" % (f["path"], f["from"]["commit"][:12], _from(f), f["to"]["commit"][:12],
                                                          f["to"]["tag"]))
        click.echo("range      %s" % (f["range"]["declared"] or "none declared"))
        click.echo("class      %s (%s: %s)" % (f["change"]["class"], f["change"]["class_source"], f["change"]["class_detail"]))
        click.echo("commits    %d%s" % (f["change"]["commits"]["count"], " DIVERGED" if f["change"]["diverged"] else ""))
        for x in f["defects"]["fixed"]:
            click.echo("fixes      %s %s" % (x["id"], x["title"]))
        for x in f["defects"]["introduced"]:
            click.echo("INTRODUCES %s %s" % (x["id"], x["title"]))
        click.echo("matrix     %s" % (f["matrix"]["verdict"] or "not run: nanosoc-rm compat %s %s"
                                      % (f["configuration"], " ".join("--pin " + p for p in f["matrix"]["pins"]))))
        click.echo("waits for  %s" % (", ".join(f["waits_for"]) or "-"))
        click.echo("would      %s (auto-merge never in shadow mode); blockers:" % f["decision"]["would"])
        for b in f["decision"]["blockers"]:
            click.echo("  - %s" % b)
        if f["change"]["changelog"]["excerpt"]:
            click.echo("\n%s:\n%s" % (f["change"]["changelog"]["file"], f["change"]["changelog"]["excerpt"]))
        click.echo("\npatch: nanosoc-rm bumps patch %s --out %s" % (f["id"], f["patch"]["file"]))
        return EXIT_OK

    @grp.command("patch")
    @click.argument("proposal_id")
    @click.option("--out", "out", default=None, help="Write the patch to this new file (default: stdout).")
    def patch_cmd(proposal_id, out):
        """The patch for one kept proposal (gitlink move only; generated RTL is not regenerated)."""
        from nanosoc_rm import bumps
        d, err = _guard(lambda: bumps.get(proposal_id))
        if err is not None:
            return err
        text = bumps.patch_text(d["fact"])
        if not out:
            click.echo(text, nl=False)
            return EXIT_OK
        try:                                              # O_EXCL|O_NOFOLLOW: never overwrite, never follow a link
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except FileExistsError:
            click.echo("nanosoc-rm: %s exists: give a new file for the patch" % out, err=True)
            return EXIT_USAGE
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        click.echo("patch: %s; a person applies it with `git apply --index %s` in a checkout of %s (%s)"
                   % (out, Path(out).name, d["fact"]["consumer"]["component"], d["fact"]["consumer"]["branch"]), err=True)
        return EXIT_OK

    @grp.command("mark")
    @click.argument("proposal_id")
    @click.argument("decision", type=click.Choice(["done", "declined"]))
    @click.option("--by", "login", required=True, help="The login of the person who decided.")
    @click.option("--note", default=None)
    @json_option
    def mark_cmd(proposal_id, decision, login, note, as_json):
        """Record a person's word: they applied the proposal (done) or will not (declined)."""
        from nanosoc_rm import bumps
        m, err = _guard(lambda: bumps.mark(proposal_id, login, decision, note))
        if err is not None:
            return err
        click.echo(json.dumps(m, separators=(",", ":")) if as_json else
                   "%s marked %s by %s at %s (the ledger still reads the mirror)" % (proposal_id, decision, login, m["at"]))
        return EXIT_OK

    @grp.command("ledger")
    @json_option
    def ledger_cmd(as_json):
        """E2: what the bot would have done against what people did, read from the mirrors."""
        from nanosoc_rm import bumps
        obs, err = _guard(lambda: bumps.ledger())
        if err is not None:
            return err
        f = obs["fact"]
        if as_json:
            click.echo(json.dumps(obs, separators=(",", ":")))
        else:
            c = f["counts"]
            click.echo("shadow ledger: %d agreed, %d disagreed, %d pending, %d superseded, %d unknown; agreement %s"
                       % (c["agreed"], c["disagreed"], c["pending"], c["superseded"], c["unknown"],
                          "%.0f%%" % (100 * f["agreement_rate"]) if f["agreement_rate"] is not None else "n/a"))
            click.echo("threshold (E2): %s" % f["threshold"]["reason"])
            click.echo("auto-merge: OFF (%s)" % f["auto_merge"]["gate"])
            if f["entries"]:
                click.echo(table([dict(e, consumer="%s:%s" % (e["consumer"]["host"], e["consumer"]["path"]))
                                  for e in f["entries"]],
                                 ("id", "consumer", "branch", "path", "tag", "class", "outcome", "note")))
            for p in f["problems"]:
                click.echo("PROBLEM: %s" % p)
        return EXIT_FINDING if f["problems"] else EXIT_OK

    @grp.command("schedule")
    @json_option
    def schedule_cmd(as_json):
        """The bump schedule as shipped: disabled, naming the Phase 4 entry gate."""
        from nanosoc_rm import bumps
        s = bumps.schedule_status()
        click.echo(json.dumps(s, separators=(",", ":")) if as_json else
                   "bump schedule: DISABLED. Gate: %s (%s). %s" % (s["gate"], s["gate_status"], s["reason"]))
        return EXIT_OK
