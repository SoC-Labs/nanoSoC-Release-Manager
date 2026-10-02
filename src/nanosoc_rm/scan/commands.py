"""`nanosoc-rm scan bundle DIR [--rules PATH] [--json]` (interfaces.md section 11).

Exit 0 clean, 1 finding, 2 unverified (nothing scanned, or a rule not armed), 3 a rule file that cannot be used.
Findings print as locations (rule, file, line), never the matched text.
"""
from __future__ import annotations

from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup


def register(main: click.Group) -> None:
    grp = subgroup(main, "scan", help="Vendor-content scanner (D11): bundles before they are stored.")

    @grp.command("bundle")
    @click.argument("directory", type=click.Path(file_okay=False, path_type=Path))
    @click.option("--rules", "rules_file", type=click.Path(dir_okay=False, path_type=Path), default=None,
                  help="Private rule file (default: $RM_SCAN_RULES; built-in arm.banner always applies).")
    @json_option
    def bundle(directory: Path, rules_file: Path | None, as_json: bool) -> int:
        """Scan a bundle directory: exit 0 clean, 1 finding, 2 unverified."""
        from nanosoc_rm.scan.rules import RulesError, rule_set
        from nanosoc_rm.scan.scan import scan_dir
        try:
            rules = rule_set(rules_file)
        except RulesError as e:
            click.echo("nanosoc-rm scan: %s" % e, err=True)
            return EXIT_USAGE
        r = scan_dir(directory, rules)
        if as_json:
            emit([r.as_doc()], True)
        else:
            click.echo("%s: %s, %d file(s) scanned, rules armed: %s%s" % (
                directory, r.verdict, r.files_scanned,
                ", ".join("%s=%s" % (k, "yes" if v else "NO") for k, v in sorted(r.armed.items())),
                ("; " + r.why) if r.why else ""))
            for f in r.findings:
                click.echo("  %s at %s:%s" % (f["rule"], f["file"], f["line"] if f["line"] is not None else "-"))
        return {"clean": EXIT_OK, "finding": EXIT_FINDING}.get(r.verdict, EXIT_UNVERIFIED)
