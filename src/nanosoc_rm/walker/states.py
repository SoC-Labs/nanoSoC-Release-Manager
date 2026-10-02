"""Per-host pin states (README section 5; interfaces.md section 4 step 4; B3).

For one pinned commit and one host holding a copy of the child, first match wins:

    UNREADABLE        the repo's latest mirror_status is not ok and the mirror holds no refs for it; or the host
                      answered 401/403/404 for the repo itself
    TAGGED            a tag other than keep/* points exactly at the commit
    ON_DEFAULT        reachable from the default branch; lag = commits the pin lacks
    PROTECTED_BY_TAG  not on the default branch, reachable from at least one tag
    BRANCH_ONLY       reachable only from non-default branches
    REWRITTEN         reachable now from no head or tag of this host, but an ancestor of an old head of the default
                      branch (this host's reflog in the mirror), or listed in catalogue known_rewrites for this host
    PR_ONLY           the host's API has the commit and links it to a pull or merge request
    ORPHAN            the host's API has the commit and no ref of any kind reaches it
    MISSING           the host does not have the commit, and the repo itself is readable

"Reachable" always means from a branch or tag OF THAT HOST: only the host's own namespace `refs/r/<key>/` is read,
although the mirror holds both hosts' objects. The `.gitmodules` `branch=` value is never read here.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config
from nanosoc_rm.mirror.catalogue import Entry
from nanosoc_rm.mirror.layout import API_TIMEOUT_S, api_get, default_branch, git_env, repo_key, run_in

__all__ = ["HostJudge", "WalkError", "GREEN_STATES", "RED_STATES"]

GREEN_STATES = ("TAGGED", "ON_DEFAULT", "PROTECTED_BY_TAG")
RED_STATES = ("LOCAL_URL", "NO_GITLINK", "UNREADABLE", "REWRITTEN", "PR_ONLY", "ORPHAN", "MISSING")   # + BRANCH_ONLY when frozen


class WalkError(RuntimeError):
    """The walk could not answer (a host API failed in a way that decides nothing). CLI exit 2."""


@dataclass
class _RepoView:
    """What the mirror knows about one repo: its namespace, default branch, whether it holds refs, last sync."""
    key: str
    has_refs: bool
    default: str | None
    status: dict | None                         # latest mirror_status fact, or None
    reflog_heads: list[str] | None = None       # old heads of the default branch, loaded on first need


@dataclass
class HostJudge:
    """Judges pins host by host, reading one mirror per entry. Caches per (repo, commit)."""
    config: Config
    _views: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)
    _client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=API_TIMEOUT_S, follow_redirects=True)
        return self._client

    def view(self, mirror: Path, repo: dict) -> _RepoView:
        k = (str(mirror), repo["host"], repo["path"])
        if k not in self._views:
            key = repo_key(repo["host"], repo["path"])
            has_refs = False
            if (mirror / "HEAD").is_file():
                r = run_in(mirror, "for-each-ref", "--count=1", "--format=%(refname)", "refs/r/%s/" % key)
                has_refs = r.ok and bool(r.stdout.strip())
            subject = "mirror_status:%s:%s" % (repo["host"], repo["path"])
            rows = [d for d in db.latest("mirror_status", subject, config=self.config) if d["subject"] == subject]
            self._views[k] = _RepoView(key, has_refs, default_branch(mirror, key), rows[-1]["fact"] if rows else None)
        return self._views[k]

    # -------------------------------------------------------------------------------------------------------
    def judge(self, mirror: Path, entry: Entry, repo: dict, role: str, pinned: bool, commit: str) -> dict:
        """The pin-state host_state document of `commit` on `repo`'s host."""
        ck = (str(mirror), repo["host"], repo["path"], commit)
        if ck not in self._cache:
            self._cache[ck] = self._judge(mirror, entry, repo, commit)
        state, default, lag, branches, tags, sources, observed_at = self._cache[ck]
        return {"repo": dict(repo), "role": role, "pinned": pinned, "state": state, "default_branch": default,
                "lag": lag, "branches": list(branches), "tags": list(tags), "observed_at": observed_at,
                "sources": list(sources)}

    def _judge(self, mirror: Path, entry: Entry, repo: dict, commit: str):
        view = self.view(mirror, repo)
        now = clock.iso()
        status = view.status or {}
        observed_at = status.get("last_success_at") or now
        if not view.has_refs and not status.get("ok"):
            src = {"kind": "derived", "command": "mirror_status:%s:%s ok=%s, and git -C %s for-each-ref refs/r/%s/ "
                                                 "lists no ref" % (repo["host"], repo["path"], status.get("ok"),
                                                                   mirror, view.key)}
            return "UNREADABLE", view.default, None, [], [], [src], now
        ns = "refs/r/%s" % view.key
        sources = []
        env = git_env(self.config, repo["host"])
        exists = run_in(mirror, "cat-file", "-e", "%s^{commit}" % commit, env=env)
        sources.append(exists.source())
        heads, tags, points = [], [], []
        if exists.ok:
            cont = run_in(mirror, "for-each-ref", "--contains", commit, "--format=%(refname)", ns + "/heads",
                          ns + "/tags")
            sources.append(cont.source())
            for ref in cont.stdout.split():
                if ref.startswith(ns + "/heads/"):
                    heads.append(ref[len(ns + "/heads/"):])
                elif ref.startswith(ns + "/tags/"):
                    tags.append(ref[len(ns + "/tags/"):])
            if tags:
                pts = run_in(mirror, "for-each-ref", "--points-at", commit, "--format=%(refname)", ns + "/tags")
                sources.append(pts.source())
                points = [r[len(ns + "/tags/"):] for r in pts.stdout.split()]
        heads.sort()
        tags.sort()
        default = view.default
        lag = None
        if default and default in heads:
            cnt = run_in(mirror, "rev-list", "--count", "%s..%s/heads/%s" % (commit, ns, default)).check()
            sources.append(cnt.source())
            lag = int(cnt.stdout.strip())
        if any(not t.startswith("keep/") for t in points):
            return "TAGGED", default, lag, heads, tags, sources, observed_at
        if lag is not None:
            return "ON_DEFAULT", default, lag, heads, tags, sources, observed_at
        if tags:
            return "PROTECTED_BY_TAG", default, None, heads, tags, sources, observed_at
        if heads:
            return "BRANCH_ONLY", default, None, heads, tags, sources, observed_at
        # Reachable from no head or tag of this host: the host's reflog, then known_rewrites, then the host's API.
        if exists.ok and default:
            for old in self._reflog_heads(mirror, view, sources):
                anc = run_in(mirror, "merge-base", "--is-ancestor", commit, old)
                if anc.returncode == 0:
                    sources.append(anc.source())
                    return "REWRITTEN", default, None, heads, tags, sources, observed_at
        for rw in entry.known_rewrites:
            if rw["host"] == repo["host"] and commit in rw["lost"]:
                sources.append({"kind": "registry", "command": "catalogue.yaml %s known_rewrites (%s %s, %s)"
                                % (entry.id, rw["host"], rw["branch"], rw["date"])})
                return "REWRITTEN", default, None, heads, tags, sources, observed_at
        state = self._ask_host(repo, commit, sources)
        return state, default, None, heads, tags, sources, now

    def _reflog_heads(self, mirror: Path, view: _RepoView, sources: list) -> list[str]:
        if view.reflog_heads is None:
            r = run_in(mirror, "reflog", "show", "--format=%H", "refs/r/%s/heads/%s" % (view.key, view.default))
            sources.append(r.source())
            seen, heads = set(), []
            for sha in r.stdout.split() if r.ok else []:
                if sha not in seen:
                    seen.add(sha)
                    heads.append(sha)
            view.reflog_heads = heads
        return view.reflog_heads

    # -------------------------------------------------------------------------------------------------------
    def _get(self, host: str, path: str, sources: list):
        ans = api_get(self.config, host, path, client=self._http())
        sources.append(ans.source)
        return ans

    def _ask_host(self, repo: dict, commit: str, sources: list) -> str:
        """PR_ONLY, ORPHAN, MISSING or UNREADABLE, from the host's read API (GET only)."""
        host, path = repo["host"], repo["path"]
        if host == "github":
            base = "repos/%s" % path
            commit_path, prs_path = "%s/commits/%s" % (base, commit), "%s/commits/%s/pulls" % (base, commit)
        else:
            base = "projects/%s" % urllib.parse.quote(path, safe="")
            commit_path = "%s/repository/commits/%s" % (base, commit)
            prs_path = "%s/repository/commits/%s/merge_requests" % (base, commit)
        ans = self._get(host, commit_path, sources)
        if ans.status == 200:
            prs = self._get(host, prs_path, sources)
            if prs.status != 200 or not isinstance(prs.body, list):
                raise WalkError("%s: %s answered %s" % (host, prs.source["command"], prs.error or prs.status))
            return "PR_ONLY" if prs.body else "ORPHAN"
        if ans.status in (401, 403):
            return "UNREADABLE"
        if ans.status in (404, 422):
            whole = self._get(host, base, sources)
            if whole.status == 200:
                return "MISSING"
            if whole.status in (401, 403, 404):
                return "UNREADABLE"
            raise WalkError("%s: %s answered %s" % (host, whole.source["command"], whole.error or whole.status))
        raise WalkError("%s: %s answered %s" % (host, ans.source["command"], ans.error or ans.status))
