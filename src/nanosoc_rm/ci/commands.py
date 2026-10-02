"""CLI for the CI module (interfaces.md sections 10 and 17.3; plug-in seam of nanosoc_rm.cli).

    nanosoc-rm t0 trigger CONFIG|--all [--by LOGIN] [--json]   a shell user's run is manual: --by is required
    nanosoc-rm t0 fetch [--json]                                fetch, verify and store every pending run
    nanosoc-rm t0 runs [CONFIG] [--json]                        stored runs; exit 1 if a configuration's latest
                                                                final run is red
    nanosoc-rm t0 items NONCE GATE [--json]                     one gate's items from the stored bundle
    nanosoc-rm t0 coverage [--night DATE] [--json]              exit 1 when a configuration or @registry has no
                                                                accepted scheduled run that night (D2)
    nanosoc-rm runner [--json]                                  runner settings (D10): exit 1 drift, 2 unreadable

`t0` is shared with the runner's `t0 run` (registered by nanosoc_rm.t0), through cliutil.subgroup.
"""
from __future__ import annotations

import datetime as dt
import json

import click

from nanosoc_rm.cliutil import EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, exit_code, json_option, subgroup

_RED = ("FAIL", "UNVERIFIED", "INFRA")


def _run_row(doc: dict) -> dict:
    f = doc["fact"]
    return {"configuration": f["configuration"], "nonce": f["run"]["nonce"], "trigger": f["run"]["trigger"],
            "by": f["run"]["triggered_by"], "triggered_at": f["run"]["triggered_at"], "acceptance": f["acceptance"],
            "verdict": f["verdict"] or "-", "pipeline": f["run"]["pipeline_id"] or "-",
            "over_budget": "yes" if f["over_budget"] else "", "reason": f["reason"] or ""}


_RUN_COLUMNS = ("configuration", "nonce", "trigger", "by", "triggered_at", "acceptance", "verdict", "pipeline",
                "over_budget", "reason")


def _echo_err(msg: str) -> None:
    click.echo("nanosoc-rm: %s" % msg, err=True)


