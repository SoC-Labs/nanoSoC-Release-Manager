"""Quality settings: thresholds from the registry's `quality.yaml` merged over the defaults (Q1), and the
service config's optional `[quality]` table (Q4 rewrite: `api_key_file`, `model`).

`quality.yaml` is optional and additive (eligibility.schema.json `$defs/quality_file`, CR-14). Without a
registry or the file, the defaults in `languages.DEFAULTS` apply and the report says so.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from nanosoc_rm import contracts
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.quality.languages import DEFAULTS

__all__ = ["Settings", "load_settings", "QualityConfigError", "api_key", "DEFAULT_MODEL"]

DEFAULT_MODEL = "claude-sonnet-5-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"


class QualityConfigError(ValueError):
    """quality.yaml is present but invalid (exit 3)."""


class Settings(dict):
    """The merged settings for one component: `defaults`, `languages`, `exclude`, `max_file_bytes`,
    `stale_todo_days`. `origin` says where they came from (shown in reports)."""

    origin: str = "defaults"

    def threshold(self, language: str) -> dict:
        out = dict(self["defaults"])
        out.update(self["languages"].get(language, {}))
        return out


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k in ("defaults",) and isinstance(v, dict):
            out[k] = {**out.get(k, {}), **v}
        elif k == "languages" and isinstance(v, dict):
            langs = out.setdefault("languages", {})
            for lang, t in v.items():
                langs[lang] = {**langs.get(lang, {}), **t}
        elif k == "exclude":
            out["exclude"] = list(out.get("exclude", [])) + list(v)
        elif k not in ("schema", "components"):
            out[k] = v
    return out


def _config_or_none(config: Config | str | os.PathLike | None) -> Config | None:
    if isinstance(config, Config):
        return config
    if config is None and not os.environ.get("NANOSOC_RM_CONFIG"):
        return None
    try:
        return load_config(config)
    except ConfigError:
        if config is not None:
            raise
        return None


def load_settings(component: str | None = None, *, config: Config | str | os.PathLike | None = None,
                  file: str | os.PathLike | None = None) -> Settings:
    """Defaults, then `quality.yaml` (the `file` given, else `<registry path>/quality.yaml` when a config names
    a registry), then that file's entry for `component`."""
    s = Settings(copy.deepcopy(DEFAULTS))
    s.origin = "defaults"
    path: Path | None = Path(file) if file else None
    if path is None:
        cfg = _config_or_none(config)
        reg = (cfg or {}).get("registry", {}).get("path") if cfg else None
        if reg and (Path(reg) / "quality.yaml").is_file():
            path = Path(reg) / "quality.yaml"
    if path is None:
        return s
    try:
        doc = contracts.load_yaml(path.read_text())
    except OSError as e:
        raise QualityConfigError("%s: unreadable (%s)" % (path, e.strerror)) from None
    except Exception as e:                                   # yaml.YAMLError: duplicate key, syntax
        raise QualityConfigError("%s: not valid YAML: %s" % (path, e)) from None
    problems = contracts.validate("eligibility", doc, "/$defs/quality_file")
    if problems:
        raise QualityConfigError("%s: %s" % (path, "; ".join(problems)))
    merged = Settings(_merge(s, doc))
    origin = "quality.yaml"
    if component and component in (doc.get("components") or {}):
        merged = Settings(_merge(merged, doc["components"][component]))
        origin = "quality.yaml (component %s)" % component
    merged.origin = origin
    return merged


def quality_config(config: Config | str | os.PathLike | None = None) -> dict[str, Any]:
    cfg = _config_or_none(config)
    return dict((cfg or {}).get("quality", {})) if cfg else {}


def api_key(config: Config | str | os.PathLike | None = None) -> str | None:
    """The Anthropic API key: $ANTHROPIC_API_KEY, else `[quality] api_key_file`; None when neither is set.
    The key is never logged or stored."""
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env
    f = quality_config(config).get("api_key_file")
    if not f:
        return None
    try:
        value = Path(f).read_text().strip()
    except OSError:
        raise ConfigError("[quality] api_key_file is unreadable") from None
    return value or None


def model(config: Config | str | os.PathLike | None = None) -> str:
    return quality_config(config).get("model") or DEFAULT_MODEL
