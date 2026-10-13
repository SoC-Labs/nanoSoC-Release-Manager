"""Forks of the example projects (I1, and the check half of I2): read-only.

    forks(component=None, *, config=None, get=None) -> [fork_status observation]

1. **Find.** For each catalogue component of class `example` (or the one named), and each of its repos (canonical
   and copies), list the forks through the host's fork API, read ANONYMOUSLY (no token is sent, so only public
   forks are seen): GitHub `GET repos/<path>/forks`, GitLab `GET projects/<path>/forks`, 100 per page, at most
   MAX_PAGES pages. Forks registered by URL are not read: the registry has no field for them yet.
2. **Divergence.** The fork's default branch is fetched anonymously (`git fetch --filter=blob:none`) into a
   throwaway repository that borrows the upstream mirror's objects, so only the fork's own commits travel. Then:
   merge base with the upstream default branch, ahead/behind counts, `based_on` = the newest upstream tag the merge
   base contains, `releases_behind` = upstream release tags (vX.Y.Z) the merge base lacks.
3. **Errata.** Open or permanent errata of every configuration whose component is the upstream.
4. **User paths (I2).** Only when the upstream's `nanosoc-component.yaml` (default branch) declares a
   `fork_contract`: the files the fork changed since the merge base that lie outside `user_paths` (FAIL when any).
   Without a contract the check is NOT-RUN with the reason. `contract.upgrade_steps`: whether the upstream README
   or UPGRADE.md documents the upgrade (merge the release tag, then update the submodules).

Nothing is written to any fork or host. The throwaway repository lives in a temporary directory and is removed.
"""
from __future__ import annotations

import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Callable

from nanosoc_rm import clock, contracts
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.gitrun import run_git
from nanosoc_rm.mirror.layout import api_get, default_branch, git_url, mirror_path, ref_prefix, repo_key
from nanosoc_rm.releases.core import RELEASE_TAG, ReleaseUsage

__all__ = ["forks", "anonymous", "MAX_PAGES", "list_forks"]

MAX_PAGES = 5
_UPGRADE = re.compile(r"git\s+submodule\s+update", re.I)
_MERGE = re.compile(r"git\s+(merge|pull)\b", re.I)


def anonymous(config: Config) -> Config:
    """The config without credentials: every API read and fetch below is anonymous (I1: public forks)."""
    return Config({k: v for k, v in config.items() if k != "credentials"}, config.path)


def list_forks(config: Config, repo: dict, get: Callable = api_get) -> tuple[list[dict], list[dict], str | None]:
    """(forks [{host, path, default_branch}], sources, error) for one upstream repo."""
    host, path = repo["host"], repo["path"]
    enc = urllib.parse.quote(path, safe="")
    api_path = "repos/%s/forks" % path if host == "github" else "projects/%s/forks" % enc
    out, sources = [], []
    for page in range(1, MAX_PAGES + 1):
        ans = get(config, host, api_path, {"per_page": 100, "page": page})
        sources.append(ans.source)
        if ans.status != 200 or not isinstance(ans.body, list):
            why = ans.error or "HTTP %s" % ans.status
            if ans.status in (403, 429):
                why += " (anonymous rate limit or a private repo)"
            return out, sources, why
        for f in ans.body:
            fp = f.get("full_name") if host == "github" else f.get("path_with_namespace")
            if fp:
                out.append({"host": host, "path": fp, "default_branch": f.get("default_branch")})
        if len(ans.body) < 100:
            break
    return out, sources, None


def _git(cwd: Path, *args: str, timeout: float = 600):
    return run_git(*args, cwd=cwd, timeout=timeout, isolate=True,
                   env={"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o IdentitiesOnly=yes "
                        "-o IdentityFile=/dev/null -o IdentityAgent=none"})


def _upstream_file(mirror: Path, ref: str, name: str) -> str | None:
    r = run_git("show", "%s:%s" % (ref, name), cwd=mirror)
    return r.stdout if r.ok else None


def _contract(mirror: Path, ref: str) -> tuple[dict | None, bool | None]:
    """(fork_contract or None, upgrade steps documented?) of the upstream at `ref`."""
    text = _upstream_file(mirror, ref, "nanosoc-component.yaml")
    contract = None
    if text:
        try:
            man = contracts.load_yaml(text)
            contract = man.get("fork_contract") if isinstance(man, dict) else None
        except ValueError:
            contract = None
    docs = "\n".join(t for t in (_upstream_file(mirror, ref, "UPGRADE.md"), _upstream_file(mirror, ref, "README.md"))
                     if t)
    steps = bool(docs) and bool(_UPGRADE.search(docs)) and bool(_MERGE.search(docs))
    return contract, steps if docs else None


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in roots)


