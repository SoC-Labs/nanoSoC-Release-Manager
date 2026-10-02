"""Independent review of t0-scan (R1-R8): each test reproduces one finding and pins its fix.

R1 finalise is idempotent; R2 in-run controls walk and scan like the real input, and a blind walk is not a PASS;
R3 the scrub removes every variable that carries the job token; R4 the throwaway repo holds every bundle file;
R5 a non-UTF-8 file name never blinds the vendor gate; R6 INFRA on a control's broken input is not a fired control;
R7 '.' is above every tree path; R8 `t0 run --json`, and the toolkit scanner's git goes through the argv guard.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import nanosoc_rm
from conftest import rm_request
from nanosoc_rm.contracts import parse_verdict_row, validate_provenance
from nanosoc_rm.gitrun import run_git
from nanosoc_rm.scan import rules, scan_dir, scan_tree
from nanosoc_rm.t0 import gates, gitwork

NONCE = "0123456789abcdef0123456789abcdef"
SECRET = "glcbt-SECRET-VALUE-never-written"
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@invalid"}


def ctx_for(tree, tmp_path, name="w", **kw):
    return gates.Context.for_tests(nonce="ab" * 16, workdir=tmp_path / name, request=tree.request,
                                   url_map=tree.url_map, **kw)


def rolled(row, ctl, gid):
    return {g["id"]: g for g in gates.rollup([row, ctl], gates.GATES[gid].scope)}[gid]


# ---------------------------------------------------------------------------------------------------------
# R1: finalise_bundle twice (the job's script, then its after_script) never loses the first scan's findings
# ---------------------------------------------------------------------------------------------------------
def test_finalise_twice_keeps_the_findings_and_never_downgrades(at, tmp_path, runner_module):
    b = at.write_bundle(tmp_path, at.example_provenance(), at.example_verdicts(),
                        {"logs/t0.log": at.T0_LOG + rules.specimen("arm.banner")})
    first = runner_module.finalise_bundle(b)
    want = [{"rule": "arm.banner", "file": "logs/t0.log", "line": 3}]
    assert first.verdict == "finding" and first.findings == want
    second = runner_module.finalise_bundle(b)                       # what after_script does
    assert second.verdict == "finding" and second.findings == want
    prov = json.loads((b / "provenance.json").read_text())
    assert prov["scan"]["verdict"] == "finding" and prov["scan"]["findings"] == want
    assert validate_provenance(prov) == []


def test_finalise_after_an_unverified_scan_stays_unverified(at, tmp_path, monkeypatch, runner_module):
    b = at.write_bundle(tmp_path, at.example_provenance(), at.example_verdicts())
    monkeypatch.setenv("RM_SCAN_TOOLKIT", str(tmp_path / "absent.sh"))
    assert runner_module.finalise_bundle(b).verdict == "unverified"
    monkeypatch.delenv("RM_SCAN_TOOLKIT")
    assert runner_module.finalise_bundle(b).verdict == "unverified"
    assert json.loads((b / "provenance.json").read_text())["scan"]["verdict"] == "unverified"


def test_the_runner_s_pending_placeholder_is_not_a_prior_scan(at, tmp_path, runner_module):
    prov = at.example_provenance()
    prov["scan"] = {"tool": "pending", "verdict": "unverified"}
    b = at.write_bundle(tmp_path, prov, at.example_verdicts())
    assert runner_module.finalise_bundle(b).verdict == "clean"
    assert (b / "verdicts.tsv").exists()


def test_a_flagged_name_that_names_no_file_deletes_everything_but_provenance(at, tmp_path, monkeypatch,
                                                                           runner_module):
    """A scanner that names a flagged file differently from the file system (quoting, an encoding) must not
    leave the real file behind for upload."""
    from nanosoc_rm.scan import scan as scanmod
    b = at.write_bundle(tmp_path, at.example_provenance(), at.example_verdicts(), {"logs/t0.log": "x\n"})
    monkeypatch.setattr(scanmod, "scan_dir", lambda root, rules=None, toolkit=None: scanmod.ScanResult(
        "finding", [{"rule": "file.ext", "file": '"logs/t0.log"', "line": None}], 3, {"arm.banner": True},
        "fake", "", ['"logs/t0.log"']))
    res = runner_module.finalise_bundle(b)
    assert res.verdict == "unverified" and "not found" in res.why
    assert sorted(p.name for p in b.rglob("*") if p.is_file()) == ["provenance.json"]


# ---------------------------------------------------------------------------------------------------------
# R2: each control's broken input goes through the same walk and the same scanner as the real input
# ---------------------------------------------------------------------------------------------------------
def test_a_blind_tree_scanner_is_caught_in_the_run(tree, tmp_path, monkeypatch):
    from nanosoc_rm.scan import scan as scanmod
    monkeypatch.setattr(scanmod, "scan_tree", lambda *a, **k: scanmod.scan_texts([("x", b"clean")]))
    row, ctl = gates.run_with_control("t0.vendor.no-arm-ip", ctx_for(tree, tmp_path))
    assert ctl["status"] == "FAIL", ctl                                  # the control did not fire
    assert rolled(row, ctl, "t0.vendor.no-arm-ip")["verdict"] != "PASS"


@pytest.mark.parametrize("gid", ["t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public"])
def test_a_blind_walk_is_never_a_pass(tree, tmp_path, monkeypatch, gid):
    monkeypatch.setattr(gitwork, "parse_gitmodules", lambda text: [])
    monkeypatch.setattr(gates, "parse_gitmodules", lambda text: [])
    row, ctl = gates.run_with_control(gid, ctx_for(tree, tmp_path))
    assert row["status"] == "UNVERIFIED", row
    assert "submodule" in row["detail"]
    assert ctl["status"] == "FAIL", ctl
    assert rolled(row, ctl, gid)["verdict"] == "UNVERIFIED"


@pytest.mark.parametrize("gid", ["t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public",
                                 "t0.vendor.no-arm-ip", "t0.manifest.valid"])
def test_controls_build_a_scratch_superproject_and_walk_it(tree, tmp_path, gid):
    ctx = ctx_for(tree, tmp_path)
    broken = gates.GATES[gid].control(ctx)
    assert broken.parents is None and broken.request["commit"] != tree.request["commit"]
    assert broken.repo_url.startswith("https://negctl.nanosoc-rm.invalid/")
    assert broken.walk() is not None and broken.root_fetch().ok
    assert gates.GATES[gid].check(broken)[0] == "FAIL"


def test_controls_fire_with_no_request(tmp_path):
    """No superproject URL to pin against: the scratch pin points at a scratch repo that lacks the commit."""
    ctx = gates.Context.for_tests(nonce="cd" * 16, workdir=tmp_path / "w")
    for gid in ("t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public", "t0.vendor.no-arm-ip"):
        assert gates.GATES[gid].check(gates.GATES[gid].control(ctx))[0] == "FAIL", gid


def test_a_superproject_without_gitmodules_still_passes(at, tmp_path):
    bare = tmp_path / "rm-plain.git"
    sha = at.fx_commit(bare, files={"README": "x\n"}, ref="refs/heads/main")
    url = "https://github.com/SoC-Labs/rm-plain.git"
    req = {"configuration": "rm-plain", "repo": {"host": "github", "path": "SoC-Labs/rm-plain"}, "commit": sha,
           "upgrade_from": None, "canonical_urls": {}}
    ctx = gates.Context.for_tests(nonce="ef" * 16, workdir=tmp_path / "w", request=req, url_map={url: str(bare)})
    for gid in ("t0.pins.fetchable", "t0.pins.reachable"):
        row, ctl = gates.run_with_control(gid, ctx)
        assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (gid, row, ctl)


# ---------------------------------------------------------------------------------------------------------
# R3: CI_REPOSITORY_URL (and anything else holding the job token) is scrubbed, and the gate checks it
# ---------------------------------------------------------------------------------------------------------
def test_scrub_removes_every_variable_carrying_the_job_token(runner_module):
    env = {"CI_JOB_TOKEN": SECRET, "CI_JOB_ID": "1", "HOME": "/h",
           "CI_REPOSITORY_URL": "https://gitlab-ci-token:%s@gitlab.example.invalid/g/p.git" % SECRET,
           "SOME_TOOL_URL": "https://x/?t=%s" % SECRET,
           "OTHER_URL": "https://user:pw@example.invalid/x.git", "PLAIN_URL": "https://example.invalid/x.git"}
    assert runner_module.scrub_environment(env, keep_registry_token=True) == SECRET
    assert env == {"CI_JOB_ID": "1", "HOME": "/h", "PLAIN_URL": "https://example.invalid/x.git"}
    assert SECRET not in json.dumps(env)


def test_hardened_fails_on_a_url_with_credentials_left(tmp_path):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {"CI_COMMIT_REF_PROTECTED": "true", "HOME": str(home), "TMPDIR": str(tmp), "GITLAB_USER_LOGIN": "svc",
           "CI_REPOSITORY_URL": "https://gitlab-ci-token:%s@gitlab.example.invalid/g/p.git" % SECRET}
    ctx = gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path / "w", env=env, service_account="svc")
    status, detail = gates.GATES["t0.runner.hardened"].check(ctx)
    assert status == "FAIL" and "CI_REPOSITORY_URL" in detail and SECRET not in detail


def test_the_job_drops_ci_repository_url_before_any_git_call(tree, tmp_path, runner_module):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {"RM_REQUEST": rm_request(NONCE, tree.request), "HOME": str(home), "TMPDIR": str(tmp),
           "CI_COMMIT_REF_PROTECTED": "true", "USER": "svc", "RM_SERVICE_ACCOUNT": "svc", "CI_JOB_TOKEN": SECRET,
           "CI_REPOSITORY_URL": "https://gitlab-ci-token:%s@gitlab.example.invalid/g/p.git" % SECRET,
           "PATH": os.environ["PATH"]}
    out = tmp_path / "rm-bundle"
    runner_module.run(out, environ=env, url_map=tree.url_map)
    assert "CI_REPOSITORY_URL" not in env and "CI_JOB_TOKEN" not in env
    rows = {r["gate"]: r for r in (parse_verdict_row(x) for x in (out / "verdicts.tsv").read_text().splitlines())}
    assert rows["t0.runner.hardened"]["status"] == "PASS", rows["t0.runner.hardened"]
    assert SECRET not in "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------------------------------------
# R4 / R8b: the toolkit scanner sees every bundle file, and its git calls pass the runner's argv guard
# ---------------------------------------------------------------------------------------------------------
def _fake(tmp_path, mode="normal", extra=""):
    from test_scan_toolkit import fake_toolkit
    s = fake_toolkit(tmp_path / ("tk-" + mode), mode)
    if extra:
        with open(s.parent / "lib.sh", "a") as f:
            f.write(extra)
    return s


def _bundle(d: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def test_a_gitignore_in_the_bundle_does_not_hide_a_file_from_the_toolkit(tmp_path):
    b = _bundle(tmp_path / "b", {"provenance.json": "{}\n", "logs/cell.lef": "MACRO x\nEND x\n",
                                 ".gitignore": "logs/cell.lef\n"})
    r = scan_dir(b, toolkit=_fake(tmp_path))
    assert r.verdict == "finding", r.as_doc()
    assert {"rule": "file.ext", "file": "logs/cell.lef", "line": None} in r.findings


def test_a_nested_repo_in_the_bundle_makes_the_toolkit_scan_unverified(tmp_path):
    b = _bundle(tmp_path / "b", {"provenance.json": "{}\n"})
    run_git("init", "-q", b / "logs" / "inner").check()
    (b / "logs" / "inner" / "cell.lef").write_text("MACRO x\n")
    run_git("add", "-A", cwd=b / "logs" / "inner").check()
    run_git("commit", "-q", "-m", "c", cwd=b / "logs" / "inner", env=IDENT).check()
    r = scan_dir(b, toolkit=_fake(tmp_path))
    assert r.verdict in ("unverified", "finding"), r.as_doc()
    assert r.verdict == "unverified" and "files" in r.why, r.as_doc()


def test_the_toolkit_scanner_cannot_push(tmp_path):
    log = tmp_path / "push.log"
    s = _fake(tmp_path, "normal", "PUSHLOG=%s\n" % log)
    text = s.read_text().replace('rec PASS vendor.arm "fake rules armed"',
                                 'rec PASS vendor.arm "fake rules armed"\n'
                                 'git push --dry-run . HEAD:refs/heads/rm-x >/dev/null 2>&1; '
                                 'echo "push-rc=$?" >> "$PUSHLOG"')
    s.write_text(text)
    r = scan_dir(_bundle(tmp_path / "b", {"a.txt": "x\n"}), toolkit=s)
    assert r.verdict == "clean", r.as_doc()
    assert log.read_text().strip() == "push-rc=97"


# ---------------------------------------------------------------------------------------------------------
# R5: a file name that is not UTF-8 is scanned like any other, and one tree's crash stays that tree's item
# ---------------------------------------------------------------------------------------------------------
def test_a_non_utf8_file_name_is_scanned_and_named(tmp_path):
    repo = tmp_path / "r"
    run_git("init", "-q", repo).check()
    (repo / "leak.v").write_text(rules.specimen("arm.banner"))
    with open(os.path.join(os.fsencode(repo), b"caf\xe9.v"), "w") as f:
        f.write(rules.specimen("arm.banner"))
    run_git("add", "-A", cwd=repo).check()
    run_git("commit", "-q", "-m", "c", cwd=repo, env=IDENT).check()
    sha = run_git("rev-parse", "HEAD", cwd=repo).check().stdout.strip()
    r = scan_tree(repo, sha)
    assert r.verdict == "finding" and r.files_scanned == 2
    assert sorted(f["file"] for f in r.findings) == ["caf\\xe9.v", "leak.v"]
    json.dumps(r.as_doc(), ensure_ascii=False).encode("utf-8")           # a finding is always printable


def test_one_tree_that_cannot_be_scanned_is_that_tree_s_item(tree, tmp_path, monkeypatch):
    from nanosoc_rm.scan import scan as scanmod
    real = scanmod.scan_tree

    def flaky(repo, commit, *a, **k):
        if commit == tree.a1:
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        return real(repo, commit, *a, **k)
    monkeypatch.setattr(scanmod, "scan_tree", flaky)
    ctx = ctx_for(tree, tmp_path)
    status, detail = gates.GATES["t0.vendor.no-arm-ip"].check(ctx)
    items = {i["tree_path"]: i for i in ctx.items("t0.vendor.no-arm-ip")}
    assert items["sub-a"]["result"] == "unverified" and "UnicodeDecodeError" in items["sub-a"]["why"]
    assert items["sub-c"]["result"] == "pass" and items["."]["result"] == "pass"
    assert status == "UNVERIFIED"


# ---------------------------------------------------------------------------------------------------------
# R6: a control whose broken input comes back INFRA did not fire
# ---------------------------------------------------------------------------------------------------------
def test_infra_on_the_broken_input_is_not_a_fired_control(tmp_path, monkeypatch):
    gate = gates.GATES["t0.infra.probe"]
    marker = object()

    def check(ctx):
        return ("INFRA", "host unreachable") if getattr(ctx, "_probe_urls", None) == [marker] else ("PASS", "ok")
    monkeypatch.setitem(gates.GATES, gate.id, dataclasses.replace(
        gate, check=check, control=lambda ctx: ctx.derive(_probe_urls=[marker])))
    row, ctl = gates.run_with_control(gate.id, gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path))
    assert (row["status"], ctl["status"]) == ("PASS", "FAIL") and "INFRA" in ctl["detail"]
    assert (rolled(row, ctl, gate.id)["verdict"], rolled(row, ctl, gate.id)["control"]) == ("UNVERIFIED", "not-fired")


def test_an_unreachable_host_during_the_control_is_not_fired(tmp_path):
    """The real probe control against a host that does not resolve: INFRA, so not fired."""
    ctx = gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path, probe_urls=[])
    broken = ctx.derive(_probe_urls=["https://rm-host.nanosoc-rm.invalid/x.git"])
    status, _ = gates.GATES["t0.infra.probe"].check(broken)
    assert status == "INFRA"


# ---------------------------------------------------------------------------------------------------------
# R7: the superproject's item ('.') is above every tree path
# ---------------------------------------------------------------------------------------------------------
def test_a_waived_superproject_covers_the_not_reached_pins_below_it():
    g = [{"id": "t0.pins.fetchable", "verdict": "FAIL", "control": "fired", "detail": "d"}]
    items = {"t0.pins.fetchable": [
        {"tree_path": ".", "result": "fail", "why": "w"},
        {"tree_path": "sub-a", "result": "not-reached", "why": "w"},
        {"tree_path": "sub-a/leaf", "result": "not-reached", "why": "w"}]}
    w = [{"gate": "t0.pins.fetchable", "tree_path": ".", "result": "fail", "reason": "frozen",
          "approved_by": "someone", "approved_at": "2026-10-01"}]
    (out,), unused = gates.apply_waivers(g, items, w)
    assert out["verdict"] == "WAIVED" and unused == []
    assert {x["tree_path"]: x["covered_by"] for x in out["waived"]} == {".": ".", "sub-a": ".", "sub-a/leaf": "."}
    items["t0.pins.fetchable"].append({"tree_path": "sub-b", "result": "fail", "why": "new"})
    assert gates.apply_waivers(g, items, w)[0][0]["verdict"] == "FAIL"     # '.' covers not-reached only


# ---------------------------------------------------------------------------------------------------------
# R8a: `nanosoc-rm t0 run --json` prints the verdict rows, one JSON document per line
# ---------------------------------------------------------------------------------------------------------
def test_cli_t0_run_json_prints_the_rows(tree, tmp_path, runner_module):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {"RM_REQUEST": rm_request(NONCE, tree.request), "HOME": str(home), "TMPDIR": str(tmp),
           "CI_COMMIT_REF_PROTECTED": "true", "USER": "svc", "RM_SERVICE_ACCOUNT": "svc", "PATH": os.environ["PATH"],
           "PYTHONPATH": str(Path(nanosoc_rm.__file__).resolve().parents[1])}      # the package under test
    m = tmp_path / "map.json"
    m.write_text(json.dumps(tree.url_map))
    p = subprocess.run([os.path.join(os.path.dirname(sys.executable), "nanosoc-rm"), "t0", "run", "--out",
                        str(tmp_path / "b"), "--url-map", str(m), "--json"], env=env, capture_output=True, text=True,
                       timeout=600)
    assert p.returncode == 1, p.stderr
    docs = [json.loads(x) for x in p.stdout.splitlines()]
    tsv = [parse_verdict_row(x) for x in (tmp_path / "b" / "verdicts.tsv").read_text().splitlines()]
    assert docs == tsv and len(docs) == 16 and all(d["nonce"] == NONCE for d in docs)
