"""Mirror sync, status, rewrite detection and inventory listing (interfaces.md sections 3 and 8; H6, B8, A1)."""
from __future__ import annotations

import json

import pytest
from mw_fixtures import load_at

from nanosoc_rm import db
from nanosoc_rm.mirror.layout import default_branch, repo_key


@pytest.fixture(scope="session")
def at_pins():
    return load_at("test_pins")


@pytest.fixture(scope="session")
def at_web():
    return load_at("test_scope_web")


@pytest.fixture
def hermetic(at_pins, tmp_path):
    hosts = at_pins.LocalHosts(tmp_path / "hermetic")
    yield hosts
    hosts.api.close()


def _two_host_fixture(at_pins, h):
    """Component `dual`: GitHub canonical with a tag, a PR ref and a second branch; GitLab copy one commit behind."""
    shas = at_pins._chain(h, "github", "SoC-Labs/dual", 3)
    h.tag("github", "SoC-Labs/dual", "v0.1", shas[0])
    side = h.commit("github", "SoC-Labs/dual", parents=[shas[1]], files={"n": "side\n"})
    h.push("github", "SoC-Labs/dual", side, "feat/side")
    pr = h.commit("github", "SoC-Labs/dual", parents=[shas[2]], files={"n": "pr\n"})
    h.push("github", "SoC-Labs/dual", pr, "refs/pull/3/head")
    h.push("github", "SoC-Labs/dual", shas[1], "main", to_host="gitlab", to_path="soclabs/dual")
    catalogue = {"schema": 1, "components": [
        {"id": "dual", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/dual"},
         "copies": [{"host": "gitlab", "path": "soclabs/dual"}]}]}
    eligibility = {"schema": 1, "configurations": [{"id": "dual-main", "component": "dual", "ref": {"branch": "main"}}]}
    return at_pins.write_registry(h.root / "registry-dual", catalogue, eligibility, examples=False), shas, pr


def test_sync_layout_heads_and_tags_only(at_pins, at_web, hermetic):
    registry, shas, pr = _two_host_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "layout")
    with env.active():
        docs = at_pins.rm("mirror.sync_component")("dual")
    assert [d["fact"]["ok"] for d in docs] == [True, True]
    mirror = env.mirror("dual")
    assert at_web.check_h6_refs(mirror) == []
    refs = at_pins.git("--git-dir", mirror, "for-each-ref", "--format=%(refname)").split()
    gh = at_pins.ref_prefix("github", "SoC-Labs/dual")
    gl = at_pins.ref_prefix("gitlab", "soclabs/dual")
    assert sorted(refs) == sorted([gh + "/heads/main", gh + "/heads/feat/side", gh + "/tags/v0.1", gl + "/heads/main"])
    assert at_pins.git("--git-dir", mirror, "cat-file", "-t", pr, check=False) != "commit"   # PR-only: never fetched
    for key, value in (("core.logAllRefUpdates", "always"), ("gc.reflogExpire", "never"),
                       ("gc.reflogExpireUnreachable", "never"), ("gc.pruneExpire", "never")):
        assert at_pins.git("--git-dir", mirror, "config", "--get", key) == value
    assert default_branch(mirror, repo_key("github", "SoC-Labs/dual")) == "main"
    fetch = [s["command"] for d in docs for s in d["sources"] if " fetch " in s["command"]]
    assert all("--no-tags" in c and "--filter=blob:none" in c and "--mirror" not in c for c in fetch) and len(fetch) == 2
    # the status table prints an age in seconds per repo
    status = env.cli("mirror", "status")
    assert status.returncode == 0 and status.stdout.splitlines()[0].split()[:5] == ["host", "path", "component", "ok", "age"]


