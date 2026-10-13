"""Hermetic inputs for the versioning tests: the example registry, work repos with classed changelogs and tags,
and component mirrors in the interfaces.md section 3 layout (`refs/r/<host>/<quoted path>/{heads,tags}`), built
with raw git. Nothing here imports nanosoc_rm.versioning, so a fixture cannot agree with the code by construction."""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_TERMINAL_PROMPT": "0"}
ARCH = ("github", "SoC-Labs/nanoSoC-Arch-Tech")
ARCH_COPY = ("gitlab", "soclabs/nanosoc-m0/nanosoc_arch_tech")
GEN = ("gitlab", "soclabs/nanosoc_gen")


def git(*args, cwd=None, date=None, input=None, check=True):
    env = dict(os.environ, **ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, capture_output=True, text=True, env=env, input=input)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), p.stderr.strip()))
    return p.stdout.strip()


def key(host, path):
    return "%s/%s" % (host, urllib.parse.quote(path, safe=""))


def example_registry(root: Path) -> Path:
    for src, dst in (("component-catalogue.example.yaml", "catalogue.yaml"),
                     ("eligibility.example.yaml", "eligibility.yaml"),
                     ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
                     ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
                     ("import-ledger.example.yaml", "imports.yaml")):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, root / dst)
    return root


def write_config(tmp_path: Path, monkeypatch, extra: str = "") -> tuple[Path, Path, Path]:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    registry = example_registry(tmp_path / "registry")
    text = "\n".join(["[service]", 'data_dir = "%s"' % data, "[registry]", 'repo = "soclabs/nanosoc-registry"',
                      'ref = "main"', 'path = "%s"' % registry,
                      "[hosts.github]", 'web = "https://github.com"', 'api = "https://api.github.com"',
                      'ssh = "git@github.com:"', 'org = "SoC-Labs"',
                      "[hosts.gitlab]", 'web = "https://gitlab.example.invalid"',
                      'api = "https://gitlab.example.invalid/api/v4"', 'ssh = "git@gitlab.example.invalid:"',
                      'group = "soclabs"', "[roles]", 'default = ["member"]', extra, ""])
    path = tmp_path / "rm.toml"
    path.write_text(text)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(path))
    return path, data, registry


