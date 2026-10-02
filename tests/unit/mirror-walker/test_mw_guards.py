"""Guards added after the independent review of mirror-walker (findings MW-R1 to MW-R5):

- R1  a configuration whose root commit is not in its mirror is "could not answer" (exit 2, nothing stored), never
      an empty, green walk; a root commit with no .gitmodules is a real, empty walk (exit 0);
- R2  BRANCH_ONLY on the pinned host is red in a frozen configuration and not red otherwise (section 4 CLI);
- R3  the inventory never follows a GitHub `Link: rel=next` off the configured API base (section 0.3);
- R4  a pinned repo whose mirror was never synced is missing data (exit 2), unless the host itself answers
      401/403/404 for the repo (README section 5 UNREADABLE);
- R5  the API halves of UNREADABLE: the host answers 401/403/404 for the repo, or 401/403 for the commit.
"""
from __future__ import annotations

import json
import re

import pytest
from mw_fixtures import load_at

from nanosoc_rm import db

GONE = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"           # a commit no host has


@pytest.fixture(scope="session")
def at_pins():
    return load_at("test_pins")


@pytest.fixture(scope="session")
def at_web():
    return load_at("test_scope_web")


@pytest.fixture
def hermetic(at_pins, tmp_path):
    """LocalHosts whose FakeApi can be told to deny chosen GETs: `hosts.deny` is [(path regex, status)]."""
    class Denying(at_pins.LocalHosts):
        def api_answer(self, raw_path):
            path = raw_path.split("?", 1)[0]
            for pattern, status in self.deny:
                if re.fullmatch(pattern, path):
                    return status, {"message": "denied by the fixture"}
            return super().api_answer(raw_path)

    hosts = Denying(tmp_path / "hermetic")
    hosts.deny = []
    yield hosts
    hosts.api.close()


def _ip(cid, host, path):
    return {"id": cid, "class": "ip_tech", "canonical": {"host": host, "path": path}}


def _snapshots(scope):
    with db.connect() as con:
        return con.execute("SELECT count(*) FROM snapshots WHERE scope = ?", (scope,)).fetchone()[0]


# ---------------------------------------------------------------------------------------------------- R1
def _root_fixture(at_pins, h):
    """top main = base (no .gitmodules). F (a child of base, pinning deps/dep) exists only in a developer's repo."""
    gh, gl = "github", "gitlab"
    dep = at_pins._chain(h, gl, "soclabs/dep", 1)
    base = h.commit(gh, "SoC-Labs/top", files={"README": "base\n"})
    h.push(gh, "SoC-Labs/top", base, "main")
    f = h.commit(gh, "SoC-Labs/top", parents=[base], gitlinks={"deps/dep": dep[0]},
                 gitmodules=[{"path": "deps/dep", "url": h.url(gl, "soclabs/dep")}])
    catalogue = {"schema": 1, "components": [
        {"id": "top", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/top"}},
        _ip("dep", gl, "soclabs/dep")]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "top-f", "component": "top", "ref": {"branch": "main"}, "commit": f, "frozen": True},
        {"id": "top-base", "component": "top", "ref": {"branch": "main"}, "commit": base, "frozen": True}]}
    return at_pins.write_registry(h.root / "registry-root", catalogue, eligibility, examples=False), f


def test_root_never_synced_is_could_not_answer(at_pins, hermetic):
    registry, _f = _root_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "root-unsynced")
    with env.active():
        with pytest.raises(at_pins.rm("walker.WalkError"), match="no mirror"):
            at_pins.rm("walker.walk")("top-f")
    for cmd in ("walk", "pins"):
        proc = env.cli(cmd, "top-f", "--json")
        assert proc.returncode == 2 and proc.stdout == "", (cmd, proc.returncode, proc.stdout, proc.stderr)
    with env.active():
        assert _snapshots("pin_state:top-f") == 0


def test_frozen_commit_absent_from_mirror_is_could_not_answer(at_pins, hermetic):
    registry, f = _root_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "root-absent")
    with env.active():
        for cid in ("top", "dep"):
            at_pins.rm("mirror.sync_component")(cid)
        with pytest.raises(at_pins.rm("walker.WalkError"), match=f[:12]):
            at_pins.rm("walker.walk")("top-f")
        # a root commit with no .gitmodules is a real answer: no pins, one empty snapshot, exit 0
        assert at_pins.rm("walker.walk")("top-base") == []
        assert _snapshots("pin_state:top-base") == 1
    for cmd in ("walk", "pins"):
        proc = env.cli(cmd, "top-f", "--json")
        assert proc.returncode == 2 and proc.stdout == "" and f[:12] in proc.stderr, (cmd, proc.stderr)
    assert env.cli("walk", "top-base", "--json").returncode == 0
    with env.active():
        assert _snapshots("pin_state:top-f") == 0
        # once F is on the host and synced, the same configuration walks: one pin, green
        hermetic.push("github", "SoC-Labs/top", f, "main")
        at_pins.rm("mirror.sync_component")("top")
        docs = at_pins.rm("walker.walk")("top-f")
    assert [d["fact"]["hosts"]["gitlab"]["state"] for d in docs] == ["ON_DEFAULT"]
    assert env.cli("pins", "top-f", "--json").returncode == 0


