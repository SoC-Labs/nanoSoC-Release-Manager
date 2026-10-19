"""`nanosoc-rm alerts` (interfaces.md section 8): the open alerts, and clearing one.

    nanosoc-rm alerts [--all] [--json]          exit 1 when an open alert is listed, else 0
    nanosoc-rm alerts clear SUBJECT --by LOGIN  a write to the service's own database only; logged in actions

`nanosoc-rm store-maint compact --dry-run|--apply` (interfaces.md section 12): collapse the runs
of identical set-kind snapshots stored before keep-on-change, keeping each state's first and last observed times.
One transaction; --dry-run rolls it back and reports what --apply would remove. An operator command: stop the
service and back up rm.sqlite first (DEPLOY.md, "Store growth and retention").
"""
from __future__ import annotations

import click

from nanosoc_rm import db
from nanosoc_rm.cliutil import EXIT_OK, EXIT_USAGE, emit, exit_code, json_option


def _row(doc: dict) -> dict:
    f = doc["fact"]
    return {"raised_at": f["raised_at"], "kind": f["alert_kind"], "about": f["about"], "message": f["message"],
            "cleared": ("%s by %s" % (f["cleared_at"], f["cleared_by"])) if f["cleared_at"] else ""}


_COLUMNS = ("raised_at", "kind", "about", "message", "cleared")
_COMPACT_COLUMNS = ("kind", "scopes", "scopes_compacted", "snapshots_removed", "rows_removed", "mb_removed", "skipped")


def _compact_row(r: dict) -> dict:
    return dict(r, mb_removed="%.1f" % (r["bytes_removed"] / 1e6), skipped=len(r["skipped"]))


def register(main: click.Group) -> None:
    @main.group("alerts", invoke_without_command=True)
    @click.option("--all", "show_all", is_flag=True, help="Include cleared alerts.")
    @json_option
    @click.pass_context
    def alerts_cmd(ctx: click.Context, show_all: bool, as_json: bool) -> int | None:
        """Open alerts (dashboard banners): rewrite, unknown_url, token_expiry, runner_drift, tag_moved,
        mirror_failed, waiver_unused. Exit 1 when any listed alert is open."""
        if ctx.invoked_subcommand is not None:
            return None
        docs = emit(db.alerts(open_only=not show_all), as_json, _COLUMNS, _row)
        return exit_code(findings=any(not d["fact"]["cleared_at"] for d in docs))

    @alerts_cmd.command("clear")
    @click.argument("subject")
    @click.option("--by", "login", required=True, help="The login clearing it (logged in actions).")
    @json_option
    def clear_cmd(subject: str, login: str, as_json: bool) -> int:
        """Clear the alert SUBJECT (alert:<kind>:<about>)."""
        try:
            db.clear_alert(subject, login)
        except KeyError:
            click.echo("nanosoc-rm: no alert with subject %s" % subject, err=True)
            return EXIT_USAGE
        emit([d for d in db.alerts(open_only=False) if d["subject"] == subject], as_json, _COLUMNS, _row)
        return EXIT_OK

    @main.group("store-maint")
    def store_maint() -> None:
        """Store maintenance for the operator. Stop the service and back up rm.sqlite first."""

    @store_maint.command("compact")
    @click.option("--dry-run", "dry_run", is_flag=True, help="Report what --apply would remove; change nothing.")
    @click.option("--apply", "apply_", is_flag=True, help="Remove the repeated snapshots (one transaction).")
    @json_option
    def compact_cmd(dry_run: bool, apply_: bool, as_json: bool) -> int:
        """Collapse each run of identical set-kind snapshots of one scope into its first snapshot, which keeps the
        run's last check (first and last observed times). Safe to rerun: a second run removes nothing. Afterwards
        `VACUUM` returns the space to the file system (DEPLOY.md)."""
        if dry_run == apply_:
            click.echo("nanosoc-rm: store-maint compact needs exactly one of --dry-run and --apply", err=True)
            return EXIT_USAGE
        rows = db.compact(apply=apply_)
        total = {"kind": "TOTAL", **{k: sum(r[k] for r in rows) for k in
                                     ("scopes", "scopes_compacted", "snapshots_removed", "rows_removed",
                                      "bytes_removed")}, "skipped": [s for r in rows for s in r["skipped"]]}
        emit(rows + [total], as_json, _COMPACT_COLUMNS, _compact_row)
        if not as_json:
            click.echo("%s: %d snapshot(s), %d row(s), %.1f MB of documents%s" % (
                "applied" if apply_ else "dry run, nothing changed", total["snapshots_removed"],
                total["rows_removed"], total["bytes_removed"] / 1e6,
                "; skipped (current is not the last stored): %s" % ", ".join(total["skipped"])
                if total["skipped"] else ""))
        return EXIT_OK
