"""Registry records as facts (interfaces.md section 2 "Facts"; observation kind registry_record; A3, H1).

Every record a page shows (a configuration, component, defect, erratum or import source) is also a
`registry_record` observation, so registry facts carry an age and a command like every other fact:

    subject      registry_record:<file>#<pointer>
    snapshot     scope registry_record:<file>: one snapshot per file per registry read (README section 4)
    source       {kind: registry, command: "git -C <registry path> show <commit>:<file>"}
    observed_at  when the registry was read

    registry_records(registry, observed_at=None) -> [observation]   pure; needs registry.commit
    store_records(registry, config=None)          -> [snapshot id]  writes them, one snapshot per file

A registry read from a directory that is not a clean git checkout has no commit, and no command would
reproduce what was read, so it yields no facts: RegistryUncommitted.
"""
from __future__ import annotations

import secrets
import shlex
from typing import TYPE_CHECKING, Any

from nanosoc_rm import clock, contracts

if TYPE_CHECKING:
    from nanosoc_rm.registry.loader import Registry

__all__ = ["RegistryUncommitted", "registry_records", "store_records", "scope_of"]


class RegistryUncommitted(ValueError):
    """The registry was not read at a commit (not a git checkout, or dirty), so its records cannot be facts."""


def scope_of(file: str) -> str:
    return "registry_record:%s" % file


def _entries(registry: "Registry") -> list[tuple[str, str, str, str, dict]]:
    """(file, pointer, record_kind, id, record) for every record, in a stable order."""
    files = registry.files()
    out = []
    for i, c in enumerate(files["catalogue.yaml"]["components"]):
        out.append(("catalogue.yaml", "/components/%d" % i, "component", c["id"], c))
    for i, c in enumerate(files["eligibility.yaml"]["configurations"]):
        out.append(("eligibility.yaml", "/configurations/%d" % i, "configuration", c["id"], c))
    for f in sorted(files):
        if f.startswith("defects/"):
            out.append((f, "", "defect", files[f]["id"], files[f]))
    for f in sorted(files):
        if f.startswith("errata/"):
            out.append((f, "", "erratum", files[f]["id"], files[f]))
    if "imports.yaml" in files:
        for i, s in enumerate(files["imports.yaml"]["sources"]):
            out.append(("imports.yaml", "/sources/%d" % i, "import_source",
                        "%s:%s" % (s["doc"]["component"], s["doc"]["path"]), s))
    return out


def registry_records(registry: "Registry", observed_at: str | None = None) -> list[dict]:
    """One registry_record observation per record, each valid against the observation schema."""
    if not registry.commit:
        raise RegistryUncommitted("registry %s was not read at a commit (%s), so its records cannot cite a "
                                  "`git show` source" % (registry.path, "dirty checkout" if registry.dirty
                                                         else "not a git checkout"))
    at = observed_at or clock.iso()
    run = secrets.token_hex(4)
    out = []
    for file, pointer, kind, rid, record in _entries(registry):
        doc: dict[str, Any] = {
            "schema": 1,
            "kind": "registry_record",
            "subject": "registry_record:%s#%s" % (file, pointer),
            "observed_at": at,
            "sources": [{"kind": "registry", "command": shlex.join(
                ["git", "-C", str(registry.path), "show", "%s:%s%s" % (registry.commit, registry.prefix, file)])}],
            "snapshot": {"id": "%s@%s#%s" % (scope_of(file), at, run), "scope": scope_of(file)},
            "fact": {"record_kind": kind, "id": rid, "file": file, "pointer": pointer, "commit": registry.commit,
                     "record": record},
        }
        contracts.require_valid("observation", doc)
        out.append(doc)
    return out


def store_records(registry: "Registry", *, config: Any = None, observed_at: str | None = None) -> list[str]:
    """Store the registry's records, one complete snapshot per file. A file the previous read had and this one
    lacks (a deleted erratum, say) gets an empty snapshot, so its records stop being current (README section 4).
    Returns the snapshot ids written."""
    from nanosoc_rm import db
    docs = registry_records(registry, observed_at=observed_at)
    by_scope: dict[str, list[dict]] = {}
    for d in docs:
        by_scope.setdefault(d["snapshot"]["scope"], []).append(d)
    stale = {d["snapshot"]["scope"] for d in db.latest("registry_record", config=config)} - set(by_scope)
    ids = [db.put_snapshot(scope, rows, config=config) for scope, rows in by_scope.items()]
    ids += [db.put_snapshot(scope, [], config=config) for scope in sorted(stale)]
    return ids
