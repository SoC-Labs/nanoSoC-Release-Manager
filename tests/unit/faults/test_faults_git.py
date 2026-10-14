"""Failure injection on git: a host that is down, a fetch that times out, objects missing from a mirror (a corrupt
pack, a partial fetch), a promisor that cannot be reached for a lazy blob, unicode and very long submodule paths.
The rules under test: a mirror that could not be read is `ok: false` with a cause and exit 2; a walk that could not
read its tree is WalkError (exit 2) and stores nothing; nothing reads as fewer pins, or as PASS.

Hermetic: the acceptance file's LocalHosts (bare repos behind url.insteadOf) as the mirror-walker tests use them.

Robustness angle: faults. A test that exposes a defect is a strict xfail named ROB-faults-<n>."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import faults_fix  # sys.path for the other unit-test directories

assert faults_fix.UNIT.is_dir()
from mw_fixtures import load_at
from nanosoc_rm import db, gitrun
from nanosoc_rm.gitrun import GitResult
from nanosoc_rm.mirror import private_read
from nanosoc_rm.walker import WalkError

GH, GL = "github", "gitlab"


@pytest.fixture(scope="session")
def at_pins():
    return load_at("test_pins")


@pytest.fixture
def h(at_pins, tmp_path):
    hosts = at_pins.LocalHosts(tmp_path / "hermetic")
    yield hosts
    hosts.api.close()


def build(at_pins, h, *, leaf_path="deps/leaf", mid_path="a"):
    """top (github) pins mid at `mid_path`; mid (gitlab) pins leaf at `leaf_path`. Three components, two pins."""
    leaf = at_pins._chain(h, GL, "soclabs/leaf", 1)[0]
    mid = h.commit(GL, "soclabs/mid", gitlinks={leaf_path: leaf},
                   gitmodules=[{"path": leaf_path, "url": h.url(GL, "soclabs/leaf")}])
    h.push(GL, "soclabs/mid", mid, "main")
    top = h.commit(GH, "SoC-Labs/top", files={"README": "top\n"}, gitlinks={mid_path: mid},
                   gitmodules=[{"path": mid_path, "url": h.url(GL, "soclabs/mid")}])
    h.push(GH, "SoC-Labs/top", top, "main")
    catalogue = {"schema": 1, "components": [
        {"id": "top", "class": "soc_ref", "canonical": {"host": GH, "path": "SoC-Labs/top"}},
        {"id": "mid", "class": "ip_tech", "canonical": {"host": GL, "path": "soclabs/mid"}},
        {"id": "leaf", "class": "ip_tech", "canonical": {"host": GL, "path": "soclabs/leaf"}}]}
    eligibility = {"schema": 1, "configurations": [{"id": "top-main", "component": "top", "ref": {"branch": "main"}}]}
    registry = at_pins.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
    env = h.tool_env(registry, "faults")
    with env.active():
        for cid in ("top", "mid", "leaf"):
            at_pins.rm("mirror.sync_component")(cid)
    return {"env": env, "top": top, "mid": mid, "leaf": leaf}


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env, capture_output=True, text=True, check=True
                          ).stdout.strip()


def explode_and_drop(mirror: Path, sha: str, h, host_repo: Path) -> None:
    """Unpack every pack of a mirror into loose objects, then delete one object: what a corrupt or truncated pack
    leaves behind. The host repo is removed too (the host is down), so git cannot repair the hole with a lazy
    fetch from the promisor remote."""
    env = h.env
    packs = sorted((mirror / "objects" / "pack").glob("*.pack"))
    for pack in packs:
        moved = pack.with_suffix(".unpacking")
        pack.rename(moved)
        for ext in (".idx", ".rev", ".promisor", ".bitmap"):
            pack.with_suffix(ext).unlink(missing_ok=True)
        subprocess.run(["git", "--git-dir", str(mirror), "unpack-objects", "-q"], stdin=open(moved, "rb"),
                       env=env, check=True)
        moved.unlink()
    loose = mirror / "objects" / sha[:2] / sha[2:]
    assert loose.is_file(), "object %s is not in %s" % (sha, mirror)
    os.chmod(loose, 0o644)
    loose.unlink()
    shutil.rmtree(host_repo)


def walk_rows(env):
    with env.active():
        return {d["fact"]["tree_paths"][0] for d in db.latest("pin_state", "pin_state:top-main:")}


# ----- the walk over a damaged mirror ------------------------------------------------------------------------------
def test_the_healthy_tree_walks_two_pins(at_pins, h):
    """The control for every damaged-mirror test below: same scene, nothing broken."""
    fx = build(at_pins, h)
    with fx["env"].active():
        docs = at_pins.rm("walker.walk")("top-main")
    assert sorted(d["fact"]["tree_paths"][0] for d in docs) == ["a", "a/deps/leaf"]


@pytest.mark.xfail(strict=True, reason="ROB-faults-9: a root commit whose tree object is missing (corrupt pack) walks "
                                       "as a tree with NO pins: walker._read_node returns silently when "
                                       "`ls-tree <c> -- .gitmodules` fails, and the empty walk is stored as current")
def test_a_root_tree_missing_from_the_mirror_is_walkerror_not_no_pins(at_pins, h):
    fx = build(at_pins, h)
    env = fx["env"]
    with env.active():
        at_pins.rm("walker.walk")("top-main")                     # a good walk is stored first
    tree = git("--git-dir", env.mirror("top"), "rev-parse", fx["top"] + "^{tree}", env=h.env)
    explode_and_drop(env.mirror("top"), tree, h, h.bare(GH, "SoC-Labs/top"))
    with env.active():
        with pytest.raises(WalkError):
            at_pins.rm("walker.walk")("top-main")
    assert walk_rows(env) == {"a", "a/deps/leaf"}                 # the last good walk is still the current one


@pytest.mark.xfail(strict=True, reason="ROB-faults-9: a child commit whose tree object is missing (corrupt pack in "
                                       "its mirror) silently drops every pin below it; the walk is stored complete")
def test_a_child_tree_missing_from_its_mirror_never_drops_the_pins_below_it(at_pins, h):
    fx = build(at_pins, h)
    env = fx["env"]
    tree = git("--git-dir", env.mirror("mid"), "rev-parse", fx["mid"] + "^{tree}", env=h.env)
    explode_and_drop(env.mirror("mid"), tree, h, h.bare(GL, "soclabs/mid"))
    with env.active():
        try:
            docs = at_pins.rm("walker.walk")("top-main")
        except WalkError:
            return                                                # an honest "could not answer" is fine
    assert sorted(d["fact"]["tree_paths"][0] for d in docs) == ["a", "a/deps/leaf"], [
        d["fact"]["tree_paths"] for d in docs]


def test_a_gitmodules_blob_with_its_host_down_is_walkerror_and_stores_nothing(at_pins, h):
    """The mirror is blobless (filter=blob:none): .gitmodules arrives by lazy fetch. With the host gone that fetch
    fails, and the walk must say so (exit 2) instead of reading the tree as having no submodules."""
    fx = build(at_pins, h)
    env = fx["env"]
    blob = git("--git-dir", env.mirror("top"), "rev-parse", fx["top"] + ":.gitmodules", env=h.env)
    have = subprocess.run(["git", "--git-dir", str(env.mirror("top")), "cat-file", "-e", blob],
                          env={**h.env, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True)
    if have.returncode == 0:
        pytest.skip("this git fetched the .gitmodules blob eagerly; nothing is lazy to break")
    shutil.rmtree(h.hosts_dir / GH)                               # the host is down
    proc = env.cli("walk", "top-main", "--json")
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "gitmodules" in proc.stderr
    assert walk_rows(env) == set()                                # nothing stored: no empty "no pins" snapshot


# ----- mirror sync with the host down, timing out, or refusing -----------------------------------------------------
def test_a_host_that_vanished_is_ok_false_keeps_the_last_success_and_exits_2(at_pins, h):
    fx = build(at_pins, h)
    env = fx["env"]
    with env.active():
        before = {d["subject"]: d["fact"] for d in db.latest("mirror_status")}
    shutil.rmtree(h.hosts_dir / GL)
    proc = env.cli("mirror", "sync", "--component", "mid", "--json")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    with env.active():
        st = {d["subject"]: d["fact"] for d in db.latest("mirror_status")}
        alerts = {a["subject"] for a in db.alerts()}
    f = st["mirror_status:gitlab:soclabs/mid"]
    assert f["ok"] is False and f["access"] == "unreadable" and f["error"]
    assert f["last_success_at"] == before["mirror_status:gitlab:soclabs/mid"]["last_success_at"]
    assert "alert:mirror_failed:mirror_status:gitlab:soclabs/mid" in alerts
    with env.active():          # the old refs are kept; a blob not fetched yet makes the walk say so, never "no pins"
        try:
            got = {d["fact"]["tree_paths"][0] for d in at_pins.rm("walker.walk")("top-main")}
        except WalkError:
            got = None
    assert got in (None, {"a", "a/deps/leaf"}), got


def test_a_fetch_that_times_out_is_cause_timeout_and_never_ok(at_pins, h, monkeypatch):
    fx = build(at_pins, h)
    from nanosoc_rm.mirror import sync as S
    real = S.run_in

    def slow(mirror, *args, env=None, timeout=300):
        if args and args[0] in ("fetch", "ls-remote"):
            return GitResult(("git", "-C", str(mirror), *args), -1, "", "\ntimed out after %ss" % timeout,
                             timed_out=True)
        return real(mirror, *args, env=env, timeout=timeout)
    monkeypatch.setattr(S, "run_in", slow)
    with fx["env"].active():
        docs = S.sync_component("leaf")
    f = docs[0]["fact"]
    assert f["ok"] is False and f["cause"] == "timeout" and f["access"] == "unreadable", f


def test_run_git_reports_a_real_hang_as_timed_out(tmp_path):
    """A transport that never answers (a black-holed host): run_git returns timed_out, never hangs or raises."""
    hang = tmp_path / "hang.sh"
    hang.write_text("#!/bin/sh\nexec sleep 3\n")
    hang.chmod(0o755)
    r = gitrun.run_git("ls-remote", "ssh://example.invalid/x.git", env={"GIT_SSH_COMMAND": str(hang)}, timeout=0.5)
    assert r.timed_out and not r.ok and r.returncode == -1
    assert private_read.classify(r.stderr) == "timeout"


# Realistic stderr from git 2.4x for each failure; the cause decides the alert group and whether private_read is
# tried (RETRY_CAUSES: auth, not-found).
NETWORK_STDERR = {
    "https dns": "fatal: unable to access 'https://github.com/SoC-Labs/x.git/': Could not resolve host: github.com",
    "https refused": "fatal: unable to access 'https://github.com/SoC-Labs/x.git/': Failed to connect to github.com "
                     "port 443 after 3 ms: Couldn't connect to server",
}
SSH_NETWORK_STDERR = {
    "ssh dns": "ssh: Could not resolve hostname github.com: Name or service not known\r\nfatal: Could not read from "
               "remote repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.",
    "ssh refused": "ssh: connect to host github.com port 22: Connection refused\r\nfatal: Could not read from remote "
                   "repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.",
    "ssh unreachable": "ssh: connect to host github.com port 22: Network is unreachable\r\nfatal: Could not read from "
                       "remote repository.",
}


@pytest.mark.parametrize("name", sorted(NETWORK_STDERR))
def test_an_https_network_failure_is_cause_network(name):
    assert private_read.classify(NETWORK_STDERR[name]) == "network"


@pytest.mark.xfail(strict=True, reason="ROB-faults-10: an ssh fetch that fails for a network reason (DNS, refused, "
                                       "unreachable) is classified not-found, because git's trailing 'Could not read "
                                       "from remote repository' matches the not-found pattern before the network one; "
                                       "the alert then reads 'private or missing' during an outage")
@pytest.mark.parametrize("name", sorted(SSH_NETWORK_STDERR))
def test_an_ssh_network_failure_is_cause_network_not_not_found(name):
    assert private_read.classify(SSH_NETWORK_STDERR[name]) == "network"


# ----- unicode and very long paths ----------------------------------------------------------------------------------
@pytest.mark.parametrize("leaf_path", ["deps/lëaf ünï", "deps/" + "x" * 200 + "/leaf", "deps/日本/leaf"])
def test_unicode_and_long_submodule_paths_walk_exactly(at_pins, h, leaf_path):
    fx = build(at_pins, h, leaf_path=leaf_path)
    with fx["env"].active():
        docs = at_pins.rm("walker.walk")("top-main")
    by = {d["fact"]["tree_paths"][0]: d["fact"] for d in docs}
    assert sorted(by) == sorted(["a", "a/" + leaf_path]), sorted(by)
    leaf = by["a/" + leaf_path]
    assert leaf["entry_type"] == "commit" and leaf["state"] is None
    assert leaf["hosts"][GL]["state"] not in (None, "UNREADABLE"), leaf["hosts"]
