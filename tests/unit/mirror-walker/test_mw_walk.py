"""The walker on a nested hermetic tree: recursion, relative URLs, aliases, tree paths, split, unknown URLs, the
frozen-tag alert, storage, and the CLI exit codes (interfaces.md section 4; B2, B3, B7, B9)."""
from __future__ import annotations

import json

import pytest
from mw_fixtures import load_at

from nanosoc_rm import contracts, db

UNKNOWN_SHA = "1234567890abcdef1234567890abcdef12345678"


@pytest.fixture(scope="session")
def at_pins():
    return load_at("test_pins")


@pytest.fixture
def hermetic(at_pins, tmp_path):
    hosts = at_pins.LocalHosts(tmp_path / "hermetic")
    yield hosts
    hosts.api.close()


def build_nested(at_pins, h):
    """top (github) pins mid twice (once through an alias URL) and leaf by ssh; mid pins leaf by a RELATIVE URL;
    top also pins an uncatalogued repo. leaf is pinned at two commits (split)."""
    gh, gl = "github", "gitlab"
    leaf = at_pins._chain(h, gl, "soclabs/leaf", 2)
    mid = h.commit(gl, "soclabs/mid", gitlinks={"deps/leaf": leaf[0]},
                   gitmodules=[{"path": "deps/leaf", "url": "../leaf.git", "branch": "main"}])
    h.push(gl, "soclabs/mid", mid, "main")
    top = h.commit(gh, "SoC-Labs/top", gitlinks={"a": mid, "b": mid, "c": leaf[1], "u": UNKNOWN_SHA},
                   gitmodules=[{"path": "a", "url": h.url(gl, "soclabs/mid")},
                               {"path": "b", "url": "https://gitlab.example.invalid/soclabs/old-mid.git"},
                               {"path": "c", "url": h.url(gl, "soclabs/leaf", ssh=True)},
                               {"path": "u", "url": "https://example.org/someone/thing.git"}])
    h.push(gh, "SoC-Labs/top", top, "main")
    h.tag(gh, "SoC-Labs/top", "v1", top)
    catalogue = {"schema": 1, "components": [
        {"id": "top", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/top"}},
        {"id": "mid", "class": "ip_tech", "canonical": {"host": gl, "path": "soclabs/mid"},
         "aliases": [{"host": gl, "path": "soclabs/old-mid"}]},
        {"id": "leaf", "class": "ip_tech", "canonical": {"host": gl, "path": "soclabs/leaf"}},
    ]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "top-v1", "component": "top", "ref": {"tag": "v1"}, "commit": top, "frozen": True},
        {"id": "top-main", "component": "top", "ref": {"branch": "main"}}]}
    registry = at_pins.write_registry(h.root / "registry-nested", catalogue, eligibility, examples=False)
    return {"registry": registry, "top": top, "mid": mid, "leaf": leaf}


def _synced(at_pins, h, fx, name="nested"):
    env = h.tool_env(fx["registry"], name)
    with env.active():
        for cid in ("top", "mid", "leaf"):
            at_pins.rm("mirror.sync_component")(cid)
    return env


def test_nested_walk(at_pins, hermetic):
    fx = build_nested(at_pins, hermetic)
    env = _synced(at_pins, hermetic, fx)
    with env.active():
        docs = at_pins.rm("walker.walk")("top-v1")
        stored = db.latest("pin_state", "pin_state:top-v1:")
        alerts = {a["subject"] for a in db.alerts()}
    for d in docs:
        assert contracts.validate_observation(d) == [], d["subject"]
    by = {d["fact"]["tree_paths"][0]: d["fact"] for d in docs}
    assert sorted(by) == ["a", "a/deps/leaf", "b", "c", "u"]
    assert sorted(d["subject"] for d in stored) == sorted(d["subject"] for d in docs)
    # mid at one commit, reached at two tree paths: its pin is listed once, with both paths (B2)
    leaf_pin = by["a/deps/leaf"]
    assert leaf_pin["tree_paths"] == ["a/deps/leaf", "b/deps/leaf"] and leaf_pin["depth"] == 2
    assert leaf_pin["parent"] == {"component": "mid", "repo": {"host": "gitlab", "path": "soclabs/mid"},
                                  "commit": fx["mid"]}
    assert leaf_pin["url_kind"] == "relative" and leaf_pin["child"]["repo"] == {"host": "gitlab", "path": "soclabs/leaf"}
    assert leaf_pin["branch_field"] == "main" and leaf_pin["hosts"]["gitlab"]["state"] == "ON_DEFAULT"
    assert leaf_pin["hosts"]["gitlab"]["lag"] == 1 and leaf_pin["split"] is True and by["c"]["split"] is True
    # the alias URL is normalised to the component's repo (B9)
    assert by["b"]["child"] == {"component": "mid", "catalogued_as": "component",
                                "repo": {"host": "gitlab", "path": "soclabs/mid"}, "commit": fx["mid"]}
    assert by["c"]["url_kind"] == "ssh" and by["c"]["hosts"]["gitlab"]["lag"] == 0
    assert by["a"]["parent"]["repo"] == {"host": "github", "path": "SoC-Labs/top"} and by["a"]["depth"] == 1
    # an uncatalogued URL: no host state, flagged with an unknown_url alert
    assert by["u"]["child"]["catalogued_as"] == "unknown" and by["u"]["hosts"] == {} and by["u"]["url_kind"] == "other"
    assert "alert:unknown_url:pin_state:top-v1:u" in alerts
    # the envelope is never fresher than its data (README section 4 rule 3)
    for d in docs:
        assert all(h["observed_at"] >= d["observed_at"] for h in d["fact"]["hosts"].values())
    # CLI: the same tree; nothing red, but the unknown pin cannot be judged: exit 2
    proc = env.cli("walk", "top-v1", "--json")
    assert proc.returncode == 2, proc.stderr
    assert sorted(json.loads(x)["subject"] for x in proc.stdout.splitlines()) == sorted(d["subject"] for d in docs)