def write(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        p = root / rel
        if text is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def work_repo(root: Path, files: dict[str, str], date="2026-09-01T10:00:00Z", msg="initial") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", ".", cwd=root)
    write(root, files)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", msg, cwd=root, date=date)
    return root


def commit(root: Path, files: dict[str, str | None], date: str, msg: str = "change") -> str:
    write(root, files)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", msg, cwd=root, date=date)
    return git("rev-parse", "HEAD", cwd=root)


def gitlink(root: Path, path: str, sha: str, url: str, date: str, msg="pin") -> str:
    gm = root / ".gitmodules"
    text = gm.read_text() if gm.exists() else ""
    if '"%s"' % path not in text:
        text += '[submodule "%s"]\n\tpath = %s\n\turl = %s\n' % (path, path, url)
        gm.write_text(text)
    (root / path).mkdir(parents=True, exist_ok=True)       # an empty directory: an uninitialised submodule, so
    git("add", ".gitmodules", cwd=root)                     # a later `git add -A` keeps the gitlink
    git("update-index", "--add", "--cacheinfo", "160000,%s,%s" % (sha, path), cwd=root)
    git("commit", "-q", "-m", msg, cwd=root, date=date)
    return git("rev-parse", "HEAD", cwd=root)


def tag(root: Path, name: str, rev="HEAD", annotated=True, date="2026-09-02T10:00:00Z", force=False):
    args = ["tag"] + (["-a", "-m", name] if annotated else []) + (["-f"] if force else []) + [name, rev]
    git(*args, cwd=root, date=date)


def mirror_from(data: Path, component: str, repos: dict[tuple[str, str], Path], default="main") -> Path:
    """A component mirror holding each work repo's heads and tags in its own namespace (a full clone, so no
    promisor fetch is ever attempted); reflogs kept as `mirror sync` configures them."""
    m = data / "mirrors" / ("%s.git" % component)
    if not m.exists():
        git("init", "-q", "--bare", m)
        git("config", "core.logAllRefUpdates", "always", cwd=m)
    for (host, path), work in repos.items():
        ns = "refs/r/%s" % key(host, path)
        git("fetch", "-q", "--no-tags", "--force", str(work), "+refs/heads/*:%s/heads/*" % ns,
            "+refs/tags/*:%s/tags/*" % ns, cwd=m)
        git("config", "nanosoc-rm.%s.defaultbranch" % key(host, path), default, cwd=m)
    return m


CHANGELOG_010 = """# Changelog

## [Unreleased]

## [0.1.0] - 2026-09-02
### feature
- First release of the flist layout.

[Unreleased]: https://github.com/SoC-Labs/nanoSoC-Arch-Tech/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SoC-Labs/nanoSoC-Arch-Tech/releases/tag/v0.1.0
"""


def unreleased(entries: dict[str, list[str]], base: str = CHANGELOG_010) -> str:
    body = "".join("### %s\n" % c + "".join("- %s\n" % e for e in es) for c, es in entries.items())
    return base.replace("## [Unreleased]\n", "## [Unreleased]\n" + body, 1)


def arch_site(tmp_path: Path, monkeypatch, *, entries=None, api_after=None, gen_entries=None, extra_config=""):
    """nanosoc_arch_tech (re-exports nanosoc_gen) released as v0.1.0, then changed; nanosoc_gen moved under it.
    Returns a dict of paths and shas."""
    cfg, data, registry = write_config(tmp_path, monkeypatch, extra_config)
    gen = work_repo(tmp_path / "work" / "gen", {"README.md": "gen\n", "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n"},
                    date="2026-08-01T10:00:00Z")
    gen_a = git("rev-parse", "HEAD", cwd=gen)
    gen_cl = "# Changelog\n\n## [Unreleased]\n" + "".join(
        "### %s\n" % c + "".join("- %s\n" % e for e in es) for c, es in (gen_entries or {"fix": ["Generator fix."]}).items())
    gen_b = commit(gen, {"CHANGELOG.md": gen_cl}, "2026-09-10T10:00:00Z", "gen change")
    arch = work_repo(tmp_path / "work" / "arch", {
        "README.md": "arch\n", "CHANGELOG.md": CHANGELOG_010,
        "API.md": "# API\n\n- port HSEL_O\n- flist layout v1\n",
        "nanosoc-component.yaml": MANIFEST}, date="2026-09-01T10:00:00Z")
    base = gitlink(arch, "nanosoc_gen", gen_a, "https://gitlab.example.invalid/soclabs/nanosoc_gen.git",
                   "2026-09-02T09:00:00Z")
    tag(arch, "v0.1.0", date="2026-09-02T10:00:00Z")
    tag(arch, "integ-step1", annotated=False, date="2026-09-02T10:00:00Z")
    files = {"CHANGELOG.md": unreleased(entries if entries is not None else {"fix": ["Fix the flist order."]})}
    if api_after is not None:
        files["API.md"] = api_after
    commit(arch, files, "2026-09-11T10:00:00Z", "fix: flist order")
    head = gitlink(arch, "nanosoc_gen", gen_b, "https://gitlab.example.invalid/soclabs/nanosoc_gen.git",
                   "2026-09-12T10:00:00Z", "bump nanosoc_gen")
    mirror_from(data, "nanosoc_gen", {GEN: gen})
    tag(gen, "v0.1.0", gen_a, date="2026-08-02T10:00:00Z")
    mirror_from(data, "nanosoc_gen", {GEN: gen})
    m = mirror_from(data, "nanosoc_arch_tech", {ARCH: arch, ARCH_COPY: arch})
    return {"cfg": cfg, "data": data, "registry": registry, "arch": arch, "gen": gen, "mirror": m, "base": base,
            "head": head, "gen_a": gen_a, "gen_b": gen_b}


MANIFEST = """schema: 1
id: nanosoc_arch_tech
class: arch_tech
version: "0.1.0"
canonical: https://github.com/SoC-Labs/nanoSoC-Arch-Tech.git
provides:
  - {name: nanosoc-flist-layout, version: 1}
requires:
  - {id: nanosoc_gen, path: nanosoc_gen, range: "^0.1"}
reexports: [nanosoc_gen]
variants:
  sim: {flist: "make flist-sim"}
tests:
  t0: {run: "make -C ci t0", control: "make -C ci t0-control"}
"""
