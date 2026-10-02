"""`nanosoc-rm alerts` (interfaces.md section 8): the open alerts, and clearing one.

    nanosoc-rm alerts [--all] [--json]          exit 1 when an open alert is listed, else 0
    nanosoc-rm alerts clear SUBJECT --by LOGIN  a write to the service's own database only; logged in actions
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