def _status(config: Config, component: str, upstream: dict, fork: dict, up_ref: str, mirror: Path,
            errata: list[str], contract: dict | None, steps: bool | None, sources: list[dict]) -> dict:
    fact = {"component": component, "upstream": {"host": upstream["host"], "path": upstream["path"]},
            "fork": {"host": fork["host"], "path": fork["path"]}, "default_branch": fork.get("default_branch"),
            "merge_base": None, "ahead": None, "behind": None, "based_on": None, "releases_behind": None,
            "errata": errata, "user_paths": {"verdict": "NOT-RUN", "outside": [], "reason": ""},
            "contract": {"declared": contract is not None, "upgrade_steps": steps}, "verdict": "UNVERIFIED",
            "reason": ""}
    if contract is None:
        fact["user_paths"]["reason"] = "the upstream declares no fork_contract in nanosoc-component.yaml (I2)"
    branch = fork.get("default_branch")
    url = git_url(config, fork["host"], fork["path"])
    with tempfile.TemporaryDirectory(prefix="rm-fork-") as tmp:
        work = Path(tmp) / "fork.git"
        r = run_git("init", "--quiet", "--bare", str(work), timeout=60)
        if not r.ok:
            fact["reason"] = "could not make a scratch repository"
            return fact
        (work / "objects" / "info" / "alternates").write_text(str(mirror / "objects") + "\n")
        spec = "refs/heads/%s:refs/fork" % branch if branch else "HEAD:refs/fork"
        r = _git(work, "fetch", "--quiet", "--no-tags", "--filter=blob:none", url, spec)
        sources.append({"kind": "git", "command": "git fetch --no-tags --filter=blob:none %s %s" % (url, spec)})
        if not r.ok:
            fact["reason"] = "the fork could not be fetched anonymously: %s" % (r.stderr.strip().splitlines() or ["?"])[-1][:200]
            return fact
        up = run_git("rev-parse", "--verify", "--quiet", up_ref + "^{commit}", cwd=mirror).stdout.strip()
        if not up:
            fact["reason"] = "the upstream default branch is not in the mirror (%s)" % up_ref
            return fact
        mb = _git(work, "merge-base", "refs/fork", up)
        if not mb.ok or not mb.stdout.strip():
            fact["reason"] = "the fork shares no history with %s:%s" % (upstream["host"], upstream["path"])
            fact["verdict"] = "WARN"
            return fact
        base = mb.stdout.strip()
        fact["merge_base"] = base
        counts = _git(work, "rev-list", "--left-right", "--count", "refs/fork...%s" % up).stdout.split()
        if len(counts) == 2:
            fact["ahead"], fact["behind"] = int(counts[0]), int(counts[1])
        # the upstream's tags live in its mirror (the scratch repository borrows objects, not refs)
        prefix = ref_prefix(upstream["host"], upstream["path"]) + "/tags/"
        merged = run_git("for-each-ref", "--merged", base, "--sort=-creatordate", "--format=%(refname)",
                         prefix.rstrip("/"), cwd=mirror).stdout.split()
        if merged:
            fact["based_on"] = merged[0][len(prefix):]
        released = [t[len(prefix):] for t in run_git("for-each-ref", "--merged", up, "--format=%(refname)",
                                                     prefix.rstrip("/"), cwd=mirror).stdout.split()]
        releases = [t for t in released if RELEASE_TAG.match(t)]
        if releases:
            have = {m[len(prefix):] for m in merged}
            fact["releases_behind"] = sum(1 for t in releases if t not in have)
        if contract is not None:
            changed = _git(work, "diff", "--name-only", base, "refs/fork").stdout.split("\n")
            roots = list(contract.get("user_paths") or [])
            outside = sorted(p for p in changed if p.strip() and not _under(p, roots))
            fact["user_paths"] = {"verdict": "FAIL" if outside else "PASS", "outside": outside[:200],
                                  "reason": "%d changed file(s) outside the declared user paths" % len(outside)
                                  if outside else "every change is inside the declared user paths"}
    late = (fact["behind"] or 0) > 0 or (fact["releases_behind"] or 0) > 0
    fact["verdict"] = "WARN" if late or fact["user_paths"]["verdict"] == "FAIL" else "PASS"
    fact["reason"] = ("behind the upstream by %s commit(s)%s" % (fact["behind"], "" if fact["releases_behind"] is None
                      else ", %d release(s)" % fact["releases_behind"])) if late else "up to date with the upstream"
    return fact