# ---------------------------------------------------------------------------------------------------- R2
def test_branch_only_is_red_only_in_a_frozen_configuration(at_pins, hermetic):
    gh, gl = "github", "gitlab"
    b = at_pins._chain(hermetic, gl, "soclabs/feat", 2)
    y = hermetic.commit(gl, "soclabs/feat", parents=[b[-1]], files={"n": "feature\n"})
    hermetic.push(gl, "soclabs/feat", y, "feat/y")
    top = hermetic.commit(gh, "SoC-Labs/top", gitlinks={"deps/feat": y},
                          gitmodules=[{"path": "deps/feat", "url": hermetic.url(gl, "soclabs/feat")}])
    hermetic.push(gh, "SoC-Labs/top", top, "main")
    catalogue = {"schema": 1, "components": [
        {"id": "top", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/top"}},
        _ip("feat", gl, "soclabs/feat")]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "top-frozen", "component": "top", "ref": {"branch": "main"}, "commit": top, "frozen": True},
        {"id": "top-main", "component": "top", "ref": {"branch": "main"}}]}
    registry = at_pins.write_registry(hermetic.root / "registry-bo", catalogue, eligibility, examples=False)
    env = hermetic.tool_env(registry, "branch-only")
    with env.active():
        for cid in ("top", "feat"):
            at_pins.rm("mirror.sync_component")(cid)
        for cid in ("top-frozen", "top-main"):
            docs = at_pins.rm("walker.walk")(cid)
            # the only pin, and it is BRANCH_ONLY on its pinned host: nothing else can decide the exit code
            assert [(d["fact"]["state"], {k: (h["state"], h["pinned"]) for k, h in d["fact"]["hosts"].items()})
                    for d in docs] == [(None, {"gitlab": ("BRANCH_ONLY", True)})], cid
    for cmd in ("walk", "pins"):
        assert env.cli(cmd, "top-frozen", "--json").returncode == 1, cmd
        assert env.cli(cmd, "top-main", "--json").returncode == 0, cmd


# ---------------------------------------------------------------------------------------------------- R3
def test_inventory_refuses_a_next_link_off_the_api_base(at_web, tmp_path):
    gh, gl, evil = at_web.FakeHost(), at_web.FakeHost(), at_web.FakeHost()
    try:
        def page(m, q, hd, b):
            return 200, {"Link": '<%s/collect?page=2>; rel="next"' % evil.url}, \
                [{"full_name": "SoC-Labs/r0", "private": False, "default_branch": "main"}]
        gh.route("GET", r"/orgs/SoC-Labs/repos", page)
        gl.route("GET", r"/api/v4/groups/soclabs/projects", lambda m, q, hd, b: (200, {}, []))
        evil.route("GET", r"/collect", lambda m, q, hd, b: (200, {}, []))
        cat = {"schema": 1, "components": [{"id": "r0", "class": "soc_ref",
                                            "canonical": {"host": "github", "path": "SoC-Labs/r0"}}]}
        registry = at_web.write_registry(tmp_path / "reg", catalogue=cat, eligibility={
            "schema": 1, "configurations": [{"id": "x", "component": "r0", "ref": {"branch": "main"}}]})
        cfg = at_web.write_config(tmp_path / "c.toml", data=tmp_path / "data", registry=registry, github=gh, gitlab=gl)
        rc, docs, p = at_web.cli(cfg, "inventory", "--json")
        assert evil.log == [], evil.log                         # the token never left the configured API
        assert rc == 2 and docs == [] and "next" in p.stderr, (rc, p.stderr)
        assert len(gh.log) == 1
    finally:
        for h in (gh, gl, evil):
            h.close()


