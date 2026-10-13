"""A hermetic world for the bump engine: a registry (the contract examples plus one fixture defect), component
mirrors built with raw git in the section 3 layout (real trees with gitlinks, tags, a changelog and a manifest),
and a stored walk of eth-main as the walker stores it (pin-state contract). Nothing here imports nanosoc_rm.bumps.

    eth-main (nanosoc-ethernet-chiplet, github, branch main)            r1
      nanosoc-multicore-system      (gitlab soclabs/nanosoc-multicore-system, master)   m1
        nanosoc_arch_tech           (gitlab copy soclabs/nanosoc-m0/nanosoc_arch_tech, main)  a1
          nanosoc_gen               (gitlab soclabs/nanosoc_gen)                         g1
      tidelink                      (github canonical, no release tag)                   t1

nanosoc_gen: g1 v0.1.0 -> g2 v0.1.1 (origin of SL-DEF-0042) -> g3 (fixes SL-DEF-0042) -> g4 v0.2.0 (CHANGELOG.md)
             -> g5 v0.3.0-rc.1 (a pre-release: never proposed)
nanosoc_arch_tech: a1 pins g1; a2 (child of a1, same gitlink) is v0.5.0, tagged on the gitlab copy only
nanosoc-multicore-system: m1 pins a1
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"
T = "2026-10-01T10:00:00Z"
SRC = [{"kind": "derived", "command": "unit fixture"}]

GH_ETH = {"host": "github", "path": "SoC-Labs/NanoSoC-Ethernet-Chiplet"}
GL_MC = {"host": "gitlab", "path": "soclabs/nanosoc-multicore-system"}
GH_ARCH = {"host": "github", "path": "SoC-Labs/nanoSoC-Arch-Tech"}
GL_ARCH = {"host": "gitlab", "path": "soclabs/nanosoc-m0/nanosoc_arch_tech"}
GL_GEN = {"host": "gitlab", "path": "soclabs/nanosoc_gen"}
GH_TL = {"host": "github", "path": "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"}

CHANGELOG = """# Changelog

## [0.2.0] - 2026-10-01
Class: feature
### Added
- the cpu_ss_hsel lint

## [0.1.1] - 2026-09-20
### Fixed
- a typo in the generated header

## [0.1.0] - 2026-09-01
Class: none
- first release
"""

DEFECT = """schema: 1
id: SL-DEF-0042
title: Fixture defect introduced in g2 and fixed in g3
component: nanosoc_gen
origin:
  - {component: nanosoc_gen, commit: "%(g2)s"}
forms:
  - id: A
    name: the only form
    variant: all
    fixed_in:
      - {component: nanosoc_gen, commit: "%(g3)s"}
trigger: never in a test
detectors:
  - {id: D1, kind: netlist, forms: [A], pattern: 'nothing'}
effects:
  - {id: E1, forms: [A]}
regression_test:
  component: nanosoc_gen
  commit: "%(g3)s"
  path: tests/defect
  expect: {shipped: defect, gated: clean}