def forks(component: str | None = None, *, config: Config | str | None = None, get: Callable | None = None,
          command: str | None = None, store: bool = False) -> list[dict]:
    """fork_status observations for every public fork of the example projects (or of `component`); `store` keeps
    each as an event observation (the Forks page reads the stored ones)."""
    config = config if isinstance(config, Config) else load_config(config)
    anon = anonymous(config)
    get = get or api_get
    from nanosoc_rm.releases.builder import _registry
    reg = _registry(config)
    comps = [c for c in reg.components() if c.get("class") == "example"] if component is None else \
        [c for c in reg.components() if c["id"] == component]
    if component is not None and not comps:
        raise ReleaseUsage("no catalogue component %r" % component)
    errata_of: dict[str, list[str]] = {}
    for c in reg.configurations():
        errata_of.setdefault(c["component"], []).extend(
            e["id"] for e in reg.errata(c["id"]) if e["status"] in ("open", "permanent"))
    out = []
    for comp in comps:
        mirror = mirror_path(config, comp["id"])
        for repo, _role in reg.repos(comp["id"]):
            upstream = {"host": repo.host, "path": repo.path}
            if upstream["host"] not in (config.get("hosts") or {}):
                continue
            found, sources, err = list_forks(anon, upstream, get)
            key = repo_key(upstream["host"], upstream["path"])
            branch = default_branch(mirror, key) if (mirror / "HEAD").is_file() else None
            up_ref = "%s/heads/%s" % (ref_prefix(upstream["host"], upstream["path"]), branch) if branch else None
            contract, steps = _contract(mirror, up_ref) if up_ref else (None, None)
            now = clock.iso()
            for fork in found:
                src = list(sources)
                if up_ref is None:
                    fact = {"component": comp["id"], "upstream": upstream, "fork": {"host": fork["host"],
                            "path": fork["path"]}, "default_branch": fork.get("default_branch"), "merge_base": None,
                            "ahead": None, "behind": None, "based_on": None, "releases_behind": None,
                            "errata": errata_of.get(comp["id"], []),
                            "user_paths": {"verdict": "NOT-RUN", "outside": [], "reason": "no upstream mirror"},
                            "contract": {"declared": None, "upgrade_steps": None}, "verdict": "UNVERIFIED",
                            "reason": "no mirror of %s yet: run `nanosoc-rm mirror sync --component %s`"
                            % (comp["id"], comp["id"])}
                else:
                    fact = _status(anon, comp["id"], upstream, fork, up_ref, mirror, errata_of.get(comp["id"], []),
                                   contract, steps, src)
                obs = {"schema": 1, "kind": "fork_status",
                       "subject": "fork_status:%s:%s:%s" % (comp["id"], fork["host"], fork["path"]),
                       "observed_at": now, "sources": [{"kind": "derived", "command": command or
                                                        "nanosoc-rm forks %s" % comp["id"]}] + src, "fact": fact}
                problems = contracts.validate_observation(obs)
                if problems:
                    raise RuntimeError("fork_status does not meet its contract: %s" % "; ".join(problems[:3]))
                out.append(obs)
            if err:
                out.append(_unreadable(comp["id"], upstream, err, sources, command))
    if store:
        from nanosoc_rm import db
        for obs in out:
            db.put(obs, config=config)
    return out


def _unreadable(component: str, upstream: dict, err: str, sources: list[dict], command: str | None) -> dict:
    """A fork listing that failed is reported as one UNVERIFIED row naming the upstream itself as the 'fork'."""
    fact = {"component": component, "upstream": upstream, "fork": dict(upstream), "default_branch": None,
            "merge_base": None, "ahead": None, "behind": None, "based_on": None, "releases_behind": None, "errata": [],
            "user_paths": {"verdict": "NOT-RUN", "outside": [], "reason": "fork list not read"},
            "contract": {"declared": None, "upgrade_steps": None}, "verdict": "UNVERIFIED",
            "reason": "the fork list could not be read: %s" % err}
    return {"schema": 1, "kind": "fork_status",
            "subject": "fork_status:%s:%s:%s" % (component, upstream["host"], upstream["path"]),
            "observed_at": clock.iso(), "sources": [{"kind": "derived", "command": command or
                                                     "nanosoc-rm forks %s" % component}] + sources, "fact": fact}
