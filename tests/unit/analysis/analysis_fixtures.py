"""Hermetic inputs for the analysis unit tests: a registry copied from the contract examples, component mirrors
built with raw git in the layout of interfaces.md section 3, and stored pin_state / mirror_status observations
as the walker and the mirror would store them (their CONTRACTS, sections 3 and 4; not their code).

Nothing here imports the module under test, so the fixtures cannot agree with the code by construction."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"
AT = REPO / "tests" / "acceptance"
T = "2026-10-01T10:00:00Z"
SRC = [{"kind": "derived", "command": "unit fixture"}]


def load_at(name):
    """An acceptance-test module, for its checkers (check_B4, check_B5, check_B6) and goldens."""
    modname = "rm_at_" + name
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, AT / ("%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod                    # dataclasses in the module look themselves up here
    spec.loader.exec_module(mod)
    return mod


def git(*args, cwd=None, input=None, check=True):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, input=input, capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), p.stderr.strip()))
    return p.stdout.strip()


def example_registry(root):
    """README section 1 layout, from the contract examples (as the acceptance tests build it)."""
    root = Path(root)
    for src, dst in (("component-catalogue.example.yaml", "catalogue.yaml"),
                     ("eligibility.example.yaml", "eligibility.yaml"),
                     ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
                     ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
                     ("import-ledger.example.yaml", "imports.yaml")):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, root / dst)
    return root


def write_config(tmp_path, monkeypatch, *, git_base=None):
    """A service config pointing at tmp data and registry; $NANOSOC_RM_CONFIG names it. git_base, when given,
    is the hosts' `git` fetch base (hermetic routing goes through url.insteadOf instead)."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    registry = example_registry(tmp_path / "registry")
    lines = ["[service]", 'data_dir = "%s"' % data, "[registry]", 'repo = "soclabs/nanosoc-registry"',
             'ref = "main"', 'path = "%s"' % registry,
             "[hosts.github]", 'web = "https://github.com"', 'api = "https://api.github.com"',
             'ssh = "git@github.com:"', 'org = "SoC-Labs"',
             "[hosts.gitlab]", 'web = "https://gitlab.example.invalid"',
             'api = "https://gitlab.example.invalid/api/v4"', 'ssh = "git@gitlab.example.invalid:"',
             'group = "soclabs"', ""]
    path = tmp_path / "rm.toml"
    path.write_text("\n".join(lines))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(path))
    return path, data, registry


def route_hosts(tmp_path, monkeypatch):
    """url.insteadOf: https://github.com/ and https://gitlab.example.invalid/ fetch from <tmp>/hosts/<host>/."""
    hosts = tmp_path / "hosts"
    gc = tmp_path / "gitconfig"
    gc.write_text("\n".join([
        "[user]", "\tname = rm-unit", "\temail = rm-unit@example.invalid",
        '[protocol "file"]', "\tallow = always", "[init]", "\tdefaultBranch = main",
        '[url "%s/"]' % (hosts / "github").as_uri(), "\tinsteadOf = https://github.com/",
        '[url "%s/"]' % (hosts / "gitlab").as_uri(), "\tinsteadOf = https://gitlab.example.invalid/", ""]))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return hosts


def key(host, path):
    return "%s/%s" % (host, urllib.parse.quote(path, safe=""))


