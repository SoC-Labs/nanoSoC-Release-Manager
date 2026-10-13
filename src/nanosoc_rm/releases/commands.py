"""CLI: `nanosoc-rm release draft|verify|list` and `nanosoc-rm forks` (CR-R1; G2, G3, G4, G7, F4, I1, I2).

Built for local use under CR-15; nothing here tags, pushes or publishes (the Phase 5 gate governs that). Exit
codes (interfaces.md section 0.2): 0 the draft's verdict is PASS, WARN or WAIVED; 1 FAIL; 2 UNVERIFIED or INFRA
(a draft with person steps or pending lock fields still open is UNVERIFIED); 3 usage.
"""
from __future__ import annotations

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup

__all__ = ["register"]


def _show_draft(doc: dict, d: str | None) -> None:
    f = doc["fact"]
    click.echo("release draft %s %s (%s), commit %s, kind %s%s" % (f["component"], f["version"], f["configuration"],
                                                                  f["commit"][:12], f["kind"],
                                                                  ", silicon" if f["silicon"] else ""))
    if d:
        click.echo("folder: %s" % d)
    click.echo("lock: %s (%s), %d pins%s" % (f["lock"]["file"], "complete" if f["lock"]["complete"] else "NOT complete",
                                            f["lock"]["pins"], "; unprotected: %s" % ", ".join(f["lock"]["unprotected"])
                                            if f["lock"]["unprotected"] else ""))
    click.echo("errata: revision %s, %d record(s)" % (f["errata"]["revision"], f["errata"]["records"]))
    if f["archive"]:
        click.echo("archive: %d file(s), md5 %s, manifest md5 %s" % (f["archive"]["files"], f["archive"]["md5"],
                                                                     f["archive"]["manifest_md5"]))
    click.echo("")
    for c in f["checks"]:
        click.echo("%-11s %-24s %s%s" % (c["verdict"], c["id"], c["detail"][:160], "  [person]" if c["person"] else ""))
    click.echo("")
    click.echo("person steps: %d (CHECKLIST.md); train members: %d" % (len(f["person_steps"]), len(f["train"])))
    click.echo("verdict %s; ready for the person steps: %s. Nothing was tagged, pushed or published." % (
        f["verdict"], "yes" if f["ready"] else "no"))


