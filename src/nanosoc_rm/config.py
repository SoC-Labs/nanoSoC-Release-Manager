"""The service config (interfaces.md sections 1 and 17.1).

A TOML file on the service VM, never in this repo; its path is `$NANOSOC_RM_CONFIG`. Every function reads it
through `load_config(path=None)` PER CALL, never cached at import: tests switch configs inside one process.

`load_config` checks the shape of every key it knows (17.1) and requires only `[service] data_dir`, because each
module needs different sections and a command must not refuse to run for a section it never reads. A module
that needs a key asks for it with `Config.require(...)`, which raises ConfigError naming the key. A missing
file, a TOML syntax error, a wrong type or an unknown section is a ConfigError, and the CLI turns every
ConfigError into exit 3 (interfaces.md section 0.2).

Defaults filled in (section 1): `hosts.<key>.git` = `hosts.<key>.web`; `paths.quarantine` =
`<data_dir>/quarantine`; `schedule` = 15 / "02:00" / 2; `roles` = {} (an empty table refuses everyone).
Relative paths are resolved against the config file's directory.

Secrets: `read_secret(name)` reads a credential's token file. Neither its value nor its path's content ever
appears in an error message, a log or a command (interfaces.md section 0.3).
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

ENV_VAR = "NANOSOC_RM_CONFIG"
HOST_KEYS = ("github", "gitlab")                      # defs.schema.json#/$defs/host

_STR, _INT, _BOOL = "str", "int", "bool"
_STRS, _INTS, _TABLE = "list[str]", "list[int]", "table"
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

# section -> key -> type. Sections with free keys (hosts, credentials, stores, roles) are checked below.
_SECTIONS: dict[str, dict[str, str]] = {
    "service": {"data_dir": _STR},
    "registry": {"repo": _STR, "ref": _STR, "path": _STR},
    "ci": {"project": _STR, "project_id": _INT, "ref": _STR, "job": _STR, "allowed_refs": _STRS,
           "allowed_runner_ids": _INTS, "runner_tag": _STR, "service_account": _STR,
           "runner_expected": _TABLE, "fetch_timeout_minutes": _INT},
    "session": {"secret_file": _STR},
    "schedule": {"poll_minutes": _INT, "nightly_utc": _STR, "fetch_minutes": _INT},
    "paths": {"quarantine": _STR},
}
_HOST = {"web": _STR, "api": _STR, "ssh": _STR, "git": _STR, "org": _STR, "group": _STR}
_CREDENTIAL = {"token_file": _STR, "scope": _STRS, "expires": _STR, "project": _STR, "client_id": _STR}
_STORES = {"disk": {"path": _STR}, "artifactory": {"url": _STR, "repo": _STR}}
_KNOWN = set(_SECTIONS) | {"hosts", "credentials", "stores", "roles"}
# Keys holding a filesystem path, resolved against the config file's directory.
_PATH_KEYS = (("service", "data_dir"), ("registry", "path"), ("session", "secret_file"), ("paths", "quarantine"))


class ConfigError(Exception):
    """The config is missing or invalid, or lacks a key a command needs. CLI exit 3."""


def _type_ok(value: Any, kind: str) -> bool:
    if kind == _STR:
        return isinstance(value, str)
    if kind == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _STRS:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == _INTS:
        return isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    if kind == _TABLE:
        return isinstance(value, dict)
    raise AssertionError(kind)


def _check_table(where: str, table: Any, keys: dict[str, str], problems: list[str]) -> None:
    if not isinstance(table, dict):
        problems.append("[%s] must be a table" % where)
        return
    for k, v in table.items():
        if k not in keys:
            problems.append("[%s] unknown key %r" % (where, k))
        elif not _type_ok(v, keys[k]):
            problems.append("[%s] %s must be %s" % (where, k, keys[k]))


def validate(doc: dict) -> list[str]:
    """Problems with a parsed config (empty = valid). Pure: does not read any file the config names."""
    problems: list[str] = []
    for section in doc:
        if section not in _KNOWN:
            problems.append("unknown section [%s]" % section)
    for section, keys in _SECTIONS.items():
        if section in doc:
            _check_table(section, doc[section], keys, problems)
    service = doc.get("service")
    if not isinstance(service, dict) or not isinstance(service.get("data_dir"), str) or not service.get("data_dir"):
        problems.append("[service] data_dir is required")
    sched = doc.get("schedule")
    if isinstance(sched, dict) and isinstance(sched.get("nightly_utc"), str) and not _HHMM.match(sched["nightly_utc"]):
        problems.append("[schedule] nightly_utc must be HH:MM (UTC)")
    hosts = doc.get("hosts", {})
    if not isinstance(hosts, dict):
        problems.append("[hosts] must be a table")
    else:
        for name, table in hosts.items():
            if name not in HOST_KEYS:
                problems.append("[hosts.%s] unknown host key; known: %s" % (name, ", ".join(HOST_KEYS)))
                continue
            _check_table("hosts.%s" % name, table, _HOST, problems)
    creds = doc.get("credentials", {})
    if not isinstance(creds, dict):
        problems.append("[credentials] must be a table")
    else:
        for name, table in creds.items():
            # Any credential name is accepted here: AT-A2 and `credentials check` (H3) must SEE an extra one
            # (a Phase 4 bump_writer, say) and report it, so the loader never hides it by refusing the file.
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                problems.append("[credentials.%s] name must be lower_snake_case" % name)
            _check_table("credentials.%s" % name, table, _CREDENTIAL, problems)
            exp = table.get("expires") if isinstance(table, dict) else None
            if isinstance(exp, str) and exp and not _DATE.match(exp):
                problems.append("[credentials.%s] expires must be YYYY-MM-DD or empty" % name)
    stores = doc.get("stores", {})
    if not isinstance(stores, dict):
        problems.append("[stores] must be a table")
    else:
        for name, table in stores.items():
            if name not in _STORES:
                problems.append("[stores.%s] unknown store; known: %s" % (name, ", ".join(_STORES)))
            else:
                _check_table("stores.%s" % name, table, _STORES[name], problems)
    roles = doc.get("roles", {})
    if not isinstance(roles, dict):
        problems.append("[roles] must be a table")
    else:
        for login, value in roles.items():
            if not _type_ok(value, _STRS):
                problems.append("[roles] %s must be a list of role names" % login)
    return problems


class Config(dict):
    """The parsed config with defaults filled in: a plain dict of the TOML tables, plus helpers.

    `cfg["ci"]["project"]` works as on the TOML; `cfg.require("ci", "project_id")` raises ConfigError naming
    the key when it is absent."""

    def __init__(self, doc: dict, path: Path):
        super().__init__(doc)
        self.path = path

    @property
    def data_dir(self) -> Path:
        return Path(self["service"]["data_dir"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rm.sqlite"

    @property
    def mirrors_dir(self) -> Path:
        return self.data_dir / "mirrors"

    @property
    def quarantine(self) -> Path:
        return Path(self["paths"]["quarantine"])

    def require(self, *keys: str) -> Any:
        """The value at keys (section, key, ...); ConfigError naming the full key if any part is absent."""
        if not keys:
            raise TypeError("require() needs at least one key")
        node: Any = self
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                what = "[%s]" % keys[0] if len(keys) == 1 else "[%s] %s" % (".".join(keys[:-1]), keys[-1])
                raise ConfigError("%s: %s is required for this command" % (self.path, what))
            node = node[k]
        return node

    def host(self, name: str) -> dict:
        """hosts.<name>, with `git` defaulted to `web`. ConfigError if the host is not configured."""
        if name not in self.get("hosts", {}):
            raise ConfigError("%s: [hosts.%s] is required for this command" % (self.path, name))
        return self["hosts"][name]

    def read_secret(self, credential: str) -> str:
        """The token in credentials.<credential>.token_file (or session.secret_file for "session").
        Errors name the credential, never the content."""
        if credential == "session":
            p = Path(self.require("session", "secret_file"))
        else:
            p = Path(self.require("credentials", credential, "token_file"))
        try:
            value = p.read_text().strip()
        except OSError as e:
            raise ConfigError("%s: token file of %s is unreadable (%s)" % (self.path, credential, e.strerror)) from None
        if not value:
            raise ConfigError("%s: token file of %s is empty" % (self.path, credential))
        return value


def _resolve(base: Path, value: str) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (base / p))


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Read, check and return the service config. path=None means $NANOSOC_RM_CONFIG, read now (never cached).
    Raises ConfigError for a missing or invalid file."""
    if path is None:
        path = os.environ.get(ENV_VAR)
        if not path:
            raise ConfigError("no service config: set %s to the config file's path" % ENV_VAR)
    p = Path(path).expanduser()
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise ConfigError("%s: cannot read the service config (%s)" % (p, e.strerror)) from None
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError("%s: not valid TOML: %s" % (p, e)) from None
    problems = validate(doc)
    if problems:
        raise ConfigError("%s: invalid service config:\n  %s" % (p, "\n  ".join(problems)))
    base = p.resolve().parent
    for section, key in _PATH_KEYS:
        if section in doc and key in doc[section]:
            doc[section][key] = _resolve(base, doc[section][key])
    for table in doc.get("credentials", {}).values():
        if "token_file" in table:
            table["token_file"] = _resolve(base, table["token_file"])
    if "disk" in doc.get("stores", {}) and "path" in doc["stores"]["disk"]:
        doc["stores"]["disk"]["path"] = _resolve(base, doc["stores"]["disk"]["path"])
    for table in doc.get("hosts", {}).values():
        if "web" in table:
            table.setdefault("git", table["web"])
    doc.setdefault("paths", {}).setdefault("quarantine", str(Path(doc["service"]["data_dir"]) / "quarantine"))
    sched = doc.setdefault("schedule", {})
    sched.setdefault("poll_minutes", 15)
    sched.setdefault("nightly_utc", "02:00")
    sched.setdefault("fetch_minutes", 2)
    doc.setdefault("roles", {})
    return Config(doc, p)