def test_failed_fetch_keeps_refs_and_alerts_once(at_pins, hermetic):
    registry, shas, _pr = _two_host_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "failing")
    with env.active():
        sync = at_pins.rm("mirror.sync_component")
        first = {d["subject"]: d["fact"] for d in sync("dual")}
        gone = hermetic.bare("gitlab", "soclabs/dual")
        gone.rename(gone.with_name("dual-moved.git"))
        for _ in range(2):
            second = {d["subject"]: d["fact"] for d in sync("dual")}
        subject = "mirror_status:gitlab:soclabs/dual"
        assert second[subject]["ok"] is False and second[subject]["error"]
        assert second[subject]["last_success_at"] == first[subject]["last_success_at"]
        assert second["mirror_status:github:SoC-Labs/dual"]["ok"] is True
        refs = at_pins.git("--git-dir", env.mirror("dual"), "for-each-ref", "--format=%(refname)",
                           at_pins.ref_prefix("gitlab", "soclabs/dual"))
        assert refs.split() == [at_pins.ref_prefix("gitlab", "soclabs/dual") + "/heads/main"]   # old refs stay
        failed = [a for a in db.alerts() if a["fact"]["alert_kind"] == "mirror_failed"]
        assert [a["fact"]["about"] for a in failed] == [subject]                                 # once, not per poll
    assert env.cli("mirror", "status", "--json").returncode == 2
    assert env.cli("mirror", "sync", "--component", "dual", "--host", "github", "--json").returncode == 0
    assert env.cli("mirror", "sync", "--component", "nope").returncode == 3
    # a host-restricted sync keeps the other host's row in the snapshot
    proc = env.cli("mirror", "status", "--json")
    assert sorted(json.loads(x)["subject"] for x in proc.stdout.splitlines()) == \
        ["mirror_status:github:SoC-Labs/dual", "mirror_status:gitlab:soclabs/dual"]


def test_rewrite_detector_unit(at_pins, hermetic):
    registry, shas, _pr = _two_host_fixture(at_pins, hermetic)
    env = hermetic.tool_env(registry, "rewrite")
    with env.active():
        sync = at_pins.rm("mirror.sync_component")
        sync("dual")
        detect = at_pins.rm("mirror.detect_rewrite")
        key = repo_key("github", "SoC-Labs/dual")
        assert detect(env.mirror("dual"), key, "main", shas[0], shas[2]) is None        # a descendant
        assert detect(env.mirror("dual"), key, "main", shas[2], shas[2]) is None
        # The default branch moves to another branch NAME with unrelated history: not a rewrite (B8 keys on the
        # branch name). Then a real force-push of main: one alert, 1 commit lost (shas[2]).
        other = hermetic.commit("github", "SoC-Labs/dual", files={"n": "unrelated\n"})
        hermetic.push("github", "SoC-Labs/dual", other, "trunk")
        hermetic.git("--git-dir", hermetic.bare("github", "SoC-Labs/dual"), "symbolic-ref", "HEAD", "refs/heads/trunk")
        quiet = sync("dual")
        assert [d for d in quiet if d["kind"] == "rewrite_alert"] == []
        assert default_branch(env.mirror("dual"), key) == "trunk"
        hermetic.git("--git-dir", hermetic.bare("github", "SoC-Labs/dual"), "symbolic-ref", "HEAD", "refs/heads/main")
        sync("dual")
        hermetic.push("github", "SoC-Labs/dual", shas[1], "main", force=True)
        loud = [d for d in sync("dual") if d["kind"] == "rewrite_alert"]
        assert len(loud) == 1 and loud[0]["fact"]["lost_commits"] == 1 and loud[0]["fact"]["old_head"] == shas[2]
        assert loud[0]["fact"]["pinned_lost"] == []
        assert [a["fact"]["alert_kind"] for a in db.alerts()] == ["rewrite"]
    assert env.cli("mirror", "sync", "--json").returncode == 0      # quiet now


