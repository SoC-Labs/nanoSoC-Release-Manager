"""`nanosoc-rm compat --tier t1|t2` (CR-12, MVP S3; interfaces.md section 9a "Tiers"): the registry's tiers.yaml,
the sandbox checkout of the resolved tree, the budget and kill limit, the verdicts file, the T0 skip, the
NANOSOC_GEN_OK refusal and the run verdict ladder.

Hermetic: the compat scene of test_compat.py (bare repos behind url.insteadOf); tier commands are small shell
strings run in the sandbox checkout.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

import pytest
import yaml
from test_compat import Scene, _cli, _developer_env

from nanosoc_rm import contracts
from nanosoc_rm.analysis import compat as C
from nanosoc_rm.analysis import tierrun as T
from nanosoc_rm.registry.loader import RegistryError, load_registry

_developer_env = _developer_env       # test_compat's autouse fixture (no CI job, no private rules) applies here too


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    from analysis_fixtures import load_at
    at = load_at("test_pins")
    h = at.LocalHosts(tmp_path_factory.mktemp("tiers"))
    yield Scene(at, h)
    h.api.close()


def set_tiers(scene, configs: dict) -> None:
    (scene.registry / "tiers.yaml").write_text(yaml.safe_dump({"schema": 1, "configurations": configs},
                                                               sort_keys=False))


def spec(command, budget=60, **kw):
    return dict({"command": command, "cwd": ".", "budget_s": budget}, **kw)


def valid(doc):
    problems = contracts.validate_observation(doc)
    assert problems == [], problems
    return doc["fact"]


def rows_of(t):
    return {r["gate"]: r for r in t["rows"]}


# ---------------------------------------------------------------------------------------------------------
# Through compat: the tree, the exit code, the T0 skip, the refusals
# ---------------------------------------------------------------------------------------------------------
def test_tier_command_exiting_1_is_fail_and_fails_the_run(scene):
    set_tiers(scene, {"top-main": {"t1": spec("echo linting; exit 1")}})
    f = valid(scene.run("top-main", tier="t1"))
    t = f["tier"]
    assert f["verdict"] == "PASS"                                   # T0 alone
    assert t["verdict"] == "FAIL" and t["exit_code"] == 1 and not t["killed"]
    assert rows_of(t)["tier.t1"]["status"] == "FAIL" and "exit 1" in rows_of(t)["tier.t1"]["detail"]
    assert f["run_verdict"] == "FAIL" and C.exit_for(f["run_verdict"]) == 1
    assert "linting" in t["log_tail"]
    r = _cli(scene, "compat", "top-main", "--tier", "t1")
    assert r.exit_code == 1, r.output
    assert "tier t1: FAIL" in r.output and "run verdict (T0 + t1): FAIL" in r.output


def test_sandbox_is_the_resolved_tree_with_overrides_and_a_clean_env(scene, monkeypatch):
    monkeypatch.setenv("ARM_IP_LIBRARY_PATH", "/should/not/leak")
    check = ('test -f README && test -f a/rtl/a.v && test -d b/deps/gamma/.git && '
             'test "$(git -C a rev-parse HEAD)" = "$A" && grep -q "module a2" a/rtl/a.v && test -d a/vendor/gamma/.git '
             '&& test -z "${ARM_IP_LIBRARY_PATH:-}" && test "$TREE" = "$PWD" && test -z "${NANOSOC_GEN_OK:-}" '
             '&& test "$PATH" = /usr/bin:/bin && echo tree-ok')
    set_tiers(scene, {"top-main": {"t2": spec(check, env={"A": scene.a2})}})
    f = valid(scene.run("top-main", {"a": scene.a2}, tier="t2"))
    t = f["tier"]
    assert t["verdict"] == "PASS", t
    assert t["log_tail"] == ["tree-ok"] and t["env_keys"] == ["A"] and t["missing_paths"] == []
    assert f["run_verdict"] == f["verdict"]                        # the T0 verdict of this what-if decides


def test_t0_fail_skips_the_tier_and_force_runs_it_marked(scene, tmp_path):
    marker = tmp_path / "ran"
    set_tiers(scene, {"frozen-nowaiver": {"t1": spec("touch %s" % marker)}})
    f = valid(scene.run("frozen-nowaiver", tier="t1"))
    assert f["verdict"] == "FAIL" and f["tier"]["verdict"] == "NOT-RUN" and f["run_verdict"] == "FAIL"
    assert "T0 is FAIL" in f["tier"]["reason"] and not marker.exists()
    r = _cli(scene, "compat", "frozen-nowaiver", "--tier", "t1")
    assert r.exit_code == 1 and "tier t1: NOT-RUN" in r.output and not marker.exists()
    f = valid(scene.run("frozen-nowaiver", tier="t1", force_tier=True))
    t = f["tier"]
    assert marker.exists() and t["verdict"] == "PASS" and t["forced"] and C.FORCED_NOTE in t["reason"]
    assert f["run_verdict"] == "FAIL"                               # a forced tier never lifts the T0 FAIL


def test_regenerates_without_nanosoc_gen_ok_is_refused_with_exit_3(scene, tmp_path, monkeypatch):
    marker = tmp_path / "regen"
    set_tiers(scene, {"top-main": {"t1": spec("echo $NANOSOC_GEN_OK > %s" % marker, regenerates=True)}})
    monkeypatch.delenv("NANOSOC_GEN_OK", raising=False)
    with pytest.raises(T.TierUsage, match="NANOSOC_GEN_OK=1"):
        scene.run("top-main", tier="t1")
    r = _cli(scene, "compat", "top-main", "--tier", "t1")
    assert r.exit_code == 3 and "NANOSOC_GEN_OK=1" in r.output and not marker.exists()
    f = valid(scene.run("top-main", tier="t1", environ={"NANOSOC_GEN_OK": "1"}))
    assert f["tier"]["verdict"] == "PASS" and marker.read_text().strip() == "1"


def test_no_declared_command_and_force_without_tier_are_usage_errors(scene):
    set_tiers(scene, {"top-main": {"t2": spec("true")}})
    r = _cli(scene, "compat", "top-main", "--tier", "t1")
    assert r.exit_code == 3 and "declares no t1 command for top-main" in r.output
    with pytest.raises(C.CompatUsage):
        scene.run("top-main", force_tier=True)


def test_pins_list_lays_out_only_what_the_command_needs(scene):
    set_tiers(scene, {"top-main": {"t1": spec("test ! -e a/rtl && test -d b/deps/gamma/.git", pins=["b"])}})
    t = valid(scene.run("top-main", tier="t1"))["tier"]
    assert t["verdict"] == "PASS" and t["not_laid_out"] == 1, t
    set_tiers(scene, {"top-main": {"t1": spec("true", pins=["nope"])}})
    t = valid(scene.run("top-main", tier="t1"))["tier"]
    assert t["verdict"] == "UNVERIFIED" and t["missing_paths"] == ["nope"] and t["seconds"] is None


# ---------------------------------------------------------------------------------------------------------
# The runner itself: budget, kill, verdicts file, seat
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "tree"
    d.mkdir()
    return d


def run(tmp_path, tree, s, **kw):
    return T.run_tier(s, "t1", tree, tmp_path, "0123456789abcdef0123456789abcdef", **kw)


def test_over_budget_but_inside_the_kill_limit_is_a_warning(tmp_path, tree):
    t = run(tmp_path, tree, spec("sleep 2.5", budget=2))
    assert t["verdict"] == "PASS" and t["over_budget"] and not t["killed"] and t["seconds"] >= 2.5


def test_past_one_and_a_half_budgets_the_run_is_killed_and_fails(tmp_path, tree):
    t0 = time.monotonic()
    t = run(tmp_path, tree, spec("sleep 30 & sleep 6; echo finished", budget=1))
    took = time.monotonic() - t0
    assert t["killed"] and t["verdict"] == "FAIL" and t["over_budget"] and t["exit_code"] is None, t
    assert "killed after" in t["reason"] and "budget 1 s x 1.5" in t["reason"]
    assert took < 5.5 and "finished" not in t["log_tail"]


def test_a_verdicts_file_row_fail_fails_the_tier_even_on_exit_0(tmp_path, tree):
    body = "2026-10-11T10:00:00Z\tPASS\tlint.leaf\tclean\n2026-10-11T10:00:01Z\tFAIL\tlint.shim\t3 findings\n"
    (tree / "verdicts.tsv").write_text("2026-10-11T09:00:00Z\tPASS\tstale\tfrom an older run\n")
    s = spec("printf '%s' \"$BODY\" > \"$NANOSOC_RM_VERDICTS\"", verdicts="verdicts.tsv", env={"BODY": body})
    t = run(tmp_path, tree, s)
    r = rows_of(t)
    assert r["tier.t1"]["status"] == "PASS" and r["tier.t1.lint.leaf"]["status"] == "PASS"
    assert r["tier.t1.lint.shim"]["status"] == "FAIL" and r["tier.t1.lint.shim"]["detail"] == "3 findings"
    assert "tier.t1.stale" not in r                                  # the stale file was removed first
    assert t["verdict"] == "FAIL" and "tier.t1.lint.shim: 3 findings" in t["reason"]
    assert all(contracts.validate_verdict_row(x) == [] for x in t["rows"])


def test_a_declared_verdicts_file_never_written_is_unverified(tmp_path, tree):
    t = run(tmp_path, tree, spec("true", verdicts="out/verdicts.tsv"))
    assert t["verdict"] == "UNVERIFIED" and rows_of(t)["tier.t1.verdicts"]["status"] == "UNVERIFIED"


def test_vcs_seat_held_elsewhere_is_infra_without_running(tmp_path, tree):
    lock = tmp_path / "locks"
    lock.mkdir()
    fd = os.open(str(lock / "nanosoc-rm-vcs-seat.lock"), os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = run(tmp_path, tree, spec("touch ran", needs=["vcs-seat"]), lock_dir=lock)
    finally:
        os.close(fd)
    assert t["verdict"] == "INFRA" and not (tree / "ran").exists()
    assert run(tmp_path, tree, spec("touch ran", needs=["vcs-seat"]), lock_dir=lock)["verdict"] == "PASS"


def test_a_tool_this_host_lacks_is_infra_without_running(tmp_path, tree):
    """A tier whose env names a tool path this host does not have (VCS_HOME, VERILATOR) is INFRA with the path
    named, not a FAIL of the code and not a crash; paths that exist, and paths inside the sandbox, pass."""
    gone = "/nonexistent-%s/eda/vcs" % tmp_path.name                  # outside the sandbox (work = tmp_path)
    t = run(tmp_path, tree, spec("touch ran", env={"VCS_HOME": gone, "PATH": "${VCS_HOME}/bin:${PATH}"}))
    assert t["verdict"] == "INFRA" and not (tree / "ran").exists(), t
    assert t["reason"].startswith("tool not on this host: VCS_HOME=%s does not exist" % gone), t["reason"]
    assert "PATH=%s/bin does not exist" % gone in t["reason"]
    t = run(tmp_path, tree, spec("touch ran", env={"VERILATOR": "/bin/sh", "OUT": str(tree / "out" / "x"),
                                                 "LIC": "27020@lm1:27006@lm2"}))
    assert t["verdict"] == "PASS" and (tree / "ran").exists(), t


def test_check_spec_refuses_set_env_sourcing_and_reserved_env():
    for cmd in ("source set_env.sh && make lint", "cd x; . ../set_env.sh; make", ["bash", "-c", "source ./set_env.sh"]):
        with pytest.raises(T.TierUsage, match="set_env"):
            T.check_spec(spec(cmd), "t1", "top-main", {})
    with pytest.raises(T.TierUsage, match="TREE"):
        T.check_spec(spec("true", env={"TREE": "/x"}), "t1", "top-main", {})
    T.check_spec(spec("grep '^export' set_env.sh"), "t1", "top-main", {})      # reading it as text is fine


def test_env_expands_only_known_names(tmp_path, tree):
    t = run(tmp_path, tree, spec('test "$X" = "$TREE/sub"', env={"X": "${TREE}/sub"}))
    assert t["verdict"] == "PASS"
    with pytest.raises(T.TierUsage, match="HOSTONLY"):
        run(tmp_path, tree, spec("true", env={"X": "${HOSTONLY}"}))


# ---------------------------------------------------------------------------------------------------------
# tiers.yaml: schema and loader rule 14
# ---------------------------------------------------------------------------------------------------------
def test_tiers_yaml_naming_an_unknown_configuration_is_refused(scene):
    set_tiers(scene, {"top-main": {"t1": spec("true")}, "no-such-config": {"t1": spec("true")}})
    try:
        with pytest.raises(RegistryError) as e:
            load_registry(scene.registry)
        assert [p.rule for p in e.value.problems] == ["rule-14"] and "no-such-config" in str(e.value)
        set_tiers(scene, {"top-main": {"t1": spec("source set_env.sh; make")}})
        with pytest.raises(RegistryError) as e:
            load_registry(scene.registry)
        assert {p.rule for p in e.value.problems} == {"schema"}
    finally:
        set_tiers(scene, {"top-main": {"t1": spec("true")}})
    assert load_registry(scene.registry).tiers("top-main")["t1"]["command"] == "true"


def test_ladder():
    assert C.ladder("PASS", "FAIL") == "FAIL" and C.ladder("WAIVED", "WARN") == "WAIVED"
    assert C.ladder("PASS", "NOT-RUN") == "PASS" and C.ladder("INFRA", "UNVERIFIED") == "UNVERIFIED"


def test_registry_without_tiers_yaml_still_loads(scene):
    p = scene.registry / "tiers.yaml"
    keep = p.read_text() if p.exists() else None
    if keep is not None:
        p.unlink()
    try:
        assert load_registry(scene.registry).tiers("top-main") == {}
    finally:
        if keep is not None:
            p.write_text(keep)


def test_examples_dir_has_a_tiers_example():
    ex = Path(contracts.schemas_dir()) / "examples" / "eligibility.tiers.example.yaml"
    doc = contracts.load_yaml(ex.read_text())
    assert contracts.validate("eligibility", doc, "/$defs/tiers_file") == []
