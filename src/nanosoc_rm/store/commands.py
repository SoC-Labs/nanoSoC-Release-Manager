"""`nanosoc-rm store ...`: the artefact store index (Wave E; interfaces.md section 22).

    store index [--store] [--no-cache] [--json]   read the store (GET, HEAD, AQL) and print the store_index fact
    store show [--json]                           the stored index: probes, counts, sections
    store search QUERY [--json]                   stored rows whose path, name, run tag, commit or md5 matches
    store findings [--json]                       store_mismatch / release_not_archived / tapeout_unregistered

Exit codes (section 0.2): 0 answered (index PASS; no finding), 1 a finding (`findings`), 2 could not answer
(index UNVERIFIED or INFRA, nothing stored yet), 3 usage or config error (no [stores.artifactory], no token file).
"""
from __future__ import annotations

import json

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, emit, json_option, subgroup, table

__all__ = ["register"]


def register(main: click.Group) -> None:
    grp = subgroup(main, "store", help="The artefact store index: what Artifactory holds (read-only).")

    @grp.command("index")
    @click.option("--store", "do_store", is_flag=True, help="Store the index as the store_index fact.")
    @click.option("--no-cache", is_flag=True, help="Do not download documents for the GUI's download links.")
    @json_option
    def index_cmd(do_store: bool, no_cache: bool, as_json: bool) -> int:
        """Read the store with GET, HEAD and AQL only, probe each feature it relies on, print the index."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.store.index import index
        config = load_config()
        doc = index(config, store=do_store, cache=not no_cache)
        f = doc["fact"]
        if as_json:
            click.echo(json.dumps(doc, separators=(",", ":")))
        else:
            click.echo("store %s: %s%s" % (f["store"], f["verdict"], (" (%s)" % f["reason"]) if f["reason"] else ""))
            click.echo(table(f["probes"], ("feature", "verdict", "detail")))
            c = f["counts"]
            click.echo("files %d, with properties %d, with a git commit %d, with a repository %d; rows kept: %d "
                       "artefacts, %d foundry returns, %d builds, %d manifests%s"
                       % (c["files"], c["with_properties"], c["with_commit"], c["with_repo"], len(f["artefacts"]),
                          len(f["foundry"]), len(f["builds"]), len(f["manifests"]),
                          "; stored" if do_store else ""))
        return EXIT_OK if f["verdict"] == "PASS" else EXIT_UNVERIFIED

    @grp.command("show")
    @json_option
    def show_cmd(as_json: bool) -> int:
        """The stored index (no store call)."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.store import views
        doc = views.latest(load_config())
        if doc is None:
            click.echo("no store index is stored: run `nanosoc-rm store index --store`", err=True)
            return EXIT_UNVERIFIED
        if as_json:
            click.echo(json.dumps(doc, separators=(",", ":")))
        else:
            f = doc["fact"]
            click.echo("store %s indexed %s: %s" % (f["store"], doc["observed_at"], f["verdict"]))
            click.echo(table([dict(r, roles=", ".join("%s %d" % kv for kv in sorted(r["roles"].items())))
                              for r in f["counts"]["repos"]], ("repo", "files", "bytes", "roles")))
        return EXIT_OK if doc["fact"]["verdict"] == "PASS" else EXIT_UNVERIFIED

    @grp.command("search")
    @click.argument("query")
    @json_option
    def search_cmd(query: str, as_json: bool) -> int:
        """Stored rows matching an artefact path, a name, a run tag, a commit or an md5 prefix."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.store import views
        config = load_config()
        if views.latest(config) is None:
            click.echo("no store index is stored: run `nanosoc-rm store index --store`", err=True)
            return EXIT_UNVERIFIED
        hits = views.search(config, query, limit=200)
        if as_json:
            for h in hits:
                click.echo(json.dumps(h, separators=(",", ":")))
        else:
            click.echo(table(hits, ("kind", "label", "sub")))
        return EXIT_OK

    @grp.command("findings")
    @json_option
    def findings_cmd(as_json: bool) -> int:
        """What the inbox shows from the store: mismatches, releases not archived, unregistered tapeouts."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.store import views
        config = load_config()
        if views.latest(config) is None:
            click.echo("no store index is stored: run `nanosoc-rm store index --store`", err=True)
            return EXIT_UNVERIFIED
        rows = views.findings(config)
        emit(rows, as_json, ("kind", "about", "title"), row=lambda r: r)
        return EXIT_FINDING if rows else EXIT_OK
