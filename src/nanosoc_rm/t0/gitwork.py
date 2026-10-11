"""Git work for the T0 gates: anonymous fetches, canonical-ref clones and the pinned-tree walk.

Everything goes through the one git runner (`nanosoc_rm.gitrun.run_git`, isolate=True: no global or system
config, no prompts, ssh BatchMode). Nothing here writes outside the job's own work directory.

Two walks share one pass over the tree (interfaces.md section 9):

- **pinned**: each pin is fetched as compute's pin-check workflow does, `git fetch --depth=1 <url as pinned>
  <sha>` into an empty repo (t0.pins.fetchable). An ssh or local URL is never attempted: no anonymous fetch
  can use it. Its .gitmodules is read from what was fetched.
- **canonical**: each pin's canonical https URL (`request.canonical_urls`, else the pinned URL with ssh
  rewritten to https) is cloned bare and treeless, heads and tags only (`git clone --bare --filter=tree:0`;
  never PR or MR refs), and the pin is judged with `git for-each-ref --contains` (t0.pins.reachable: the
  toolkit check-pins method against fetched refs, never `ls-remote` tips). When the pinned fetch failed, the
  walk reads the pin's .gitmodules through the canonical clone, so the pins below an ssh-pinned parent are
  still listed (G15 in the gate-2 gap index).

`url_map` (tests only; Context refuses it under CI_JOB_ID) routes URLs to local repos with `url.insteadOf`
in a per-run git config file, so the gates run the same git commands as in a real job. In a real job the only
routes are the negative controls' own: reserved `.invalid` URLs of scratch repos they built (gates.NEGCTL_HOST).
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from nanosoc_rm.gitmodules import parse_gitmodules as _parse_gitmodules
from nanosoc_rm.gitrun import GitResult, run_git

__all__ = ["Fetch", "Clone", "Pin", "GitWork", "parse_gitmodules", "url_kind", "to_https", "resolve_relative",
           "repo_key", "classify_failure", "GIT_TIMEOUT_S", "GITHUB_WEB"]

GIT_TIMEOUT_S = 300
GITHUB_WEB = "https://github.com"
MAX_DEPTH = 10

# ---------------------------------------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------------------------------------
_SCP = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9_.-]+):(?!//)(.+)$")


def url_kind(url: str) -> str:
    """https | ssh | local | relative | other (defs.schema.json url_kind)."""
    u = url.strip()
    if u.startswith(("./", "../")):
        return "relative"
    if u.startswith("/") or u.startswith("file://"):
        return "local"
    if u.startswith("ssh://") or u.startswith("git+ssh://") or (_SCP.match(u) and "://" not in u):
        return "ssh"
    if u.startswith("https://"):
        return "https"
    return "other"


def to_https(url: str) -> str:
    """An ssh URL as the https URL of the same repo (git@h:p.git, ssh://git@h[:port]/p.git); others unchanged."""
    u = url.strip()
    if u.startswith(("ssh://", "git+ssh://")):
        parts = urlsplit(u.replace("git+ssh://", "ssh://", 1))
        return "https://%s/%s" % (parts.hostname, parts.path.lstrip("/"))
    m = _SCP.match(u)
    if m and "://" not in u and not u.startswith(("/", "./", "../")):
        return "https://%s/%s" % (m.group(1), m.group(2).lstrip("/"))
    return u


def resolve_relative(url: str, parent_url: str) -> str:
    """A relative submodule URL resolved against the parent's URL, as git does (../x.git from
    https://h/g/p.git is https://h/g/x.git)."""
    base = parent_url.rstrip("/")
    rel = url.strip()
    while True:
        if rel.startswith("./"):
            rel = rel[2:]
        elif rel.startswith("../"):
            rel = rel[3:]
            base = base.rsplit("/", 1)[0] if "/" in base.split("://", 1)[-1] else base
        else:
            break
    return base + "/" + rel


def repo_key(url: str) -> str:
    """One spelling per repo: lower-case host and path, no scheme, no user, no .git, no trailing slash."""
    u = to_https(url.strip())
    if "://" in u:
        parts = urlsplit(u)
        host, path = (parts.hostname or ""), parts.path
    else:
        host, path = "", u
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return ("%s/%s" % (host, path)).lower()


def url_path(url: str) -> str:
    """The namespace path of an https URL, without .git (SoC-Labs/NanoSoC-Ethernet-Chiplet)."""
    p = urlsplit(to_https(url)).path.strip("/")
    return p[:-4] if p.endswith(".git") else p


# ---------------------------------------------------------------------------------------------------------
# Failure classes (compute's pin-check workflow): not-our-ref and auth are FAIL; an unreachable host is INFRA.
# ---------------------------------------------------------------------------------------------------------
_NOT_OUR_REF = re.compile(r"not our ref|couldn't find remote ref|unadvertised object|no such remote ref|"
                          r"not a valid object|did not send all necessary objects|reference is not a tree|"
                          r"upload-pack: not our ref|remote error: .*(object|ref)", re.I)
_UNREACHABLE = re.compile(r"could not resolve host|failed to connect|connection refused|connection timed out|"
                          r"operation timed out|timed out after|network is unreachable|no route to host|"
                          r"connection reset|ssl|gnutls|tls|the requested url returned error: 5\d\d|"
                          r"empty reply from server|temporary failure in name resolution", re.I)
_AUTH = re.compile(r"could not read username|could not read password|authentication failed|"
                   r"terminal prompts disabled|repository not found|not found|access denied|"
                   r"the requested url returned error: 40[134]|does not appear to be a git repository|"
                   r"could not read from remote repository|permission denied|http basic", re.I)


def classify_failure(result: GitResult) -> str:
    """not-our-ref | unreachable | auth | error, from a failed git call's stderr."""
    err = result.stderr or ""
    if result.timed_out:
        return "unreachable"
    if _NOT_OUR_REF.search(err):
        return "not-our-ref"
    if _UNREACHABLE.search(err):
        return "unreachable"
    if _AUTH.search(err):
        return "auth"
    return "error"


def _tail(result: GitResult) -> str:
    lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
    return " / ".join(lines[-2:])[:240]


# ---------------------------------------------------------------------------------------------------------
# .gitmodules
# ---------------------------------------------------------------------------------------------------------
def parse_gitmodules(text: str) -> list[dict]:
    """[{name, path, url, branch}] in file order, from the shared git-config reader (nanosoc_rm.gitmodules). An
    entry without path or url is dropped (git ignores it too)."""
    out = []
    for m in _parse_gitmodules(text or ""):
        e = {"name": m["name"], "path": m.get("path"), "url": m.get("url"), "branch": m.get("branch")}
        if e["path"] and e["url"]:
            out.append(e)
    return out


# ---------------------------------------------------------------------------------------------------------
# Fetches and clones, cached per run
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Fetch:
    ok: bool
    cls: str                     # ok | ssh | local | not-our-ref | auth | unreachable | error
    url: str
    commit: str | None
    repo: Path | None = None
    fetched: str | None = None   # rev-parse FETCH_HEAD^{commit}
    why: str = ""
    command: str = ""


@dataclass
class Clone:
    ok: bool
    cls: str                     # ok | auth | unreachable | error
    url: str
    repo: Path | None = None
    why: str = ""
    command: str = ""


@dataclass
class Pin:
    tree_path: str               # "" for the superproject
    url: str                     # as pinned in the parent's .gitmodules (the request's repo URL for the root)
    commit: str | None
    parent: "Pin | None" = None
    resolved: str | None = None  # absolute URL (relative URLs resolved against the parent's)
    kind: str = "https"
    canonical: str | None = None
    host: str | None = None      # host key of the canonical URL
    duplicate: bool = False      # (repo, commit) seen earlier in the walk: judged, not recursed
    gitmodules: str | None = None
    gm_repo: Path | None = None  # where the .gitmodules was read
    gm_via: str | None = None    # pinned | canonical
    children: list["Pin"] = field(default_factory=list)
    gitlink_read_from_pinned: bool = False   # the parent's gitlink was read in a pinned-URL fetch
    override_of: str | None = None   # local mode (CR-11): the gitlink this pin's commit replaced (`pin_overrides`)

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class GitWork:
    """One run's git: environment, work directory and caches. Not thread-safe."""

    def __init__(self, workdir: Path, *, url_map: Mapping[str, str] | None = None,
                 host_bases: Mapping[str, str] | None = None, extra_env: Mapping[str, str] | None = None):
        # Absolute: git reads GIT_CONFIG_GLOBAL (the routing file under workdir) AFTER `-C <dir>`, so a relative
        # workdir (`t0 run --out rm-bundle`) made every routed URL unreachable (the controls never fired).
        self.workdir = Path(workdir).absolute()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.url_map = dict(url_map or {})
        self.host_bases = {"github": GITHUB_WEB}
        self.host_bases.update({k: v.rstrip("/") for k, v in (host_bases or {}).items() if v})
        self.env = {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes"}
        self.env.update(extra_env or {})
        self._extra = dict(extra_env or {})
        self._fetches: dict[tuple[str, str], Fetch] = {}
        self._clones: dict[str, Clone] = {}
        self._n = [0]
        self.log: list[str] = []
        if self.url_map:
            self.env["GIT_CONFIG_GLOBAL"] = str(self._write_url_map())

    # -- environment ------------------------------------------------------------------------------------
    def _write_url_map(self) -> Path:
        """The test-only routing: url.<local>.insteadOf = <url>, and the file protocol allowed for submodules."""
        lines = ['[protocol "file"]', "\tallow = always"]
        for url, path in sorted(self.url_map.items()):
            lines += ['[url "%s"]' % Path(path).resolve().as_uri(), "\tinsteadOf = %s" % url]
        cfg = self._slot("url-map", repr(sorted(self.url_map.items()))).with_suffix(".gitconfig")
        cfg.write_text("\n".join(lines) + "\n")
        return cfg

    def derived(self, *, url_map: Mapping[str, str] | None = None,
                extra_env: Mapping[str, str] | None = None) -> "GitWork":
        """The same run's git with more routes or env. It shares this run's fetch and clone caches (a URL
        means the same repo whatever routes a test adds), its work area and its log."""
        g = GitWork.__new__(GitWork)
        g.workdir, g.host_bases, g._n, g.log = self.workdir, dict(self.host_bases), self._n, self.log
        g._fetches, g._clones = self._fetches, self._clones
        g.url_map = dict(self.url_map if url_map is None else url_map)
        g._extra = dict(self._extra, **(extra_env or {}))
        g.env = {k: v for k, v in self.env.items() if k != "GIT_CONFIG_GLOBAL" or not self.url_map}
        g.env.update(extra_env or {})
        if g.url_map:
            g.env["GIT_CONFIG_GLOBAL"] = str(g._write_url_map())
        return g

    def run(self, *args, cwd=None, timeout: float = GIT_TIMEOUT_S, env: Mapping[str, str] | None = None,
            **kw) -> GitResult:
        r = run_git(*args, cwd=cwd, env=dict(self.env, **(env or {})), isolate=True, timeout=timeout, **kw)
        self.log.append("%s -> %s" % (r.command, "ok" if r.ok else "exit %s" % r.returncode))
        return r

    def _slot(self, prefix: str, key: str) -> Path:
        self._n[0] += 1
        h = hashlib.sha1(key.encode()).hexdigest()[:10]
        return self.workdir / ("%s-%03d-%s" % (prefix, self._n[0], h))

    # -- hosts -----------------------------------------------------------------------------------------
    def host_of(self, url: str | None) -> str | None:
        """The host key (github | gitlab) an https URL names, or None when it is neither configured host."""
        if not url:
            return None
        net = (urlsplit(to_https(url)).hostname or "").lower()
        for key, base in self.host_bases.items():
            if net and net == (urlsplit(base).hostname or "").lower():
                return key
        return None

    def repo_url(self, repo: Mapping[str, str]) -> str | None:
        """The anonymous https URL of {host, path}, or None when the host's base URL is unknown."""
        base = self.host_bases.get(repo.get("host", ""))
        return "%s/%s.git" % (base, repo["path"]) if base else None

    # -- fetch and clone --------------------------------------------------------------------------------
    def fetch_commit(self, url: str, sha: str) -> Fetch:
        """`git fetch --depth=1 <url> <sha>` into an empty repo, once per (repo, commit) in a run."""
        key = (repo_key(url), sha)
        if key in self._fetches:
            return self._fetches[key]
        kind = url_kind(url)
        if kind in ("ssh", "local"):
            f = Fetch(False, kind, url, sha, why="%s URL %s: no anonymous fetch can use it" % (kind, url))
        else:
            repo = self._slot("fetch", url + sha)
            self.run("init", "-q", repo).check()
            r = self.run("fetch", "-q", "--no-tags", "--depth=1", url, sha, cwd=repo)
            if r.ok:
                got = self.run("rev-parse", "FETCH_HEAD^{commit}", cwd=repo)
                fetched = got.stdout.strip() if got.ok else None
                f = Fetch(fetched == sha, "ok" if fetched == sha else "error", url, sha, repo, fetched,
                          "" if fetched == sha else "fetched %s, not %s" % (fetched, sha), r.command)
            else:
                f = Fetch(False, classify_failure(r), url, sha, repo, None, _tail(r), r.command)
        self._fetches[key] = f
        return f

    def refs_clone(self, url: str) -> Clone:
        """A bare treeless clone of `url`: every branch and tag (never PR/MR refs), commits only."""
        key = repo_key(url)
        if key in self._clones:
            return self._clones[key]
        if url_kind(url) not in ("https", "other"):
            c = Clone(False, "auth", url, why="not an anonymous URL: %s" % url)
        else:
            repo = self._slot("refs", url)
            r = self.run("clone", "-q", "--bare", "--filter=tree:0", url, repo)
            c = Clone(r.ok, "ok" if r.ok else classify_failure(r), url, repo if r.ok else None,
                      "" if r.ok else _tail(r), r.command)
            if c.cls == "not-our-ref":
                c.cls = "error"
        self._clones[key] = c
        return c

    def containing_ref(self, clone: Clone, sha: str) -> str | None:
        """A branch or tag of the cloned host containing `sha`, or None (also when the object is absent)."""
        r = self.run("for-each-ref", "--count=1", "--format=%(refname)", "--contains", sha, "refs/heads", "refs/tags",
                     cwd=clone.repo)
        return (r.stdout.strip() or None) if r.ok else None

    def show(self, repo: Path, sha: str, path: str) -> str | None:
        r = self.run("cat-file", "-p", "%s:%s" % (sha, path), cwd=repo)
        return r.stdout if r.ok else None

    def gitlink(self, repo: Path, sha: str, path: str) -> tuple[str | None, str | None]:
        """(type, object id) of `path` in `sha`'s tree: ("commit", sha) for a gitlink."""
        r = self.run("ls-tree", sha, "--", path, cwd=repo)
        if not r.ok or not r.stdout.strip():
            return None, None
        meta = r.stdout.split("\t", 1)[0].split()
        return (meta[1], meta[2]) if len(meta) >= 3 else (None, None)

    # -- the walk ---------------------------------------------------------------------------------------
    def canonical_for(self, pin_url: str, resolved: str | None, canonical_urls: Mapping[str, str]) -> str | None:
        for k in (pin_url, resolved):
            if k and k in canonical_urls:
                return canonical_urls[k]
        if resolved is None or url_kind(resolved) == "local":
            return None
        return to_https(resolved)

    def walk(self, root_url: str, commit: str, canonical_urls: Mapping[str, str],
             overrides: Mapping[str, str] | None = None) -> Pin:
        """The pinned tree below `commit` of `root_url`: every .gitmodules entry with a gitlink, recursed once
        per (repo, commit), depth <= 10. .gitmodules is read from the pinned fetch, else the canonical clone.

        `overrides` ({tree path: sha}; the local-mode request's `pin_overrides`, CR-11) replaces the gitlink read
        at that tree path: the pin is judged at the override's commit and the tree below it is walked from that
        commit's own .gitmodules. The replaced gitlink is kept in `Pin.override_of`."""
        overrides = dict(overrides or {})
        root = Pin("", root_url, commit, resolved=root_url, kind=url_kind(root_url))
        root.canonical = self.canonical_for(root_url, root_url, canonical_urls)
        root.host = self.host_of(root.canonical)
        seen = set()
        stack = [(root, 0)]
        while stack:
            pin, depth = stack.pop(0)
            key = (repo_key(pin.resolved or pin.url), pin.commit)
            below = pin.tree_path + "/" if pin.tree_path else ""
            if key in seen and not any(t.startswith(below) for t in overrides):
                pin.duplicate = True              # (an override below it: walked again, so the override applies)
                continue
            seen.add(key)
            if depth >= MAX_DEPTH or pin.commit is None:
                continue
            self._read_gitmodules(pin)
            if pin.gitmodules is None:
                continue
            for e in parse_gitmodules(pin.gitmodules):
                typ, sha = self.gitlink(pin.gm_repo, pin.commit, e["path"])
                if typ != "commit":
                    continue                       # .gitmodules names a path the tree holds no gitlink at
                kind = url_kind(e["url"])
                resolved = (resolve_relative(e["url"], pin.resolved or pin.url) if kind == "relative"
                            else e["url"])
                tree_path = "/".join(x for x in (pin.tree_path, e["path"]) if x)
                override_of = None
                if tree_path in overrides:
                    override_of, sha = sha, overrides[tree_path]
                child = Pin(tree_path, e["url"], sha, parent=pin,
                            resolved=resolved, kind=kind if kind != "relative" else url_kind(resolved),
                            gitlink_read_from_pinned=(pin.gm_via == "pinned"), override_of=override_of)
                child.canonical = self.canonical_for(e["url"], resolved, canonical_urls)
                child.host = self.host_of(child.canonical)
                pin.children.append(child)
            stack[0:0] = [(c, depth + 1) for c in pin.children]      # depth first, in .gitmodules order
        return root

    def _read_gitmodules(self, pin: Pin) -> None:
        f = self.fetch_commit(pin.resolved or pin.url, pin.commit)
        if f.ok:
            pin.gm_repo, pin.gm_via = f.repo, "pinned"
            pin.gitmodules = self.show(f.repo, pin.commit, ".gitmodules") or ""
            return
        if pin.canonical:
            c = self.refs_clone(pin.canonical)
            if c.ok:
                text = self.show(c.repo, pin.commit, ".gitmodules")
                if text is not None or self.run("cat-file", "-e", pin.commit + "^{tree}", cwd=c.repo).ok:
                    pin.gm_repo, pin.gm_via = c.repo, "canonical"
                    pin.gitmodules = text or ""


def iter_pins(root: Pin):
    """Every pin below the root, depth first in .gitmodules order (the root itself is not a pin)."""
    stack = list(root.children)
    while stack:
        p = stack.pop(0)
        yield p
        stack[0:0] = p.children


def git_version() -> str:
    r = run_git("version", timeout=30)
    return r.stdout.strip().replace("git version ", "") if r.ok else "unknown"


def sha_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def env_has(name: str) -> bool:
    return bool(os.environ.get(name))
