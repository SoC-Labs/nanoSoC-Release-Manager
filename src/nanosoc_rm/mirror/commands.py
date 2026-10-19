"""CLI of the mirror module (interfaces.md section 3):

    nanosoc-rm mirror sync [--component ID] [--host H] [--json]   exit 1 a rewrite was found, 2 a fetch failed
    nanosoc-rm mirror status [--component ID] [--json]           the table prints each repo's `age` in seconds (H6)
                                                                  and how it was read (`access`);
                                                                  exit 2 when a mirror's last fetch failed
    nanosoc-rm mirror status --summary [--json]                  one line per (access, cause, method): how many
                                                                  repos are public, private-and-read, unreadable
    nanosoc-rm inventory [--unclassified] [--json]               exit 1 if any repo is unclassified (A1), 2 when a
                                                                  host listing could not be read
"""
from __future__ import annotations

import click

from nanosoc_rm import clock
from nanosoc_rm.cliutil import EXIT_UNVERIFIED, EXIT_USAGE, emit, exit_code, json_option

_HOSTS = click.Choice(["github", "gitlab"])


def _age(fact: dict, now) -> str:
    if not fact.get("last_success_at"):
        return "-"
    return str(max(0, int((now - clock.parse(fact["last_success_at"])).total_seconds())))


def access_cell(fact: dict) -> str:
    """One word: anonymous, credentials:<method> (private, readable with credentials), or unreadable:<cause>.
    A row stored before private reads existed has no `access`: ok reads anonymous, a failure
    unreadable:<cause from its error>."""
    from nanosoc_rm.mirror.private_read import classify
    access = fact.get("access") or ("anonymous" if fact["ok"] else "unreadable")
    if access == "credentials":
        return "credentials:%s" % (fact.get("method") or "?")
    if access == "unreadable":
        return "unreadable:%s" % (fact.get("cause") or classify(fact.get("error")))
    return access


def status_table(docs: list[dict], now=None) -> str:
    """The `mirror status` table. Header names are lower case; every cell but the last is one word, so a reader
    can split rows on whitespace; `age` is now - last_success_at in whole seconds ("-" if never fetched)."""
    now = now or clock.now()
    cols = ("host", "path", "component", "ok", "age", "last_success_at", "access", "error")
    rows = [[d["fact"]["repo"]["host"], d["fact"]["repo"]["path"], d["fact"]["component"] or "-",
             "yes" if d["fact"]["ok"] else "NO", _age(d["fact"], now), d["fact"]["last_success_at"] or "-",
             access_cell(d["fact"]), d["fact"]["error"] or ""] for d in docs]
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()]
    lines += ["  ".join(r[i].ljust(widths[i]) for i in range(len(cols))).rstrip() for r in rows]
    return "\n".join(lines)


def _sync_row(doc: dict) -> dict:
    f = doc["fact"]
    if doc["kind"] == "rewrite_alert":
        return {"kind": "REWRITE", "repo": "%s:%s" % (f["repo"]["host"], f["repo"]["path"]), "component": f["component"],
                "result": "%s %s -> %s, %d lost" % (f["branch"], f["old_head"][:12], f["new_head"][:12],
                                                    f["lost_commits"])}
    return {"kind": "mirror", "repo": "%s:%s" % (f["repo"]["host"], f["repo"]["path"]), "component": f["component"],
            "result": "ok" if f["ok"] else "FAILED: %s" % f["error"]}


def _inventory_row(doc: dict) -> dict:
    f = doc["fact"]
    return {"repo": "%s:%s" % (f["repo"]["host"], f["repo"]["path"]), "role": f["role"],
            "component": f["component"] or "", "visibility": f["visibility"], "default_branch": f["default_branch"]}


def register(main: click.Group) -> None:
    @main.group("mirror")
    def mirror_group() -> None:
        """Local mirrors of every catalogued repo: heads and tags only (H6)."""

    @mirror_group.command("sync")
    @click.option("--component", "component", default=None, help="Sync one component or external.")
    @click.option("--host", "host", type=_HOSTS, default=None, help="Only the repos on this host.")
    @json_option
    def sync_cmd(component: str | None, host: str | None, as_json: bool) -> int:
        """Fetch heads and tags of every catalogued repo (or one component's) into its mirror."""
        from nanosoc_rm.mirror.sync import sync_all, sync_component
        if component is not None:
            try:
                docs = sync_component(component, host)
            except KeyError:
                click.echo("nanosoc-rm: %s is not a catalogue component or external" % component, err=True)
                return EXIT_USAGE
        else:
            docs = sync_all(host)
        emit(docs, as_json, ("kind", "repo", "component", "result"), _sync_row)
        return exit_code(findings=any(d["kind"] == "rewrite_alert" for d in docs),
                         unverified=any(d["kind"] == "mirror_status" and not d["fact"]["ok"] for d in docs))

    @mirror_group.command("status")
    @click.option("--component", "component", default=None, help="One component or external.")
    @click.option("--summary", is_flag=True, help="Count repos by how they were read and why a read failed.")
    @json_option
    def status_cmd(component: str | None, summary: bool, as_json: bool) -> int:
        """The latest mirror_status of every mirrored repo, with its age (now - last_success_at, seconds)."""
        from nanosoc_rm.mirror.sync import access_summary, mirror_status
        docs = mirror_status(component)
        if summary:
            rows = access_summary(docs)
            emit(rows, as_json, ("count", "access", "cause", "method", "hosts"),
                 lambda r: {"count": r["count"], "access": r["access"], "cause": r["cause"] or "-",
                            "method": r["method"] or "-",
                            "hosts": ", ".join("%d %s" % (n, h) for h, n in sorted(r["by_host"].items()))})
        elif as_json:
            emit(docs, True)
        else:
            click.echo(status_table(docs))
        return EXIT_UNVERIFIED if any(not d["fact"]["ok"] for d in docs) else 0

    @main.command("inventory")
    @click.option("--unclassified", is_flag=True, help="Only the repos in no catalogue entry (flagged, A1).")
    @json_option
    def inventory_cmd(unclassified: bool, as_json: bool) -> int:
        """Every repo in the GitHub org and the GitLab group (with subgroups), classified by the catalogue."""
        from nanosoc_rm.mirror.inventory import InventoryError, inventory
        try:
            docs = inventory()
        except InventoryError as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_UNVERIFIED
        flagged = [d for d in docs if d["fact"]["role"] == "unclassified"]
        emit(flagged if unclassified else docs, as_json, ("repo", "role", "component", "visibility", "default_branch"),
             _inventory_row)
        return exit_code(findings=bool(flagged))
