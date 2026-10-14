"""Repo inventory (interfaces.md section 3; A1).

    inventory() -> one repo_inventory per repo in the GitHub org and the GitLab group (with subgroups)

Listing, GET only, every page followed:

- GitHub: `GET orgs/<org>/repos?type=all&per_page=100`, following the `Link: <...>; rel="next"` header (never
  off the configured API base: the read token goes with every page);
- GitLab: `GET groups/<group>/projects?include_subgroups=true&per_page=100`, following `X-Next-Page`.

Each repo is classified against the catalogue: canonical, copy, alias, external, ignored, or `unclassified`
(flagged: `inventory --unclassified` lists them and exits 1). The rows of one host are stored as one snapshot
(scope `repo_inventory:<host>`). A listing that fails stores nothing and raises InventoryError, so the previous
complete listing stays current and shows its age.
"""
from __future__ import annotations

import re
import urllib.parse

import httpx

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.mirror.catalogue import Catalogue, load_catalogue
from nanosoc_rm.mirror.layout import API_TIMEOUT_S, api_get, snapshot_id

__all__ = ["inventory", "list_host", "InventoryError", "MAX_PAGES"]

MAX_PAGES = 100                     # 10 000 repos per host: a guard against a pagination loop, never reached
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class InventoryError(RuntimeError):
    """A host listing could not be read completely. The CLI exits 2 (could not answer)."""


def _visibility(host: str, item: dict) -> str:
    v = item.get("visibility")
    if v in ("public", "private", "internal"):
        return v
    return "private" if item.get("private") else "public"


def _under(url: str, api: str) -> bool:
    """True when `url` is on the same scheme and host as the API base, at or below its path."""
    u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(api.rstrip("/"))
    return (u.scheme.lower(), u.netloc.lower()) == (b.scheme.lower(), b.netloc.lower()) and \
        (u.path == b.path or u.path.startswith(b.path + "/"))


def list_host(config: Config, host: str, *, anonymous: bool = False) -> list[tuple[dict, dict]]:
    """[(item, source)] for every repo the host lists for the configured org or group, all pages. `anonymous`
    sends no credential (section 23.2: the host then lists only what it shows anyone)."""
    table = config.host(host)
    if host == "github":
        owner = table.get("org")
        path, params = "orgs/%s/repos" % urllib.parse.quote(owner or "", safe=""), {"type": "all", "per_page": 100}
    else:
        owner = table.get("group")
        path = "groups/%s/projects" % urllib.parse.quote(owner or "", safe="")
        params = {"include_subgroups": "true", "per_page": 100}
    if not owner:
        raise ConfigError("%s: [hosts.%s] %s is required for the inventory"
                          % (config.path, host, "org" if host == "github" else "group"))
    out: list[tuple[dict, dict]] = []
    with httpx.Client(timeout=API_TIMEOUT_S, follow_redirects=True) as client:
        url, page = None, 1
        for _ in range(MAX_PAGES):
            ans = api_get(config, host, path, None if url else params, url=url, client=client, anonymous=anonymous)
            if ans.status != 200:
                raise InventoryError("%s listing failed at %s: %s" % (host, ans.source["command"],
                                                                     ans.error or "HTTP %s" % ans.status))
            if not isinstance(ans.body, list):
                ctype = (ans.headers.get("content-type") or "no content-type").split(";")[0].strip()
                raise InventoryError("%s listing failed at %s: HTTP 200 but the body is not a JSON list (%s, %s)"
                                     % (host, ans.source["command"],
                                        "not JSON" if ans.body is None else type(ans.body).__name__, ctype))
            out += [(item, ans.source) for item in ans.body]
            if host == "github":
                m = _NEXT.search(ans.headers.get("link", ""))
                if not m:
                    return out
                base = url or "%s/%s?%s" % (table["api"].rstrip("/"), path, urllib.parse.urlencode(params))
                url = urllib.parse.urljoin(base, m.group(1))
                if not _under(url, table["api"]):
                    # The read token goes with every page: never follow a link off the configured API (0.3).
                    raise InventoryError("github listing: rel=next link leaves the configured API (%s)"
                                         % urllib.parse.urlsplit(url).netloc)
            else:
                nxt = (ans.headers.get("x-next-page") or "").strip()
                if not nxt:
                    return out
                if not nxt.isdigit() or int(nxt) <= page:
                    raise InventoryError("gitlab listing: X-Next-Page %r after page %d" % (nxt, page))
                page = int(nxt)
                url = "%s/%s?%s" % (table["api"].rstrip("/"), path, urllib.parse.urlencode(dict(params, page=page)))
    raise InventoryError("%s listing: more than %d pages" % (host, MAX_PAGES))


def _row(host: str, item: dict, source: dict, catalogue: Catalogue, observed_at: str, snap: dict) -> dict:
    path = item.get("full_name") if host == "github" else item.get("path_with_namespace")
    entry_id, role, known = catalogue.resolve(host, path)
    repo = {"host": host, "path": known["path"] if known else path}
    return {"schema": 1, "kind": "repo_inventory", "subject": "repo_inventory:%s:%s" % (host, repo["path"]),
            "observed_at": observed_at, "sources": [source], "snapshot": snap,
            "fact": {"repo": repo, "component": entry_id, "role": role, "visibility": _visibility(host, item),
                     "default_branch": item.get("default_branch") or None}}


def inventory(*, config: Config | None = None, hosts: tuple[str, ...] | None = None) -> list[dict]:
    """List and classify every repo of the configured org and group; store one snapshot per host; return the
    rows (GitHub first). Hosts without an `org`/`group` in the config are not listed."""
    config = config if config is not None else load_config()
    catalogue = load_catalogue(config)
    wanted = hosts or tuple(h for h in ("github", "gitlab")
                            if h in config.get("hosts", {}) and config["hosts"][h].get("org" if h == "github" else "group"))
    if not wanted:
        raise ConfigError("%s: no [hosts.github] org or [hosts.gitlab] group to inventory" % config.path)
    listings = {}
    for host in wanted:
        observed_at = clock.iso()
        listings[host] = (observed_at, list_host(config, host))
    out = []
    for host, (observed_at, items) in listings.items():
        scope = "repo_inventory:%s" % host
        snap = {"id": snapshot_id(scope), "scope": scope}
        rows, seen = [], set()
        for item, source in items:
            doc = _row(host, item, source, catalogue, observed_at, snap)
            if doc["subject"] in seen:          # a repo moved between pages while they were read: keep one row
                continue
            seen.add(doc["subject"])
            rows.append(doc)
        db.put_snapshot(scope, rows, config=config)
        out += rows
    return out
