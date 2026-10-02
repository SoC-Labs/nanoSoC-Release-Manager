"""T0 gates, their negative controls, and the roll-up (interfaces.md section 9; README section 6).

Shared by the runner (which runs in CI only) and the service (which rolls up a verified bundle). Importing this
module never imports the runner.

- `GATES`: every declared gate. Each has a `check(ctx) -> (status, detail)` and a `control(ctx) -> Context`
  that returns the BROKEN input only. `run_with_control` runs the gate's own check twice, on the real input and
  on the broken one, in one run with one nonce; the control's row `<gate>.negctl` is PASS only when the check
  went red (FAIL) on the broken input. So a check that cannot fail has a control that does not fire, and the
  gate rolls up UNVERIFIED (D6). The controls of the gates that walk the pinned tree (fetchable, reachable,
  no-ssh, manifest, vendor) build a scratch superproject holding the defect and hand it to the gate as its
  request, so the broken input goes through the same walk, parser and scanner as the real one. Those walks also
  census themselves: a .gitmodules that declares submodules under which no gitlink pin was found is `unverified`.
- `rollup(rows, scope)`: README section 6, one gate item per declared gate of the scope.
- `apply_waivers(gates, items, waivers)`: GATE2 Q1, pure.
- `run_verdict(gates)`: FAIL > UNVERIFIED (or no PASS gate) > INFRA > WAIVED > PASS, with the NOT-RUN count.

Each check records the items it judged (`ctx.items(gate)`; the runner writes them to
`rm-bundle/items/<gate>.jsonl`). Items name locations and results, never file content.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

import yaml

from nanosoc_rm import clock
from nanosoc_rm.contracts import load_yaml, schemas_dir, validate, validate_gate_item
from nanosoc_rm.t0.gitwork import (GitWork, Pin, classify_failure, iter_pins, parse_gitmodules, repo_key, url_kind,
                                   url_path)

__all__ = ["Gate", "GATES", "Context", "STATUSES", "MAY_SKIP", "HOST_DEPENDENT", "run_with_control", "rollup",
           "apply_waivers", "run_verdict", "gates_for", "clean_detail", "credential_vars", "synthetic_commit"]

STATUSES = ("PASS", "FAIL", "UNVERIFIED", "WARN", "SKIP", "INFRA")      # verdict_row status
SCOPES = ("configuration", "registry")
# Gates that need a host to answer: when t0.infra.probe cannot reach one, the runner records them INFRA.
HOST_DEPENDENT = ("t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public", "t0.manifest.valid",
                  "t0.clone.upgrade-path", "t0.vendor.no-arm-ip")


def clean_detail(text: Any) -> str:
    """A verdicts.tsv detail: no tab, no newline (README section 2)."""
    return " ".join(str(text).replace("\t", " ").replace("\r", " ").split("\n")).strip()


_URL_WITH_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s:]*:[^/@\s]*@", re.I)


def credential_vars(env: Mapping[str, str]) -> list[str]:
    """The variables the job must not hold after the scrub: CI_*TOKEN*, CI_*PASSWORD*, CI_JOB_JWT*
    (interfaces.md section 9, Environment), and every variable whose value carries a credential: a URL with a
    password in it (GitLab's CI_REPOSITORY_URL is https://gitlab-ci-token:<CI_JOB_TOKEN>@...), or CI_JOB_TOKEN's
    value itself. Names only: a value is never returned."""
    token = env.get("CI_JOB_TOKEN") or ""
    out = {k for k in env if k.startswith("CI_") and ("TOKEN" in k or "PASSWORD" in k or k.startswith("CI_JOB_JWT"))}
    for k, v in env.items():
        if isinstance(v, str) and (_URL_WITH_PASSWORD.search(v) or (len(token) >= 8 and token in v)):
            out.add(k)
    return sorted(out)


def synthetic_commit(nonce: str, what: str) -> str:
    """A 40-hex id that names no real commit, distinct per run and control."""
    return hashlib.sha1(("nanosoc-rm negative control %s %s" % (what, nonce)).encode()).hexdigest()


def _host_bases_default() -> dict[str, str]:
    """github is public; gitlab's base URL is the job's CI_SERVER_URL, else the service config's (tests on the VM)."""
    bases = {"github": "https://github.com"}
    if os.environ.get("CI_SERVER_URL"):
        bases["gitlab"] = os.environ["CI_SERVER_URL"].rstrip("/")
    elif os.environ.get("NANOSOC_RM_CONFIG"):
        try:
            from nanosoc_rm.config import load_config
            hosts = load_config().get("hosts") or {}
            for key in ("github", "gitlab"):
                h = hosts.get(key) or {}
                if h.get("git") or h.get("web"):
                    bases[key] = (h.get("git") or h.get("web")).rstrip("/")
        except Exception:
            pass
    return bases


# ---------------------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------------------
class Context:
    """What the runner hands a gate: the nonce, a work directory, the request, the job environment, the fetched
    parents, the registry checkout. A control returns a derived Context (`derive`) holding the broken input; it
    shares this run's git caches but records its own items."""

    def __init__(self, *, nonce: str, workdir: str | os.PathLike, request: dict | None = None,
                 parents: dict | None = None, env: Mapping[str, str] | None = None,
                 service_account: str | None = None, registry_dir: str | os.PathLike | None = None,
                 url_map: Mapping[str, str] | None = None, host_bases: Mapping[str, str] | None = None,
                 probe_urls: list[str] | None = None, start_listing: Mapping[str, list[str]] | None = None,
                 git_env: Mapping[str, str] | None = None):
        self.nonce = nonce
        self.workdir = Path(workdir).absolute()            # see GitWork: git resolves paths after `-C`
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.request = copy.deepcopy(request) if request else None
        self.parents = copy.deepcopy(parents) if parents is not None else None
        self.env = dict(env or {})
        self.service_account = service_account
        self.registry_dir = Path(registry_dir) if registry_dir else None
        self.url_map = dict(url_map or {})
        self.start_listing = dict(start_listing) if start_listing is not None else None
        self.git = GitWork(self.workdir / "git", url_map=self.url_map,
                           host_bases=host_bases if host_bases is not None else _host_bases_default(),
                           extra_env=git_env)
        self._probe_urls = list(probe_urls) if probe_urls is not None else None
        self._repo_url: str | None = None
        self._items: dict[str, list[dict]] = {}
        self._shared: dict[str, Any] = {}          # the walk, shared with derived contexts of the same request
        self.citations_override: list[dict] | None = None

    @classmethod
    def for_tests(cls, *, nonce, workdir, request=None, parents=None, env=None, service_account=None,
                  registry_dir=None, url_map=None, host_bases=None, probe_urls=None) -> "Context":
        """A Context for tests. `request` is an rm_request minus schema and nonce. `url_map` ({URL: local
        repo}) is a test-only seam and is refused when CI_JOB_ID is set: a real job never fakes reachability."""
        if url_map and os.environ.get("CI_JOB_ID"):
            raise PermissionError("url_map is a test-only seam; it is refused in a CI job (CI_JOB_ID is set)")
        return cls(nonce=nonce, workdir=workdir, request=request, parents=parents, env=env,
                   service_account=service_account, registry_dir=registry_dir, url_map=url_map,
                   host_bases=host_bases, probe_urls=probe_urls)

    # -- derived contexts (the broken inputs of controls) ------------------------------------------------
    def derive(self, *, url_map: Mapping[str, str] | None = None, git_env: Mapping[str, str] | None = None,
               new_request: bool = False, **attrs) -> "Context":
        new = copy.copy(self)
        new._items = {}
        if url_map is not None or git_env is not None:
            new.url_map = dict(url_map) if url_map is not None else dict(self.url_map)
            new.git = self.git.derived(url_map=url_map, extra_env=git_env)
        for k, v in attrs.items():
            if not hasattr(new, k):
                raise AttributeError("Context has no attribute %r" % k)
            setattr(new, k, v)
        if new_request:
            new._shared = {}
        return new

    # -- items --------------------------------------------------------------------------------------------
    def items(self, gate_id: str) -> list[dict]:
        """The items gate `gate_id` judged in this context (gate_item documents)."""
        return copy.deepcopy(self._items.get(gate_id, []))

    def _begin(self, gate_id: str) -> list[dict]:
        self._items[gate_id] = []
        return self._items[gate_id]

    @staticmethod
    def _add(items: list[dict], item: dict) -> None:
        item = {k: v for k, v in item.items() if v is not None or k in ("component", "commit", "host")}
        problems = validate_gate_item(item)
        if problems:
            raise ValueError("invalid gate item %r: %s" % (item, "; ".join(problems)))
        items.append(item)

    # -- the request and the tree -------------------------------------------------------------------------
    @property
    def repo_url(self) -> str | None:
        if self._repo_url:
            return self._repo_url
        if self.request and isinstance(self.request.get("repo"), dict):
            return self.git.repo_url(self.request["repo"])
        return None

    def canonical_urls(self) -> dict[str, str]:
        return dict((self.request or {}).get("canonical_urls") or {})

    def walk(self) -> Pin | None:
        """The pinned tree of the request (gitwork.GitWork.walk), computed once per request."""
        if "walk" not in self._shared:
            url, commit = self.repo_url, (self.request or {}).get("commit")
            self._shared["walk"] = self.git.walk(url, commit, self.canonical_urls()) if url and commit else None
        return self._shared["walk"]

    def root_fetch(self):
        root = self.walk()
        return None if root is None else self.git.fetch_commit(root.resolved or root.url, root.commit)

    def pinned_ok(self, pin: Pin) -> bool:
        return self.git.fetch_commit(pin.resolved or pin.url, pin.commit).ok if pin.commit else False

    def unfetched_ancestor(self, pin: Pin) -> Pin | None:
        """The nearest ancestor (the superproject included) that the pinned walk could not fetch."""
        for a in pin.ancestors():
            if not self.pinned_ok(a):
                return a
        return None

    def captured(self) -> dict:
        """result-bundle `captured`: the superproject commit the job fetched, and the gitlinks the pinned walk read
        (null below a parent it could not fetch)."""
        root, f = self.walk(), self.root_fetch()
        pins = []
        if root is not None:
            for p in iter_pins(root):
                pins.append({"tree_path": p.tree_path,
                             "commit": p.commit if self.unfetched_ancestor(p) is None else None})
        return {"superproject_commit": f.fetched if f is not None and f.ok else None, "pins": pins}

    def parents_map(self) -> dict:
        """{"<host>/<path>@<sha>": {public, gitmodules, tree_path, gitlinks}}: given, else every parent of the
        walk whose .gitmodules was read anonymously (so it is public)."""
        if self.parents is not None:
            return self.parents
        root = self.walk()
        out = {}
        if root is None:
            return out
        for p in [root] + list(iter_pins(root)):
            if p.gitmodules is None or p.duplicate:
                continue
            host = self.git.host_of(p.canonical or p.resolved) or "unknown"
            key = "%s/%s@%s" % (host, url_path(p.canonical or p.resolved or p.url), p.commit)
            out[key] = {"public": True, "gitmodules": p.gitmodules, "tree_path": p.tree_path,
                        "gitlinks": {c.tree_path: c.commit for c in p.children}}
        return out


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def _paths(items: Iterable[dict], result: str, limit: int = 8) -> str:
    ps = [i.get("tree_path") or "%s#%s" % (i.get("file"), i.get("pointer")) for i in items if i["result"] == result]
    more = len(ps) - limit
    return ", ".join(ps[:limit]) + (" and %d more" % more if more > 0 else "")


def _s7(sha: str | None) -> str:
    return (sha or "?")[:7]


# ---------------------------------------------------------------------------------------------------------
# t0.infra.probe (interfaces.md section 9)
# ---------------------------------------------------------------------------------------------------------
PROBE_ENV = "RM_PROBE_URLS"      # optional, whitespace separated: one known public repo per further host


def _probe_urls(ctx: Context) -> list[str]:
    if ctx._probe_urls is not None:
        return list(ctx._probe_urls)
    urls = [ctx.repo_url] if ctx.repo_url else []
    urls += [u for u in (ctx.env.get(PROBE_ENV) or os.environ.get(PROBE_ENV) or "").split() if u]
    return list(dict.fromkeys(urls))


def _probe_check(ctx: Context) -> tuple[str, str]:
    urls = _probe_urls(ctx)
    if not urls:
        return "UNVERIFIED", "no probe target: the request names no repository"
    ok, refused, down = [], [], []
    for u in urls:
        r = ctx.git.run("ls-remote", "--heads", u, timeout=60)
        if r.ok:
            ok.append(u)
        elif classify_failure(r) == "unreachable":
            down.append(u)
        else:
            refused.append(u)
    hosts = lambda us: ", ".join(sorted({ctx.git.host_of(u) or url_path(u) for u in us}))
    if down:
        return "INFRA", "host unreachable for an anonymous ls-remote: %s" % hosts(down)
    if refused:
        return "FAIL", "a known public repo refused an anonymous ls-remote: %s" % ", ".join(url_path(u) for u in refused)
    return "PASS", "anonymous ls-remote of %d known public repo(s) succeeded (%s)" % (len(ok), hosts(ok))


def _probe_control(ctx: Context) -> Context:
    urls = _probe_urls(ctx)
    absent = ["%s/nanosoc-rm-probe-absent-%s.git" % (u.rsplit("/", 1)[0], ctx.nonce[:8]) for u in urls]
    if ctx.url_map:
        missing = ctx.workdir / "probe-negctl-absent.git"           # a path that holds no repository
        return ctx.derive(_probe_urls=absent, url_map=dict(ctx.url_map, **{a: str(missing) for a in absent}))
    return ctx.derive(_probe_urls=absent)


# ---------------------------------------------------------------------------------------------------------
# t0.runner.hardened (D10)
# ---------------------------------------------------------------------------------------------------------
def _listing(ctx: Context, var: str) -> list[str] | None:
    if ctx.start_listing is not None and var in ctx.start_listing:
        return ctx.start_listing[var]
    d = ctx.env.get(var)
    if not d or not Path(d).is_dir():
        return None
    return sorted(os.listdir(d))


def _hardened_check(ctx: Context) -> tuple[str, str]:
    env, bad = ctx.env, []
    if env.get("CI_COMMIT_REF_PROTECTED") != "true":
        bad.append("CI_COMMIT_REF_PROTECTED=%s: the ref is not protected" % (env.get("CI_COMMIT_REF_PROTECTED") or "unset"))
    for var in ("HOME", "TMPDIR"):
        listing = _listing(ctx, var)
        if listing is None:
            bad.append("%s is unset or not a directory" % var)
        elif listing:
            bad.append("%s was not empty at start (%d entr%s)" % (var, len(listing), "y" if len(listing) == 1 else "ies"))
    user_var = "GITLAB_USER_LOGIN" if env.get("GITLAB_USER_LOGIN") else "USER"
    expected = ctx.service_account or env.get("RM_SERVICE_ACCOUNT")
    if not expected:
        bad.append("RM_SERVICE_ACCOUNT is not set: the job cannot tell its service account")
    elif env.get(user_var) != expected:
        bad.append("%s=%s is not the service account %s" % (user_var, env.get(user_var) or "unset", expected))
    left = credential_vars(env)
    if left:
        bad.append("credentials left after the scrub: %s" % ", ".join(left))
    if bad:
        return "FAIL", "; ".join(bad)
    return "PASS", ("protected ref; HOME and TMPDIR empty at start; %s is the service account; no CI_*TOKEN*, "
                    "CI_*PASSWORD* or CI_JOB_JWT* variable and no URL with a password left after the scrub" % user_var)


def _hardened_control(ctx: Context) -> Context:
    return ctx.derive(env=dict(ctx.env, CI_COMMIT_REF_PROTECTED="false"))


# ---------------------------------------------------------------------------------------------------------
# t0.pins.fetchable (D2)
# ---------------------------------------------------------------------------------------------------------
_FETCH_WHY = {"ssh": "pinned by ssh URL %(url)s: an anonymous fetch cannot use it",
              "local": "pinned by local path %(url)s: no one else can fetch it",
              "not-our-ref": "%(url)s does not serve %(c)s (not our ref)",
              "auth": "%(url)s refused an anonymous fetch (private, moved or deleted)",
              "unreachable": "host unreachable: %(url)s (INFRA, not a FAIL)",
              "error": "fetch of %(c)s from %(url)s failed: %(why)s"}


def _fetch_item(ctx: Context, pin: Pin) -> tuple[dict, bool]:
    f = ctx.git.fetch_commit(pin.resolved or pin.url, pin.commit)
    item = {"tree_path": pin.tree_path, "component": None, "commit": pin.commit,
            "host": ctx.git.host_of(pin.resolved or pin.url)}
    if f.ok:
        return dict(item, result="pass", why="fetched anonymously at %s" % _s7(pin.commit)), False
    why = _FETCH_WHY.get(f.cls, _FETCH_WHY["error"]) % {"url": pin.url, "c": _s7(pin.commit), "why": f.why}
    result = "fail" if f.cls in ("ssh", "local", "not-our-ref", "auth") else "unverified"
    return dict(item, result=result, why=why), f.cls == "unreachable"


def _fetchable_check(ctx: Context) -> tuple[str, str]:
    items, infra = ctx._begin("t0.pins.fetchable"), False
    root = ctx.walk()
    if root is None:
        return "UNVERIFIED", "no request: nothing to walk"
    rf = ctx.root_fetch()
    if not rf.ok:
        item, infra = _fetch_item(ctx, root)
        ctx._add(items, dict(item, tree_path="."))
    for pin in iter_pins(root):
        up = ctx.unfetched_ancestor(pin)
        if up is not None:
            ctx._add(items, {"tree_path": pin.tree_path, "component": None, "commit": pin.commit,
                             "host": ctx.git.host_of(pin.resolved or pin.url), "result": "not-reached",
                             "why": "below %s, which was not fetched" % (up.tree_path or "the superproject")})
            continue
        item, down = _fetch_item(ctx, pin)
        infra = infra or down
        ctx._add(items, item)
    gaps = _census(ctx, root, items, "t0.pins.fetchable")
    n, passed = len(items), sum(1 for i in items if i["result"] == "pass")
    status = "FAIL" if any(i["result"] == "fail" for i in items) else "INFRA" if infra else \
        "UNVERIFIED" if passed < n else "PASS"
    detail = "%d of %d pins fetched anonymously" % (passed, n - len(gaps))
    if any(i["result"] == "fail" for i in items):
        detail += "; not fetched: %s" % _paths(items, "fail")
    if any(i["result"] == "not-reached" for i in items):
        detail += "; not reached: %d below them" % sum(1 for i in items if i["result"] == "not-reached")
    if any(i["result"] == "unverified" for i in items if i not in gaps):
        detail += "; undecided: %s" % _paths([i for i in items if i not in gaps], "unverified")
    if gaps:
        detail += "; " + _census_detail(gaps)
    return status, detail


# A .gitmodules section header, counted on the raw text: the census that a blind parser or walk cannot fool.
_SUBMODULE_HEADER = re.compile(r"^[ \t]*\[[ \t]*submodule\b", re.M)


def _declared(text: str | None) -> int:
    return len(_SUBMODULE_HEADER.findall(text or ""))


def _census(ctx: Context, root: Pin, items: list[dict], gate_id: str) -> list[dict]:
    """The walk's census (D6: zero pins is never a PASS by default). Every parent whose .gitmodules declares a
    submodule but under which the walk found no gitlink pin gets an `unverified` item: the gate cannot vouch for
    the tree below it. Returns those items."""
    gaps = []
    for p in [root] + list(iter_pins(root)):
        if p.duplicate or not p.gitmodules or p.children:
            continue
        n = _declared(p.gitmodules)
        if n:
            gap = {"tree_path": p.tree_path or ".", "component": None, "commit": p.commit,
                   "host": ctx.git.host_of(p.resolved or p.url), "result": "unverified",
                   "why": "its .gitmodules declares %d submodule(s) but the walk found no gitlink pin below it" % n}
            ctx._add(items, gap)
            gaps.append(items[-1])
    return gaps


def _census_detail(gaps: list[dict]) -> str:
    return "census: %d parent(s) declare submodules but no gitlink pin was found below them (%s)" % (
        len(gaps), ", ".join(g["tree_path"] for g in gaps[:8]))


# ---------------------------------------------------------------------------------------------------------
# Controls of the walking gates: a scratch superproject, walked and judged exactly as the real one
# ---------------------------------------------------------------------------------------------------------
NEGCTL_HOST = "negctl.nanosoc-rm.invalid"      # a reserved name (RFC 2606): no host answers for it


def _negctl_url(ctx: Context, name: str) -> str:
    return "https://%s/%s/%s.git" % (NEGCTL_HOST, ctx.nonce[:8], name)


def _scratch_superproject(ctx: Context, what: str, files: Mapping[str, str] | None = None,
                          pins: Iterable[tuple[str, str | None]] = (),
                          routes: Mapping[str, str] | None = None) -> Context:
    """A Context whose request is a scratch superproject built here: `files` committed, and each pin
    (path, url) a gitlink to a commit made only in this work dir (no host serves it). A url of None pins the
    scratch superproject's own repo, which does not hold that commit. The superproject is served at a reserved
    `.invalid` URL routed to the scratch repo with url.insteadOf (the same routing as the tests' url_map, applied
    only to URLs this control made up), so the gate's own walk fetches it, reads its .gitmodules and gitlinks,
    and judges it with the same code as the real input."""
    git = ctx.git.derived(extra_env=_COMMIT_ENV)
    d = git._slot("negctl-" + what, ctx.nonce)
    url = _negctl_url(ctx, what)
    git.run("init", "-q", d).check()
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    sections = []
    for path, pin_url in pins:
        sub = d / path
        git.run("init", "-q", sub).check()
        (sub / "NEGCTL").write_text("nanosoc-rm negative control %s %s\n" % (what, ctx.nonce))
        git.run("add", "NEGCTL", cwd=sub).check()
        git.run("commit", "-q", "-m", "negctl " + what, cwd=sub).check()
        sections.append('[submodule "%s"]\n\tpath = %s\n\turl = %s\n' % (path, path, pin_url or url))
    if sections:
        (d / ".gitmodules").write_text("".join(sections))
    git.run("add", "-A", cwd=d).check()
    git.run("commit", "-q", "--allow-empty", "-m", "negctl " + what, cwd=d).check()
    sha = git.run("rev-parse", "HEAD", cwd=d).check().stdout.strip()
    base = ctx.request or {"configuration": "negctl", "repo": {"host": "github", "path": "SoC-Labs/nanosoc-rm-negctl"},
                           "canonical_urls": {}}
    req = dict(base, commit=sha, upgrade_from=None)
    return ctx.derive(url_map=dict(ctx.url_map, **{url: str(d)}, **dict(routes or {})), request=req, _repo_url=url,
                      parents=None, new_request=True)


def _pins_control(ctx: Context) -> Context:
    """A scratch superproject pinning a commit that does not exist on the host: in the real public
    superproject repo when the request names one (so the real host answers), else in the scratch repo itself."""
    return _scratch_superproject(ctx, "pins", pins=[("negctl/synthetic-pin", ctx.repo_url)])


# ---------------------------------------------------------------------------------------------------------
# t0.pins.reachable (D2)
# ---------------------------------------------------------------------------------------------------------
def _reach_item(ctx: Context, pin: Pin) -> tuple[dict, bool]:
    item = {"tree_path": pin.tree_path, "component": None, "commit": pin.commit, "host": pin.host}
    if not pin.canonical:
        return dict(item, result="unverified", why="no canonical https URL for %s" % pin.url), False
    c = ctx.git.refs_clone(pin.canonical)
    if not c.ok:
        if c.cls == "unreachable":
            return dict(item, result="unverified", why="host unreachable: %s (INFRA)" % pin.canonical), True
        return dict(item, result="unverified",
                    why="canonical repo %s is not readable anonymously" % url_path(pin.canonical)), False
    ref = ctx.git.containing_ref(c, pin.commit)
    if ref:
        return dict(item, result="pass", why="%s is reachable from %s of %s" % (
            _s7(pin.commit), ref, url_path(pin.canonical))), False
    return dict(item, result="fail", why="%s is on no branch or tag of the canonical repo %s" % (
        _s7(pin.commit), url_path(pin.canonical))), False


def _reachable_check(ctx: Context) -> tuple[str, str]:
    items, infra = ctx._begin("t0.pins.reachable"), False
    root = ctx.walk()
    if root is None:
        return "UNVERIFIED", "no request: nothing to walk"
    if root.gitmodules is None:
        return "UNVERIFIED", "the superproject could not be read anonymously by its pinned or canonical URL"
    pins = list(iter_pins(root))
    unreadable: set[str] = set()
    for pin in pins:
        above = next((a for a in pin.ancestors() if a.tree_path in unreadable), None)
        if above is not None:
            ctx._add(items, {"tree_path": pin.tree_path, "component": None, "commit": pin.commit, "host": pin.host,
                             "result": "not-reached",
                             "why": "below %s, whose canonical repo is not readable anonymously" % above.tree_path})
            unreadable.add(pin.tree_path)
            continue
        item, down = _reach_item(ctx, pin)
        infra = infra or down
        if item["result"] == "unverified":
            unreadable.add(pin.tree_path)
        ctx._add(items, item)
    gaps = _census(ctx, root, items, "t0.pins.reachable")
    n, passed = len(items) - len(gaps), sum(1 for i in items if i["result"] == "pass")
    fails = [i for i in items if i["result"] == "fail"]
    undecided = [i for i in items if i["result"] in ("unverified", "not-reached")]
    status = "FAIL" if fails else "UNVERIFIED" if undecided and not infra else "INFRA" if infra else "PASS"
    parts = ["%d of %d pins reachable from a branch or tag of their canonical repo" % (passed, n)]
    if fails:
        parts.append("unreachable: %s" % ", ".join("%s %s" % (i["tree_path"], _s7(i["commit"])) for i in fails[:8]))
    if any(i["result"] == "unverified" for i in undecided if i not in gaps):
        parts.append("unverified: %s" % _paths([i for i in undecided if i not in gaps], "unverified"))
    nr = sum(1 for i in undecided if i["result"] == "not-reached")
    if nr:
        parts.append("not reached: %d" % nr)
    if not pins:
        parts = ["the superproject pins nothing"]
    if gaps:
        parts.append(_census_detail(gaps))
    return status, "; ".join(p for p in parts if p)


# ---------------------------------------------------------------------------------------------------------
# t0.urls.no-ssh-public (D2)
# ---------------------------------------------------------------------------------------------------------
def _label(key: str) -> str:
    repo, _, sha = key.rpartition("@")
    return "%s@%s" % (repo.rsplit("/", 1)[-1], sha[:7])


def _no_ssh_check(ctx: Context) -> tuple[str, str]:
    items = ctx._begin("t0.urls.no-ssh-public")
    parents = {k: v for k, v in ctx.parents_map().items() if v.get("public")}
    if not parents:
        return "UNVERIFIED", "no public parent was read: nothing to check"
    offenders, gaps = {}, []
    for key, p in parents.items():
        links = p.get("gitlinks") or {}
        entries = parse_gitmodules(p.get("gitmodules") or "")
        declared = _declared(p.get("gitmodules"))
        if declared and not entries:                   # the census: a header the parser did not see
            ctx._add(items, {"tree_path": p.get("tree_path") or ".", "component": None, "commit": None, "host": None,
                             "result": "unverified", "why": "its .gitmodules declares %d submodule(s) but none "
                             "was parsed" % declared})
            gaps.append(_label(key))
        for e in entries:
            tp = "/".join(x for x in (p.get("tree_path") or "", e["path"]) if x)
            item = {"tree_path": tp, "component": None, "commit": links.get(tp), "host": ctx.git.host_of(e["url"])}
            if url_kind(e["url"]) == "ssh":
                ctx._add(items, dict(item, result="fail", why="public parent %s pins it by ssh URL" % _label(key)))
                offenders.setdefault(key, []).append(tp)
            else:
                ctx._add(items, dict(item, result="pass", why="not an ssh URL"))
    if offenders:
        return "FAIL", "; ".join("public parent %s pins by ssh URL: %s" % (_label(k), ", ".join(v))
                                 for k, v in offenders.items())
    if gaps:
        return "UNVERIFIED", "census: %d public parent(s) declare submodules but none was parsed (%s)" % (
            len(gaps), ", ".join(gaps[:8]))
    return "PASS", "%d public parent(s), %d pins, none by ssh URL" % (len(parents), len(items))


def _no_ssh_control(ctx: Context) -> Context:
    """A public parent pinning by ssh URL. Given parents (the seam a caller fills from the parents it read)
    get one more such parent, judged by the same parse; otherwise a scratch superproject whose .gitmodules pins
    by ssh URL is walked like the real tree."""
    sha = synthetic_commit(ctx.nonce, "public-parent")
    if ctx.parents is not None:
        return ctx.derive(parents={"github/SoC-Labs/nanosoc-rm-negctl@%s" % sha: {
            "public": True, "tree_path": "negctl",
            "gitmodules": '[submodule "x"]\n\tpath = x\n\turl = git@github.com:SoC-Labs/nanosoc-rm-negctl-x.git\n'}})
    ssh = "git@%s:SoC-Labs/nanosoc-rm-negctl-x.git" % NEGCTL_HOST
    absent = ctx.workdir / "negctl-absent.git"           # its https form leads nowhere, without a DNS lookup
    return _scratch_superproject(ctx, "no-ssh", pins=[("negctl/x", ssh)],
                                 routes={"https://%s/SoC-Labs/nanosoc-rm-negctl-x.git" % NEGCTL_HOST: str(absent)})


# ---------------------------------------------------------------------------------------------------------
# t0.manifest.valid (D2; component-manifest v1)
# ---------------------------------------------------------------------------------------------------------
MANIFEST = "nanosoc-component.yaml"
_MANIFEST_DOC = {"schema": 1, "id": "nanosoc-rm-negctl", "class": "ip_tech", "version": "0.1.0",
                 "canonical": "https://github.com/SoC-Labs/nanosoc-rm-negctl.git", "provides": [], "requires": [],
                 "reexports": [], "variants": {"sim": {"flist": "make flist"}},
                 "tests": {"t0": {"run": "true", "control": "false"}}}


def _walk_manifests(ctx: Context) -> tuple[list[tuple[str, str | None, str]], int]:
    root = ctx.walk()
    if root is None:
        return [], 0
    found, unfetched, seen = [], 0, set()
    for p in [root] + list(iter_pins(root)):
        if (p is not root and ctx.unfetched_ancestor(p) is not None) or not ctx.pinned_ok(p):
            unfetched += 1
            continue
        if (p.resolved, p.commit) in seen:
            continue
        seen.add((p.resolved, p.commit))
        f = ctx.git.fetch_commit(p.resolved or p.url, p.commit)
        text = ctx.git.show(f.repo, p.commit, MANIFEST)
        if text is not None:
            found.append((p.tree_path or ".", p.commit, text))
    return found, unfetched


def _manifest_check(ctx: Context) -> tuple[str, str]:
    items = ctx._begin("t0.manifest.valid")
    manifests, unfetched = _walk_manifests(ctx)
    if not manifests:
        return "SKIP", "no %s at any pinned commit%s" % (
            MANIFEST, "" if not unfetched else " (%d tree%s not fetched)" % (unfetched, "" if unfetched == 1 else "s"))
    for tp, commit, text in manifests:
        item = {"tree_path": tp, "component": None, "commit": commit, "host": None}
        try:
            doc = load_yaml(text)
            problems = validate("component-manifest", doc)
        except Exception as e:
            problems = ["not YAML: %s" % str(e).splitlines()[0]]
        else:
            if isinstance(doc, dict) and isinstance(doc.get("id"), str) and not problems:
                item["component"] = doc["id"]
        if problems:
            ctx._add(items, dict(item, result="fail", why="%s invalid: %s" % (MANIFEST, "; ".join(problems[:3]))))
        else:
            ctx._add(items, dict(item, result="pass", why="%s valid (component-manifest v1)" % MANIFEST))
    bad = [i for i in items if i["result"] == "fail"]
    if bad:
        return "FAIL", "%d of %d manifests invalid: %s" % (len(bad), len(items), _paths(items, "fail"))
    return "PASS", "%d manifest(s) valid" % len(items)


def _manifest_control(ctx: Context) -> Context:
    """A scratch superproject whose manifest's version is "1.0", not SemVer: the only defect."""
    bad = dict(_MANIFEST_DOC, version="1.0")
    return _scratch_superproject(ctx, "manifest", files={MANIFEST: yaml.safe_dump(bad, sort_keys=False)})


# ---------------------------------------------------------------------------------------------------------
# t0.clone.upgrade-path (D2)
# ---------------------------------------------------------------------------------------------------------
_COMMIT_ENV = {"GIT_AUTHOR_NAME": "nanosoc-rm negctl", "GIT_AUTHOR_EMAIL": "negctl@nanosoc-rm.invalid",
               "GIT_COMMITTER_NAME": "nanosoc-rm negctl", "GIT_COMMITTER_EMAIL": "negctl@nanosoc-rm.invalid",
               "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def _unresolved(ctx: Context, work: Path) -> list[str]:
    r = ctx.git.run("submodule", "status", "--recursive", cwd=work)
    if not r.ok:
        return ["(submodule status failed)"]
    out = []
    for line in r.stdout.splitlines():
        if line and line[0] in "-+U":
            parts = line[1:].split()
            out.append(parts[1] if len(parts) > 1 else line)
    return out


def _upgrade_check(ctx: Context) -> tuple[str, str]:
    req = ctx.request
    if not req:
        return "UNVERIFIED", "no request"
    old, new = req.get("upgrade_from"), req.get("commit")
    if not old:
        return "SKIP", "configuration %s declares no upgrade_from" % req.get("configuration", "?")
    url = ctx.repo_url
    if not url or not new:
        return "UNVERIFIED", "no repository URL or commit in the request"
    git = ctx.git
    work = git._slot("upgrade", url + old + new)
    r = git.run("clone", "-q", "--no-checkout", url, work)
    if not r.ok:
        cls = classify_failure(r)
        return ("INFRA" if cls == "unreachable" else "FAIL"), "anonymous clone of %s failed (%s)" % (url_path(url), cls)
    if not git.run("checkout", "-q", old, cwd=work).ok:
        return "FAIL", "upgrade_from %s is not in an anonymous clone of %s" % (_s7(old), url_path(url))
    r = git.run("submodule", "update", "--init", "--recursive", cwd=work)
    if not r.ok:
        cls = classify_failure(r)
        return ("INFRA" if cls == "unreachable" else "FAIL"), (
            "the anonymous recursive clone of upgrade_from %s does not resolve (%s): %s"
            % (_s7(old), cls, ", ".join(_unresolved(ctx, work)[:6]) or "see log"))
    if not git.run("checkout", "-q", new, cwd=work).ok:
        if not (git.run("fetch", "-q", "origin", new, cwd=work).ok and git.run("checkout", "-q", new, cwd=work).ok):
            return "FAIL", "commit %s cannot be checked out in an anonymous clone of %s" % (_s7(new), url_path(url))
    first = git.run("submodule", "update", "--init", "--recursive", cwd=work)
    left = _unresolved(ctx, work) if first.ok else []
    no_sync_ok = first.ok and not left
    git.run("submodule", "sync", "--recursive", cwd=work)
    second = git.run("submodule", "update", "--init", "--recursive", cwd=work)
    after_sync_ok = second.ok and not _unresolved(ctx, work)
    if no_sync_ok and after_sync_ok:
        return "PASS", "upgrade %s -> %s resolves every pin without submodule sync, and again after it" % (
            _s7(old), _s7(new))
    if not no_sync_ok:
        cls = classify_failure(first) if not first.ok else "unresolved"
        if cls == "unreachable":
            return "INFRA", "host unreachable during the upgrade %s -> %s" % (_s7(old), _s7(new))
        return "FAIL", ("upgrade %s -> %s without git submodule sync does not resolve every pin (%s)%s; after sync "
                        "it %s" % (_s7(old), _s7(new), cls, (": " + ", ".join(left[:6])) if left else "",
                                   "resolves: a host move whose old host does not serve the new pin"
                                   if after_sync_ok else "still does not resolve"))
    return "FAIL", "upgrade %s -> %s resolves without sync but not after git submodule sync" % (_s7(old), _s7(new))


def _upgrade_control(ctx: Context) -> Context:
    """A synthetic pair: the second commit moves the submodule to a new URL and pins a commit the old URL
    does not serve (a host move whose old host was frozen). Built locally with plain commits."""
    git = ctx.git.derived(extra_env=dict(_COMMIT_ENV, GIT_ALLOW_PROTOCOL="file"))
    base = git._slot("upgrade-negctl", ctx.nonce)
    old, new, sup = base / "sub-old", base / "sub-new", base / "super"
    git.run("init", "-q", old).check()
    (old / "f.v").write_text("module negctl; endmodule\n")
    git.run("add", "f.v", cwd=old).check()
    git.run("commit", "-q", "-m", "c1", cwd=old).check()
    git.run("clone", "-q", old, new).check()
    (new / "f.v").write_text("module negctl(input a); endmodule\n")
    git.run("commit", "-q", "-am", "c2", cwd=new).check()
    c2 = git.run("rev-parse", "HEAD", cwd=new).check().stdout.strip()
    git.run("init", "-q", sup).check()
    git.run("clone", "-q", old, sup / "sub").check()
    (sup / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = %s\n' % old.resolve().as_uri())
    git.run("add", ".gitmodules", "sub", cwd=sup).check()
    git.run("commit", "-q", "-m", "u1", cwd=sup).check()
    u1 = git.run("rev-parse", "HEAD", cwd=sup).check().stdout.strip()
    git.run("fetch", "-q", new.resolve().as_uri(), c2, cwd=sup / "sub").check()
    git.run("checkout", "-q", c2, cwd=sup / "sub").check()
    (sup / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = %s\n' % new.resolve().as_uri())
    git.run("add", ".gitmodules", "sub", cwd=sup).check()
    git.run("commit", "-q", "-m", "u2", cwd=sup).check()
    u2 = git.run("rev-parse", "HEAD", cwd=sup).check().stdout.strip()
    req = dict(ctx.request or {}, configuration="negctl", commit=u2, upgrade_from=u1)
    new_ctx = ctx.derive(request=req, _repo_url=sup.resolve().as_uri(), new_request=True)
    new_ctx.git = git
    return new_ctx


# ---------------------------------------------------------------------------------------------------------
# t0.vendor.no-arm-ip (D2, D11)
# ---------------------------------------------------------------------------------------------------------
def _vendor_check(ctx: Context) -> tuple[str, str]:
    from nanosoc_rm.scan import scan as scanmod
    from nanosoc_rm.scan.rules import RulesError, rule_set
    from nanosoc_rm.scan.toolkit import toolkit
    items = ctx._begin("t0.vendor.no-arm-ip")
    try:
        rules, tk = rule_set(), toolkit()       # $RM_SCAN_RULES, $RM_SCAN_TOOLKIT: compat-ci's pinned copies
    except RulesError as e:
        return "UNVERIFIED", "scanner rules unusable: %s" % e
    root = ctx.walk()
    if root is None:
        return "UNVERIFIED", "no request: nothing to scan"
    cache: dict[str, Any] = {}
    found = []
    for p in [root] + list(iter_pins(root)):
        tp = p.tree_path or "."
        item = {"tree_path": tp, "component": None, "commit": p.commit, "host": ctx.git.host_of(p.resolved or p.url)}
        up = None if p is root else ctx.unfetched_ancestor(p)
        if up is not None or not ctx.pinned_ok(p):
            ctx._add(items, dict(item, result="not-reached", why="not fetched (t0.pins.fetchable), so not scanned"))
            continue
        try:
            if p.commit not in cache:           # same commit, same tree, whichever URL served it
                f = ctx.git.fetch_commit(p.resolved or p.url, p.commit)
                cache[p.commit] = scanmod.scan_tree(f.repo, p.commit, rules, env=ctx.git.env, toolkit=tk)
            res = cache[p.commit]
        except Exception as e:                  # one tree's problem stays that tree's item
            msg = str(e).splitlines()[0] if str(e) else ""
            ctx._add(items, dict(item, result="unverified", why="scan raised %s: %s" % (type(e).__name__, msg[:200])))
            continue
        if res.verdict == "finding":
            locs = ["%s at %s:%s" % (h["rule"], h["file"], h["line"] if h["line"] is not None else "-")
                    for h in res.findings[:5]]
            found.append(tp)
            ctx._add(items, dict(item, result="fail", why="%d finding(s): %s" % (len(res.findings), "; ".join(locs))))
        elif res.verdict == "unverified":
            ctx._add(items, dict(item, result="unverified", why="scan unverified: %s" % res.why))
        else:
            ctx._add(items, dict(item, result="pass", why="%d files scanned, 0 findings" % res.files_scanned))
    scanned = sum(1 for i in items if i["result"] in ("pass", "fail"))
    detail = "%d of %d trees scanned, %d with findings" % (scanned, len(items), len(found))
    if found:                       # the list belongs to "with findings", never to the not-fetched count after it
        detail += ": " + ", ".join(found[:8]) + (" and %d more" % (len(found) - 8) if len(found) > 8 else "")
    nr = sum(1 for i in items if i["result"] == "not-reached")
    if nr:
        detail += "; %d not fetched, so not scanned: %s" % (nr, _paths(items, "not-reached"))
    if found:
        return "FAIL", detail
    if not items or any(i["result"] in ("unverified", "not-reached") for i in items):
        return "UNVERIFIED", detail
    return "PASS", detail


def _vendor_control(ctx: Context) -> Context:
    """A scratch superproject with the arm.banner specimen planted in a committed file: fetched by the walk and
    scanned by scan_tree (and the toolkit scanner, in place) like every real tree."""
    from nanosoc_rm.scan.rules import specimen
    return _scratch_superproject(ctx, "vendor",
                                 files={"rtl/planted.v": specimen("arm.banner") + "\nmodule planted; endmodule\n"})


# ---------------------------------------------------------------------------------------------------------
# t0.registry.schema (D2)
# ---------------------------------------------------------------------------------------------------------
def _schema_check(ctx: Context) -> tuple[str, str]:
    items = ctx._begin("t0.registry.schema")
    if ctx.registry_dir is None or not ctx.registry_dir.is_dir():
        return "UNVERIFIED", "no registry checkout to load"
    try:
        from nanosoc_rm.registry import RegistryError, load_registry
    except ImportError as e:
        return "UNVERIFIED", "the registry loader is not available in this build: %s" % e
    try:
        reg = load_registry(ctx.registry_dir)
    except RegistryError as e:
        problems = list(getattr(e, "problems", []) or [])
        for p in problems:
            ctx._add(items, {"file": str(getattr(p, "file", "?")) or "?", "pointer": str(getattr(p, "pointer", "")),
                             "result": "fail", "why": "%s [%s]" % (getattr(p, "message", p), getattr(p, "rule", "?"))})
        first = "; ".join("%s:%s: %s" % (getattr(p, "file", "?"), getattr(p, "pointer", ""), getattr(p, "message", p))
                          for p in problems[:3])
        return "FAIL", "the registry does not load: %d problem(s): %s" % (len(problems), first or e)
    n = lambda f: len(list(getattr(reg, f)())) if callable(getattr(reg, f, None)) else 0
    return "PASS", "the registry loads with no problem: %d components, %d configurations, %d defects, %d errata" % (
        n("components"), n("configurations"), n("defects"), n("errata"))


def _invalid_catalogue() -> str | None:
    p = schemas_dir() / "examples" / "invalid" / "component-catalogue.no-class.yaml"
    return p.read_text() if p.is_file() else None


def _schema_control(ctx: Context) -> Context:
    """The registry with the invalid example component-catalogue.no-class.yaml as its catalogue."""
    d = ctx.git._slot("registry-negctl", ctx.nonce)
    if ctx.registry_dir is not None and ctx.registry_dir.is_dir():
        shutil.copytree(ctx.registry_dir, d, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    d.mkdir(parents=True, exist_ok=True)
    bad = _invalid_catalogue()
    if bad is None:                       # a wheel without the examples: drop the first component's class
        cat = load_yaml((d / "catalogue.yaml").read_text()) if (d / "catalogue.yaml").is_file() else None
        if isinstance(cat, dict) and cat.get("components"):
            cat["components"][0].pop("class", None)
        else:
            cat = {"schema": 1, "components": [{"id": "negctl", "canonical": {"host": "github", "path": "a/b"}}]}
        bad = yaml.safe_dump(cat, sort_keys=False)
    (d / "catalogue.yaml").write_text(bad)
    return ctx.derive(registry_dir=d)


# ---------------------------------------------------------------------------------------------------------
# t0.registry.refs-resolve (D2, F3)
# ---------------------------------------------------------------------------------------------------------
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
CITING_FILES = ("defects/*.yaml", "errata/*/*.yaml", "imports.yaml")


def _pointer(parts: list) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _citations_in(doc: Any, path: list) -> Iterable[tuple[str, str, str]]:
    """(pointer to the commit, component, commit) for every {component, commit} mapping: commit_ref, cites,
    origin, fixed_in, candidate_fix, detector source, regression_test, workaround implementations,
    exposure.run.detector_source, import-source doc (loader rule 9's structured citations)."""
    if isinstance(doc, dict):
        comp, commit = doc.get("component"), doc.get("commit")
        if isinstance(comp, str) and isinstance(commit, str) and _SHA40.match(commit):
            yield _pointer(path + ["commit"]), comp, commit
        for k, v in doc.items():
            yield from _citations_in(v, path + [k])
    elif isinstance(doc, list):
        for n, v in enumerate(doc):
            yield from _citations_in(v, path + [n])


def registry_canonicals(registry_dir: Path) -> dict[str, dict]:
    """component or external id -> {host, path} of its canonical repo, from catalogue.yaml."""
    cat = load_yaml((registry_dir / "catalogue.yaml").read_text())
    out = {}
    for c in (cat or {}).get("components") or []:
        if isinstance(c, dict) and isinstance(c.get("canonical"), dict):
            out[c.get("id")] = c["canonical"]
    for e in (cat or {}).get("external") or []:
        if isinstance(e, dict) and isinstance(e.get("repo"), dict):
            out[e.get("id")] = e["repo"]
    return out


def registry_citations(registry_dir: Path) -> tuple[list[dict], list[str]]:
    """([{file, pointer, component, commit}], [files that could not be read])."""
    out, unreadable = [], []
    files = sorted({p for pat in CITING_FILES for p in registry_dir.glob(pat) if p.is_file()})
    for f in files:
        rel = f.relative_to(registry_dir).as_posix()
        try:
            doc = load_yaml(f.read_text())
        except Exception:
            unreadable.append(rel)
            continue
        for ptr, comp, commit in _citations_in(doc, []):
            out.append({"file": rel, "pointer": ptr, "component": comp, "commit": commit})
    return out, unreadable


def _refs_check(ctx: Context) -> tuple[str, str]:
    items = ctx._begin("t0.registry.refs-resolve")
    if ctx.registry_dir is None or not (ctx.registry_dir / "catalogue.yaml").is_file():
        return "UNVERIFIED", "no registry checkout with a catalogue"
    try:
        canon = registry_canonicals(ctx.registry_dir)
    except Exception as e:
        return "UNVERIFIED", "catalogue.yaml cannot be read: %s" % str(e).splitlines()[0]
    if ctx.citations_override is not None:
        cites, unreadable = ctx.citations_override, []
    else:
        cites, unreadable = registry_citations(ctx.registry_dir)
    for rel in unreadable:
        ctx._add(items, {"file": rel, "pointer": "", "result": "unverified", "why": "file cannot be parsed"})
    infra = False
    for c in cites:
        item = {"file": c["file"], "pointer": c["pointer"], "component": c["component"], "commit": c["commit"],
                "host": None}
        repo = canon.get(c["component"])
        if repo is None:
            ctx._add(items, dict(item, result="fail", why="component %s is not in the catalogue" % c["component"]))
            continue
        item["host"] = repo.get("host")
        url = ctx.git.repo_url(repo)
        if url is None:
            ctx._add(items, dict(item, result="unverified", why="no base URL for host %s in this job" % repo.get("host")))
            continue
        clone = ctx.git.refs_clone(url)
        if not clone.ok:
            infra = infra or clone.cls == "unreachable"
            ctx._add(items, dict(item, result="unverified", why=(
                "host unreachable: %s (INFRA)" % repo["path"] if clone.cls == "unreachable"
                else "canonical repo %s is not readable anonymously" % repo["path"])))
            continue
        ref = ctx.git.containing_ref(clone, c["commit"])
        if ref:
            ctx._add(items, dict(item, result="pass", why="reachable from %s of %s %s" % (ref, repo["host"], repo["path"])))
        else:
            ctx._add(items, dict(item, result="fail", why="%s is on no branch or tag of its canonical repo %s %s" % (
                _s7(c["commit"]), repo["host"], repo["path"])))
    if not items:
        return "UNVERIFIED", "no structured citation found: nothing to prove"
    fails = [i for i in items if i["result"] == "fail"]
    undecided = [i for i in items if i["result"] == "unverified"]
    detail = "%d of %d citations proved on their canonical host" % (sum(1 for i in items if i["result"] == "pass"),
                                                                    len(items))
    if fails:
        detail += "; not there: " + ", ".join("%s#%s %s (%s)" % (i["file"], i["pointer"], _s7(i["commit"]),
                                                                  i["component"]) for i in fails[:8])
    if undecided:
        detail += "; unverified: " + ", ".join("%s#%s" % (i["file"], i["pointer"]) for i in undecided[:8])
    status = "FAIL" if fails else "UNVERIFIED" if undecided and not infra else "INFRA" if infra else "PASS"
    return status, detail


def _refs_control(ctx: Context) -> Context:
    """A synthetic record citing a commit that does not exist, in a component whose canonical repo is readable."""
    if ctx.registry_dir is None or not (ctx.registry_dir / "catalogue.yaml").is_file():
        return ctx.derive(citations_override=[])
    try:
        canon = registry_canonicals(ctx.registry_dir)
        cites, _ = registry_citations(ctx.registry_dir)
    except Exception:
        return ctx.derive(citations_override=[])
    # Prefer a component whose canonical clone the check itself already made and read (no new network call:
    # building a control never fetches); else the first catalogued one.
    cloned_ok = {k for k, c in ctx.git._clones.items() if c.ok}
    known = [c["component"] for c in cites if c["component"] in canon and ctx.git.repo_url(canon[c["component"]])]
    readable = [k for k in known if repo_key(ctx.git.repo_url(canon[k])) in cloned_ok]
    pick = (readable or known or [None])[0]
    if pick is None:
        return ctx.derive(citations_override=[])
    return ctx.derive(citations_override=[{"file": "defects/SL-DEF-9999.yaml", "pointer": "/origin/0/commit",
                                           "component": pick, "commit": synthetic_commit(ctx.nonce, "citation")}])


# ---------------------------------------------------------------------------------------------------------
# The gate table
# ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Gate:
    id: str
    scope: Literal["configuration", "registry"]
    reqs: tuple[str, ...]
    may_skip: bool
    check: Callable[[Context], tuple[str, str]]
    control: Callable[[Context], Context]     # returns the BROKEN input only


GATES: dict[str, Gate] = {g.id: g for g in (
    Gate("t0.infra.probe", "configuration", ("D7",), False, _probe_check, _probe_control),
    Gate("t0.runner.hardened", "configuration", ("D10",), False, _hardened_check, _hardened_control),
    Gate("t0.pins.fetchable", "configuration", ("D2",), False, _fetchable_check, _pins_control),
    Gate("t0.pins.reachable", "configuration", ("D2",), False, _reachable_check, _pins_control),
    Gate("t0.urls.no-ssh-public", "configuration", ("D2",), False, _no_ssh_check, _no_ssh_control),
    Gate("t0.manifest.valid", "configuration", ("D2", "C11"), True, _manifest_check, _manifest_control),
    Gate("t0.clone.upgrade-path", "configuration", ("D2",), True, _upgrade_check, _upgrade_control),
    Gate("t0.vendor.no-arm-ip", "configuration", ("D2", "D11"), False, _vendor_check, _vendor_control),
    Gate("t0.registry.schema", "registry", ("D2", "PR3"), False, _schema_check, _schema_control),
    Gate("t0.registry.refs-resolve", "registry", ("D2", "F3"), False, _refs_check, _refs_control),
)}
MAY_SKIP = frozenset(g.id for g in GATES.values() if g.may_skip)


def gates_for(scope: str) -> list[str]:
    """Declared gate ids of a scope, in table order (read at call time, so a patched table is honoured)."""
    if scope not in SCOPES:
        raise ValueError("unknown gate scope %r (one of %s)" % (scope, ", ".join(SCOPES)))
    return [gid for gid, g in GATES.items() if g.scope == scope]


# ---------------------------------------------------------------------------------------------------------
# Running a gate with its control
# ---------------------------------------------------------------------------------------------------------
def _row(gate: str, status: str, detail: str, nonce: str) -> dict:
    return {"utc": clock.iso(), "status": status, "gate": gate, "detail": clean_detail(detail), "nonce": nonce}


def _safe_check(gate: Gate, ctx: Context) -> tuple[str, str]:
    try:
        out = gate.check(ctx)
        status, detail = out
    except Exception as e:                       # a crashed check never reads as an answer
        return "UNVERIFIED", "check raised %s: %s" % (type(e).__name__, str(e).splitlines()[0] if str(e) else "")
    if status not in STATUSES:
        return "UNVERIFIED", "check returned an invalid status %r" % (status,)
    return status, str(detail)


def run_with_control(gate_id: str, ctx: Context) -> tuple[dict, dict]:
    """(row, negctl row), both verdict_row dicts with ctx's nonce. The gate's own check runs on the real input,
    then on control(ctx); the negctl row is PASS only when that second run FAILed (the control fired)."""
    gate = GATES[gate_id]
    status, detail = _safe_check(gate, ctx)
    row = _row(gate_id, status, detail, ctx.nonce)
    try:
        broken = gate.control(ctx)
    except Exception as e:
        return row, _row(gate_id + ".negctl", "FAIL", "control input could not be built (%s: %s): not fired"
                         % (type(e).__name__, e), ctx.nonce)
    cstatus, cdetail = _safe_check(gate, broken)
    if cstatus == "FAIL":
        crow = _row(gate_id + ".negctl", "PASS", "fired: on its broken input the gate reported FAIL: %s"
                    % cdetail[:300], ctx.nonce)
    else:
        crow = _row(gate_id + ".negctl", "FAIL", "did not fire: on its broken input the gate reported %s: %s"
                    % (cstatus, cdetail[:300]), ctx.nonce)
    return row, crow


# ---------------------------------------------------------------------------------------------------------
# Roll-up (README section 6)
# ---------------------------------------------------------------------------------------------------------
def rollup(rows: list[dict], scope: str) -> list[dict]:
    """One t0_result gate item {id, verdict, control, detail} per declared gate of `scope`, in table order.
    Rows of undeclared gates are ignored. Pure."""
    out = []
    for gid in gates_for(scope):
        gate = GATES[gid]
        main = [r for r in rows if r.get("gate") == gid]
        ctl = [r for r in rows if r.get("gate") == gid + ".negctl"]
        control = "missing" if not ctl else "fired" if len(ctl) == 1 and ctl[0].get("status") == "PASS" else "not-fired"
        if len(main) != 1:
            verdict = "UNVERIFIED"
            detail = "no row for this gate" if not main else "%d rows for this gate" % len(main)
        else:
            st, detail = main[0].get("status"), str(main[0].get("detail") or "")
            if st == "PASS":
                verdict = "PASS" if control == "fired" else "UNVERIFIED"
                if verdict != "PASS":
                    detail = "PASS without a fired control (%s): %s" % (control, detail)
            elif st == "FAIL":
                verdict = "FAIL"
            elif st in ("UNVERIFIED", "WARN"):
                verdict = "UNVERIFIED"
                if st == "WARN":
                    detail = "WARN: %s" % detail
            elif st == "INFRA":
                verdict = "INFRA"
            elif st == "SKIP" and gate.may_skip and detail.strip():
                verdict = "NOT-RUN"
            elif st == "SKIP":
                verdict = "UNVERIFIED"
                detail = ("SKIP on a gate that may never skip: %s" % detail) if not gate.may_skip else \
                    "SKIP without a reason"
            else:
                verdict, detail = "UNVERIFIED", "unknown status %r: %s" % (st, detail)
        if verdict == "NOT-RUN":
            control = "not-applicable"
        out.append({"id": gid, "verdict": verdict, "control": control, "detail": detail})
    return out


def apply_waivers(gates: list[dict], items: Mapping[str, list[dict]], waivers: list[dict]) -> tuple[list[dict], list[dict]]:
    """GATE2 Q1 (README section 6): (gates with WAIVED, raw_verdict and waived set where every non-pass item is
    covered, the waivers that covered nothing). An item is covered by a waiver naming its gate, tree path and
    result, or, when it is `not-reached`, by a directly covered item above it in the same gate (the superproject's
    item, tree path '.', is above every other). A gate becomes
    WAIVED only when its verdict is FAIL or UNVERIFIED, its control fired, it has at least one non-pass item and
    every one is covered. Pure: the inputs are never changed."""
    out, used = [], set()
    for g in gates:
        g = copy.deepcopy(g)
        gid = g.get("id")
        bad = [i for i in (items.get(gid) or []) if i.get("result") != "pass"]
        direct: dict[tuple[str, str], int] = {}          # (tree path, result) -> the waiver naming that item
        for n, w in enumerate(waivers):
            if w.get("gate") != gid:
                continue
            for i in bad:
                if i.get("tree_path") is not None and (w.get("tree_path"), w.get("result")) == (i["tree_path"], i["result"]):
                    direct.setdefault((i["tree_path"], i["result"]), n)
                    used.add(n)
        covered_paths = {tp: n for (tp, _r), n in direct.items()}
        waived = []
        for i in bad:
            tp = i.get("tree_path")
            if tp is None:
                break                                     # a registry item: never waived
            if (tp, i["result"]) in direct:
                cover, n = tp, direct[(tp, i["result"])]
            elif i["result"] == "not-reached":
                cover = next((t for t in sorted(covered_paths, key=len, reverse=True)
                              if (t == "." and tp != ".") or tp.startswith(t + "/")), None)
                n = covered_paths.get(cover) if cover is not None else None
            else:
                cover = None
            if cover is None:
                break
            w = waivers[n]
            waived.append({"tree_path": tp, "result": i["result"], "covered_by": cover, "reason": w["reason"],
                           "approved_by": w["approved_by"]})
        if (g.get("verdict") in ("FAIL", "UNVERIFIED") and g.get("control") == "fired" and bad
                and len(waived) == len(bad)):
            g["raw_verdict"], g["verdict"], g["waived"] = g["verdict"], "WAIVED", waived
        out.append(g)
    return out, [copy.deepcopy(w) for n, w in enumerate(waivers) if n not in used]


def run_verdict(items: list[dict]) -> tuple[str, int]:
    """(run verdict, NOT-RUN count): FAIL > UNVERIFIED (also when no gate PASSed) > INFRA > WAIVED > PASS."""
    verdicts = [g.get("verdict") for g in items]
    not_run = verdicts.count("NOT-RUN")
    if "FAIL" in verdicts:
        return "FAIL", not_run
    if "UNVERIFIED" in verdicts or "PASS" not in verdicts:
        return "UNVERIFIED", not_run
    if "INFRA" in verdicts:
        return "INFRA", not_run
    if "WAIVED" in verdicts:
        return "WAIVED", not_run
    return "PASS", not_run