def register(main: click.Group) -> None:
    t0 = subgroup(main, "t0", help="T0 runs: trigger them in CI, fetch and verify their results (A4, D2, D5).")

    @t0.command("trigger")
    @click.argument("configuration", required=False)
    @click.option("--all", "all_", is_flag=True, help="Every registered configuration, and @registry.")
    @click.option("--by", "login", help="Your login: a run from the shell is manual and is logged (H2).")
    @json_option
    def trigger_cmd(configuration: str | None, all_: bool, login: str | None, as_json: bool) -> int:
        """Start T0 for CONFIGURATION (or --all) in the CI project. Exit 0 started, 2 a trigger failed,
        3 usage (unknown configuration, no --by)."""
        from nanosoc_rm.ci import configs
        from nanosoc_rm.ci.trigger import RunPending, TriggerError, trigger
        if bool(configuration) == all_:
            _echo_err("give exactly one of CONFIGURATION or --all")
            return EXIT_USAGE
        if not login or login == "schedule":
            _echo_err("a run started from the shell is manual: --by LOGIN is required (H2)")
            return EXIT_USAGE
        try:
            ids = [c["id"] for c in configs.configurations()] + [configs.REGISTRY_JOB] if all_ else [configuration]
        except configs.RegistryUnreadable as e:
            _echo_err(str(e))
            return EXIT_UNVERIFIED
        docs, failed, usage = [], False, False
        for cfg in ids:
            try:
                docs.append(trigger(cfg, by=login, kind="manual"))
            except configs.UnknownConfiguration as e:
                _echo_err(str(e))
                usage = True
            except (RunPending, TriggerError, configs.RegistryUnreadable) as e:
                _echo_err("%s: %s" % (cfg, e))
                failed = True
        emit(docs, as_json, _RUN_COLUMNS, _run_row)
        failed = failed or any(d["fact"]["acceptance"] != "pending" for d in docs)
        if usage and not docs:
            return EXIT_USAGE
        return EXIT_UNVERIFIED if failed else EXIT_OK

    @t0.command("fetch")
    @json_option
    def fetch_cmd(as_json: bool) -> int:
        """Fetch every pending run from the CI API, verify it (D5) and store the result. Exit 1 if a fetched run is
        FAIL, rejected or refused; 2 if one is UNVERIFIED or INFRA."""
        from nanosoc_rm.ci.fetch import fetch_pending
        docs = emit(fetch_pending(), as_json, _RUN_COLUMNS, _run_row)
        return exit_code(findings=any(d["fact"]["verdict"] == "FAIL" or d["fact"]["acceptance"] in
                                      ("rejected", "refused") for d in docs),
                         unverified=any(d["fact"]["verdict"] in ("UNVERIFIED", "INFRA") for d in docs))

    @t0.command("runs")
    @click.argument("configuration", required=False)
    @json_option
    def runs_cmd(configuration: str | None, as_json: bool) -> int:
        """Stored T0 runs, oldest first (latest state of each). Exit 1 when the latest final run of a listed
        configuration is red (FAIL, UNVERIFIED or INFRA)."""
        from nanosoc_rm.ci.fetch import results
        docs = emit(results(configuration), as_json, _RUN_COLUMNS, _run_row)
        last: dict[str, dict] = {}
        for d in docs:
            if d["fact"]["acceptance"] != "pending":
                last[d["fact"]["configuration"]] = d["fact"]
        return exit_code(findings=any(f["verdict"] in _RED for f in last.values()))

    @t0.command("items")
    @click.argument("nonce")
    @click.argument("gate")
    @json_option
    def items_cmd(nonce: str, gate: str, as_json: bool) -> int:
        """The items one gate judged in run NONCE, from its stored bundle. Exit 1 if any is not `pass`."""
        from nanosoc_rm.ci.fetch import items
        try:
            rows = items(nonce, gate)
        except KeyError as e:
            _echo_err(str(e.args[0]))
            return EXIT_USAGE
        except FileNotFoundError as e:
            _echo_err(str(e))
            return EXIT_UNVERIFIED
        if as_json:
            for r in rows:
                click.echo(json.dumps(r, separators=(",", ":")))
        else:
            emit([dict(r, where=r.get("tree_path") or "%s#%s" % (r.get("file"), r.get("pointer"))) for r in rows],
                 False, ("where", "result", "commit", "host", "why"), lambda r: r)
        return exit_code(findings=any(r.get("result") != "pass" for r in rows))

    @t0.command("coverage")
    @click.option("--night", "night", help="UTC date YYYY-MM-DD (default: the last night whose trigger time passed).")
    @json_option
    def coverage_cmd(night: str | None, as_json: bool) -> int:
        """Configurations (and @registry) with no accepted scheduled T0 that night (D2). Exit 1 if any."""
        from nanosoc_rm.ci import configs
        from nanosoc_rm.ci.fetch import coverage, default_night
        try:
            day = dt.date.fromisoformat(night) if night else default_night()
        except ValueError:
            _echo_err("--night must be a date YYYY-MM-DD")
            return EXIT_USAGE
        try:
            rows = coverage(day)
        except configs.RegistryUnreadable as e:
            _echo_err(str(e))
            return EXIT_UNVERIFIED
        if as_json:
            for r in rows:
                click.echo(json.dumps(r, separators=(",", ":")))
        else:
            emit(rows, False, ("configuration", "night", "reason"), lambda r: r)
        return exit_code(findings=bool(rows))

    @main.command("runner")
    @json_option
    def runner_cmd(as_json: bool) -> int:
        """The hardened runner's settings against ci.runner_expected (D10). Exit 1 drift, 2 unreadable."""
        from nanosoc_rm.ci.runners import runner_settings
        docs = emit(runner_settings(), as_json, ("runner", "verdict", "drift"),
                    lambda d: {"runner": d["fact"]["runner_id"], "verdict": d["fact"]["verdict"],
                               "drift": "; ".join(d["fact"]["drift"])})
        verdicts = {d["fact"]["verdict"] for d in docs}
        return exit_code(findings="drift" in verdicts, unverified="unverified" in verdicts or not docs)
