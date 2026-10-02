"""The registry: loader and validator, URL normalisation, the F5 import census, and registry facts
(interfaces.md sections 2 and 2a; README sections 1 and 3).

    load_registry(path) -> Registry           RegistryError(problems: list[Problem]) on any problem
    Registry.components() / configurations() / defects() / errata() / imports() / resolve_repo(repo)
    Registry.records()                         registry_record observations (A3)
    normalise_url(url, hosts, parent=None) -> (url_kind, Repo | None)     (B9)
    nanosoc_rm.registry.imports.census(ledger, read_doc) -> [CensusRow]   (F5)

Requirements: B1, F1, F2, F3 (structure), F5; also A1 (catalogue), B2 (configurations), B9.
"""
from nanosoc_rm.registry.loader import (KNOWN_SCHEMA_VERSION, Problem, Registry, RegistryError, Role,
                                        load_registry)
from nanosoc_rm.registry.records import RegistryUncommitted, registry_records, store_records
from nanosoc_rm.registry.rules import CONFIGURATION_GATES, WAIVABLE_GATES
from nanosoc_rm.registry.urls import HostTable, Repo, UrlKind, normalise_url

__all__ = ["load_registry", "Registry", "RegistryError", "Problem", "Repo", "Role", "normalise_url", "UrlKind",
           "HostTable", "registry_records", "store_records", "RegistryUncommitted", "CONFIGURATION_GATES",
           "WAIVABLE_GATES", "KNOWN_SCHEMA_VERSION"]
