"""`nanosoc-rm matrix` (D3, D4, D7, D8, D9, B7; interfaces.md section 18).

    nanosoc-rm matrix CONFIG [--columns pinned,in-range,latest-tag,head] [--component C]... [--run [--wait]] [--json]
    nanosoc-rm matrix cell CONFIG --column COL [--component C] [--store] [--json]      one cell, in the foreground
    nanosoc-rm matrix quarantine CONFIG [--owner GATE=LOGIN] [--json]                 D8 quarantine list
    nanosoc-rm matrix nightly CONFIG [--json]                                 what the DISABLED nightly would queue

`matrix CONFIG` is `matrix show CONFIG`. `--run` queues every runnable cell of the (filtered) view as a local job,
pinned first; `--wait` then works the queue in this process (unless the service's worker holds it) and prints the
view again when the cells are done.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, json_option

__all__ = ["register"]

_SUBCOMMANDS = ("show", "cell", "quarantine", "nightly")


class _MatrixGroup(click.Group):
    """`matrix CONFIG ...` means `matrix show CONFIG ...`."""

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["show"] + list(args)
        return super().parse_args(ctx, args)


def _login() -> str:
    try:
        return getpass.getuser()
    except Exception:                                             # noqa: BLE001
        return "cli"


def _fail(e: Exception) -> None:
    from nanosoc_rm.analysis._inputs import AnalysisError
    from nanosoc_rm.config import ConfigError
    code = getattr(e, "exit_code", None)
    if isinstance(e, AnalysisError):
        code = e.exit_code
    elif isinstance(e, (ConfigError,)):
        code = EXIT_USAGE
    click.echo("nanosoc-rm matrix: %s" % e, err=True)
    sys.exit(code if code in (2, 3) else EXIT_USAGE)


_SHORT = {"PASS": "PASS", "WARN": "WARN", "WAIVED": "WAIVED", "FAIL": "FAIL", "FLAKY": "FLAKY",
          "UNVERIFIED": "UNVERIF", "INFRA": "INFRA", "NOT-RUN": "NOT-RUN"}


def _age(s) -> str:
    if s is None:
        return ""
    from nanosoc_rm.web.page import human_age
    return human_age(s)


def _cell_text(cv: dict) -> str:
    t = cv["target"]
    ref = t.get("ref") or ((t.get("commit") or "")[:7])
    st = cv["status"]
    if st == "no-target":
        return "-- (%s)" % (t.get("why") or "no target")[:40]
    if st == "same-as-pinned":
        return "= pinned %s" % (cv["verdict"] or "")
    if st in ("queued", "running"):
        return "%s %s" % (st, ref)
    if st.startswith("job-") and not cv["verdict"]:
        return "%s %s" % (st, ref)
    v = _SHORT.get(cv["verdict"] or "", cv["verdict"] or "not run")
    extra = (" STALE" if cv["stale"] else "") + (" q" if cv["quarantined_gates"] else "")
    return "%s %s %s%s" % (v, ref, _age(cv["age_s"]), extra) if cv["verdict"] else "not run %s" % ref


def _print_view(doc: dict) -> None:
    f = doc["fact"]
    p = f["pinned"]
    click.echo("matrix %s at %s (data from %s)" % (f["configuration"], f["commit"][:12], doc["observed_at"]))
    click.echo("pinned run (D9 baseline): %s" % (_cell_text(p) if p["status"] != "not-run" else "not run yet"))
    cols = f["columns"]
    head = ["component"] + cols
    rows = []
    for r in f["rows"]:
        name = r["component"] + (" [split]" if r["split"] else "")
        rows.append([name] + [_cell_text(r["cells"][c]) for c in cols])
    widths = [max(len(x[i]) for x in [head] + rows) for i in range(len(head))]
    click.echo("  ".join(h.upper().ljust(w) for h, w in zip(head, widths)))
    for x in rows:
        click.echo("  ".join(v.ljust(w) for v, w in zip(x, widths)).rstrip())
    for s in f["splits"]:
        click.echo("B7 split %s: %s" % (s["component"], ", ".join("%s@%s" % (p_["tree_path"], (p_["commit"] or "?")[:7])
                                                             for p_ in s["pins"])))
    for r in f["rows"]:
        for c in cols:
            cmp_ = r["cells"][c].get("comparison")
            if cmp_ and cmp_["baseline"] and cmp_["changes"]:
                changes = ", ".join("%s %s->%s" % (x["gate"], x["baseline"], x["cell"]) for x in cmp_["changes"])
                click.echo("D9 %s/%s vs pinned run %s: %s" % (r["component"], c, cmp_["baseline"]["nonce"][:12],
                                                              changes))
    act = [q for q in f["quarantine"] if q["active"]]
    if act:
        for q in act:
            click.echo("QUARANTINED %s: owner %s, since %s, expires %s" % (q["test"], q["owner"], q["since"],
                                                                        q["expires_at"]))
    n = f["nightly"]
    click.echo("nightly matrix: %s (%s)" % ("ENABLED" if n["enabled"] else "DISABLED", n["gate"]))


def _view_exit(doc: dict) -> int:
    vs = [cv["verdict"] for r in doc["fact"]["rows"] for cv in r["cells"].values()] + [doc["fact"]["pinned"]["verdict"]]
    if any(v in ("FAIL", "FLAKY") for v in vs):
        return EXIT_FINDING
    return EXIT_OK if any(v for v in vs) else EXIT_UNVERIFIED


def _wait(job_ids: list[int], config) -> None:
    """Work the queue here until every job in `job_ids` has finished (or watch the service's worker do it)."""
    from nanosoc_rm import jobs
    w = jobs.Worker(config.path, log=lambda m: click.echo("worker: %s" % m, err=True))
    mine = w.acquire(config)
    if not mine:
        click.echo("another worker (the service) holds the queue: waiting for it to run the cells", err=True)
    try:
        while True:
            left = [j for j in (jobs.get(config, i) for i in job_ids) if j and j["state"] not in jobs.FINAL]
            if not left:
                return
            if mine:
                ran = w.step()
                if ran is not None:
                    j = jobs.get(config, ran)
                    click.echo("job %d %s %s%s" % (ran, j["state"], j.get("verdict") or "",
                                                   ": %s" % j["reason"] if j.get("reason") else ""), err=True)
                    continue
            time.sleep(2)
    finally:
        w.release()


def register(main: click.Group) -> None:
    grp = main.commands.get("matrix")
    if grp is None:
        grp = _MatrixGroup(name="matrix", help="Compatibility matrix (D3): `matrix CONFIG` shows it; "
                                                "`matrix cell|quarantine|nightly` (Phase 2, built per CR-15).")
        main.add_command(grp)

    @grp.command("show")
    @click.argument("configuration")
    @click.option("--columns", default=None, help="Comma list of pinned,in-range,latest-tag,head (default all).")
    @click.option("--component", "components", multiple=True, help="Restrict to these rows (repeatable).")
    @click.option("--run", is_flag=True, help="Queue every runnable cell of this view as a local job.")
    @click.option("--wait", is_flag=True, help="With --run: work the queue here until the cells are done.")
    @json_option
    def show(configuration, columns, components, run, wait, as_json):
        """One configuration's matrix: a row per component, a cell per column."""
        from nanosoc_rm import matrix as M
        from nanosoc_rm.config import load_config
        try:
            config = load_config()
            if wait and not run:
                raise M.MatrixUsage("--wait needs --run")
            if run:
                queued = M.queue(configuration, columns=M.parse_columns(columns), components=components,
                                 by=_login(), config=config)
                for j in queued:
                    click.echo("queued job %d: %s" % (j["id"], j["command"]), err=True)
                if wait and queued:
                    _wait([j["id"] for j in queued], config)
            doc = M.matrix(configuration, columns=M.parse_columns(columns), components=components, config=config)
        except Exception as e:                                    # noqa: BLE001
            if isinstance(e, M.MatrixUsage):
                click.echo("nanosoc-rm matrix: %s" % e, err=True)
                sys.exit(EXIT_USAGE)
            _fail(e)
        if as_json:
            click.echo(json.dumps(doc, separators=(",", ":")))
        else:
            _print_view(doc)
        sys.exit(_view_exit(doc))

    @grp.command("cell")
    @click.argument("configuration")
    @click.option("--column", required=True, type=click.Choice(["pinned", "in-range", "latest-tag", "head"]))
    @click.option("--component", default=None, help="The row (not with --column pinned).")
    @click.option("--store", is_flag=True, help="Store the result (the job always does).")
    @json_option
    def cell(configuration, column, component, store, as_json):
        """Run one cell now, in the foreground: compat T0 with the component moved, D8 re-run on FAIL."""
        from nanosoc_rm import jobs
        from nanosoc_rm.config import load_config
        from nanosoc_rm.matrix import cells as K
        try:
            config = load_config()
            job_id = os.environ.get("NANOSOC_RM_JOB")
            job = jobs.get(config, int(job_id)) if job_id and job_id.isdigit() else None
            docs = K.run_cell(configuration, column, component, config=config, store=store,
                              by=job["by"] if job else _login(), job=job["id"] if job else None)
        except K.CellUsage as e:
            click.echo("nanosoc-rm matrix cell: %s" % e, err=True)
            sys.exit(EXIT_USAGE)
        except Exception as e:                                    # noqa: BLE001
            _fail(e)
        cell_doc = docs[0]
        f = cell_doc["fact"]
        if as_json:
            for d in docs:
                click.echo(json.dumps(d, separators=(",", ":")))
        else:
            t = f["target"]
            click.echo("%s %s/%s -> %s %s" % (configuration, component or "(tree)", column,
                                             t.get("ref") or "", (t.get("commit") or "")[:12]))
            for a in f["attempts"]:
                click.echo("  attempt %s: %s (%s .. %s, load %s -> %s on %s cpus)%s" % (
                    a["nonce"][:12], a["verdict"], a["started_at"], a["finished_at"], a["host"]["load_start"],
                    a["host"]["load_end"], a["host"]["cpus"], ": " + a["reason"] if a["reason"] else ""))
            click.echo("verdict %s%s" % (f["verdict"], ": " + f["reason"] if f["reason"] else ""))
            if f["comparison"]:
                b = f["baseline"]
                click.echo("D9 baseline: %s" % ("pinned run %s (%s)" % (b["nonce"], b["verdict"]) if b
                                                else f["comparison"]["note"]))
                for r in f["comparison"]["rows"]:
                    if r["change"] != "same":
                        click.echo("  %s: %s -> %s (%s)" % (r["gate"], r["baseline"], r["cell"], r["change"]))
            for q in docs[1:]:
                click.echo("QUARANTINED %s (owner %s, expires %s)" % (q["fact"]["test"], q["fact"]["owner"],
                                                                     q["fact"]["expires_at"]))
        sys.exit(K.exit_for(f["verdict"]))

    @grp.command("quarantine")
    @click.argument("configuration")
    @click.option("--owner", "owner", default=None, help="GATE=LOGIN: name the owner of an active quarantine.")
    @json_option
    def quarantine(configuration, owner, as_json):
        """D8: tests quarantined after two FLAKY results in 14 days (30-day expiry), active and expired."""
        from nanosoc_rm import clock
        from nanosoc_rm.config import load_config
        from nanosoc_rm.matrix import cells as K
        try:
            config = load_config()
            if owner:
                test, sep, login = owner.partition("=")
                if not sep or not test or not login:
                    raise K.CellUsage("--owner expects GATE=LOGIN")
                K.set_owner(configuration, test.strip(), login.strip(), _login(), config=config)
            docs = K.quarantine(configuration, config)
        except K.CellUsage as e:
            click.echo("nanosoc-rm matrix quarantine: %s" % e, err=True)
            sys.exit(EXIT_USAGE)
        except Exception as e:                                    # noqa: BLE001
            _fail(e)
        now = clock.now()
        if as_json:
            for d in docs:
                click.echo(json.dumps(d, separators=(",", ":")))
        else:
            if not docs:
                click.echo("no quarantined test for %s" % configuration)
            for d in docs:
                f = d["fact"]
                state = "ACTIVE" if clock.parse(f["expires_at"]) > now else "expired"
                click.echo("%-8s %s owner %s since %s expires %s (%d FLAKY cells)" % (
                    state, f["test"], f["owner"], f["since"], f["expires_at"], len(f["flaky_cells"])))
        sys.exit(EXIT_OK)

    @grp.command("nightly")
    @click.argument("configuration")
    @json_option
    def nightly(configuration, as_json):
        """What the nightly matrix WOULD queue tonight. It ships disabled until the Phase 2 entry gate is met."""
        from nanosoc_rm import clock
        from nanosoc_rm import matrix as M
        try:
            plan = M.nightly_plan(configuration, clock.now())
        except Exception as e:                                    # noqa: BLE001
            _fail(e)
        out = {"name": M.NIGHTLY.name, "enabled": M.NIGHTLY.enabled, "gate": M.NIGHTLY.gate,
               "would_queue": [{"component": c, "column": col} for c, col in plan]}
        if as_json:
            click.echo(json.dumps(out, separators=(",", ":")))
        else:
            click.echo("nightly matrix: DISABLED until the %s. Nothing is scheduled." % M.NIGHTLY.gate)
            click.echo("It would queue %d cell(s) tonight:" % len(plan))
            for c, col in plan:
                click.echo("  %s/%s" % (c or "(tree)", col))
        sys.exit(EXIT_OK)
