"""The registered configurations, as the CI module needs them (interfaces.md section 10; registry README section 1).

Read from the registry checkout (`registry.path`): `eligibility.yaml` for the configurations, `catalogue.yaml`
for each configuration's canonical repo. Both are read with the contracts' strict YAML reader and validated
against their schemas before use; a registry that does not validate is refused whole (RegistryUnreadable),
never read in part. The cross-file rules (README section 3) are the registry loader's and T0's job
(`t0.registry.schema`); this module only reads the fields a trigger and a roll-up need.

    configurations(config)            every configuration, in file order
    configuration(cfg_id, config)     one, or UnknownConfiguration
    canonical_repo(component, config) {host, path} of a catalogue component, or UnknownConfiguration
    waivers(cfg_id, config)           the configuration's t0_waivers (a frozen one's only; [] otherwise)
    REGISTRY_JOB                      "@registry", the registry job's configuration id
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nanosoc_rm import contracts
from nanosoc_rm.config import Config, load_config

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


def _read(config: Config, name: str, schema: str) -> dict:
    path = Path(config.require("registry", "path")) / name
    try:
        doc = contracts.load_yaml(path.read_text())
    except OSError as e:
        raise RegistryUnreadable("registry file %s is unreadable (%s)" % (path, e.strerror)) from None
    except yaml.YAMLError as e:
        raise RegistryUnreadable("registry file %s is not valid YAML: %s" % (path, e)) from None
    problems = contracts.validate(schema, doc)
    if problems:
        raise RegistryUnreadable("registry file %s does not validate against %s: %s"
                                 % (path, schema, "; ".join(problems[:5])))
    return doc


def configurations(config: Config | None = None) -> list[dict]:
    return list(_read(_cfg(config), "eligibility.yaml", "eligibility")["configurations"])


def configuration(cfg_id: str, config: Config | None = None) -> dict:
    for c in configurations(config):
        if c["id"] == cfg_id:
            return c
    raise UnknownConfiguration("no registered configuration %r" % cfg_id)


def canonical_repo(component: str, config: Config | None = None) -> dict:
    for c in _read(_cfg(config), "catalogue.yaml", "component-catalogue")["components"]:
        if c["id"] == component:
            return {"host": c["canonical"]["host"], "path": c["canonical"]["path"]}
    raise UnknownConfiguration("component %r is not in the catalogue" % component)


def waivers(cfg_id: str, config: Config | None = None) -> list[dict]:
    """README section 6: only a frozen configuration may carry t0_waivers; the registry job has none."""
    if cfg_id == REGISTRY_JOB:
        return []
    c = configuration(cfg_id, config)
    return list(c.get("t0_waivers") or []) if c.get("frozen") else []


def registry_repo(config: Config | None = None) -> dict:
    """The registry repo: `registry.repo` on the lab GitLab (interfaces.md section 1)."""
    return {"host": "gitlab", "path": _cfg(config).require("registry", "repo")}