def register(main: click.Group) -> None:
    grp = subgroup(main, "release", help="Local release drafts (G2, G3, G7, F4): lock, errata, notes, checklist. "
                                         "Never tags or publishes.")

    @grp.command("draft")
    @click.argument("configuration")
    @click.argument("version")
    @click.option("--kind", type=click.Choice(["fix", "feature", "tapeout"]), default=None,
                  help="G2 gates: fix T0-T3, feature +T4, tapeout +T6 (default: tapeout for silicon, else feature).")
    @click.option("--archive", default=None, help="Generated-RTL archive (tar of build_soc/) for the G7 manifest.")
    @click.option("--generator", default=None, help="Tree path of the generator pin (default: the shallowest nanosoc_gen).")
    @click.option("--rtl-committed", is_flag=True, help="The generated RTL is committed (not gitignored).")
    @click.option("--gds", default=None, help="Silicon: the GDS that went to the foundry (hashed, never copied).")
    @click.option("--netlist", default=None, help="Silicon: the P&R netlist (hashed, never copied).")
    @click.option("--artefact", "artefacts", multiple=True, help="KIND=PATH (flists, bitstream, logs, source-archive).")
    @click.option("--waiver", "waivers", multiple=True, help="PATHS=REASON: pins released without a tag (* = all).")
    @click.option("--since", default=None, help="Previous release tag for the notes (default: the newest older vX.Y.Z).")
    @click.option("--candidate", type=int, default=None, help="Also list the vX.Y.Z-rc.N candidate tag command.")
    @click.option("--out", default=None, help="Draft folder (default: <data_dir>/releases/CONFIGURATION/VERSION).")
    @click.option("--replace", is_flag=True, help="Rebuild an existing draft folder.")
    @json_option
    def draft_cmd(configuration, version, kind, archive, generator, rtl_committed, gds, netlist, artefacts, waivers,
                  since, candidate, out, replace, as_json):
        """Build a release draft folder of CONFIGURATION as VERSION (not published)."""
        from nanosoc_rm.releases import ReleaseError, ReleaseUsage, draft, exit_for, parse_waiver
        try:
            arts = []
            for a in artefacts:
                k, sep, p = a.partition("=")
                if not sep:
                    raise ReleaseUsage("--artefact %r is not KIND=PATH" % a)
                arts.append((k, p))
            doc = draft(configuration, version, kind=kind, archive=archive, generator=generator,
                        rtl_committed=rtl_committed, gds=gds, netlist=netlist, artefacts=arts,
                        waivers=[parse_waiver(w) for w in waivers], since=since, candidate=candidate, out=out,
                        replace=replace)
        except ReleaseUsage as e:
            click.echo("nanosoc-rm release draft: %s" % e, err=True)
            return EXIT_USAGE
        except ReleaseError as e:
            click.echo("nanosoc-rm release draft: could not answer: %s" % e, err=True)
            return EXIT_UNVERIFIED
        if as_json:
            emit([doc], True)
        else:
            from nanosoc_rm.releases import draft_path_of
            _show_draft(doc, str(draft_path_of(doc, out)))
        return exit_for(doc["fact"]["verdict"])

    @grp.command("verify")
    @click.argument("draft")
    @click.option("--against", default=None, help="A directory holding build_soc/: compare it with the archive's "
                                                  "manifest file for file (G7).")
    @json_option
    def verify_cmd(draft, against, as_json):
        """Re-check DRAFT (a folder, or CONFIGURATION/VERSION) against the mirrors and the registry now."""
        from nanosoc_rm.releases import ReleaseError, ReleaseUsage, exit_for, verify
        try:
            doc = verify(draft, against=against)
        except ReleaseUsage as e:
            click.echo("nanosoc-rm release verify: %s" % e, err=True)
            return EXIT_USAGE
        except ReleaseError as e:
            click.echo("nanosoc-rm release verify: could not answer: %s" % e, err=True)
            return EXIT_UNVERIFIED
        f = doc["fact"]
        if as_json:
            emit([doc], True)
        else:
            for c in f["checks"]:
                click.echo("%-11s %-24s %s" % (c["verdict"], c["id"], c["detail"][:160]))
            click.echo("drift: %d; verdict %s" % (len(f["drift"]), f["verdict"]))
        return exit_for(f["verdict"])

    @grp.command("list")
    @click.argument("configuration", required=False)
    @json_option
    def list_cmd(configuration, as_json):
        """Every release draft on this host (newest first)."""
        from nanosoc_rm.releases import drafts
        rows = drafts(configuration=configuration)
        docs = [r["doc"] for r in rows if r["doc"]]
        if as_json:
            emit(docs, True)
        else:
            click.echo("\n".join("%-20s %-10s %-11s %-6s %s" % (
                r["configuration"], r["version"], (r["doc"] or {}).get("fact", {}).get("verdict", "UNREADABLE"),
                "ready" if (r["doc"] or {}).get("fact", {}).get("ready") else "", r["dir"]) for r in rows)
                or "no release drafts")
        return EXIT_OK if all(r["doc"] for r in rows) else EXIT_UNVERIFIED

    @main.command("forks")
    @click.argument("component", required=False)
    @click.option("--store", is_flag=True, help="Store each fork_status (the Forks page shows the stored ones).")
    @json_option
    def forks_cmd(component, store, as_json):
        """Forks of the example projects (I1): base release, divergence, errata, user-path changes (read-only,
        anonymous)."""
        from nanosoc_rm.releases import ReleaseUsage, forks
        try:
            docs = forks(component, store=store)
        except ReleaseUsage as e:
            click.echo("nanosoc-rm forks: %s" % e, err=True)
            return EXIT_USAGE
        if as_json:
            emit(docs, True)
        else:
            for d in docs:
                f = d["fact"]
                click.echo("%-10s %s:%s  ahead %s behind %s  based on %s  releases behind %s  user paths %s  %s" % (
                    f["verdict"], f["fork"]["host"], f["fork"]["path"], f["ahead"], f["behind"], f["based_on"],
                    f["releases_behind"], f["user_paths"]["verdict"], f["reason"]))
            if not docs:
                click.echo("no forks found")
        if any(d["fact"]["verdict"] == "UNVERIFIED" for d in docs):
            return EXIT_UNVERIFIED
        return EXIT_FINDING if any(d["fact"]["verdict"] == "WARN" for d in docs) else EXIT_OK
