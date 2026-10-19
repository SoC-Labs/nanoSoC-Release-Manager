"""Mirror sync, mirror status and the rewrite detector (interfaces.md sections 3 and 8; H6, B8).

    sync_component(component_id) -> [mirror_status per repo] + [rewrite_alert per rewritten default branch]

One sync of one component: for each of its repos (canonical and copies, or the external's repo):

1. read the default branch the host advertises (`git ls-remote --symref <url> HEAD`);
2. fetch heads and tags only, into the repo's own namespace (explicit refspecs, never `--mirror`, always
   `--no-tags`, so no PR/MR refs and no tag auto-follow outside the namespace):

       git -C <id>.git fetch --no-tags --prune --filter=blob:none <url> \\
           +refs/heads/*:refs/r/<key>/heads/*  +refs/tags/*:refs/r/<key>/tags/*

3. if the default branch kept its NAME and its head moved to a commit that does not descend from the old head,
   record a rewrite_alert and raise a `rewrite` alert (section 8);
4. write one mirror_status: ok, the time of the last success, and the error of a failed fetch. A failed fetch
   keeps the old refs and the previous `last_success_at`, and raises a `mirror_failed` alert when the repo was
   fine before. A mirror is never deleted or re-cloned.

Everything returned is stored: the mirror_status rows as one snapshot (scope `mirror_status:<component>`), each
rewrite_alert as an event.

Private repos: when the anonymous fetch fails the way a private repo fails (`private_read.RETRY_CAUSES`)
and the host has a `private_read` method, steps 1 and 2 run once more with it (`--filter=blob:limit=1m`). The
mirror_status says how the repo was read: `access` anonymous, credentials (private, readable with credentials) or
unreadable, with the `cause` of a failure and the `method` tried.
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.mirror.catalogue import Catalogue, Entry, load_catalogue
from nanosoc_rm.mirror.layout import (FETCH_TIMEOUT_S, default_branch, ensure_mirror, git_env, git_url, repo_key,
                                      run_in, set_default_branch, snapshot_id,
                                      split_key)
from nanosoc_rm.mirror import private_read

__all__ = ["sync_component", "sync_all", "detect_rewrite", "mirror_status", "fetch_argv", "failure_message",
           "access_summary"]


def fetch_argv(url: str, key: str, filter_spec: str = "--filter=blob:none") -> list[str]:
    """The section 3 fetch, after `git -C <mirror>` (a credentialed fetch passes private_read.CREDENTIAL_FILTER)."""
    return ["fetch", "--no-tags", "--prune", filter_spec, url,
            "+refs/heads/*:refs/r/%s/heads/*" % key, "+refs/tags/*:refs/r/%s/tags/*" % key]


def _head(mirror: Path, key: str, branch: str | None) -> str | None:
    if not branch:
        return None
    r = run_in(mirror, "rev-parse", "--verify", "--quiet", "refs/r/%s/heads/%s^{commit}" % (key, branch))
    return (r.stdout.strip() or None) if r.ok else None


def _advertised_default(mirror: Path, url: str, env: dict) -> tuple[str | None, object]:
    r = run_in(mirror, "ls-remote", "--symref", url, "HEAD", env=env, timeout=FETCH_TIMEOUT_S)
    if r.ok:
        for line in r.stdout.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                return line[len("ref: refs/heads/"):-len("\tHEAD")], r
    return None, r


def _previous_status(config: Config, host: str, path: str) -> dict | None:
    rows = db.latest("mirror_status", "mirror_status:%s:%s" % (host, path), config=config)
    rows = [d for d in rows if d["subject"] == "mirror_status:%s:%s" % (host, path)]
    return rows[-1]["fact"] if rows else None


def _error_text(result) -> str:
    lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
    return " / ".join(lines[-3:]) or ("exit %s" % result.returncode)


def detect_rewrite(mirror: Path, key: str, branch: str, old: str, new: str, *,
                   config: Config | None = None) -> dict | None:
    """B8: a rewrite_alert observation when `new` does not descend from `old` (same branch name), else None.

    lost_commits = `git rev-list --count <old> --not --glob=refs/r/<key>/*` (commits of the old head that no
    branch or tag of this host reaches now); pinned_lost = the pins, in the latest stored pin_state snapshot of
    every configuration, of this mirror's component whose commit is among them. Pure: stores nothing."""
    mirror = Path(mirror)
    if old == new:
        return None
    anc = run_in(mirror, "merge-base", "--is-ancestor", old, new)
    if anc.returncode == 0:
        return None
    if anc.returncode != 1:
        raise RuntimeError("cannot decide whether %s descends from %s: %s" % (new, old, _error_text(anc)))
    glob = "--glob=refs/r/%s/*" % key
    count = run_in(mirror, "rev-list", "--count", old, "--not", glob).check()
    listing = run_in(mirror, "rev-list", old, "--not", glob).check()
    lost = set(listing.stdout.split())
    component = mirror.name[:-len(".git")] if mirror.name.endswith(".git") else mirror.name
    cfg = config if config is not None else load_config()
    pinned = []
    for doc in db.latest("pin_state", config=cfg):
        f = doc["fact"]
        if f["child"]["commit"] in lost and f["child"]["component"] in (component, None):
            for tree_path in f["tree_paths"]:
                pinned.append({"configuration": f["configuration"], "tree_path": tree_path,
                               "commit": f["child"]["commit"]})
    pinned.sort(key=lambda p: (p["configuration"], p["tree_path"]))
    host, path = split_key(key)
    fact = {"component": component, "repo": {"host": host, "path": path}, "branch": branch, "old_head": old,
            "new_head": new, "lost_commits": int(count.stdout.strip()), "pinned_lost": pinned}
    return {"schema": 1, "kind": "rewrite_alert",
            "subject": "rewrite_alert:%s:%s:%s:%s" % (host, path, branch, old),
            "observed_at": clock.iso(), "sources": [anc.source(), count.source()], "fact": fact}


