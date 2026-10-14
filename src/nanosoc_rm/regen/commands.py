"""CLI front ends of the regenerator (interfaces.md section 20). No logic of their own.

    nanosoc-rm regen run     CONFIG --component C --commit SHA [--at TREE_PATH]... [--out DIR] [--fresh] [--json]
    nanosoc-rm regen class   CONFIG --component C --from SHA --to SHA [--at TREE_PATH]... [--store] [--fresh] [--json]
    nanosoc-rm regen pending [--run] [--json]      moves a plan or bump would class, not yet measured (--run: measure)

`run` regenerates one configuration with the component at one commit and prints the render; `--out DIR` (new or
empty) keeps the sandbox, whose `tree/<output>` is the regenerated build_soc. `class` measures the change class of
a move (C3 3/3, C7). Exit: 0 PASS, 1 FAIL (the generator failed), 2 INFRA or UNVERIFIED (not run here), 3 usage.
"""
from __future__ import annotations

import re
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup, table

__all__ = ["register"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_EXIT = {"PASS": EXIT_OK, "FAIL": EXIT_FINDING, "INFRA": EXIT_UNVERIFIED, "UNVERIFIED": EXIT_UNVERIFIED}


def _sha(value: str, what: str) -> str:
    if not _SHA.match(value or ""):
        raise click.UsageError("%s %r is not a full 40-hex commit id" % (what, value))
    return value


def _guard(fn):
    from nanosoc_rm.ci.trigger import TriggerError
    from nanosoc_rm.regen.spec import RegenUsage
    from nanosoc_rm.walker import UnknownConfiguration
    try:
        return fn(), None
    except (RegenUsage, UnknownConfiguration) as e:
        click.echo("nanosoc-rm: %s" % e, err=True)
        return None, EXIT_USAGE
    except TriggerError as e:
        click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
        return None, EXIT_UNVERIFIED


def _config():
    from nanosoc_rm.config import load_config
    return load_config()


def _print_render(r: dict) -> None:
    click.echo("render %s: %s (%s)%s" % (r["commit"][:12], r["status"], r["verdict"],
                                        "; " + r["reason"] if r.get("reason") else ""))
    if r["status"] == "ok":
        click.echo("  %d file(s), %d left out (binary or large), digest %s, %s s%s"
                   % (r["files"], r["binary"], r["digest"][:16], r["seconds"], " (cached)" if r["cached"] else ""))
    for c in r["coverage"][:8]:
        click.echo("  coverage: %s" % c)


def register(main: click.Group) -> None:
    from nanosoc_rm.regen import jobkind  # noqa: F401 - registers the regen-class job kind
    regen = subgroup(main, "regen", help="Regenerate a configuration's generated output in a sandbox; class a "
                                         "move by the diff (C3, C7; section 20).")

    @regen.command("run")
    @click.argument("configuration")
    @click.option("--component", required=True)
    @click.option("--commit", "commit", required=True)
    @click.option("--at", "at", multiple=True, help="Tree path to move (default: every pin of the component).")
    @click.option("--out", "out", default=None, help="Keep the sandbox in this new or empty directory.")
    @click.option("--fresh", is_flag=True, help="Ignore the render cache.")
    @json_option
    def run_cmd(configuration, component, commit, at, out, fresh, as_json):
        """Regenerate CONFIGURATION with COMPONENT at COMMIT (never stored; a render is not a fact)."""
        from nanosoc_rm.regen import sandbox
        _sha(commit, "--commit")
        if out is not None:
            p = Path(out)
            if p.exists() and (not p.is_dir() or any(p.iterdir())):
                raise click.UsageError("--out %s: must be a new or empty directory" % out)
        cfg = _config()
        r, rc = _guard(lambda: sandbox.render(configuration, component, commit, config=cfg, paths=list(at) or None,
                                              fresh=fresh, keep_work=Path(out) if out else None))
        if rc is not None:
            return rc
        s = dict(r.summary(), configuration=configuration, component=component, tree_paths=r.tree_paths,
                 root_commit=r.root_commit)
        if as_json:
            emit([s], True)
        else:
            click.echo("%s: %s at %s (tree paths %s; %s @ %s)" % (configuration, component, commit[:12],
                                                                  ", ".join(r.tree_paths), configuration,
                                                                  (r.root_commit or "-")[:12]))
            _print_render(s)
            if out:
                click.echo("sandbox kept: %s" % out)
        return _EXIT[r.verdict]

    @regen.command("class")
    @click.argument("configuration")
    @click.option("--component", required=True)
    @click.option("--from", "frm", required=True)
    @click.option("--to", "to", required=True)
    @click.option("--at", "at", multiple=True, help="Tree path to move (default: every pin of the component).")
    @click.option("--store", is_flag=True, help="Store the regen_class observation (the classifier reads it).")
    @click.option("--fresh", is_flag=True, help="Ignore the render cache.")
    @json_option
    def class_cmd(configuration, component, frm, to, at, store, fresh, as_json):
        """The change class of moving COMPONENT from --from to --to in CONFIGURATION, by regenerating both."""
        from nanosoc_rm.regen import measure
        _sha(frm, "--from")
        _sha(to, "--to")
        cfg = _config()
        obs, rc = _guard(lambda: measure.measure(configuration, component, frm, to, config=cfg, store=store,
                                                 paths=list(at) or None, fresh=fresh))
        if rc is not None:
            return rc
        f = obs["fact"]
        if as_json:
            emit([obs], True)
        else:
            click.echo("%s: %s %s -> %s: class %s (%s)%s" % (configuration, component, frm[:12], to[:12],
                                                             f["class"] or "unmeasured", f["verdict"],
                                                             "; " + f["reason"] if f["reason"] else ""))
            for side in ("from", "to"):
                _print_render(f[side])
            if f["files"]:
                c = f["files"]["counts"]
                click.echo("files: %d added, %d removed, %d changed, %d unchanged; build-affecting (C7): %s"
                           % (c["added"], c["removed"], c["changed"], c["unchanged"], f["build_affecting"]))
            for r in f["reasons"][:20]:
                click.echo("  %s" % r)
            if store:
                click.echo("stored %s" % obs["subject"])
        return measure.EXIT[f["verdict"]]

    @regen.command("pending")
    @click.option("--run", "run", is_flag=True, help="Measure each pending move now (stored).")
    @json_option
    def pending_cmd(run, as_json):
        """Moves a stored version plan or bump proposal would class by regeneration, not yet measured."""
        from nanosoc_rm.regen import measure
        cfg = _config()
        rows = measure.pending(cfg)
        worst = EXIT_OK
        if run:
            for r in rows:
                obs, rc = _guard(lambda r=r: measure.measure(r["configuration"], r["component"], r["from"], r["to"],
                                                             config=cfg, store=True))
                r["verdict"] = obs["fact"]["verdict"] if obs else "usage"
                r["class"] = obs["fact"]["class"] if obs else None
                worst = max(worst, rc if rc is not None else measure.EXIT[obs["fact"]["verdict"]])
        if as_json:
            emit(rows, True)
        else:
            click.echo(table([dict(r, **{"from": r["from"][:12], "to": r["to"][:12]}) for r in rows],
                             ("configuration", "component", "from", "to", "why") + (("verdict", "class") if run
                                                                                   else ())) if rows else
                       "nothing pending: every move a plan or bump would class is measured (or none declares a "
                       "regen command)")
        return worst