class Mirror:
    """A component mirror: one bare repo, one namespace per repo (interfaces.md section 3)."""

    def __init__(self, data_dir, component):
        self.path = Path(data_dir) / "mirrors" / ("%s.git" % component)
        if not self.path.exists():
            git("init", "--quiet", "--bare", self.path)
        self.empty_tree = git("--git-dir", self.path, "mktree", input="")
        self.n = 0

    def commit(self, *parents, msg=None):
        self.n += 1
        args = [a for p in parents for a in ("-p", p)]
        return git("--git-dir", self.path, "commit-tree", self.empty_tree, *args, "-m", msg or "%s c%d" % (self.path.name, self.n),
                   input="")

    def chain(self, n, base=None):
        out, prev = [], base
        for _ in range(n):
            prev = self.commit(*([prev] if prev else []))
            out.append(prev)
        return out

    def ref(self, host, path, kind, name):
        return "refs/r/%s/%s/%s" % (key(host, path), kind, name)

    def set(self, host, path, kind, name, sha):
        git("--git-dir", self.path, "update-ref", self.ref(host, path, kind, name), sha)

    def delete(self, host, path, kind, name):
        git("--git-dir", self.path, "update-ref", "-d", self.ref(host, path, kind, name))

    def head(self, host, path, branch):
        """Record the repo's default branch where the mirror module's sync records it: the mirror's own git
        config, `nanosoc-rm.<key>.defaultbranch` (build/mirror-walker, mirror/layout.py set_default_branch)."""
        git("--git-dir", self.path, "config", "nanosoc-rm.%s.defaultbranch" % key(host, path), branch)

    def forget_head(self, host, path):
        git("--git-dir", self.path, "config", "--unset", "nanosoc-rm.%s.defaultbranch" % key(host, path))

    def refs(self, host, path):
        return git("--git-dir", self.path, "for-each-ref", "--format=%(refname)", "refs/r/" + key(host, path)).split()


def host_state(repo, role, pinned, state, *, default_branch="main", lag=None, branches=(), tags=(), at=T):
    return {"repo": repo, "role": role, "pinned": pinned, "state": state, "default_branch": default_branch,
            "lag": lag, "branches": list(branches), "tags": list(tags), "observed_at": at, "sources": SRC}


PARENT = {"component": "nanosoc-ethernet-chiplet",
          "repo": {"host": "github", "path": "SoC-Labs/NanoSoC-Ethernet-Chiplet"},
          "commit": "ab73e40924317e45e7b3b941f52a7ed35f20832e"}


def pin_doc(configuration, tree_path, component, commit, hosts, *, at=T, extra_paths=()):
    """One pin_state observation as the walker stores it (pin-state.schema.json)."""
    pinned = next(s for s in hosts.values() if s["pinned"])
    return {"schema": 1, "kind": "pin_state", "subject": "pin_state:%s:%s" % (configuration, tree_path),
            "observed_at": at, "sources": SRC,
            "fact": {"configuration": configuration, "parent": PARENT, "path": tree_path.rsplit("/", 1)[-1],
                     "url": "https://example.invalid/%s.git" % pinned["repo"]["path"], "url_kind": "https",
                     "branch_field": None, "entry_type": "commit", "state": None,
                     "child": {"component": component, "catalogued_as": "component", "repo": pinned["repo"],
                               "commit": commit},
                     "tree_paths": [tree_path, *extra_paths], "depth": 1, "split": False, "effective": "unknown",
                     "hosts": hosts}}


def store_tree(configuration, docs, snap_id=None):
    """Store a walk the way section 4 says walk() does: one complete pin_state snapshot."""
    from nanosoc_rm import db
    scope = "pin_state:%s" % configuration
    snap = {"id": snap_id or "%s@%s" % (scope, T), "scope": scope}
    for d in docs:
        d["snapshot"] = dict(snap)
    db.put_snapshot(scope, docs)


def store_mirror_statuses(component, statuses, at=T):
    """One mirror sync of a component (one complete mirror_status snapshot): statuses = [(repo, ok)]."""
    from nanosoc_rm import db
    scope = "mirror_status:%s" % component
    snap = {"id": "%s@%s" % (scope, at), "scope": scope}
    db.put_snapshot(scope, [
        {"schema": 1, "kind": "mirror_status", "subject": "mirror_status:%s:%s" % (repo["host"], repo["path"]),
         "observed_at": at, "sources": SRC, "snapshot": dict(snap),
         "fact": {"repo": repo, "component": component, "ok": ok, "last_success_at": at if ok else None,
                  "error": None if ok else "fetch failed: 404"}}
        for repo, ok in statuses])