def _sync_repo(config: Config, mirror: Path, entry: Entry, repo: dict
               ) -> tuple[dict, dict | None, list[dict], dict | None]:
    """(mirror_status fact, rewrite_alert or None, sources, the previous mirror_status fact) for one repo."""
    host, path = repo["host"], repo["path"]
    key = repo_key(host, path)
    url = git_url(config, host, path)
    env = git_env(config, host)
    old_default = default_branch(mirror, key)
    old_head = _head(mirror, key, old_default)
    new_default, symref = _advertised_default(mirror, url, env)
    fetch = run_in(mirror, *fetch_argv(url, key), env=env, timeout=FETCH_TIMEOUT_S)
    sources = [symref.source(), fetch.source()]
    previous = _previous_status(config, host, path)
    access, cause, used, error = "anonymous", None, None, None
    if not fetch.ok:
        cause = private_read.classify(fetch.stderr)
        error = _error_text(fetch)
        if cause in private_read.RETRY_CAUSES:
            try:
                method = private_read.method(config, host)
                penv = method.env() if method is not None else None
            except ConfigError as e:                      # names the host, never a secret
                method, penv, cause, error = None, None, "config", str(e)
            if method is not None:
                used = method.kind
                purl = method.url(path)
                new_default, symref = _advertised_default(mirror, purl, penv)
                fetch = run_in(mirror, *fetch_argv(purl, key, private_read.CREDENTIAL_FILTER), env=penv,
                               timeout=FETCH_TIMEOUT_S)
                sources += [symref.source(), fetch.source()]
                if fetch.ok:
                    access, cause, error = "credentials", None, None
                else:
                    cause, error = private_read.classify(fetch.stderr), _error_text(fetch)
    alert = None
    if fetch.ok:
        if new_default and new_default != old_default:
            set_default_branch(mirror, key, new_default)
        # B8 is keyed on the branch NAME: a change of which branch is the default is not a rewrite.
        if old_head and new_default in (None, old_default):
            new_head = _head(mirror, key, old_default)
            if new_head and new_head != old_head:
                alert = detect_rewrite(mirror, key, old_default, old_head, new_head, config=config)
        fact = {"repo": {"host": host, "path": path}, "component": entry.id, "ok": True,
                "last_success_at": clock.iso(), "error": None, "access": access, "cause": None, "method": used}
    else:
        fact = {"repo": {"host": host, "path": path}, "component": entry.id, "ok": False,
                "last_success_at": previous["last_success_at"] if previous else None, "error": error,
                "access": "unreadable", "cause": cause, "method": used}
    return fact, alert, sources, previous


def _failure_way(fact: dict) -> tuple:
    """What decides a failed repo's alert group: the read method tried, and whether private_read is misconfigured
    (other cause changes, such as a network flap, do not raise the alert again)."""
    return fact.get("method"), fact.get("cause") == "config"


def failure_message(fact: dict) -> str:
    """The `mirror_failed` alert text: the cause first, so alerts group by it (web.alertgroups)."""
    repo = "%s:%s" % (fact["repo"]["host"], fact["repo"]["path"])
    if fact.get("method"):
        return "mirror fetch of %s failed with read credentials (%s): %s" % (repo, fact["method"], fact["error"])
    if fact.get("cause") == "config":
        return "mirror fetch of %s failed: private_read is misconfigured: %s" % (repo, fact["error"])
    if fact.get("cause") in private_read.RETRY_CAUSES:
        return "mirror fetch of %s failed: private or missing, no private_read for %s: %s" % (
            repo, fact["repo"]["host"], fact["error"])
    return "mirror fetch of %s failed: %s" % (repo, fact["error"])


