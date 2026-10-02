"""The registered configurations, as the CI module needs them (interfaces.md section 10; registry README section 1).

Read from the registry checkout (`registry.path`) through the registry loader (`nanosoc_rm.registry.
load_registry`): `eligibility.yaml` for the configurations, `catalogue.yaml` for each configuration's canonical
repo. A registry with any problem (schema or cross-file rule) is refused whole (RegistryUnreadable), never read
in part; this module only reads the fields a trigger and a roll-up need.

    configurations(config)            every configuration, in file order
    configuration(cfg_id, config)     one, or UnknownConfiguration
    canonical_repo(component, config) {host, path} of a catalogue component, or UnknownConfiguration
    waivers(cfg_id, config)           the configuration's t0_waivers (a frozen one's only; [] otherwise)
    REGISTRY_JOB                      "@registry", the registry job's configuration id
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm.config import Config, load_config
from nanosoc_rm.registry import RegistryError, load_registry

__all__ = ["REGISTRY_JOB", "RegistryUnreadable", "UnknownConfiguration", "configurations", "configuration",
           "canonical_repo", "waivers", "registry_repo"]

REGISTRY_JOB = "@registry"


class RegistryUnreadable(RuntimeError):
    """The registry checkout is missing, or a file does not validate. CLI exit 2 (could not answer)."""


class UnknownConfiguration(KeyError):
    """No such configuration (or component) is registered. CLI exit 3."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "unknown configuration"


def _cfg(config: Config | None) -> Config:
    return config if config is not None else load_config()


def _registry(config: Config):
    """The registry checkout at `registry.path`, through the registry loader (never partial)."""
    path = Path(config.require("registry", "path"))
    if not path.is_dir():
        raise RegistryUnreadable("registry checkout %s is missing" % path)
    try:
        return load_registry(path)
    except RegistryError as e:
        raise RegistryUnreadable("the registry at %s does not load: %s" % (path, e)) from None


def configurations(config: Config | None = None) -> list[dict]:
    return _registry(_cfg(config)).configurations()


def configuration(cfg_id: str, config: Config | None = None) -> dict:
    for c in configurations(config):
        if c["id"] == cfg_id:
            return c
    raise UnknownConfiguration("no registered configuration %r" % cfg_id)


def canonical_repo(component: str, config: Config | None = None) -> dict:
    c = _registry(_cfg(config)).component(component)
    if c is None:
        raise UnknownConfiguration("component %r is not in the catalogue" % component)
    return {"host": c["canonical"]["host"], "path": c["canonical"]["path"]}


def waivers(cfg_id: str, config: Config | None = None) -> list[dict]:
    """README section 6: only a frozen configuration may carry t0_waivers; the registry job has none."""
    if cfg_id == REGISTRY_JOB:
        return []
    c = configuration(cfg_id, config)
    return list(c.get("t0_waivers") or []) if c.get("frozen") else []


def registry_repo(config: Config | None = None) -> dict:
    """The registry repo: `registry.repo` on the lab GitLab (interfaces.md section 1)."""
    return {"host": "gitlab", "path": _cfg(config).require("registry", "repo")}
