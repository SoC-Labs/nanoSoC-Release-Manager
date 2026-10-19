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

import re
import urllib.parse

__all__ = ["classify", "host_names", "join_relative", "repo_url"]

_SCP = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[^:/]+):(?P<path>.+)\Z")


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


def _local_not_ssh(url: str) -> bool:
    """git's url_is_local_not_ssh: no colon, or a slash before the first colon."""
    colon, slash = url.find(":"), url.find("/")
    return colon < 0 or 0 <= slash < colon


def join_relative(parent_url: str, rel: str) -> str:
    """git's resolution of a relative submodule URL against the superproject's URL (submodule--helper
    relative_url and chop_last_dir, git 2.43): only leading `./` and `../` are read; each `../` drops the URL's
    last `/` component, or, with no `/` left, its last `:` component (the rest is then joined with `:`). A `..`
    that climbs past the path climbs into the host part exactly as git does, so `https://h/a/b.git` with
    `../../../x/y` is `https://x/y` (another host), never a path on `h`. ValueError when git
    would refuse ("cannot strip one component off url")."""
    remote = parent_url.rstrip("/")
    is_relative = _local_not_ssh(remote) and not remote.startswith("/")
    if is_relative and not remote.startswith(("./", "../")):
        remote = "./" + remote
    colonsep = False
    while rel:
        if rel.startswith("../"):
            rel = rel[3:]
            cut = remote.rfind("/")
            if cut >= 0:
                remote = remote[:cut]
                continue
            cut = remote.rfind(":")
            if cut >= 0:
                remote, colonsep = remote[:cut], True
                continue
            if is_relative or remote == ".":
                raise ValueError("cannot strip one component off url %r" % remote)
            remote = "."
        elif rel.startswith("./"):
            rel = rel[2:]
        else:
            break
    # git appends the rest as written; its `.` and empty segments name the same repo, so they are dropped here
    # (an inner `..` is kept as written: it is not git's to climb with).
    rest = "/".join(seg for seg in rel.split("/") if seg not in (".", ""))
    out = remote + (":" if colonsep else "/") + rest
    return out[2:] if out.startswith("./") else out


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
        try:
            kind, url = "relative", join_relative(parent_url, url)
        except ValueError:                                      # git cannot resolve it either
            return "relative", None, url
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
