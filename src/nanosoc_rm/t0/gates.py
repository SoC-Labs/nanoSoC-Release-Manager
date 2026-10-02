"""T0 gates, their negative controls, and the roll-up (interfaces.md section 9; README section 6).

Shared by the runner (which runs in CI only) and the service (which rolls up a verified bundle). Importing this
module never imports the runner.

- `GATES`: every declared gate. Each has a `check(ctx) -> (status, detail)` and a `control(ctx) -> Context`
  that returns the BROKEN input only. `run_with_control` runs the gate's own check twice, on the real input and
  on the broken one, in one run with one nonce; the control's row `<gate>.negctl` is PASS only when the check
  went red (FAIL) on the broken input. So a check that cannot fail has a control that does not fire, and the
  gate rolls up UNVERIFIED (D6).
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


def credential_vars(env: Mapping[str, str]) -> list[str]:
    """The CI credential variables the job must not hold after the scrub: CI_*TOKEN*, CI_*PASSWORD*,
    CI_JOB_JWT* (interfaces.md section 9, Environment)."""
    return sorted(k for k in env if k.startswith("CI_") and ("TOKEN" in k or "PASSWORD" in k
                                                            or k.startswith("CI_JOB_JWT")))


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
        self.workdir = Path(workdir)
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
        self.pins_override: list[Pin] | None = None
        self.trees_override: list[tuple[str, Path]] | None = None
        self.manifests_override: list[tuple[str, str | None, str]] | None = None
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
                    "CI_*PASSWORD* or CI_JOB_JWT* variable left after the scrub" % user_var)


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
    if ctx.pins_override is not None:
        pins = ctx.pins_override
    else:
        root = ctx.walk()
        if root is None:
            return "UNVERIFIED", "no request: nothing to walk"
        rf = ctx.root_fetch()
        if not rf.ok:
            item, infra = _fetch_item(ctx, root)
            ctx._add(items, dict(item, tree_path="."))
        pins = list(iter_pins(root))
    for pin in pins:
        up = None if ctx.pins_override is not None else ctx.unfetched_ancestor(pin)
        if up is not None:
            ctx._add(items, {"tree_path": pin.tree_path, "component": None, "commit": pin.commit,
                             "host": ctx.git.host_of(pin.resolved or pin.url), "result": "not-reached",
                             "why": "below %s, which was not fetched" % (up.tree_path or "the superproject")})
            continue
        item, down = _fetch_item(ctx, pin)
        infra = infra or down
        ctx._add(items, item)
    if not items and ctx.pins_override is not None:
        return "UNVERIFIED", "no pin to judge"
    n, passed = len(items), sum(1 for i in items if i["result"] == "pass")
    status = "FAIL" if any(i["result"] == "fail" for i in items) else "INFRA" if infra else \
        "UNVERIFIED" if passed < n else "PASS"
    detail = "%d of %d pins fetched anonymously" % (passed, n)
    if any(i["result"] == "fail" for i in items):
        detail += "; not fetched: %s" % _paths(items, "fail")
    if any(i["result"] == "not-reached" for i in items):
        detail += "; not reached: %d below them" % sum(1 for i in items if i["result"] == "not-reached")
    if any(i["result"] == "unverified" for i in items):
        detail += "; undecided: %s" % _paths(items, "unverified")
    return status, detail


def _synthetic_pin(ctx: Context) -> list[Pin]:
    url = ctx.repo_url
    if not url:
        return []
    canonical = ctx.git.canonical_for(url, url, ctx.canonical_urls())
    return [Pin("negctl/synthetic-pin", url, synthetic_commit(ctx.nonce, "pin"), resolved=url,
                kind=url_kind(url), canonical=canonical, host=ctx.git.host_of(canonical))]


def _pins_control(ctx: Context) -> Context:
    """A synthetic pin at a commit that does not exist, in the real public superproject repo."""
    return ctx.derive(pins_override=_synthetic_pin(ctx))


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
    if ctx.pins_override is not None:
        pins = ctx.pins_override
        if not pins:
            return "UNVERIFIED", "no pin to judge"
    else:
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
    n, passed = len(items), sum(1 for i in items if i["result"] == "pass")
    fails = [i for i in items if i["result"] == "fail"]
    undecided = [i for i in items if i["result"] in ("unverified", "not-reached")]
    status = "FAIL" if fails else "UNVERIFIED" if undecided and not infra else "INFRA" if infra else "PASS"
    parts = ["%d of %d pins reachable from a branch or tag of their canonical repo" % (passed, n)]
    if fails:
        parts.append("unreachable: %s" % ", ".join("%s %s" % (i["tree_path"], _s7(i["commit"])) for i in fails[:8]))
    if any(i["result"] == "unverified" for i in undecided):
        parts.append("unverified: %s" % _paths(undecided, "unverified"))
    nr = sum(1 for i in undecided if i["result"] == "not-reached")
    if nr:
        parts.append("not reached: %d" % nr)
    if not pins:
        parts = ["the superproject pins nothing"]
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
    offenders = {}
    for key, p in parents.items():
        links = p.get("gitlinks") or {}
        for e in parse_gitmodules(p.get("gitmodules") or ""):
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
    return "PASS", "%d public parent(s), %d pins, none by ssh URL" % (len(parents), len(items))


def _no_ssh_control(ctx: Context) -> Context:
    sha = synthetic_commit(ctx.nonce, "public-parent")
    return ctx.derive(parents={"github/SoC-Labs/nanosoc-rm-negctl@%s" % sha: {
        "public": True, "tree_path": "negctl",
        "gitmodules": '[submodule "x"]\n\tpath = x\n\turl = git@github.com:SoC-Labs/nanosoc-rm-negctl-x.git\n'}})


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
    if ctx.manifests_override is not None:
        manifests, unfetched = ctx.manifests_override, 0
    else:
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
    """A manifest whose version is "1.0", not SemVer: the only defect."""
    bad = dict(_MANIFEST_DOC, version="1.0")
    return ctx.derive(manifests_override=[("negctl", synthetic_commit(ctx.nonce, "manifest"),
                                           yaml.safe_dump(bad, sort_keys=False))])


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
    from nanosoc_rm.scan.rules import RulesError, rule_set
    from nanosoc_rm.scan.scan import scan_dir, scan_tree
    from nanosoc_rm.scan.toolkit import toolkit
    items = ctx._begin("t0.vendor.no-arm-ip")
    try:
        rules, tk = rule_set(), toolkit()       # $RM_SCAN_RULES, $RM_SCAN_TOOLKIT: compat-ci's pinned copies
    except RulesError as e:
        return "UNVERIFIED", "scanner rules unusable: %s" % e
    targets: list[tuple[str, Any]] = []
    if ctx.trees_override is not None:
        targets = [(tp, ("dir", path)) for tp, path in ctx.trees_override]
    else:
        root = ctx.walk()
        if root is None:
            return "UNVERIFIED", "no request: nothing to scan"
        for p in [root] + list(iter_pins(root)):
            tp = p.tree_path or "."
            up = None if p is root else ctx.unfetched_ancestor(p)
            if up is not None or not ctx.pinned_ok(p):
                ctx._add(items, {"tree_path": tp, "component": None, "commit": p.commit,
                                 "host": ctx.git.host_of(p.resolved or p.url), "result": "not-reached",
                                 "why": "not fetched (t0.pins.fetchable), so not scanned"})
                continue
            targets.append((tp, ("tree", p)))
    cache: dict[tuple, Any] = {}
    found = []
    for tp, (kind, obj) in targets:
        if kind == "dir":
            res, commit, host = scan_dir(obj, rules, toolkit=tk), None, None
        else:
            f = ctx.git.fetch_commit(obj.resolved or obj.url, obj.commit)
            key = (f.repo, obj.commit)
            if key not in cache:
                cache[key] = scan_tree(f.repo, obj.commit, rules, env=ctx.git.env, toolkit=tk)
            res, commit, host = cache[key], obj.commit, ctx.git.host_of(obj.resolved or obj.url)
        item = {"tree_path": tp, "component": None, "commit": commit, "host": host}
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
    nr = sum(1 for i in items if i["result"] == "not-reached")
    if nr:
        detail += "; %d not fetched, so not scanned" % nr
    if found:
        return "FAIL", detail + ": " + ", ".join(found[:8])
    if not items or any(i["result"] in ("unverified", "not-reached") for i in items):
        return "UNVERIFIED", detail
    return "PASS", detail


def _vendor_control(ctx: Context) -> Context:
    """A scratch tree with the arm.banner specimen planted in a file."""
    from nanosoc_rm.scan.rules import specimen
    d = ctx.git._slot("vendor-negctl", ctx.nonce)
    (d / "rtl").mkdir(parents=True, exist_ok=True)
    (d / "rtl" / "planted.v").write_text(specimen("arm.banner") + "\nmodule planted; endmodule\n")
    return ctx.derive(trees_override=[("negctl", d)])


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
    result, or, when it is `not-reached`, by a directly covered item above it in the same gate. A gate becomes
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
                cover = next((t for t in sorted(covered_paths, key=len, reverse=True) if tp.startswith(t + "/")), None)
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
