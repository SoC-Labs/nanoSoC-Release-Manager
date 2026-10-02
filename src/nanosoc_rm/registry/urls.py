"""URL normalisation (interfaces.md section 2, B9; ported from walk.py's norm()).

    normalise_url(url, hosts, parent=None) -> (url_kind, Repo | None)

`url_kind` is the defs.schema.json url_kind of the URL as written:

    https     https://...                     (an anonymous fetch can work)
    ssh       git@host:path, ssh://...        (the SSH-only badge, B3)
    local     /path, file://...               (pin state LOCAL_URL)
    relative  ../x.git, ./x.git               (resolved against the parent's repo, as git does)
    other     any other scheme, or a host the service config does not name

The Repo is the host key and namespace path the URL names, with `.git` and trailing `/` stripped, or None when
the URL names no configured host or no valid path. Host names come only from the service config's host table
(`hosts.<key>.web`, `.git`, `.ssh`), never from this code (README section 8, m14). Alias redirects are not
applied here: `Registry.resolve_repo` maps a Repo to its component, through catalogue `aliases`.

A URL under a configured `git` fetch base that is neither https nor ssh (offline tests point `git` at a
`file://` directory, interfaces.md section 17.1) names that host's repo; its kind is `other`, because the pin-
state schema ties `https` and `local` to the URL's own form, and `local` would make it state LOCAL_URL.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal, Mapping

__all__ = ["Repo", "UrlKind", "HostTable", "normalise_url", "REPO_PATH"]

UrlKind = Literal["https", "ssh", "local", "relative", "other"]
HostTable = Mapping[str, Mapping[str, Any]]

# defs.schema.json#/$defs/repo path
REPO_PATH = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")
_SCP = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[^:/\s]+):(?P<path>.*)$")


@dataclass(frozen=True)
class Repo:
    """One repository on one host (defs.schema.json#/$defs/repo)."""
    host: str
    path: str

    @classmethod
    def of(cls, value: Any) -> "Repo":
        """A Repo from a Repo, a {host, path} dict or a (host, path) pair."""
        if isinstance(value, Repo):
            return value
        if isinstance(value, Mapping):
            return cls(host=str(value["host"]), path=str(value["path"]))
        host, path = value
        return cls(host=str(host), path=str(path))

    def as_doc(self) -> dict:
        return {"host": self.host, "path": self.path}

    def key(self) -> tuple[str, str]:
        """Identity for matching: hosts route repo paths case-insensitively."""
        return (self.host, self.path.lower())


def _host_tables(hosts: Any) -> Mapping[str, Mapping[str, Any]]:
    """Accept the config's `hosts` table, or a whole config (anything with a `hosts` mapping)."""
    if hosts is None:
        return {}
    if isinstance(hosts, Mapping) and isinstance(hosts.get("hosts"), Mapping):
        return hosts["hosts"]
    return hosts


def _clean_path(path: str) -> str | None:
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    return path if REPO_PATH.match(path) else None


def _strip_userinfo(url: str) -> str:
    """https://user:secret@host/p -> https://host/p (credentials never take part in matching)."""
    parts = urllib.parse.urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _under(url: str, base: str) -> str | None:
    """The rest of `url` after `base` (a URL prefix), or None. Scheme and host compare case-insensitively."""
    if not base:
        return None
    b = base.rstrip("/")
    if b.endswith(":"):                     # scp-like ssh base "git@host:"
        return url[len(b):] if url.lower().startswith(b.lower()) else None
    if url.lower().startswith(b.lower() + "/"):
        return url[len(b) + 1:]
    return None


def _ssh_hostname(base: str) -> str | None:
    """The host name of an ssh base: "git@host:" or "ssh://git@host[:port]/"."""
    m = _SCP.match(base)
    if m:
        return m.group("host").lower()
    if base.lower().startswith("ssh://"):
        return (urllib.parse.urlsplit(base).hostname or "").lower() or None
    return None


def _kind_of(url: str) -> UrlKind:
    low = url.lower()
    if low.startswith("https://"):
        return "https"
    if low.startswith("ssh://") or _SCP.match(url):
        return "ssh"
    if url.startswith("/") or low.startswith("file://"):
        return "local"
    if url.startswith("./") or url.startswith("../"):
        return "relative"
    return "other"


def _resolve_relative(url: str, parent: Repo) -> Repo | None:
    """git's rule for a relative submodule URL: each leading ../ drops one component of the parent's path,
    ./ keeps it; the rest is appended."""
    parts = parent.path.split("/")
    rest = url
    while True:
        if rest.startswith("./"):
            rest = rest[2:]
        elif rest.startswith("../"):
            if not parts:
                return None
            parts.pop()
            rest = rest[3:]
        else:
            break
    if rest in ("", ".", ".."):
        return None
    path = _clean_path("/".join(parts + [rest]))
    return Repo(parent.host, path) if path else None


def normalise_url(url: str, hosts: Any, parent: Repo | Mapping | None = None) -> tuple[UrlKind, Repo | None]:
    """(url_kind, Repo or None) for a .gitmodules URL. `hosts` is the service config's host table
    (`config["hosts"]`, or the config itself); `parent` is the repo whose .gitmodules holds a relative URL."""
    url = (url or "").strip()
    kind = _kind_of(url)
    if kind == "relative":
        return kind, (_resolve_relative(url, Repo.of(parent)) if parent is not None else None)
    table = _host_tables(hosts)
    plain = _strip_userinfo(url) if kind != "ssh" else url
    for host, cfg in table.items():
        if not isinstance(cfg, Mapping):
            continue
        for key in ("web", "git"):
            rest = _under(plain, str(cfg.get(key) or ""))
            if rest is not None:
                path = _clean_path(rest)
                # A local-looking URL under a configured base is that host's repo, not a local pin
                # (module docstring), so its kind is `other`.
                return ("other" if kind == "local" else kind), (Repo(host, path) if path else None)
        ssh = str(cfg.get("ssh") or "")
        if kind == "ssh" and ssh:
            rest = _under(url, ssh)
            if rest is None and url.lower().startswith("ssh://"):
                parts = urllib.parse.urlsplit(url)
                if (parts.hostname or "").lower() == _ssh_hostname(ssh):
                    rest = parts.path
            elif rest is None:
                m = _SCP.match(url)
                if m and m.group("host").lower() == _ssh_hostname(ssh):
                    rest = m.group("path")
            if rest is not None:
                path = _clean_path(rest)
                return kind, (Repo(host, path) if path else None)
    if kind == "local":
        return kind, None
    return "other", None
