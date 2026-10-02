"""nanosoc_rm.t0.runner: the job contract (interfaces.md section 9) end to end on hermetic hosts."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import yaml

from conftest import rm_request
from nanosoc_rm.contracts import parse_verdict_row, validate_gate_item, validate_provenance, validate_verdict_row
from nanosoc_rm.scan import rules
from nanosoc_rm.t0 import gates

@pytest.fixture
def runner(runner_module):
    return runner_module


NONCE = "0123456789abcdef0123456789abcdef"
SECRET = "glcbt-SECRET-VALUE-never-written"


def job_env(tmp_path, request, **extra):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {"RM_REQUEST": rm_request(NONCE, request), "HOME": str(home), "TMPDIR": str(tmp),
           "CI_COMMIT_REF_PROTECTED": "true", "USER": "svc", "RM_SERVICE_ACCOUNT": "svc",
           "CI_JOB_TOKEN": SECRET, "CI_REGISTRY_PASSWORD": SECRET, "CI_JOB_JWT_V2": SECRET,
           "SSH_AUTH_SOCK": "/tmp/agent.sock", "PATH": os.environ["PATH"]}
    env.update(extra)
    return env


def read_bundle(out):
    rows = [parse_verdict_row(line) for line in (out / "verdicts.tsv").read_text().splitlines()]
    prov = json.loads((out / "provenance.json").read_text())
    items = {p.name[:-len(".jsonl")]: [json.loads(x) for x in p.read_text().splitlines()]
             for p in (out / "items").glob("*.jsonl")}
    return rows, prov, items


def all_text(root):
    return "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


def test_configuration_run_writes_a_valid_bundle(tree, tmp_path, runner):
    env = job_env(tmp_path, tree.request)
    out = tmp_path / "rm-bundle"
    rc = runner.run(out, environ=env, url_map=tree.url_map)
    rows, prov, items = read_bundle(out)
    assert rc == 1                                             # fetchable, reachable, no-ssh FAIL
    assert all(validate_verdict_row(r) == [] and r["nonce"] == NONCE for r in rows)
    assert validate_provenance(prov) == [], validate_provenance(prov)
    assert prov["run_nonce"] == NONCE and prov["request"] == tree.request
    assert prov["captured"]["superproject_commit"] == tree.super
    assert prov["scan"]["verdict"] == "clean" and prov["scan"]["findings"] == []
    rolled = {g["id"]: (g["verdict"], g["control"]) for g in gates.rollup(rows, "configuration")}
    assert rolled == {"t0.infra.probe": ("PASS", "fired"), "t0.runner.hardened": ("PASS", "fired"),
                      "t0.pins.fetchable": ("FAIL", "fired"), "t0.pins.reachable": ("FAIL", "fired"),
                      "t0.urls.no-ssh-public": ("FAIL", "fired"), "t0.manifest.valid": ("NOT-RUN", "not-applicable"),
                      "t0.clone.upgrade-path": ("NOT-RUN", "not-applicable"),
                      "t0.vendor.no-arm-ip": ("UNVERIFIED", "fired")}
    assert all(validate_gate_item(i) == [] for its in items.values() for i in its)
    assert len(items["t0.pins.reachable"]) == 4
    # the scrub ran before any git call, and no credential reached the bundle or stayed in the environment
    assert not gates.credential_vars(env) and "SSH_AUTH_SOCK" not in env
    assert SECRET not in all_text(out) and SECRET not in all_text(tmp_path / "rm-bundle.work")


def test_missing_or_invalid_request_writes_no_rows_and_exits_3(tmp_path, runner):
    env = job_env(tmp_path, {"configuration": "x"})
    env.pop("RM_REQUEST")
    assert runner.run(tmp_path / "a", environ=env) == 3 and not (tmp_path / "a").exists()
    env["RM_REQUEST"] = "{not json"
    assert runner.run(tmp_path / "b", environ=env) == 3 and not (tmp_path / "b").exists()
    env["RM_REQUEST"] = json.dumps({"schema": 1, "nonce": NONCE})
    assert runner.run(tmp_path / "c", environ=env) == 3 and not (tmp_path / "c").exists()


def test_url_map_is_refused_in_a_ci_job(tree, tmp_path, runner):
    env = job_env(tmp_path, tree.request, CI_JOB_ID="77")
    assert runner.run(tmp_path / "o", environ=env, url_map=tree.url_map) == 3


def test_unprotected_ref_fails_the_hardened_gate_in_the_job(tree, tmp_path, runner):
    env = job_env(tmp_path, tree.request, CI_COMMIT_REF_PROTECTED="false")
    out = tmp_path / "rm-bundle"
    runner.run(out, environ=env, url_map=tree.url_map)
    rows, _, _ = read_bundle(out)
    h = {g["id"]: g for g in gates.rollup(rows, "configuration")}["t0.runner.hardened"]
    assert h["verdict"] == "FAIL" and "CI_COMMIT_REF_PROTECTED" in h["detail"]


def test_registry_run_uses_the_job_token_once_then_drops_it(at, tree, tmp_path, runner):
    reg_src = at.example_registry(tmp_path / "reg-files")
    files = {str(p.relative_to(reg_src)): p.read_text() for p in reg_src.rglob("*") if p.is_file()}
    reg_commit = at.fx_commit(tmp_path / "registry.git", files=files, ref="refs/heads/main")
    url = "https://gitlab.example.invalid/soclabs/nanosoc-registry.git"
    request = {"configuration": "@registry", "repo": {"host": "gitlab", "path": "soclabs/nanosoc-registry"},
               "commit": reg_commit, "upgrade_from": None, "canonical_urls": {}}
    env = job_env(tmp_path, request, CI_SERVER_URL="https://gitlab.example.invalid")
    # every other github repo the example registry cites resolves to nothing local: no network in a unit test
    url_map = dict(tree.url_map, **{url: str(tmp_path / "registry.git"),
                                    "https://github.com/": str(tmp_path / "no-such-host")})
    out = tmp_path / "rm-bundle"
    old = os.environ.get("CI_SERVER_URL")
    os.environ["CI_SERVER_URL"] = "https://gitlab.example.invalid"       # the job's host table comes from CI
    try:
        runner.run(out, environ=env, url_map=url_map)
    finally:
        os.environ.pop("CI_SERVER_URL") if old is None else os.environ.__setitem__("CI_SERVER_URL", old)
    rows, prov, items = read_bundle(out)
    assert {r["gate"] for r in rows} == {"t0.registry.schema", "t0.registry.schema.negctl",
                                         "t0.registry.refs-resolve", "t0.registry.refs-resolve.negctl"}
    assert prov["captured"]["superproject_commit"] == reg_commit
    cited = {(i["component"], i["commit"]) for i in items["t0.registry.refs-resolve"]}
    assert cited == at.R0_REFS | {("nanosoc-ethernet-chiplet", at.BENCH_UNPUSHED)}
    assert "CI_JOB_TOKEN" not in env and SECRET not in all_text(out) and SECRET not in all_text(tmp_path / "rm-bundle.work")
    log = (out / "logs" / "t0.log").read_text()
    assert "credential.helper" in log and "$RM_REGISTRY_TOKEN" in log      # the helper reads it from the env


def test_finalise_bundle_drops_flagged_files_before_upload(at, tmp_path, runner):
    b = at.write_bundle(tmp_path, at.example_provenance(), at.example_verdicts(),
                        {"logs/t0.log": at.T0_LOG + rules.specimen("arm.banner")})
    res = runner.finalise_bundle(b)
    assert res.verdict == "finding" and not (b / "logs" / "t0.log").exists()
    assert (b / "verdicts.tsv").exists()
    prov = json.loads((b / "provenance.json").read_text())
    assert prov["scan"]["verdict"] == "finding" and validate_provenance(prov) == []
    assert prov["scan"]["findings"] == [{"rule": "arm.banner", "file": "logs/t0.log", "line": 3}]
    assert rules.specimen("arm.banner").splitlines()[0] not in all_text(b)


def test_finalise_bundle_with_an_unarmed_rule_keeps_only_provenance(at, tmp_path, monkeypatch, runner):
    f = tmp_path / "rules.yaml"
    f.write_text(yaml.safe_dump({"schema": 1, "rules": [{"id": "dead.rule", "patterns": ["zzz-never"],
                                                         "specimen": "does not match"}]}))
    monkeypatch.setenv("RM_SCAN_RULES", str(f))
    b = at.write_bundle(tmp_path / "x", at.example_provenance(), at.example_verdicts())
    res = runner.finalise_bundle(b)
    assert res.verdict == "unverified"
    assert sorted(p.name for p in b.rglob("*") if p.is_file()) == ["provenance.json"]
    assert json.loads((b / "provenance.json").read_text())["scan"]["verdict"] == "unverified"


def test_scrub_environment_keeps_only_the_registry_token_in_hand(runner):
    env = {"CI_JOB_TOKEN": "t", "CI_DEPENDENCY_PROXY_PASSWORD": "p", "CI_JOB_JWT": "j", "CI_JOB_ID": "1",
           "SSH_AUTH_SOCK": "s", "HOME": "/h"}
    assert runner.scrub_environment(env, keep_registry_token=True) == "t"
    assert env == {"CI_JOB_ID": "1", "HOME": "/h"}


def test_cli_t0_run_is_the_runner(tree, tmp_path, runner):
    env = job_env(tmp_path, tree.request)
    m = tmp_path / "map.json"
    m.write_text(json.dumps(tree.url_map))
    p = subprocess.run([os.path.join(os.path.dirname(sys.executable), "nanosoc-rm"), "t0", "run", "--out",
                        str(tmp_path / "b"), "--url-map", str(m)], env=env, capture_output=True, text=True,
                       timeout=600)
    assert p.returncode == 1, p.stderr
    assert (tmp_path / "b" / "provenance.json").is_file()
    assert SECRET not in p.stderr + p.stdout


def test_cli_t0_finalise(at, tmp_path, runner):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    b = at.write_bundle(tmp_path, at.example_provenance(), at.example_verdicts())
    assert CliRunner().invoke(main, ["t0", "finalise", str(b)], standalone_mode=False).return_value == 0


@pytest.mark.parametrize("statuses, code", [(["PASS", "SKIP"], 0), (["PASS", "INFRA"], 2), (["INFRA", "FAIL"], 1)])
def test_exit_code_rule(statuses, code, monkeypatch, tree, tmp_path, runner):
    seq = iter(statuses * 10)
    monkeypatch.setattr(gates, "run_with_control", lambda gid, ctx: (
        {"utc": "2026-10-01T00:00:00Z", "status": next(seq), "gate": gid, "detail": "d", "nonce": NONCE},
        {"utc": "2026-10-01T00:00:00Z", "status": "PASS", "gate": gid + ".negctl", "detail": "d", "nonce": NONCE}))
    monkeypatch.setattr(gates.Context, "captured", lambda self: {"superproject_commit": tree.super, "pins": []})
    assert runner.run(tmp_path / "o", environ=job_env(tmp_path, tree.request), url_map=tree.url_map) == code
