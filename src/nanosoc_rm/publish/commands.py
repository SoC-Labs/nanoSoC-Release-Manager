"""CLI: `nanosoc-rm publish-release` and `nanosoc-rm register-tapeout` (CR-ART-publish; G8, D12).

Two steps, always: `--dry-run` builds the record, reads the store (GET/HEAD only) and prints the preview with the
record's sha256; `--confirm SHA256` publishes exactly that record (refused when anything changed since). Exit codes
(interfaces.md section 0.2): 0 previewed or published (a retry that found the same bytes is published too);
1 refused (a path holding other bytes, a changed record, a draft not ready, a read-back or signature mismatch);
2 could not answer (no store, token or signing key; the store unreachable); 3 usage.
"""
from __future__ import annotations

import json

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, json_option

__all__ = ["register"]


def _show_preview(doc: dict) -> None:
    click.echo("%s %s -> %s" % (doc["kind"], doc["key"], doc["store_path"]))
    click.echo("record sha256 %s" % doc["sha256"])
    for t in doc["targets"]:
        click.echo("  %-8s %s" % (t["state"], t["path"]))
    if doc["build"]:
        b = doc["build"]
        click.echo("build %s/%s: %s; promotion: %s" % (b["name"], b["number"], b["state"], b["status"] or "none"))
    click.echo("changes since the last %s of %s:" % (doc["kind"], doc["project"]))
    for line in doc["diff"][:200]:
        click.echo("  %s" % line)
    for p in doc["problems"]:
        click.echo("problem: %s" % p)
    click.echo("To publish exactly this: --confirm %s" % doc["sha256"])


def _run(kind: str, key: str, dry_run: bool, confirm: str | None, by: str | None, artefacts, draft, as_json) -> int:
    from nanosoc_rm.publish.core import PublishError, PublishRefused, PublishUsage
    from nanosoc_rm.publish.service import preview, publish
    from nanosoc_rm.publish.client import StoreError, StoreRefused
    from nanosoc_rm.publish.manifest import parse_artefact
    from nanosoc_rm.publish.signing import SignatureError
    name = "publish-release" if kind == "release" else "register-tapeout"
    if dry_run == bool(confirm):
        click.echo("nanosoc-rm %s: give exactly one of --dry-run (preview) or --confirm SHA256 (publish what the "
                   "preview showed)" % name, err=True)
        return EXIT_USAGE
    try:
        arts = [parse_artefact(a) for a in artefacts]
        if dry_run:
            doc = preview(None, kind, key, by=by, artefacts=arts, draft=draft)
            if as_json:
                click.echo(json.dumps(doc, separators=(",", ":"), sort_keys=True))
            else:
                _show_preview(doc)
            return EXIT_OK
        obs = publish(None, kind, key, confirm=confirm, by=by, artefacts=arts, draft=draft)
    except PublishUsage as e:
        click.echo("nanosoc-rm %s: %s" % (name, e), err=True)
        return EXIT_USAGE
    except (PublishRefused, StoreRefused, SignatureError) as e:
        click.echo("nanosoc-rm %s: refused: %s" % (name, e), err=True)
        return EXIT_FINDING
    except (PublishError, StoreError) as e:
        click.echo("nanosoc-rm %s: could not answer: %s" % (name, e), err=True)
        return EXIT_UNVERIFIED
    f = obs["fact"]
    if as_json:
        click.echo(json.dumps(obs, separators=(",", ":")))
    else:
        click.echo("%s %s: %s (sha256 %s), signed %s, build %s/%s %s" % (
            f["outcome"], f["store_path"], "verified" if f["verified"]["signature"] else "NOT verified",
            f["manifest_sha256"], f["signature"]["fingerprint"], f["build"]["name"], f["build"]["number"],
            f["build"]["status"] or "(no promotion)"))
        for n in f.get("notes") or []:
            click.echo("note: %s" % n)
    return EXIT_OK


def register(main: click.Group) -> None:
    common = [click.option("--dry-run", is_flag=True, help="Build and check; print the preview and its sha256."),
              click.option("--confirm", default=None, metavar="SHA256",
                           help="Publish the record whose sha256 the preview printed (refused if it changed)."),
              click.option("--by", default=None, help="The GUI login this publish is for (a listed publisher); "
                                                      "default cli:<your user name>."),
              json_option]

    def opts(f):
        for o in reversed(common):
            f = o(f)
        return f

    @main.command("publish-release")
    @click.argument("release", metavar="CONFIGURATION/VERSION")
    @click.option("--artefact", "artefacts", multiple=True, metavar="ROLE=REPO/PATH",
                  help="A file already in the store this release references (archive, gds, netlist, bitstream, logs, "
                       "flists, signoff, foundry-return, doc, other); read with GET, joined by raw md5.")
    @opts
    def publish_release(release, artefacts, dry_run, confirm, by, as_json):
        """Publish a ready local release draft's manifest to the release store (asic-release/<project>/<version>/):
        documents, release-manifest.json and its signature, a build-info record promoted RELEASED."""
        return _run("release", release.strip("/"), dry_run, confirm, by, artefacts, None, as_json)

    @main.command("register-tapeout")
    @click.argument("tapeout_id")
    @click.option("--draft", default=None, help="The tapeout draft file (default: <registry>/tapeouts/ID.tapeout.yaml).")
    @opts
    def register_tapeout(tapeout_id, draft, dry_run, confirm, by, as_json):
        """Publish a tapeout record (tapeout-<id>.json and its signature) beside its release; promoted TAPED-OUT
        once submitted."""
        return _run("tapeout", tapeout_id, dry_run, confirm, by, (), draft, as_json)
