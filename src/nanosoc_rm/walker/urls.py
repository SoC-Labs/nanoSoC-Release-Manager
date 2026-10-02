"""Submodule URL normalisation for the walker (README section 5; interfaces.md sections 2 and 4; B9).

    classify(url, hosts, parent_url) -> (url_kind, (host, path) | None, resolved_url)

- `relative` (`../x.git`, `./x.git`): resolved against the parent's pinned URL as git does, then normalised;
- `local` (`/...`, `file://...`): no host (state LOCAL_URL);
- `https` / `ssh` (`git@host:path`, `ssh://...`): the host is found by comparing the URL's host name with the
  config's host table (`web`, `git` and `ssh` of each host), never a hard-coded name; `.git` and a trailing `/`
  are stripped from the path;
- `other`: any other scheme, or a host name that is not in the table.

Alias normalisation (catalogue `aliases`) happens after this, in the catalogue (mirror.catalogue).
"""
from __future__ import annotations

import posixpath
import re
import urllib.parse

__all__ = ["classify", "host_names", "join_relative", "repo_url"]

_SCP = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[^:/]+):(?P<path>.+)$")


def _strip(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path.rstrip("/")


def host_names(hosts: dict) -> dict[str, str]:
    """{host name (lower case): host key} from the config's host table (web, git and ssh bases)."""
    names = {}
    for key, table in hosts.items():
        for field in ("web", "git"):
            base = table.get(field) or ""
            netloc = urllib.parse.urlsplit(base).hostname
            if netloc:
                names.setdefault(netloc.lower(), key)
        m = _SCP.match((table.get("ssh") or "") + "x")
        if m:
            names.setdefault(m.group("host").lower(), key)
        elif table.get("ssh", "").startswith("ssh://"):
            h = urllib.parse.urlsplit(table["ssh"]).hostname
            if h:
                names.setdefault(h.lower(), key)
    return names


def join_relative(parent_url: str, rel: str) -> str:
    """git's resolution of a relative submodule URL against the superproject's URL."""
    m = _SCP.match(parent_url)
    if m and "://" not in parent_url:
        prefix, path = "%s@%s:" % (m.group("user"), m.group("host")), m.group("path")
    else:
        parts = urllib.parse.urlsplit(parent_url)
        prefix, path = "%s://%s" % (parts.scheme, parts.netloc), parts.path
    path = path.rstrip("/")
    for seg in rel.split("/"):
        if seg == "..":
            path = posixpath.dirname(path)
        elif seg in (".", ""):
            continue
        else:
            path = path + "/" + seg
    if prefix.endswith(":"):
        return prefix + path.lstrip("/")
    return prefix + ("/" if not path.startswith("/") else "") + path


def repo_url(hosts: dict, host: str, path: str) -> str:
    """The https URL of a repo on a configured host (`web` base), as a pin's parent URL for relative URLs."""
    return "%s/%s.git" % (hosts[host]["web"].rstrip("/"), path)


def classify(url: str, hosts: dict, parent_url: str | None = None) -> tuple[str, tuple[str, str] | None, str]:
    """(url_kind, (host, path) or None, the absolute URL it resolves to)."""
    url = url.strip()
    kind = None
    if url.startswith(("../", "./")):
        if not parent_url:
            return "relative", None, url
        kind, url = "relative", join_relative(parent_url, url)
    if url.startswith("/") or url.startswith("file://"):
        return kind or "local", None, url
    names = host_names(hosts)
    m = _SCP.match(url)
    if m and "://" not in url:
        host = names.get(m.group("host").lower())
        if host is None:
            return kind or "other", None, url
        return kind or "ssh", (host, _strip(m.group("path"))), url
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http", "ssh", "git"):
        return kind or "other", None, url
    host = names.get((parts.hostname or "").lower())
    if host is None:
        return kind or "other", None, url
    path = _strip(parts.path)
    if "/" not in path:
        return kind or "other", None, url
    if scheme == "ssh":
        return kind or "ssh", (host, path), url
    if scheme == "https":
        return kind or "https", (host, path), url
    return kind or "other", (host, path), url
