"""nanosoc_rm.t0.gates: every gate on hermetic inputs, its control fires, and an always-PASS mutant is caught."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from nanosoc_rm.contracts import validate_gate_item
from nanosoc_rm.t0 import gates


def ctx_for(tree, tmp_path, name="w", **kw):
    return gates.Context.for_tests(nonce="ab" * 16, workdir=tmp_path / name, request=tree.request,
                                   url_map=tree.url_map, **kw)


def by_path(items):
    return {i["tree_path"]: i for i in items}


def test_url_map_is_refused_in_a_ci_job(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_JOB_ID", "1")
    with pytest.raises(PermissionError):
        ctx_for(tree, tmp_path)
    gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path / "plain")      # no url_map: allowed


def test_fetchable_judges_every_pin_by_its_pinned_url(tree, tmp_path):
    ctx = ctx_for(tree, tmp_path)
    row, ctl = gates.run_with_control("t0.pins.fetchable", ctx)
    items = by_path(ctx.items("t0.pins.fetchable"))
    assert row["status"] == "FAIL" and ctl["status"] == "PASS", (row, ctl)
    assert {k: v["result"] for k, v in items.items()} == {
        "sub-a": "pass", "sub-b": "fail", "sub-b/leaf": "not-reached", "sub-c": "pass"}
    assert "ssh" in items["sub-b"]["why"] and "sub-b" in row["detail"]
    assert items["sub-b/leaf"]["commit"] == tree.l2            # listed through the canonical walk
    assert all(validate_gate_item(i) == [] for i in items.values())
    assert row["nonce"] == ctl["nonce"] == "ab" * 16


def test_reachable_judges_every_pin_on_its_canonical_host(tree, tmp_path):
    ctx = ctx_for(tree, tmp_path)
    row, ctl = gates.run_with_control("t0.pins.reachable", ctx)
    items = by_path(ctx.items("t0.pins.reachable"))
    assert (row["status"], ctl["status"]) == ("FAIL", "PASS"), (row, ctl)
    assert {k: v["result"] for k, v in items.items()} == {
        "sub-a": "pass", "sub-b": "pass", "sub-b/leaf": "pass", "sub-c": "fail"}
    assert "refs/tags/v1" in items["sub-b/leaf"]["why"]        # tag-only commit: reachable from a tag
    assert items["sub-c"]["commit"] == tree.c2 and "rm-sub-c" in items["sub-c"]["why"]
    assert all(i["host"] == "github" for i in items.values())  # judged on the canonical host, not the copy


def test_reachable_unreadable_canonical_is_unverified_and_below_it_not_reached(at, tree, tmp_path):
    url_map = dict(tree.url_map)
    url_map[gates_web("rm-sub-b")] = str(tmp_path / "gone.git")      # canonical repo not readable
    ctx = gates.Context.for_tests(nonce="cd" * 16, workdir=tmp_path / "w", request=tree.request, url_map=url_map)
    row, _ = gates.run_with_control("t0.pins.reachable", ctx)
    items = by_path(ctx.items("t0.pins.reachable"))
    assert items["sub-b"]["result"] == "unverified"
    assert "sub-b/leaf" not in items                          # its .gitmodules could not be read at all
    assert row["status"] == "FAIL"                            # sub-c still fails


def gates_web(name):
    return "https://github.com/SoC-Labs/%s.git" % name


def test_no_ssh_public_names_only_the_ssh_pins(tree, tmp_path):
    ctx = ctx_for(tree, tmp_path)
    row, ctl = gates.run_with_control("t0.urls.no-ssh-public", ctx)
    items = by_path(ctx.items("t0.urls.no-ssh-public"))
    assert (row["status"], ctl["status"]) == ("FAIL", "PASS")
    assert [k for k, v in items.items() if v["result"] == "fail"] == ["sub-b"]
    assert items["sub-b/leaf"]["result"] == "pass"            # sub-b is public through its canonical URL
    assert "rm-super@" in row["detail"] and "sub-b" in row["detail"] and "sub-a" not in row["detail"]


def test_vendor_scans_fetched_trees_only(tree, tmp_path):
    ctx = ctx_for(tree, tmp_path)
    row, ctl = gates.run_with_control("t0.vendor.no-arm-ip", ctx)
    items = by_path(ctx.items("t0.vendor.no-arm-ip"))
    assert (row["status"], ctl["status"]) == ("UNVERIFIED", "PASS"), (row, ctl)
    assert {k: v["result"] for k, v in items.items()} == {
        ".": "pass", "sub-a": "pass", "sub-b": "not-reached", "sub-b/leaf": "not-reached", "sub-c": "pass"}
    # Integration (eth-rc4 run): the not-fetched count names the trees it counts, not the trees with findings.
    assert row["detail"].endswith("2 not fetched, so not scanned: sub-b, sub-b/leaf"), row["detail"]


def test_captured_records_gitlinks_read_and_null_below_an_unfetched_parent(tree, tmp_path):
    ctx = ctx_for(tree, tmp_path)
    cap = ctx.captured()
    assert cap["superproject_commit"] == tree.super
    assert {p["tree_path"]: p["commit"] for p in cap["pins"]} == {
        "sub-a": tree.a1, "sub-b": tree.b1, "sub-b/leaf": None, "sub-c": tree.c2}


def test_probe_passes_and_its_control_fires(tree, tmp_path):
    row, ctl = gates.run_with_control("t0.infra.probe", ctx_for(tree, tmp_path))
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (row, ctl)


def test_probe_unreachable_host_is_infra_never_fail(tmp_path):
    ctx = gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path / "w",
                                  probe_urls=["https://nanosoc-rm-probe.invalid/x/y.git"])
    status, detail = gates.GATES["t0.infra.probe"].check(ctx)
    assert status == "INFRA", detail


def test_manifest_skips_when_no_manifest_exists(tree, tmp_path):
    row, ctl = gates.run_with_control("t0.manifest.valid", ctx_for(tree, tmp_path))
    assert row["status"] == "SKIP" and "no nanosoc-component.yaml" in row["detail"]
    assert ctl["status"] == "PASS"                            # the control still proves the gate can fail


def test_upgrade_path_skips_without_upgrade_from(tree, tmp_path):
    row, _ = gates.run_with_control("t0.clone.upgrade-path", ctx_for(tree, tmp_path))
    assert row["status"] == "SKIP" and "upgrade_from" in row["detail"]


def test_d2_hermetic_gate_fixtures(at, tmp_path):
    """AT-D2 control part 2 (needs the registry module for t0.registry.schema, built elsewhere): the other
    three gates PASS on the good input with their control fired, and FAIL on the broken one."""
    from nanosoc_rm.scan import rules
    fx = at.hermetic_gate_fixtures(tmp_path / "fx", arm_specimen=rules.specimen("arm.banner"))
    for gid, good, bad in fx["cases"]:
        if gid == "t0.registry.schema":
            continue
        row, ctl = gates.run_with_control(gid, at.gates_ctx(nonce="1" * 32, workdir=tmp_path / ("w-" + gid), **good))
        assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (gid, row["detail"], ctl["detail"])
        row, _ = gates.run_with_control(gid, at.gates_ctx(nonce="2" * 32, workdir=tmp_path / ("b-" + gid), **bad))
        assert row["status"] == "FAIL", (gid, row["detail"])


def _env(tmp_path, name, **changes):
    home, tmp = tmp_path / name / "home", tmp_path / name / "tmp"
    home.mkdir(parents=True)
    tmp.mkdir(parents=True)
    env = {"CI_COMMIT_REF_PROTECTED": "true", "HOME": str(home), "TMPDIR": str(tmp), "USER": "svc"}
    env.update(changes)
    return env


@pytest.mark.parametrize("change, named", [
    ({"CI_COMMIT_REF_PROTECTED": "false"}, "CI_COMMIT_REF_PROTECTED"),
    ({"USER": "jbloggs"}, "USER"),
    ({"CI_JOB_TOKEN": "s3cr3t-value"}, "CI_JOB_TOKEN"),
    ({"CI_REGISTRY_PASSWORD": "s3cr3t-value"}, "CI_REGISTRY_PASSWORD"),
    ({"CI_JOB_JWT_V2": "s3cr3t-value"}, "CI_JOB_JWT_V2"),
    ({"GITLAB_USER_LOGIN": "jbloggs"}, "GITLAB_USER_LOGIN"),
])
def test_hardened_fails_naming_the_variable(tmp_path, change, named):
    ctx = gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path / "w", env=_env(tmp_path, "e", **change),
                                  service_account="svc")
    status, detail = gates.GATES["t0.runner.hardened"].check(ctx)
    assert status == "FAIL" and named in detail, detail
    assert "s3cr3t-value" not in detail                         # a credential's value is never reported


def test_hardened_reads_the_start_listing_and_the_service_account_variable(tmp_path):
    env = _env(tmp_path, "e", RM_SERVICE_ACCOUNT="svc", GITLAB_USER_LOGIN="svc")
    (tmp_path / "e" / "home" / ".cache").mkdir()               # written AFTER start: the snapshot decides
    ctx = gates.Context(nonce="0" * 32, workdir=tmp_path / "w", env=env,
                        start_listing={"HOME": [], "TMPDIR": []})
    assert gates.GATES["t0.runner.hardened"].check(ctx)[0] == "PASS"
    ctx = gates.Context(nonce="0" * 32, workdir=tmp_path / "w2", env=env,
                        start_listing={"HOME": [".bashrc"], "TMPDIR": []})
    status, detail = gates.GATES["t0.runner.hardened"].check(ctx)
    assert status == "FAIL" and "HOME" in detail


def test_every_gate_mutant_is_caught_on_hermetic_inputs(tree, tmp_path, monkeypatch, at):
    """D6 on real inputs: each gate made always-PASS has a control that does not fire, so it rolls up
    UNVERIFIED (the AT does this on rc4's .gitmodules; here on a hermetic tree with every input present)."""
    registry = at.example_registry(tmp_path / "reg")
    for gid, gate in sorted(gates.GATES.items()):
        monkeypatch.setitem(gates.GATES, gid, dataclasses.replace(gate, check=lambda ctx: ("PASS", "mutant")))
        ctx = gates.Context.for_tests(nonce="ef" * 16, workdir=tmp_path / gid, request=tree.request,
                                      url_map=tree.url_map, registry_dir=registry, env=_env(tmp_path, gid))
        row, ctl = gates.run_with_control(gid, ctx)
        assert (row["status"], ctl["status"]) == ("PASS", "FAIL"), gid
        rolled = {g["id"]: g for g in gates.rollup([row, ctl], gate.scope)}[gid]
        assert (rolled["verdict"], rolled["control"]) == ("UNVERIFIED", "not-fired"), gid
        monkeypatch.setitem(gates.GATES, gid, gate)


def test_a_crashing_check_is_unverified_and_its_control_does_not_fire(tmp_path, monkeypatch):
    gate = gates.GATES["t0.infra.probe"]

    def boom(ctx):
        raise RuntimeError("bang")
    monkeypatch.setitem(gates.GATES, gate.id, dataclasses.replace(gate, check=boom))
    row, ctl = gates.run_with_control(gate.id, gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path))
    assert row["status"] == "UNVERIFIED" and "bang" in row["detail"] and ctl["status"] == "FAIL"


