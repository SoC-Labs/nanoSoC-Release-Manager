"""`nanosoc-rm catalogue ...` (interfaces.md section 23; PR4: front ends over one function each).

    catalogue show     [PATH] [--group G]... [--gaps] [--unconfirmed] [--json]
                       one row per component: group, type, role, wraps, wrapped_by, unconfirmed fields, the open
                       question; `--gaps` keeps the SoCs no forkable project wraps. Exit 0; 1 with --gaps when
                       any gap exists; 2 with --gaps when a soc_ref entry has no stated group (the check cannot
                       see an uncharacterised SoC, so "no gap" is UNVERIFIED).
    catalogue org-scan [--host github|gitlab]... [--store] [--json]
                       anonymous listing of the org and the group; one repo_seen per repo not in the catalogue.
                       Exit 0 nothing new, 1 new repos seen, 2 a host could not be listed.
    catalogue preview  CHOICES [--registry PATH] [--out FILE] [--json]
                       the catalogue.yaml patch for a JSON list of choices (file, or - for stdin). Exit 0 the
                       patched catalogue would load, 1 it would be refused (problems listed), 3 a bad choice.

PATH and --registry default to the service config's `[registry] path`. Nothing here writes the registry: `--out`
under the registry directory is refused (exit 3); the person applies the patch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup

__all__ = ["register"]


def _registry(path: Path | None):
    from nanosoc_rm.registry.commands import _registry_path
    from nanosoc_rm.registry import load_registry
    return load_registry(_registry_path(path))


def _show_row(doc: dict) -> dict:
    return {"id": doc["id"], "group": doc["group"], "type": doc["type"] or "", "role": doc["role"] or "",
            "wraps": ",".join(doc["wraps"]), "inside": ",".join(doc["wrapped_by"]),
            "unconfirmed": ",".join(doc["unconfirmed"]), "gap": "GAP" if doc["gap"] else "",
            "question": doc["question"] or ""}


def _seen_row(doc: dict) -> dict:
    f = doc["fact"]
    return {"repo": "%s:%s" % (f["repo"]["host"], f["repo"]["path"]), "created": f["created_on"] or "",
            "nanosoc": "yes" if f["mentions_nanosoc"] else "", "first_seen": f["first_seen"]}


def register(main: click.Group) -> None:
    group = subgroup(main, "catalogue", help="Catalogue characterisation: groups and types, new repos, change previews.")

    @group.command("show")
    @click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option("--group", "groups", multiple=True, help="Only these groups (repeatable).")
    @click.option("--gaps", is_flag=True, help="Only SoCs that no forkable project wraps.")
    @click.option("--unconfirmed", is_flag=True, help="Only entries with a guessed or derived field.")
    @json_option
    def show_cmd(path: Path | None, groups: tuple[str, ...], gaps: bool, unconfirmed: bool, as_json: bool) -> int:
        """Every component as the front page groups it (section 23.1)."""
        from nanosoc_rm.catalogue.model import GROUPS, gap_unverified, socs_without_forkable_project
        bad = [g for g in groups if g not in GROUPS]
        if bad:
            raise click.UsageError("--group %s is not one of %s" % (bad[0], ", ".join(GROUPS)))
        chars = _registry(path).characterisations()
        gap = set(socs_without_forkable_project(chars))
        docs = [dict(ch.as_doc(), gap=ch.id in gap) for ch in chars.values()
                if (not groups or ch.group in groups) and (not gaps or ch.id in gap)
                and (not unconfirmed or ch.unconfirmed)]
        emit(docs, as_json, ("id", "group", "type", "role", "wraps", "inside", "unconfirmed", "gap", "question"),
             _show_row)
        if gaps:
            blind = gap_unverified(chars)
            if blind:
                click.echo("catalogue show: gap check UNVERIFIED: %d soc_ref entr%s with no stated group (%s); "
                           "an uncharacterised SoC reads as a project" % (
                               len(blind), "y" if len(blind) == 1 else "ies", ", ".join(blind)), err=True)
                return EXIT_UNVERIFIED
        return EXIT_FINDING if gaps and docs else EXIT_OK

    @group.command("org-scan")
    @click.option("--host", "hosts", multiple=True, type=click.Choice(["github", "gitlab"]),
                  help="Scan only this host (repeatable; default every host with an org or group configured).")
    @click.option("--store", is_flag=True, help="Store one repo_seen snapshot per host listed.")
    @json_option
    def org_scan_cmd(hosts: tuple[str, ...], store: bool, as_json: bool) -> int:
        """List the org and the group anonymously; report each repo the catalogue does not hold (section 23.2)."""
        from nanosoc_rm.catalogue.orgscan import org_scan
        res = org_scan(hosts=hosts or None, store=store)
        emit(res.docs, as_json, ("repo", "created", "nanosoc", "first_seen"), _seen_row)
        listed = ", ".join("%s %d" % (h, n) for h, n in res.listed.items()) or "none"
        for host, why in res.errors.items():
            click.echo("org-scan: could not list %s: %s" % (host, why), err=True)
        click.echo("org-scan: %d new repo(s) not in the catalogue; listed (anonymous): %s%s" % (
            len(res.docs), listed, "; could not list %s" % ", ".join(res.errors) if res.errors else ""), err=True)
        if res.errors:
            return EXIT_UNVERIFIED
        return EXIT_FINDING if res.docs else EXIT_OK

    @group.command("preview")
    @click.argument("choices", type=click.File("r"))
    @click.option("--registry", "path", type=click.Path(exists=True, file_okay=False, path_type=Path),
                  help="The registry checkout (default: [registry] path).")
    @click.option("--out", type=click.Path(dir_okay=False, path_type=Path), help="Also write the patch here.")
    @json_option
    def preview_cmd(choices, path: Path | None, out: Path | None, as_json: bool) -> int:
        """The catalogue.yaml patch for a JSON list of choices (section 23.3). Writes nothing to the registry."""
        from nanosoc_rm.catalogue.preview import PreviewUsage, preview
        reg = _registry(path)
        try:
            data = json.load(choices)
        except ValueError as e:
            raise click.UsageError("CHOICES is not JSON: %s" % e)
        if out is not None:
            o, r = out.expanduser().resolve(), Path(reg.path).resolve()
            if o == r or r in o.parents:
                raise click.UsageError("--out %s is inside the registry: the tool never writes the registry" % out)
        try:
            p = preview(reg, data)
        except PreviewUsage as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE
        if out is not None:
            out.write_text(p.patch, encoding="utf-8")
        if as_json:
            click.echo(json.dumps(p.as_doc()))
        else:
            sys.stdout.write(p.patch)
            for c in p.changes:
                click.echo("change: %s" % c, err=True)
            for pr in p.problems:
                click.echo("problem: %s" % pr, err=True)
            click.echo("preview: %d change(s); the patched catalogue %s" % (
                len(p.changes), "loads" if p.valid else "would be refused (%d problem(s))" % len(p.problems)),
                err=True)
        return EXIT_OK if p.valid else EXIT_FINDING