def test_an_ignored_repo_pinned_in_a_registered_tree_is_unknown_and_alerts(at_pins, hermetic):
    """K1 (option a, David 2026-10-10): `ignored` hides a repo from the inventory, not from a registered tree."""
    gh = "github"
    other = hermetic.commit(gh, "SoC-Labs/other-family", files={"README": "another SoC family\n"})
    hermetic.push(gh, "SoC-Labs/other-family", other, "main")
    top = hermetic.commit(gh, "SoC-Labs/k1-top", gitlinks={"i": other},
                          gitmodules=[{"path": "i", "url": hermetic.url(gh, "SoC-Labs/other-family")}])
    hermetic.push(gh, "SoC-Labs/k1-top", top, "main")
    catalogue = {"schema": 1, "components": [{"id": "k1-top", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/k1-top"}}],
                 "ignored": [{"repo": {"host": gh, "path": "SoC-Labs/other-family"}, "reason": "another SoC family"}]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "k1", "component": "k1-top", "ref": {"branch": "main"}, "commit": top, "frozen": True}]}
    registry = at_pins.write_registry(hermetic.root / "registry-k1", catalogue, eligibility, examples=False)
    env = hermetic.tool_env(registry, "k1")
    with env.active():
        at_pins.rm("mirror.sync_component")("k1-top")
        (doc,) = at_pins.rm("walker.walk")("k1")
        alerts = {a["subject"] for a in db.alerts()}
    assert doc["fact"]["child"]["catalogued_as"] == "unknown" and doc["fact"]["hosts"] == {}
    assert doc["fact"]["child"]["repo"] == {"host": gh, "path": "SoC-Labs/other-family"}
    assert "alert:unknown_url:pin_state:k1:i" in alerts


def test_frozen_tag_moved_and_branch_configuration(at_pins, hermetic):
    fx = build_nested(at_pins, hermetic)
    env = _synced(at_pins, hermetic, fx, "moved")
    with env.active():
        at_pins.rm("walker.walk")("top-v1")
        assert not [a for a in db.alerts() if a["fact"]["alert_kind"] == "tag_moved"]
        # the tag moves to a new commit: the frozen configuration is still walked at its commit, with an alert
        new = hermetic.commit("github", "SoC-Labs/top", parents=[fx["top"]], gitlinks={"a": fx["mid"]},
                              gitmodules=[{"path": "a", "url": hermetic.url("gitlab", "soclabs/mid")}])
        hermetic.push("github", "SoC-Labs/top", new, "main")
        hermetic.tag("github", "SoC-Labs/top", "v1", new)
        at_pins.rm("mirror.sync_component")("top")
        docs = at_pins.rm("walker.walk")("top-v1")
        assert {d["fact"]["parent"]["commit"] for d in docs if d["fact"]["depth"] == 1} == {fx["top"]}
        moved = [a for a in db.alerts() if a["fact"]["alert_kind"] == "tag_moved"]
        assert [a["fact"]["about"] for a in moved] == ["pin_state:top-v1"]
        # a branch configuration follows its branch in the mirror (the new commit pins only mid, at "a")
        main = at_pins.rm("walker.walk")("top-main")
        assert {d["fact"]["parent"]["commit"] for d in main if d["fact"]["depth"] == 1} == {new}
        assert sorted(d["fact"]["tree_paths"][0] for d in main) == ["a", "a/deps/leaf"]


def test_cli_errors_and_pins_filters(at_pins, hermetic):
    fx = build_nested(at_pins, hermetic)
    env = hermetic.tool_env(fx["registry"], "errors")
    assert env.cli("walk", "no-such-config", "--json").returncode == 3
    assert env.cli("pins", "no-such-config", "--json").returncode == 3
    nomirror = env.cli("walk", "top-main", "--json")
    assert nomirror.returncode == 2 and "no mirror" in nomirror.stderr
    with env.active():
        for cid in ("top", "mid", "leaf"):
            at_pins.rm("mirror.sync_component")(cid)
    proc = env.cli("pins", "top-v1", "--state", "ON_DEFAULT", "--host", "gitlab", "--json")
    shown = [json.loads(x)["fact"]["tree_paths"][0] for x in proc.stdout.splitlines()]
    assert sorted(shown) == ["a", "a/deps/leaf", "b", "c"]
    assert proc.returncode == 2                    # judged on every pin: u cannot be judged
    table = env.cli("pins", "top-v1")
    assert "a/deps/leaf" in table.stdout and "gitlab:ON_DEFAULT(lag 1)" in table.stdout