# ---------------------------------------------------------------------------------------------------- R4, R5
def _deep_fixture(at_pins, h):
    """top pins `deep` (on the host, never synced), `gone` (catalogued, no such repo on the host, never synced),
    and one pin per API answer of UNREADABLE (R5) plus a MISSING control, each at a commit no host has."""
    gh, gl = "github", "gitlab"
    at_pins._chain(h, gl, "soclabs/deep", 1)
    repos = {"deep": (gl, "soclabs/deep"), "gone": (gl, "soclabs/gone"),
             "r404": (gl, "soclabs/r404"), "r403": (gh, "SoC-Labs/r403"), "c403": (gl, "soclabs/c403"),
             "c401": (gh, "SoC-Labs/c401"), "miss": (gl, "soclabs/miss")}
    for cid in ("r404", "r403", "c403", "c401", "miss"):
        at_pins._chain(h, *repos[cid], 1)
    deep_sha = h.git("--git-dir", h.bare(gl, "soclabs/deep"), "rev-parse", "main")
    pins = {cid: (deep_sha if cid == "deep" else GONE) for cid in repos}
    top = h.commit(gh, "SoC-Labs/top", gitlinks=pins,
                   gitmodules=[{"path": cid, "url": h.url(*repos[cid])} for cid in repos])
    h.push(gh, "SoC-Labs/top", top, "main")
    catalogue = {"schema": 1, "components": [
        {"id": "top", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/top"}}]
        + [_ip(cid, *repos[cid]) for cid in repos]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "top-f", "component": "top", "ref": {"branch": "main"}, "commit": top, "frozen": True}]}
    return at_pins.write_registry(h.root / "registry-deep", catalogue, eligibility, examples=False)


def test_never_synced_mirror_is_missing_data_not_unreadable(at_pins, hermetic):
    registry = _deep_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "deep")
    with env.active():
        for cid in ("top", "r404", "r403", "c403", "c401", "miss"):         # not deep, not gone
            at_pins.rm("mirror.sync_component")(cid)
        with pytest.raises(at_pins.rm("walker.WalkError"), match="deep"):
            at_pins.rm("walker.walk")("top-f")
        assert _snapshots("pin_state:top-f") == 0
    proc = env.cli("pins", "top-f", "--json")
    assert proc.returncode == 2 and "never been synced" in proc.stderr, proc.stderr
    with env.active():
        at_pins.rm("mirror.sync_component")("deep")
        docs = {d["fact"]["path"]: d["fact"] for d in at_pins.rm("walker.walk")("top-f")}
    assert docs["deep"]["hosts"]["gitlab"]["state"] == "ON_DEFAULT"
    # `gone` was never synced either, but the host answers 404 for the repo itself: README section 5 UNREADABLE
    assert docs["gone"]["hosts"]["gitlab"]["state"] == "UNREADABLE"
    assert any("projects/soclabs%2Fgone" in s["command"] for s in docs["gone"]["hosts"]["gitlab"]["sources"])


def test_unreadable_from_the_host_api(at_pins, hermetic):
    registry = _deep_fixture(at_pins, hermetic)
    hermetic.deny = [(r"/gl/api/v4/projects/soclabs%2Fr404", 404),
                     (r"/gh/repos/SoC-Labs/r403", 403),
                     (r"/gl/api/v4/projects/soclabs%2Fc403/repository/commits/[0-9a-f]+", 403),
                     (r"/gh/repos/SoC-Labs/c401/commits/[0-9a-f]+", 401)]
    env = hermetic.tool_env(registry, "api")
    with env.active():
        for cid in ("top", "deep", "r404", "r403", "c403", "c401", "miss"):
            at_pins.rm("mirror.sync_component")(cid)
        docs = {d["fact"]["path"]: d["fact"] for d in at_pins.rm("walker.walk")("top-f")}
    states = {p: next(iter(f["hosts"].values()))["state"] for p, f in docs.items()}
    assert states == {"deep": "ON_DEFAULT", "gone": "UNREADABLE", "r404": "UNREADABLE", "r403": "UNREADABLE",
                      "c403": "UNREADABLE", "c401": "UNREADABLE", "miss": "MISSING"}, states
    # each was decided by the API, after the mirror said "held by no ref of this host"
    for p in ("r404", "r403", "c403", "c401", "miss"):
        cmds = [s["command"] for s in next(iter(docs[p]["hosts"].values()))["sources"]]
        assert any(c.startswith("GET ") for c in cmds), (p, cmds)
    assert env.cli("pins", "top-f", "--json").returncode == 1
    out = env.cli("pins", "top-f", "--state", "UNREADABLE", "--json")
    assert sorted(json.loads(x)["fact"]["path"] for x in out.stdout.splitlines()) == \
        ["c401", "c403", "gone", "r403", "r404"]
