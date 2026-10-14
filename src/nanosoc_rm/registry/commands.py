"""`nanosoc-rm registry ...` (interfaces.md section 2; PR4: each command is a front end over one function).

    registry validate [PATH] [--json]          exit 0 valid, 1 problems (`file:pointer: message [rule]`)
    registry imports  [PATH] [--json]          F5 census; exit 0 only when every source is censused and
                                               nothing is pending, missing or extra; 1 when the import is not
                                               done; 2 when a source document could not be read
    registry records  [PATH] [--json] [--store]  the registry_record facts (A3); --store writes them to the
                                               service's own database, one snapshot per file

PATH defaults to the service config's `[registry] path`. A PATH, or a configured path, that is not a directory
is a usage or config error (exit 3, section 0.2), not a finding about a registry.
"""
from __future__ import annotations

from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, emit, exit_code, json_option, subgroup

_PATH = click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))


def _registry_path(path: Path | None) -> Path:
    if path is not None:
        return path
    from nanosoc_rm.config import ConfigError, load_config
    config = load_config()
    root = Path(config.require("registry", "path")).expanduser()
    if not root.is_dir():
        raise ConfigError("%s: [registry] path %s is not a directory" % (config.path, root))
    return root


def _problem_row(doc: dict) -> dict:
    return {"problem": "%s:%s: %s [%s]" % (doc["file"], doc["pointer"], doc["message"], doc["rule"])}


def _census_row(doc: dict) -> dict:
    return {"path": doc["path"], "commit": doc["commit"][:12], "items": len(doc["census"]),
            "missing": ",".join(doc["missing"]), "extra": ",".join(doc["extra"]), "pending": doc["pending"],
            "done": "yes" if doc["done"] else "no", "error": doc["error"] or ""}


def _record_row(doc: dict) -> dict:
    f = doc["fact"]
    return {"kind": f["record_kind"], "id": f["id"], "subject": doc["subject"], "observed_at": doc["observed_at"]}


def register(main: click.Group) -> None:
    group = subgroup(main, "registry", help="The registry: validate it, census its imports, list its facts.")

    @group.command("validate")
    @_PATH
    @json_option
    def validate_cmd(path: Path | None, as_json: bool) -> int:
        """Load PATH (default: [registry] path) and check every file against its schema and the cross-file rules
        1 to 15. Exit 0 valid, 1 with every problem listed."""
        from nanosoc_rm.registry import RegistryError, load_registry
        root = _registry_path(path)
        try:
            reg = load_registry(root)
        except RegistryError as err:
            emit([p.as_doc() for p in err.problems], as_json, ("problem",), _problem_row)
            click.echo("registry %s refused: %d problem(s)" % (root, len(err.problems)), err=True)
            return EXIT_FINDING
        click.echo("registry %s valid at %s: %d components, %d configurations, %d defects, %d errata" % (
            root, reg.commit or ("an uncommitted tree" if reg.dirty else "no commit (not a git checkout)"),
            len(reg.components()), len(reg.configurations()), len(reg.defects()), len(reg.errata())), err=True)
        return EXIT_OK

    @group.command("imports")
    @_PATH
    @json_option
    def imports_cmd(path: Path | None, as_json: bool) -> int:
        """F5: census each imports.yaml source at its cited commit against the ledger. One row per source."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.registry import RegistryError, load_registry
        from nanosoc_rm.registry.imports import census, git_reader
        root = _registry_path(path)
        try:
            reg = load_registry(root)
        except RegistryError as err:
            for p in err.problems:
                click.echo(str(p), err=True)
            click.echo("registry %s refused, so its imports cannot be censused" % root, err=True)
            return EXIT_UNVERIFIED
        ledger = reg.imports()
        if ledger is None:
            click.echo("registry %s has no imports.yaml: nothing is imported (F5 not done)" % root, err=True)
            return EXIT_FINDING
        rows = [r.as_doc() for r in census(ledger, git_reader(reg, load_config()))]
        emit(rows, as_json, ("path", "commit", "items", "missing", "extra", "pending", "done", "error"), _census_row)
        unread = any(r["error"] and r["error"].startswith("cannot read") for r in rows)
        not_done = any(not r["done"] and not (r["error"] or "").startswith("cannot read") for r in rows)
        return exit_code(findings=not_done, unverified=unread)

    @group.command("records")
    @_PATH
    @click.option("--store", is_flag=True, help="Also store them in the service database (one snapshot per file).")
    @json_option
    def records_cmd(path: Path | None, store: bool, as_json: bool) -> int:
        """Every registry record as a registry_record observation, with its `git show` source (A3)."""
        from nanosoc_rm.registry import RegistryError, load_registry
        from nanosoc_rm.registry.records import RegistryUncommitted, store_records
        root = _registry_path(path)
        try:
            reg = load_registry(root)
            docs = reg.records()
        except RegistryError as err:
            for p in err.problems:
                click.echo(str(p), err=True)
            return EXIT_UNVERIFIED
        except RegistryUncommitted as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_UNVERIFIED
        if store:
            store_records(reg, observed_at=docs[0]["observed_at"] if docs else None)
        emit(docs, as_json, ("kind", "id", "subject", "observed_at"), _record_row)
        return EXIT_OK
