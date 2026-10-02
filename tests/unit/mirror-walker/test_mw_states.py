"""B3, the hermetic half of AT-B3 and of its negative control, run off the service VM.

AT-B3 is marked `live` as a whole (its second half needs the real mirrors), so `-m "not live"` deselects it. These
tests run exactly its hermetic half with the same fixture and the same checker (tests/acceptance/test_pins.py)."""
from __future__ import annotations

import pytest
from mw_fixtures import load_at


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


def _sync_table(at_pins, hermetic, name):
    registry, expected, rewrite = at_pins.build_state_table(hermetic)
    env = hermetic.tool_env(registry, name)
    with env.active():
        sync = at_pins.rm("mirror.sync_component")
        for component in at_pins.load_yaml(registry / "catalogue.yaml")["components"]:
            sync(component["id"])
        rewrite()
        sync("fx-rewritten")
    return env, expected


def test_state_table_yields_all_11_states(at_pins, hermetic):
    env, expected = _sync_table(at_pins, hermetic, "states")
    with env.active():
        table = at_pins.rm("walker.walk")("fx-states")
    assert at_pins.check_B3(table, expected, exact_lag=True) == []
    states = {s["state"] for d in table for s in d["fact"]["hosts"].values()} | \
             {at_pins.pin_level_state(d["fact"]) for d in table if not d["fact"]["hosts"]}
    assert states == {"TAGGED", "ON_DEFAULT", "PROTECTED_BY_TAG", "BRANCH_ONLY", "REWRITTEN", "PR_ONLY", "ORPHAN",
                      "MISSING", "UNREADABLE", "LOCAL_URL", "NO_GITLINK"}
    assert env.cli("pins", "fx-states", "--json").returncode == 1


def test_state_table_negative_control_tag_deleted(at_pins, hermetic):
    env, expected = _sync_table(at_pins, hermetic, "states-broken")
    hermetic.delete_remote_ref("github", "SoC-Labs/fx-tagged", "refs/tags/v1.0.0")
    with env.active():
        at_pins.rm("mirror.sync_component")("fx-tagged")
        table = at_pins.rm("walker.walk")("fx-states")
    problems = at_pins.check_B3(table, expected, exact_lag=True)
    assert any(p.startswith("tagged@github: state ON_DEFAULT") for p in problems), problems
