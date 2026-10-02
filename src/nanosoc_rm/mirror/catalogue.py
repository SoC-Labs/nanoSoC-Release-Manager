"""The slice of the registry the mirror, inventory and walker read: catalogue repos and configurations.

The registry checkout is `[registry] path` of the service config (interfaces.md section 1). Its `catalogue.yaml`
and `eligibility.yaml` are read strictly (contracts.load_yaml: timestamps stay strings, duplicate keys refused) and
validated against their schemas by the registry loader (`nanosoc_rm.registry.load_registry`, interfaces.md
section 2), so a registry with any cross-file problem is never used ("never partial"); its error is raised as is.

Repo roles (A1, B9): `canonical` and `copy` are independent repos of a component; an `alias` is another name of
the same repo (a host redirect), so it is never mirrored separately; an `external` is a third-party repo pinned in a
registered tree; an `ignored` repo is listed by the inventory and never mirrored or walked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.registry import load_registry

__all__ = ["Catalogue", "Entry", "CatalogueError", "load_catalogue"]


class CatalogueError(ConfigError):
    """The registry checkout cannot be read, or a file breaks its schema. A usage/config error (exit 3)."""


@dataclass
class Entry:
    """A component or an external: what the mirror syncs (one mirror per entry)."""
    id: str
    kind: str                                   # "component" | "external"
    cls: str | None                             # catalogue class; None for an external
    repos: list[tuple[dict, str]]               # (repo, role) for canonical, copies (or the external's repo)
    aliases: list[dict] = field(default_factory=list)
    known_rewrites: list[dict] = field(default_factory=list)

    @property
    def canonical(self) -> dict:
        return self.repos[0][0]

    def repo_on(self, host: str) -> tuple[dict, str] | None:
        """(repo, role) of this entry on `host` (canonical first), or None."""
        return next(((r, role) for r, role in self.repos if r["host"] == host), None)


def _fold(host: str, path: str) -> tuple[str, str]:
    return host, path.casefold()                # both hosts treat repo paths case-insensitively


class Catalogue:
    def __init__(self, catalogue: dict, eligibility: dict | None, path: Path | None = None):
        self.path = path
        self.raw = catalogue
        self._entries: dict[str, Entry] = {}
        self._index: dict[tuple[str, str], tuple[str | None, str, dict]] = {}
        for c in catalogue.get("components", []):
            e = Entry(c["id"], "component", c.get("class"),
                      [(c["canonical"], "canonical")] + [(r, "copy") for r in c.get("copies", [])],
                      list(c.get("aliases", [])), list(c.get("known_rewrites", [])))
            self._entries[e.id] = e
        for x in catalogue.get("external", []):
            self._entries[x["id"]] = Entry(x["id"], "external", None, [(x["repo"], "external")])
        for e in self._entries.values():
            for r, role in e.repos:
                self._index.setdefault(_fold(r["host"], r["path"]), (e.id, role, r))
            for a in e.aliases:
                self._index.setdefault(_fold(a["host"], a["path"]), (e.id, "alias", a))
        for g in catalogue.get("ignored", []):
            self._index.setdefault(_fold(g["repo"]["host"], g["repo"]["path"]), (None, "ignored", g["repo"]))
        self.configurations_list = list((eligibility or {}).get("configurations", []))

    # -- entries -------------------------------------------------------------------------------------------
    def entries(self) -> list[Entry]:
        """Components then externals, in catalogue order: everything the mirror syncs."""
        return list(self._entries.values())

    def entry(self, entry_id: str) -> Entry:
        if entry_id not in self._entries:
            raise KeyError(entry_id)
        return self._entries[entry_id]

    def has(self, entry_id: str) -> bool:
        return entry_id in self._entries

    # -- repos ---------------------------------------------------------------------------------------------
    def resolve(self, host: str, path: str) -> tuple[str | None, str, dict | None]:
        """(entry id or None, role, the catalogue's spelling of the repo) for a repo on a host. Role is one of
        canonical, copy, alias, external, ignored, unclassified."""
        hit = self._index.get(_fold(host, path))
        if hit is None:
            return None, "unclassified", None
        return hit

    def normalise(self, host: str, path: str) -> tuple[str | None, str, dict | None]:
        """Alias normalisation (B9): (entry id, role, repo) where an alias becomes the entry's repo on the
        alias's host (canonical or copy), else its canonical repo. Unknown repos give (None, "unknown", None);
        ignored repos give (None, "ignored", repo)."""
        entry_id, role, repo = self.resolve(host, path)
        if entry_id is None:
            return None, ("ignored" if role == "ignored" else "unknown"), repo
        if role == "alias":
            e = self._entries[entry_id]
            repo, role = e.repo_on(host) or e.repos[0]
        return entry_id, role, repo

    # -- configurations ------------------------------------------------------------------------------------
    def configurations(self) -> list[dict]:
        return list(self.configurations_list)

    def configuration(self, configuration_id: str) -> dict:
        for c in self.configurations_list:
            if c["id"] == configuration_id:
                return c
        raise KeyError(configuration_id)


def load_catalogue(config: Config) -> Catalogue:
    """The catalogue and configurations of the registry checkout named by the config, read now through the
    registry loader (`nanosoc_rm.registry.load_registry`): any problem in any file raises its RegistryError as
    is, so a registry with a cross-file problem is never used ("never partial")."""
    path = Path(config.require("registry", "path"))
    if not path.is_dir():
        raise CatalogueError("registry %s: not a directory" % path)
    files = load_registry(path).files()
    return Catalogue(files["catalogue.yaml"], files.get("eligibility.yaml"), path)
