"""The org scan: new repos seen in the org and the group, not in the catalogue (interfaces.md section 23.2).

    org_scan(config=None, hosts=None, store=False) -> ScanResult(docs, errors, listed)

Read-only and anonymous. It lists the configured GitHub org (`[hosts.github] org`) and GitLab group
(`[hosts.gitlab] group`, with subgroups) through the inventory's own pager (`mirror.inventory.list_host`, GET
only, every page) with NO credential, even when the service has a read token: what a stranger can see is what
the scan reports. Each listed repo is resolved against the catalogue (canonical, copy, alias, external,
ignored); one the catalogue does not hold becomes a `repo_seen` observation:

    subject   repo_seen:<host>:<path>
    snapshot  scope repo_seen:<host>: one complete snapshot per host per scan (empty when nothing is new, so
              a repo that has since been catalogued or ignored stops being current)
    fact      {repo, visibility, default_branch, created_on, description, mentions_nanosoc, fork, first_seen,
               listing}   (`fork`: the listing's own flag, GitHub `fork`, GitLab `forked_from_project`)

`first_seen` carries over from the current stored fact of the same subject, so the new-repo queue can say how
long a repo has waited. A host whose listing fails stores nothing (its previous snapshot stays current and shows
its age) and is reported in `errors`; the other host is still scanned. Private repos are invisible to an
anonymous listing, so the scan can miss a private repo; `inventory` (with the read token) is the A1 census.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, ConfigError, load_config

__all__ = ["org_scan", "ScanResult", "DESCRIPTION_MAX", "scanned_hosts"]

DESCRIPTION_MAX = 300
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
_NANOSOC = re.compile(r"nano[-_ ]?soc", re.I)


@dataclass
class ScanResult:
    docs: list[dict] = field(default_factory=list)          # repo_seen observations, GitHub first
    errors: dict[str, str] = field(default_factory=dict)    # host -> why its listing failed
    listed: dict[str, int] = field(default_factory=dict)    # host -> repos listed


def scanned_hosts(config: Config) -> tuple[str, ...]:
    """The hosts with an org (GitHub) or group (GitLab) configured, GitHub first."""
    hosts = config.get("hosts", {})
    return tuple(h for h in ("github", "gitlab")
                 if h in hosts and hosts[h].get("org" if h == "github" else "group"))


def _path(host: str, item: dict) -> str | None:
    p = item.get("full_name") if host == "github" else item.get("path_with_namespace")
    return p if isinstance(p, str) and p else None


def _fact(host: str, path: str, item: dict, first_seen: str) -> dict:
    from nanosoc_rm.mirror.inventory import _visibility
    desc = item.get("description")
    desc = " ".join(desc.split())[:DESCRIPTION_MAX] if isinstance(desc, str) and desc.strip() else None
    created = item.get("created_at")
    m = _DATE.match(created) if isinstance(created, str) else None
    name = path.rsplit("/", 1)[-1]
    return {"repo": {"host": host, "path": path}, "visibility": _visibility(host, item),
            "default_branch": item.get("default_branch") or None, "created_on": m.group(1) if m else None,
            "description": desc, "mentions_nanosoc": bool(_NANOSOC.search(name) or _NANOSOC.search(desc or "")),
            "fork": bool(item.get("fork")) if host == "github" else bool(item.get("forked_from_project")),
            "first_seen": first_seen, "listing": "anonymous"}


def org_scan(*, config: Config | None = None, hosts: tuple[str, ...] | None = None,
             store: bool = False) -> ScanResult:
    """List each host anonymously and return (and with `store`, store) a repo_seen observation per repo the
    catalogue does not hold. ConfigError when no host has an org or group configured."""
    from nanosoc_rm.mirror.catalogue import load_catalogue
    from nanosoc_rm.mirror.inventory import InventoryError, list_host
    from nanosoc_rm.mirror.layout import snapshot_id
    config = config if config is not None else load_config()
    catalogue = load_catalogue(config)
    wanted = hosts or scanned_hosts(config)
    if not wanted:
        raise ConfigError("%s: no [hosts.github] org or [hosts.gitlab] group to scan" % config.path)
    known = {d["subject"]: d["fact"]["first_seen"] for d in db.latest("repo_seen", config=config)} if store else {}
    out = ScanResult()
    for host in wanted:
        observed_at = clock.iso()
        try:
            items = list_host(config, host, anonymous=True)
        except (InventoryError, ConfigError) as e:
            out.errors[host] = str(e)
            continue
        out.listed[host] = len(items)
        scope = "repo_seen:%s" % host
        snap = {"id": snapshot_id(scope), "scope": scope}
        rows, seen = [], set()
        for item, source in items:
            path = _path(host, item)
            if path is None:
                continue
            _cid, role, _known = catalogue.resolve(host, path)
            if role != "unclassified":
                continue
            subject = "repo_seen:%s:%s" % (host, path)
            if subject in seen:
                continue
            seen.add(subject)
            rows.append({"schema": 1, "kind": "repo_seen", "subject": subject, "observed_at": observed_at,
                         "sources": [source], "snapshot": snap,
                         "fact": _fact(host, path, item, known.get(subject, observed_at))})
        if store:
            db.put_snapshot(scope, rows, config=config)
        out.docs += rows
    return out