def test_inventory_follows_absolute_github_links_and_fails_whole(at_web, tmp_path):
    gh, gl = at_web.FakeHost(), at_web.FakeHost()
    try:
        items = [{"full_name": "SoC-Labs/r%d" % i, "private": i % 2 == 0, "default_branch": "main"} for i in range(5)]

        def page(m, q, hd, b):
            n = int((q.get("page") or ["1"])[0])
            chunk = items[(n - 1) * 2: n * 2]
            headers = {"Link": '<%s/orgs/SoC-Labs/repos?page=%d&per_page=2>; rel="next", <x>; rel="last"' % (gh.url, n + 1)} \
                if n * 2 < len(items) else {}
            return 200, headers, chunk
        gh.route("GET", r"/orgs/SoC-Labs/repos", page)
        gl.route("GET", r"/api/v4/groups/soclabs/projects", lambda m, q, hd, b: (500, {}, {"message": "boom"}))
        cat = {"schema": 1, "components": [{"id": "r1", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/r1"}}]}
        registry = at_web.write_registry(tmp_path / "reg", catalogue=cat,
                                         eligibility={"schema": 1, "configurations": [
                                             {"id": "x", "component": "r1", "ref": {"branch": "main"}}]})
        cfg = at_web.write_config(tmp_path / "c.toml", data=tmp_path / "data", registry=registry, github=gh, gitlab=gl)
        rc, docs, p = at_web.cli(cfg, "inventory", "--json")
        assert rc == 2 and "gitlab listing failed" in p.stderr and docs == []
        assert at_web.sqlite_rows(tmp_path / "data" / "rm.sqlite", "SELECT count(*) AS n FROM observations")[0]["n"] == 0 \
            if (tmp_path / "data" / "rm.sqlite").exists() else True
        gl.route("GET", r"/api/v4/groups/soclabs/projects", lambda m, q, hd, b: (200, {}, []))
        rc, docs, p = at_web.cli(cfg, "inventory", "--json")
        assert rc == 1, p.stderr                       # 4 of the 5 repos are not in the catalogue
        rows = {d["fact"]["repo"]["path"]: d["fact"] for d in docs}
        assert sorted(rows) == ["SoC-Labs/r%d" % i for i in range(5)]
        assert rows["SoC-Labs/r1"]["role"] == "canonical" and rows["SoC-Labs/r0"]["visibility"] == "private"
        assert {e["query"].get("page", ["1"])[0] for e in gh.log} == {"1", "2", "3"}
        assert all(e["headers"].get("Authorization") == "Bearer fake-github_read" for e in gh.log)
        assert gh.writes() == [] and gl.writes() == []
    finally:
        gh.close()
        gl.close()


def test_read_token_reaches_git_only_through_the_helper(at_pins, hermetic, tmp_path):
    import os
    import subprocess

    from nanosoc_rm.config import load_config
    from nanosoc_rm.gitrun import hardened_env
    from nanosoc_rm.mirror.layout import git_env

    registry, _shas, _pr = _two_host_fixture(at_pins, hermetic)
    base = hermetic.tool_env(registry, "token")
    tok = tmp_path / "github_read"
    tok.write_text("tok-SECRET-123\n")
    cfg = dict(base.cfg, credentials={"github_read": {"token_file": str(tok), "scope": ["contents:read"],
                                                       "expires": "2027-01-01"}})
    env = at_pins.ToolEnv(tmp_path / "tokenenv", cfg, base.data_dir, registry,
                          {k: v for k, v in base.env.items() if k != "NANOSOC_RM_CONFIG"})
    with env.active():
        docs = at_pins.rm("mirror.sync_component")("dual")
        extra = git_env(load_config(), "github")
    assert all(d["fact"]["ok"] for d in docs)
    text = json.dumps(docs) + json.dumps(db.latest("mirror_status", config=env.config))
    assert "tok-SECRET-123" not in text
    # git itself gets the token from the helper, for the configured host only
    run_env = hardened_env(extra)
    fill = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", env=run_env,
                          capture_output=True, text=True, timeout=60)
    assert "password=tok-SECRET-123" in fill.stdout and "username=x-access-token" in fill.stdout
    other = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=elsewhere.invalid\n\n",
                           env=dict(run_env, GIT_TERMINAL_PROMPT="0"), capture_output=True, text=True, timeout=60)
    assert "tok-SECRET-123" not in other.stdout
    assert os.environ.get("NANOSOC_RM_GIT_TOKEN") is None
