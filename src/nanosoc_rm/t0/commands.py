"""CLI commands of the t0 module (interfaces.md section 9).

- `nanosoc-rm tiers [CONFIG] [--json]`: the tier table (D1). Exit 0, or 1 if the table itself is invalid.
- `nanosoc-rm t0 run --out DIR`: the CI job (compat-ci only). The runner is imported inside the command, so the
  service process, which imports every plug-in, never imports it (AT-A4).
- `nanosoc-rm t0 finalise DIR [--json]`: the bundle's last step on its own (the job's after_script runs it again,
  so a job killed mid-run still uploads nothing the scan flags). Exit 0 clean, 1 finding, 2 unverified.

The ci module adds `t0 trigger|fetch|runs|items|coverage` to the same group.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup


def register(main: click.Group) -> None:
    @main.command("tiers")
    @click.argument("configuration", required=False)
    @json_option
    def tiers_cmd(configuration: str | None, as_json: bool) -> int:
        """Test tiers with their budgets and, for CONFIGURATION, their commands (D1)."""
        from nanosoc_rm.t0 import tiers
        rows = tiers.tier_rows(configuration)
        emit(rows, as_json, columns=("tier", "budget_s", "budget_reason", "manual", "phase", "command"),
             row=lambda r: r)
        problems = tiers.validate_tiers(tiers.TIERS)
        for p in problems:
            click.echo("nanosoc-rm tiers: %s" % p, err=True)
        return EXIT_FINDING if problems else EXIT_OK

    t0 = subgroup(main, "t0", help="T0, the resolve tier: run (CI only), and the service's trigger and results.")

    @t0.command("run")
    @click.option("--out", "out", default="rm-bundle", show_default=True, type=click.Path(path_type=Path),
                  help="The bundle directory the job uploads.")
    @click.option("--url-map", "url_map", default=None, type=click.Path(dir_okay=False, exists=True, path_type=Path),
                  help="TESTS ONLY: JSON {URL: local repo}. Refused when CI_JOB_ID is set.")
    def run_cmd(out: Path, url_map: Path | None) -> int:
        """Run T0 for the RM_REQUEST pipeline variable and write the bundle (compat-ci job t0 only)."""
        from nanosoc_rm.t0 import runner              # never at import time: the service must not load it
        mapping = None
        if url_map is not None:
            try:
                mapping = json.loads(url_map.read_text())
            except ValueError as e:
                click.echo("nanosoc-rm t0 run: --url-map is not JSON: %s" % e, err=True)
                return EXIT_USAGE
        return runner.run(out, url_map=mapping)

    @t0.command("finalise")
    @click.argument("directory", type=click.Path(file_okay=False, exists=True, path_type=Path))
    @json_option
    def finalise_cmd(directory: Path, as_json: bool) -> int:
        """Scan a bundle, delete what the scan flags, record the scan in provenance.json (D11)."""
        from nanosoc_rm.t0 import runner
        r = runner.finalise_bundle(directory)
        if as_json:
            emit([r.as_doc()], True)
        else:
            click.echo("%s: %s, %d file(s) scanned, %d finding(s)%s" % (
                directory, r.verdict, r.files_scanned, len(r.findings), ("; " + r.why) if r.why else ""))
        return {"clean": EXIT_OK, "finding": EXIT_FINDING}.get(r.verdict, EXIT_UNVERIFIED)