"""


def git(*args, cwd=None, input=None, env=None):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, input=input, capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "u", "GIT_AUTHOR_EMAIL": "u@x",
                            "GIT_COMMITTER_NAME": "u", "GIT_COMMITTER_EMAIL": "u@x", **(env or {})})
    if p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), p.stderr.strip()))
    return p.stdout.strip()


def key(repo):
    return "%s/%s" % (repo["host"], urllib.parse.quote(repo["path"], safe=""))


class Mirror:
    def __init__(self, data, component):
        self.path = Path(data) / "mirrors" / ("%s.git" % component)
        if not self.path.exists():
            git("init", "--quiet", "--bare", self.path)
        self.n = 0

    def blob(self, text):
        return git("--git-dir", self.path, "hash-object", "-w", "--stdin", input=text)

    def commit(self, *parents, links=None, files=None, msg=None, date=None):
        """A commit whose tree holds `links` {path: sha} as gitlinks and `files` {name: text} as blobs."""
        self.n += 1
        lines = ["160000 commit %s\t%s" % (sha, path) for path, sha in sorted((links or {}).items())]
        lines += ["100644 blob %s\t%s" % (self.blob(text), name) for name, text in sorted((files or {}).items())]
        tree = git("--git-dir", self.path, "mktree", "--missing", input="\n".join(lines) + ("\n" if lines else ""))
        args = [a for p in parents for a in ("-p", p)]
        env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else None
        return git("--git-dir", self.path, "commit-tree", tree, *args, "-m", msg or "%s c%d" % (self.path.name, self.n),
                   env=env)

    def set(self, repo, kind, name, sha):
        git("--git-dir", self.path, "update-ref", "refs/r/%s/%s/%s" % (key(repo), kind, name), sha)

    def head(self, repo, branch):
        git("--git-dir", self.path, "config", "nanosoc-rm.%s.defaultbranch" % key(repo), branch)


def registry(root, defect_commits):
    root = Path(root)
    for src, dst in (("component-catalogue.example.yaml", "catalogue.yaml"),
                     ("eligibility.example.yaml", "eligibility.yaml"),
                     ("import-ledger.example.yaml", "imports.yaml")):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, root / dst)
    (root / "defects").mkdir(exist_ok=True)
    (root / "defects" / "SL-DEF-0042.yaml").write_text(DEFECT % defect_commits)
    return root


def write_config(tmp_path, registry_dir, extra=""):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "rm.toml"
    path.write_text("\n".join([
        "[service]", 'data_dir = "%s"' % data, "[registry]", 'repo = "soclabs/nanosoc-registry"', 'ref = "main"',
        'path = "%s"' % registry_dir,
        "[hosts.github]", 'web = "https://github.com"', 'api = "https://api.github.com"', 'ssh = "git@github.com:"',
        'org = "SoC-Labs"', "[hosts.gitlab]", 'web = "https://gitlab.example.invalid"',
        'api = "https://gitlab.example.invalid/api/v4"', 'ssh = "git@gitlab.example.invalid:"', 'group = "soclabs"',
        "", extra]))
    return path, data


def host_state(repo, role="canonical", pinned=True, state="ON_DEFAULT"):
    return {"repo": repo, "role": role, "pinned": pinned, "state": state, "default_branch": "main", "lag": 0,
            "branches": [], "tags": [], "observed_at": T, "sources": SRC}


def pin(configuration, tree_path, path, depth, parent, child_component, child_repo, child_commit, role="canonical"):
    return {"schema": 1, "kind": "pin_state", "subject": "pin_state:%s:%s" % (configuration, tree_path),
            "observed_at": T, "sources": SRC,
            "fact": {"configuration": configuration, "parent": parent, "path": path,
                     "url": "https://example.invalid/%s.git" % child_repo["path"], "url_kind": "https",
                     "branch_field": None, "entry_type": "commit", "state": None,
                     "child": {"component": child_component, "catalogued_as": "component", "repo": child_repo,
                               "commit": child_commit},
                     "tree_paths": [tree_path], "depth": depth, "split": False, "effective": "unknown",
                     "hosts": {child_repo["host"]: host_state(child_repo, role)}}}


def store_walk(configuration, docs):
    from nanosoc_rm import db
    scope = "pin_state:%s" % configuration
    snap = {"id": db.new_snapshot_id(scope), "scope": scope}
    for d in docs:
        d["snapshot"] = dict(snap)
    db.put_snapshot(scope, docs)


def world(tmp_path, monkeypatch, *, manifest_range=None, arch_tag_on="gitlab"):
    """Build everything; returns a namespace dict of shas and paths. $NANOSOC_RM_CONFIG names the config."""
    data = tmp_path / "data"
    gen = Mirror(data, "nanosoc_gen")
    g1 = gen.commit(msg="g1 first")
    g2 = gen.commit(g1, msg="g2 introduces the defect")
    g3 = gen.commit(g2, msg="g3 fixes the defect")
    g4 = gen.commit(g3, files={"CHANGELOG.md": CHANGELOG}, msg="g4 release 0.2.0")
    g5 = gen.commit(g4, msg="g5 candidate")
    gen.set(GL_GEN, "heads", "main", g5)
    gen.head(GL_GEN, "main")
    for name, sha in (("v0.1.0", g1), ("v0.1.1", g2), ("v0.2.0", g4), ("v0.3.0-rc.1", g5), ("keep/eth-main/x", g5)):
        gen.set(GL_GEN, "tags", name, sha)

    arch = Mirror(data, "nanosoc_arch_tech")
    files = {}
    if manifest_range:
        files["nanosoc-component.yaml"] = (
            "schema: 1\nid: nanosoc_arch_tech\nclass: arch_tech\nversion: \"0.5.0\"\n"
            "canonical: https://github.com/SoC-Labs/nanoSoC-Arch-Tech.git\nprovides: []\n"
            "requires:\n  - {id: nanosoc_gen, path: nanosoc_gen, range: \"%s\"}\nreexports: [nanosoc_gen]\n"
            "variants:\n  sim: {flist: \"make flist\"}\ntests:\n  t0: {run: \"make t0\", control: \"make t0-control\"}\n"
            % manifest_range)
    a1 = arch.commit(links={"nanosoc_gen": g1}, files=files, msg="a1")
    a2 = arch.commit(a1, links={"nanosoc_gen": g1}, files=files, msg="a2 release 0.5.0")
    arch.set(GL_ARCH, "heads", "main", a1)
    arch.set(GH_ARCH, "heads", "main", a1)
    arch.set(GH_ARCH if arch_tag_on == "github" else GL_ARCH, "tags", "v0.5.0", a2)
    if arch_tag_on == "gitlab":
        arch.set(GL_ARCH, "heads", "release-prep", a2)
    arch.head(GL_ARCH, "main")
    arch.head(GH_ARCH, "main")

    mc = Mirror(data, "nanosoc-multicore-system")
    m1 = mc.commit(links={"nanosoc_arch_tech": a1}, msg="m1")
    mc.set(GL_MC, "heads", "master", m1)
    mc.head(GL_MC, "master")

    tl = Mirror(data, "tidelink")
    t1 = tl.commit(msg="t1")
    tl.set(GH_TL, "heads", "main", t1)
    tl.head(GH_TL, "main")

    eth = Mirror(data, "nanosoc-ethernet-chiplet")
    r1 = eth.commit(links={"nanosoc-multicore-system": m1, "tidelink": t1}, msg="r1")
    eth.set(GH_ETH, "heads", "main", r1)
    eth.head(GH_ETH, "main")

    reg = registry(tmp_path / "registry", {"g2": g2, "g3": g3})
    cfg, _ = write_config(tmp_path, reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    root = {"component": "nanosoc-ethernet-chiplet", "repo": GH_ETH, "commit": r1}
    mcp = {"component": "nanosoc-multicore-system", "repo": GL_MC, "commit": m1}
    archp = {"component": "nanosoc_arch_tech", "repo": GL_ARCH, "commit": a1}
    store_walk("eth-main", [
        pin("eth-main", "nanosoc-multicore-system", "nanosoc-multicore-system", 1, root, "nanosoc-multicore-system",
            GL_MC, m1),
        pin("eth-main", "nanosoc-multicore-system/nanosoc_arch_tech", "nanosoc_arch_tech", 2, mcp,
            "nanosoc_arch_tech", GL_ARCH, a1, role="copy"),
        pin("eth-main", "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen", "nanosoc_gen", 3, archp,
            "nanosoc_gen", GL_GEN, g1),
        pin("eth-main", "tidelink", "tidelink", 1, root, "tidelink", GH_TL, t1),
    ])
    return {"cfg": cfg, "data": data, "registry": reg, "g": [g1, g2, g3, g4, g5], "a": [a1, a2], "m1": m1, "r1": r1,
            "t1": t1, "gen": gen, "arch": arch, "mc": mc, "eth": eth}