def sync_component(component_id: str, host: str | None = None, *, config: Config | None = None,
                   catalogue: Catalogue | None = None) -> list[dict]:
    """Sync one catalogue component or external (optionally only its repos on `host`). Returns and stores one
    mirror_status per repo, plus a rewrite_alert per rewritten default branch. KeyError for an unknown id."""
    config = config if config is not None else load_config()
    catalogue = catalogue if catalogue is not None else load_catalogue(config)
    entry = catalogue.entry(component_id)
    mirror = ensure_mirror(config, entry.id)
    scope = "mirror_status:%s" % entry.id
    snap = {"id": snapshot_id(scope), "scope": scope}
    statuses, alerts = [], []
    for repo, _role in entry.repos:
        if host is not None and repo["host"] != host:
            continue
        fact, alert, sources, previous = _sync_repo(config, mirror, entry, repo)
        doc = {"schema": 1, "kind": "mirror_status", "subject": "mirror_status:%s:%s" % (repo["host"], repo["path"]),
               "observed_at": clock.iso(), "sources": sources, "snapshot": snap, "fact": fact}
        statuses.append(doc)
        if not fact["ok"] and (previous is None or previous["ok"] or _failure_way(previous) != _failure_way(fact)):
            # Raised when a repo starts failing (not on every failed poll); it re-opens after a recovery, and is
            # raised again (open, even if a person cleared it) when the way it fails changes: read credentials
            # configured then refused, or a private_read that is misconfigured.
            db.raise_alert("mirror_failed", doc["subject"], failure_message(fact), sources, config=config)
        if alert is not None:
            alerts.append(alert)
    carried = []
    if host is not None:
        # A host-restricted sync carries the other host's latest row into the new snapshot, so the component's
        # current status stays complete. Only the rows of this sync are returned.
        for repo, _role in entry.repos:
            if repo["host"] != host:
                subject = "mirror_status:%s:%s" % (repo["host"], repo["path"])
                carried += [dict(d, snapshot=snap) for d in db.latest("mirror_status", subject, config=config)
                            if d["subject"] == subject]
    db.put_snapshot(scope, statuses + carried, config=config)
    for alert in alerts:
        db.put(alert, config=config)
        f = alert["fact"]
        db.raise_alert("rewrite", alert["subject"],
                       "%s:%s %s was rewritten: %s -> %s, %d commits lost, %d registered pin(s) lost"
                       % (f["repo"]["host"], f["repo"]["path"], f["branch"], f["old_head"][:12], f["new_head"][:12],
                          f["lost_commits"], len(f["pinned_lost"])), alert["sources"], config=config)
    return statuses + alerts


def sync_all(host: str | None = None, *, config: Config | None = None) -> list[dict]:
    """Sync every catalogue component and external (ignored repos are never mirrored)."""
    config = config if config is not None else load_config()
    catalogue = load_catalogue(config)
    out = []
    for entry in catalogue.entries():
        if host is None or any(r["host"] == host for r, _ in entry.repos):
            out += sync_component(entry.id, host, config=config, catalogue=catalogue)
    return out


def mirror_status(component_id: str | None = None, *, config: Config | None = None) -> list[dict]:
    """The latest mirror_status of every mirrored repo (of one component), from the store."""
    config = config if config is not None else load_config()
    docs = db.latest("mirror_status", config=config)
    if component_id is not None:
        docs = [d for d in docs if d["fact"]["component"] == component_id]
    return sorted(docs, key=lambda d: (d["fact"]["component"] or "", d["fact"]["repo"]["host"],
                                       d["fact"]["repo"]["path"]))


def access_summary(docs: list[dict]) -> list[dict]:
    """Mirror rows counted by how they were read: [{access, cause, method, count, by_host}], unreadable first,
    biggest first. Rows stored before private reads existed have no `access`: ok is anonymous, a failure is
    unreadable."""
    out: dict[tuple, dict] = {}
    for d in docs:
        f = d["fact"]
        access = f.get("access") or ("anonymous" if f["ok"] else "unreadable")
        cause = f.get("cause") if not f["ok"] else None
        if not f["ok"] and cause is None:
            cause = private_read.classify(f.get("error"))
        k = (access, cause, f.get("method"))
        row = out.setdefault(k, {"access": access, "cause": cause, "method": f.get("method"), "count": 0,
                                 "by_host": {}})
        row["count"] += 1
        h = f["repo"]["host"]
        row["by_host"][h] = row["by_host"].get(h, 0) + 1
    order = {"unreadable": 0, "credentials": 1, "anonymous": 2}
    return sorted(out.values(), key=lambda r: (order.get(r["access"], 3), -r["count"], str(r["cause"]),
                                               str(r["method"])))
