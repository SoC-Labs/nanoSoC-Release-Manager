"""CLI front ends of the freshness engine (interfaces.md section 23). No logic of their own.

    nanosoc-rm freshness scan [CONFIG]... [--store] [--today YYYY-MM-DD] [--json]   measure from the stored walks
    nanosoc-rm freshness show [CONFIG] [--tree] [--today YYYY-MM-DD] [--json]       what is stored, re-judged today

Exit: 0 answered; 2 a configuration has no stored walk (scan) or no stored scan (show); 3 usage. An outdated
tree is not an error: the scan measures, it does not gate.
"""
from __future__ import annotations

import datetime as dt
import importlib

import click

from nanosoc_rm.cliutil import EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup, table

__all__ = ["register"]


def _day(value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise click.UsageError("--today %r is not YYYY-MM-DD" % value) from None


def _behind(behind, lower_bound) -> str:
    return "-" if behind is None else ("%s%d" % (">=" if lower_bound else "", behind))


def _state(state, would_be) -> str:
    return state + (" (would be %s)" % would_be if would_be else "")


def _pin_row(d: dict) -> dict:
    f = d["fact"]
    if f["level"] == "tree":
        return {"configuration": f["configuration"], "tree_path": "(tree)", "state": _state(f["state"], f["would_be"]),
                "behind": f["total_behind"], "bumped": f["updated_days"], "note": "%s/%s direct lagging, %s/%s in tree"
                % (f["direct_lagging"] if f["counted"] else f["would_be_direct_lagging"], f["direct"],
                   f["lagging"] if f["counted"] else f["would_be_lagging"], f["pins"])}
    bump = f["bump"] or {}
    return {"configuration": f["configuration"], "tree_path": f["tree_path"], "state": _state(f["state"], f["would_be"]),
            "behind": _behind(f["behind"], f["lower_bound"]), "bumped": (bump.get("date") or "")[:10],
            "note": f["not_measured"] or f.get("caveat") or ""}


def register(main: click.Group) -> None:
    importlib.import_module("nanosoc_rm.freshness.jobkind")     # registers the freshness-scan job kind
    grp = subgroup(main, "freshness", help="How far each submodule pin lags its upstream and when it last moved "
                                           "(section 23).")

    @grp.command("scan")
    @click.argument("configurations", nargs=-1)
    @click.option("--store", is_flag=True, help="Keep the result: one snapshot per configuration.")
    @click.option("--today", default=None, help="Judge as of this UTC date (default: today).")
    @json_option
    def scan_cmd(configurations, store, today, as_json):
        """Measure each registered configuration's stored walk against the mirrors (read-only git)."""
        from nanosoc_rm.config import ConfigError
        from nanosoc_rm.freshness import scan as S
        try:
            docs = S.scan(list(configurations) or None, store=store, today=_day(today))
        except (S.FreshnessUsage, ConfigError) as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE
        emit(docs, as_json, ("configuration", "tree_path", "state", "behind", "bumped", "note"), _pin_row)
        unwalked = [d for d in docs if d["fact"]["level"] == "tree" and d["fact"]["not_measured"]]
        return EXIT_UNVERIFIED if unwalked else EXIT_OK

    @grp.command("show")
    @click.argument("configuration", required=False)
    @click.option("--tree", "as_tree", is_flag=True, help="Every node of CONFIGURATION's tree, depth first.")
    @click.option("--today", default=None, help="Judge as of this UTC date (default: today).")
    @json_option
    def show_cmd(configuration, as_tree, today, as_json):
        """The stored freshness, re-judged on the day: every tree, or one tree's nodes with --tree."""
        import json as _json
        from nanosoc_rm.freshness import view
        day = _day(today)
        if as_tree:
            if not configuration:
                raise click.UsageError("--tree needs a CONFIGURATION")
            t = view.tree(configuration, today=day)
            if t is None:
                click.echo("nanosoc-rm: %s has no stored freshness scan" % configuration, err=True)
                return EXIT_UNVERIFIED
            if as_json:
                click.echo(_json.dumps(t, separators=(",", ":"), default=str))
            else:
                click.echo(table([{"path": "  " * (n["display_depth"] - 1) + n["name"], "state": n["word"],
                                   "behind": _behind(n["behind"], n["lower_bound"]),
                                   "bumped_days": n["days_since_bump"], "worst_beneath": n["rollup_word"]}
                                  for n in t["nodes"]], ("path", "state", "behind", "bumped_days", "worst_beneath")))
            return EXIT_OK
        rows = [s for s in view.trees(today=day) if not configuration or s["configuration"] == configuration]
        if not rows:
            click.echo("nanosoc-rm: no stored freshness scan%s" % (" for %s" % configuration if configuration else ""),
                       err=True)
            return EXIT_UNVERIFIED
        if as_json:
            for s in rows:
                click.echo(_json.dumps(s, separators=(",", ":"), default=str))
        else:
            click.echo(table([{"configuration": s["configuration"], "verdict": s["word"] + (
                " (would be %s)" % s["would_be_word"] if s["would_be_word"] else ""),
                "direct_lagging": "%s/%s" % (s["direct_lagging"] if s["counted"] else s["would_be_direct_lagging"],
                                             s["direct"]),
                "tree_lagging": "%s/%s" % (s["lagging"] if s["counted"] else s["would_be_lagging"], s["pins"]),
                "updated_days": s["updated_days"], "age": s["age_word"]} for s in rows],
                ("configuration", "verdict", "direct_lagging", "tree_lagging", "updated_days", "age")))
        return EXIT_OK
