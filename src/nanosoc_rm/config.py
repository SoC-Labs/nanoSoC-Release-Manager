"""The service config (interfaces.md sections 1 and 17.1).

A TOML file on the service VM, never in this repo; its path is `$NANOSOC_RM_CONFIG`. Every function reads it
through `load_config(path=None)` PER CALL, never cached at import: tests switch configs inside one process.

`load_config` checks the shape of every key it knows (17.1) and requires only `[service] data_dir`, because each
module needs different sections and a command must not refuse to run for a section it never reads. A module
that needs a key asks for it with `Config.require(...)`, which raises ConfigError naming the key. A missing
file, a TOML syntax error, a wrong type or an unknown section is a ConfigError, and the CLI turns every
ConfigError into exit 3 (interfaces.md section 0.2).

`[hosts.<host>] private_read` (CR-PR1, optional) names a read-only method for private mirrors.

`[auth]` (CR-16) selects the sign-in: `mode = "github"` (default) or `"local"` with `[[auth.users]]` (login,
scrypt `hash`, optional `roles`).

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
    "quality": {"api_key_file": _STR, "model": _STR, "paths": _TABLE},   # CR-14: rewrite key/model; local targets
    "regen": {"work_dir": _STR, "read_only_roots": _STRS, "nice": _INT},  # section 20: sandbox, refusals, priority
}
_HOST = {"web": _STR, "api": _STR, "ssh": _STR, "git": _STR, "org": _STR, "group": _STR}
_CREDENTIAL = {"token_file": _STR, "scope": _STRS, "expires": _STR, "project": _STR, "client_id": _STR}
# [stores.artifactory] (Wave E): url and repo since gate 2; token_file and token_expires (the CR-PR1 pattern: a 0600
# file, never logged; NOT a fifth [credentials] entry, AT-A2); repos (what the store index reads; section 22);
# release_repo, publishers (logins), shared_logins, signing_key, allowed_signers, signer_identity, ssh_keygen and
# projects (component -> store project) for the publish unit (CR-ART-publish), which the index also reads.
_STORES = {"disk": {"path": _STR},
           "artifactory": {"url": _STR, "repo": _STR, "token_file": _STR, "token_expires": _STR,
                           "release_repo": _STR, "publishers": _STRS, "shared_logins": _STRS, "signing_key": _STR,
                           "allowed_signers": _STR, "signer_identity": _STR, "ssh_keygen": _STR,
                           "projects": _TABLE, "repos": _STRS}}
_ART_PATHS = ("token_file", "signing_key", "allowed_signers", "ssh_keygen")
_KNOWN = set(_SECTIONS) | {"hosts", "credentials", "stores", "roles", "auth"}
AUTH_MODES = ("github", "local")                      # CR-16: [auth] mode; github is the default
_AUTH = {"mode": _STR, "users": "list[table]"}
_AUTH_USER = {"login": _STR, "hash": _STR, "roles": _STRS}
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Keys holding a filesystem path, resolved against the config file's directory.
_PATH_KEYS = (("service", "data_dir"), ("registry", "path"), ("session", "secret_file"), ("paths", "quarantine"),
              ("quality", "api_key_file"), ("regen", "work_dir"))


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
            if isinstance(table, dict) and "private_read" in table:
                _check_private_read(name, table["private_read"], problems)
                table = {k: v for k, v in table.items() if k != "private_read"}
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
                if name == "artifactory" and isinstance(table, dict):
                    _check_artifactory(table, problems)
    _check_auth(doc.get("auth"), problems)
    roles = doc.get("roles", {})
    if not isinstance(roles, dict):
        problems.append("[roles] must be a table")
    else:
        for login, value in roles.items():
            if not _type_ok(value, _STRS):
                problems.append("[roles] %s must be a list of role names" % login)
    return problems


def _check_artifactory(table: dict, problems: list[str]) -> None:
    """The store keys: the URL ends in /artifactory (so no call can reach the Access API, where a failed login locks
    the admin out) and carries no user or token; a shared login is never a publisher; projects name store folders."""
    url = table.get("url")
    if isinstance(url, str) and not re.match(r"^https?://[^/?#@\s]+(/[^?#@\s]*)?/artifactory$", url.rstrip("/")):
        problems.append("[stores.artifactory] url must be http(s)://<host>[:port]/artifactory, with no user or token")
    exp = table.get("token_expires")
    if isinstance(exp, str) and exp and not _DATE.match(exp):
        problems.append("[stores.artifactory] token_expires must be YYYY-MM-DD or empty")
    shared = set(table.get("shared_logins") or []) if _type_ok(table.get("shared_logins", []), _STRS) else set()
    pubs = table.get("publishers") or []
    if _type_ok(pubs, _STRS):
        for login in pubs:
            if not _LOGIN.match(login):
                problems.append("[stores.artifactory] publishers: %r is not a login" % login[:40])
            elif login in shared:
                problems.append("[stores.artifactory] publishers: %s is a shared login and may never publish" % login)
    projects = table.get("projects")
    if isinstance(projects, dict):
        for k, v in projects.items():
            if not isinstance(v, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", v):
                problems.append("[stores.artifactory] projects.%s must be a store project name" % k)


_PRIVATE_READ = {"method": _STR, "identity_file": _STR, "token_file": _STR, "username": _STR}


def _check_private_read(host: str, spec: Any, problems: list[str]) -> None:
    """CR-PR1: `[hosts.<host>] private_read` = "ssh", or a table {method = "ssh", identity_file?} or
    {method = "token", token_file, username?}. Optional; absent means anonymous only."""
    where = "hosts.%s" % host
    if isinstance(spec, str):
        spec = {"method": spec}
    if not isinstance(spec, dict):
        problems.append("[%s] private_read must be \"ssh\" or a table" % where)
        return
    _check_table(where + ".private_read", spec, _PRIVATE_READ, problems)
    kind = spec.get("method")
    if kind not in ("ssh", "token"):
        problems.append("[%s] private_read method must be \"ssh\" or \"token\"" % where)
    elif kind == "token" and not isinstance(spec.get("token_file"), str):
        problems.append("[%s] private_read method \"token\" needs token_file" % where)
    elif kind == "ssh" and ("token_file" in spec or "username" in spec):
        problems.append("[%s] private_read method \"ssh\" takes no token_file or username" % where)
    elif kind == "token" and "identity_file" in spec:
        problems.append("[%s] private_read method \"token\" takes no identity_file" % where)


def _check_auth(auth: Any, problems: list[str]) -> None:
    """CR-16: `[auth] mode = "local"` with `[[auth.users]]` login, scrypt hash (`nanosoc-rm user add`), optional
    roles. Only the shape is checked here; a hash that does not parse refuses that user's sign-in."""
    if auth is None:
        return
    if not isinstance(auth, dict):
        problems.append("[auth] must be a table")
        return
    for k in auth:
        if k not in _AUTH:
            problems.append("[auth] unknown key %r" % k)
    mode = auth.get("mode", "github")
    if mode not in AUTH_MODES:
        problems.append("[auth] mode must be one of %s" % ", ".join(AUTH_MODES))
    users = auth.get("users", [])
    if not isinstance(users, list) or not all(isinstance(u, dict) for u in users):
        problems.append("[[auth.users]] must be an array of tables")
        return
    seen = set()
    for i, u in enumerate(users):
        where = "auth.users[%d]" % i
        _check_table(where, u, _AUTH_USER, problems)
        login = u.get("login")
        if not isinstance(login, str) or not _LOGIN.match(login):
            problems.append("[%s] login is required (letters, digits, _ . -)" % where)
        elif login in seen:
            problems.append("[%s] login %s is given twice" % (where, login))
        else:
            seen.add(login)
        if not isinstance(u.get("hash"), str) or not u.get("hash", "").startswith("scrypt$"):
            problems.append("[%s] hash is required: the scrypt$... line `nanosoc-rm user add` prints" % where)
    if mode == "local" and not users:
        problems.append("[auth] mode = \"local\" needs at least one [[auth.users]] entry (`nanosoc-rm user add`)")


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
    def auth_mode(self) -> str:
        """CR-16: "github" (sign-in with GitHub, the default) or "local" (named users in [[auth.users]])."""
        return (self.get("auth") or {}).get("mode", "github")

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
    for key in _ART_PATHS:
        art = doc.get("stores", {}).get("artifactory", {})
        if key in art and (key != "ssh_keygen" or "/" in art[key]):
            art[key] = _resolve(base, art[key])
    for table in doc.get("hosts", {}).values():
        if "web" in table:
            table.setdefault("git", table["web"])
        spec = table.get("private_read")
        if isinstance(spec, dict):
            for key in ("identity_file", "token_file"):
                if key in spec:
                    spec[key] = _resolve(base, spec[key])
    doc.setdefault("paths", {}).setdefault("quarantine", str(Path(doc["service"]["data_dir"]) / "quarantine"))
    sched = doc.setdefault("schedule", {})
    sched.setdefault("poll_minutes", 15)
    sched.setdefault("nightly_utc", "02:00")
    sched.setdefault("fetch_minutes", 2)
    doc.setdefault("roles", {})
    return Config(doc, p)
