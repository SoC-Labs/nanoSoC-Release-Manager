"""nanosoc_rm.scan: armed rules, locations only, census, allowlist, private rule files, the CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from nanosoc_rm.scan import rules, scan_dir, scan_paths, scan_texts, scan_tree
from nanosoc_rm.scan.rules import Rule, RuleSet, RulesError

SPEC = rules.specimen("arm.banner")


def test_arm_banner_is_armed_and_its_near_misses_stay_quiet():
    assert rules.arm(rules.BUILTIN_RULES) == {"arm.banner": True}
    r = rules.BUILTIN_RULES[0]
    assert all(not r.matches(c) for c in r.counter_specimens) and r.counter_specimens


def test_a_planted_specimen_is_found_at_its_first_line_and_never_quoted(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "t0.log").write_text("one\ntwo\n" + SPEC)
    (tmp_path / "provenance.json").write_text("{}")
    r = scan_dir(tmp_path)
    assert r.verdict == "finding" and r.files_scanned == 2
    assert r.findings == [{"rule": "arm.banner", "file": "logs/t0.log", "line": 3}]
    assert SPEC.splitlines()[0] not in json.dumps(r.as_doc())


def test_empty_directory_is_unverified_not_clean(tmp_path):
    r = scan_dir(tmp_path)
    assert (r.verdict, r.files_scanned) == ("unverified", 0)
    assert scan_dir(tmp_path / "absent").verdict == "unverified"


def test_a_rule_that_cannot_fire_makes_the_scan_unverified(tmp_path):
    (tmp_path / "f").write_text("clean\n")
    dead = Rule("dead.rule", (re.compile("will-never-match-anything-xyz"),), "specimen that does not match")
    r = scan_dir(tmp_path, RuleSet(rules.BUILTIN_RULES + (dead,)))
    assert r.verdict == "unverified" and r.armed["dead.rule"] is False and "dead.rule" in r.why
    loud = Rule("loud.rule", (re.compile("x"),), "x", ("x near miss",))      # fires on its near miss too
    assert scan_dir(tmp_path, RuleSet((loud,))).verdict == "unverified"


def test_symlinks_and_unreadable_files_are_unscannable_findings(tmp_path):
    (tmp_path / "real").write_text("ok\n")
    os.symlink(tmp_path / "real", tmp_path / "link")
    r = scan_dir(tmp_path)
    assert {"rule": "scan.unscannable", "file": "link", "line": None} in r.findings and r.verdict == "finding"


def test_scan_paths_reads_in_place_and_reports_the_path_as_given(tmp_path):
    p = tmp_path / "vendor.v"
    p.write_text("// header\n" * 4 + SPEC)
    before = p.stat().st_mtime_ns
    r = scan_paths([p])
    assert r.findings == [{"rule": "arm.banner", "file": str(p), "line": 5}]
    assert p.stat().st_mtime_ns == before


def test_private_rule_file_adds_rules_and_owned_allow_entries(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text(yaml.safe_dump({"schema": 1, "rules": [
        {"id": "fab.shape", "patterns": ["FAB-[0-9]{4}"], "specimen": "FAB-1234", "counter_specimens": ["FAB-12"]}],
        "allow": [{"rule": "fab.shape", "path": "docs/*", "reason": "a documented example", "owner": "member-a"}]}))
    rs = rules.rule_set(f)
    assert [r.id for r in rs.rules] == ["arm.banner", "fab.shape"]
    (tmp_path / "b" / "docs").mkdir(parents=True)
    (tmp_path / "b" / "docs" / "x.md").write_text("FAB-9999\n")
    (tmp_path / "b" / "y.v").write_text("FAB-9999\n")
    r = scan_dir(tmp_path / "b", rs)
    assert r.findings == [{"rule": "fab.shape", "file": "y.v", "line": 1}]


@pytest.mark.parametrize("doc, why", [
    ({"schema": 1, "rules": [{"id": "arm.banner", "patterns": ["x"], "specimen": "x"}]}, "duplicate"),
    ({"schema": 1, "rules": [{"id": "a.b", "patterns": ["x"]}]}, "specimen"),
    ({"schema": 1, "rules": [{"id": "a.b", "patterns": ["("], "specimen": "x"}]}, "bad pattern"),
    ({"schema": 1, "rules": [], "allow": [{"rule": "arm.banner", "path": "*", "reason": "r"}]}, "owner"),
    ({"schema": 2}, "schema: 1"),
])
def test_bad_rule_files_are_refused(tmp_path, doc, why):
    f = tmp_path / "rules.yaml"
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(RulesError, match=why):
        rules.rule_set(f)


def test_rules_env_names_the_private_file(tmp_path, monkeypatch):
    f = tmp_path / "rules.yaml"
    f.write_text(yaml.safe_dump({"schema": 1, "rules": [{"id": "fab.shape", "patterns": ["Q"], "specimen": "Q"}]}))
    monkeypatch.setenv("RM_SCAN_RULES", str(f))
    assert rules.specimen("fab.shape") == "Q"


def test_scan_tree_reads_a_commit_without_a_checkout(at, tmp_path):
    repo = tmp_path / "r.git"
    c = at.fx_commit(repo, files={"a/clean.v": "module a; endmodule\n", "b/leak.v": "x\n" + SPEC})
    r = scan_tree(repo, c, prefix="sub")
    assert (r.verdict, r.files_scanned) == ("finding", 2)
    assert r.findings == [{"rule": "arm.banner", "file": "sub/b/leak.v", "line": 2}]


def test_scan_texts_counts_none_as_unscannable():
    r = scan_texts([("x", None)])
    assert r.verdict == "finding" and r.findings == [{"rule": "scan.unscannable", "file": "x", "line": None}]


def test_cli_scan_bundle_exit_codes(tmp_path):
    from nanosoc_rm.cli import main
    clean, dirty, empty = tmp_path / "c", tmp_path / "d", tmp_path / "e"
    for d in (clean, dirty, empty):
        d.mkdir()
    (clean / "f").write_text("ok\n")
    (dirty / "f").write_text(SPEC)
    run = lambda *a: CliRunner().invoke(main, list(a), standalone_mode=False)
    assert run("scan", "bundle", str(clean)).return_value == 0
    res = run("scan", "bundle", str(dirty), "--json")
    assert res.return_value == 1 and json.loads(res.output)["findings"][0]["file"] == "f"
    assert SPEC.splitlines()[0] not in res.output
    assert run("scan", "bundle", str(empty)).return_value == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: 2\n")
    assert run("scan", "bundle", str(clean), "--rules", str(bad)).return_value == 3


def test_cli_scan_bundle_as_a_process(tmp_path):
    (tmp_path / "f").write_text(SPEC)
    p = subprocess.run(["nanosoc-rm", "scan", "bundle", str(tmp_path)], capture_output=True, text=True,
                       env=dict(os.environ, PATH=os.path.dirname(os.sys.executable) + os.pathsep + os.environ["PATH"]))
    assert p.returncode == 1 and "arm.banner at f:1" in p.stdout