def test_details_never_hold_a_tab_or_newline(tmp_path, monkeypatch):
    gate = gates.GATES["t0.infra.probe"]
    monkeypatch.setitem(gates.GATES, gate.id, dataclasses.replace(gate, check=lambda c: ("FAIL", "a\tb\nc")))
    row, _ = gates.run_with_control(gate.id, gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path))
    assert row["detail"] == "a b c"


def test_gate_table_matches_the_contract():
    assert list(gates.GATES) == ["t0.infra.probe", "t0.runner.hardened", "t0.pins.fetchable", "t0.pins.reachable",
                                 "t0.urls.no-ssh-public", "t0.manifest.valid", "t0.clone.upgrade-path",
                                 "t0.vendor.no-arm-ip", "t0.vendor.no-foundry-data", "t0.registry.schema",
                                 "t0.registry.refs-resolve"]
    assert gates.MAY_SKIP == {"t0.manifest.valid", "t0.clone.upgrade-path", "t0.vendor.no-foundry-data"}
    assert gates.gates_for("registry") == ["t0.registry.schema", "t0.registry.refs-resolve"]
    with pytest.raises(ValueError):
        gates.gates_for("nope")


def test_importing_gates_does_not_import_the_runner():
    import subprocess
    import sys
    code = "import nanosoc_rm.cli, nanosoc_rm.t0.gates, sys; print('nanosoc_rm.t0.runner' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, NANOSOC_RM_CONFIG=""))
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.parametrize("gid", ["t0.pins.fetchable", "t0.manifest.valid", "t0.vendor.no-arm-ip"])
def test_controls_fire_when_the_work_dir_is_relative(tmp_path, monkeypatch, gid):
    """Integration (found running T0 locally on eth-rc4): `t0 run --out rm-bundle` makes the work dir the RELATIVE
    path rm-bundle.work. The controls' routing file (GIT_CONFIG_GLOBAL) was then a relative path, which git reads
    after `-C <fetch dir>`, so the routed scratch repo was never found: the fetchable control reported INFRA, the
    manifest control SKIP and the vendor control UNVERIFIED, i.e. none fired. Here no url_map is given (as in a
    real job) and the control's own scratch superproject is the only route."""
    monkeypatch.chdir(tmp_path)
    ctx = gates.Context.for_tests(nonce="ef" * 16, workdir=Path("rm-bundle.work"))
    gate = gates.GATES[gid]
    status, detail = gates._safe_check(gate, gate.control(ctx))
    assert status == "FAIL", (status, detail)
