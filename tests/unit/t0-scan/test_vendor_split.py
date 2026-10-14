"""CR-6: the vendor gate split (David 2026-10-13, chat: work autonomously).

`t0.vendor.no-arm-ip` keeps the Arm-source rules (arm.banner, any arm.* rule, the toolkit's file.sha, unscannable
files) and the catalogue vendor_allowlist; `t0.vendor.no-foundry-data` takes every other rule (the toolkit's
foundry and site-inventory rules, private non-arm rules). Foundry findings are WARN by default and FAIL only for
a rule the catalogue's `foundry_rules` marks `severity: fail`. One scan per tree feeds both gates.

Mutations each test here is meant to catch (PR6; measured in the commit message):
- family(): everything arm (foundry findings FAIL no-arm-ip again) or everything foundry (the banner goes amber);
- the default foundry severity flipped to fail (site paths FAIL every run again);
- the `foundry_rules` fail lookup ignored (a blocking rule goes amber);
- the control's `foundry_fail_all` ignored (the control cannot fire: WARN is never FAIL);
- the per-request scan cache dropped (the toolkit runs twice per tree).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nanosoc_rm import contracts
from nanosoc_rm.scan import ScanResult
from nanosoc_rm.scan.allowlist import foundry_rules
from nanosoc_rm.scan.families import ARM, FOUNDRY, family, split
from nanosoc_rm.scan.rules import RULES_ENV, specimen
from nanosoc_rm.scan.toolkit import TOOLKIT_ENV
from nanosoc_rm.t0 import gates

from test_scan_toolkit import fake_toolkit

WEB = "https://github.com/SoC-Labs/%s.git"
ARM_GATE, FOUNDRY_GATE = "t0.vendor.no-arm-ip", "t0.vendor.no-foundry-data"
SITE = "INVENTED-SITE-PATH-42"            # the private test rule's specimen: invented, names no real site

RULES = {"schema": 1, "rules": [
    {"id": "foundry.example", "patterns": [r"INVENTED-SITE-PATH-[0-9]+"], "specimen": "a path: " + SITE,
     "counter_specimens": ["INVENTED-SITE-PATH-x"]},
]}


def test_family_of_each_rule():
    assert [family(r) for r in ("arm.banner", "arm.product-code", "file.sha", "scan.unscannable")] == [ARM] * 4
    assert [family(r) for r in ("file.ext", "file.size", "path", "ident", "licence", "value.lef",
                                "foundry.example", "armour.x")] == [FOUNDRY] * 8


def test_split_keeps_one_family_and_unverified_stays():
    res = ScanResult("finding", [{"rule": "arm.banner", "file": "a.v", "line": 1},
                                 {"rule": "path", "file": "b.tcl", "line": 3}], 2, {"arm.banner": True})
    a, f = split(res, ARM), split(res, FOUNDRY)
    assert [x["rule"] for x in a.findings] == ["arm.banner"] and a.verdict == "finding"
    assert [x["rule"] for x in f.findings] == ["path"] and f.flagged_files == ["b.tcl"]
    assert split(ScanResult("finding", [{"rule": "path", "file": "b", "line": 1}], 1, {}), ARM).verdict == "clean"
    assert split(ScanResult("unverified", [], 0, {}, why="x"), FOUNDRY).verdict == "unverified"
    with pytest.raises(ValueError):
        split(res, "other")


def test_foundry_rules_loader_fail_wins_and_bad_entries_are_skipped():
    cat = {"foundry_rules": [
        {"rule": "path", "severity": "warn", "reason": "r", "approved_by": "D", "approved_on": "2026-10-14"},
        {"rule": "path", "severity": "fail", "reason": "r", "approved_by": "D", "approved_on": "2026-10-14"},
        {"rule": "ident", "severity": "fail", "reason": "r", "approved_by": "D", "approved_on": "2026-10-14"},
        {"rule": "ident", "severity": "warn", "reason": "r", "approved_by": "D", "approved_on": "2026-10-14"},
        {"rule": "licence", "severity": "block"}, {"severity": "fail"}, "junk"]}
    got = foundry_rules(cat)
    assert {k: v.severity for k, v in got.items()} == {"path": "fail", "ident": "fail"}


class Tree:
    """rm-vs-super (github, main) pins rm-vs-ip, whose tree holds: an Arm banner (allow-listed or not, per test),
    a foundry file type (lib/cells.lef, the fake toolkit's file.ext) and a site path (the private test rule)."""

    def __init__(self, at, root: Path, files: dict[str, str]):
        bare = lambda n: root / ("%s.git" % n)
        self.ip = at.fx_commit(bare("rm-vs-ip"), files=files, ref="refs/heads/main")
        self.super = at.fx_commit(bare("rm-vs-super"), files={"README": "super\n"}, gitlinks={"ip": self.ip},
                                  gitmodules={"ip": WEB % "rm-vs-ip"}, ref="refs/heads/main")
        self.url_map = {WEB % n: str(bare(n)) for n in ("rm-vs-ip", "rm-vs-super")}
        self.request = {"configuration": "rm-vs", "repo": {"host": "github", "path": "SoC-Labs/rm-vs-super"},
                        "commit": self.super, "upgrade_from": None, "canonical_urls": {}}


FOUNDRY_FILES = {"lib/cells.lef": "invented\n", "flow/setup.tcl": "set root /%s/x\n" % SITE, "rtl/ok.v": "module ok; endmodule\n"}


@pytest.fixture(scope="module")
def foundry_tree(at, tmp_path_factory):
    return Tree(at, tmp_path_factory.mktemp("vs-f"), FOUNDRY_FILES)


@pytest.fixture(scope="module")
def clean_tree(at, tmp_path_factory):
    return Tree(at, tmp_path_factory.mktemp("vs-c"), {"rtl/ok.v": "module ok; endmodule\n"})


def registry(tmp_path, name, *, allow=(), severities=()):
    reg = tmp_path / ("registry-" + name)
    reg.mkdir()
    doc = {"schema": 1,
           "components": [{"id": "rm-vs-super", "class": "soc_ref", "canonical": {"host": "github", "path": "SoC-Labs/rm-vs-super"}},
                          {"id": "rm-vs-ip", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/rm-vs-ip"}}]}
    if allow:
        doc["vendor_allowlist"] = list(allow)
    if severities:
        doc["foundry_rules"] = [{"rule": r, "severity": s, "reason": "unit", "approved_by": "David",
                                 "approved_on": "2026-10-14"} for r, s in severities]
    assert contracts.validate("component-catalogue", doc, None) == []
    (reg / "catalogue.yaml").write_text(yaml.safe_dump(doc))
    return reg


@pytest.fixture
def engines(tmp_path, monkeypatch):
    """Both foundry engines on: the fake toolkit (counting its runs) and a private rule file."""
    count = tmp_path / "toolkit-runs.txt"
    monkeypatch.setenv(TOOLKIT_ENV, str(fake_toolkit(tmp_path / "tk", countfile=count)))
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump(RULES))
    monkeypatch.setenv(RULES_ENV, str(rules))
    return count


def both(tree, tmp_path, name="w", **reg):
    ctx = gates.Context.for_tests(nonce="cd" * 16, workdir=tmp_path / name, request=tree.request,
                                  url_map=tree.url_map, registry_dir=registry(tmp_path, name, **reg))
    out = {}
    for gid in (ARM_GATE, FOUNDRY_GATE):
        row, ctl = gates.run_with_control(gid, ctx)
        out[gid] = (row, ctl, {i["tree_path"]: i for i in ctx.items(gid)})
    return ctx, out


def test_foundry_findings_are_warn_by_default_and_do_not_touch_the_arm_gate(foundry_tree, tmp_path, engines):
    _, out = both(foundry_tree, tmp_path)
    row, ctl, items = out[ARM_GATE]
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (row, ctl)
    assert "file.ext" not in items["ip"]["why"] and "foundry.example" not in items["ip"]["why"]
    row, ctl, items = out[FOUNDRY_GATE]
    assert (row["status"], ctl["status"]) == ("WARN", "PASS"), (row, ctl)
    why = items["ip"]["why"]
    assert items["ip"]["result"] == "pass" and why.startswith("WARN: 0 findings at severity fail; 2 at severity warn")
    assert "file.ext at lib/cells.lef:-" in why and "foundry.example at flow/setup.tcl:1" in why, why
    assert SITE not in why                               # locations only, never the matched text
    rolled = {g["id"]: g for g in gates.rollup([r for o in out.values() for r in o[:2]], "configuration")}
    assert rolled[FOUNDRY_GATE]["verdict"] == "WARN" and rolled[ARM_GATE]["verdict"] == "PASS"


def test_a_rule_marked_fail_fails_the_foundry_gate_only_for_that_rule(foundry_tree, tmp_path, engines):
    _, out = both(foundry_tree, tmp_path, severities=[("foundry.example", "fail"), ("file.ext", "warn")])
    row, _, items = out[FOUNDRY_GATE]
    assert row["status"] == "FAIL" and "1 with findings at severity fail: ip" in row["detail"], row
    why = items["ip"]["why"]
    assert items["ip"]["result"] == "fail" and why.startswith("1 finding(s): foundry.example at flow/setup.tcl:1")
    assert "1 at severity warn: file.ext at lib/cells.lef:- (severity warn for rule file.ext by David" in why, why
    assert out[ARM_GATE][0]["status"] == "PASS"


def test_the_allowlist_still_applies_to_foundry_findings(foundry_tree, tmp_path, engines):
    allow = [{"rule": r, "match": {"component": "rm-vs-ip"}, "severity": "allow", "reason": "unit",
              "approved_by": "David", "approved_on": "2026-10-14"} for r in ("file.ext", "foundry.example")]
    _, out = both(foundry_tree, tmp_path, allow=allow, severities=[("foundry.example", "fail")])
    row, ctl, items = out[FOUNDRY_GATE]
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), row
    assert "2 allowed by vendor_allowlist" in items["ip"]["why"]


def test_one_scan_per_tree_feeds_both_gates(foundry_tree, tmp_path, engines):
    both(foundry_tree, tmp_path)
    runs = engines.read_text().splitlines()
    # real input: 2 trees (super, ip), once each; each control: its own scratch superproject, once
    assert len(runs) == 4 and len(set(runs)) == 4, runs
    assert runs.count(foundry_tree.ip) == 1 and runs.count(foundry_tree.super) == 1


def test_without_a_foundry_rule_the_gate_is_not_run(clean_tree, tmp_path):
    _, out = both(clean_tree, tmp_path)                 # no toolkit, no private rules: arm.banner only
    row, ctl, items = out[FOUNDRY_GATE]
    assert row["status"] == "SKIP" and row["detail"] == gates.NO_FOUNDRY_RULES and items == {}
    rolled = {g["id"]: g for g in gates.rollup([r for o in out.values() for r in o[:2]], "configuration")}
    assert rolled[FOUNDRY_GATE] == {"id": FOUNDRY_GATE, "verdict": "NOT-RUN", "control": "not-applicable",
                                    "detail": gates.NO_FOUNDRY_RULES}
    assert rolled[ARM_GATE]["verdict"] == "PASS"


@pytest.mark.parametrize("engine", ["toolkit", "rules"])
def test_the_control_fires_with_either_engine_alone(clean_tree, tmp_path, monkeypatch, engine):
    if engine == "toolkit":
        monkeypatch.setenv(TOOLKIT_ENV, str(fake_toolkit(tmp_path / "tk")))
    else:
        rules = tmp_path / "rules.yaml"
        rules.write_text(yaml.safe_dump(RULES))
        monkeypatch.setenv(RULES_ENV, str(rules))
    _, out = both(clean_tree, tmp_path)
    row, ctl, _ = out[FOUNDRY_GATE]
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (row, ctl)
    assert "on its broken input the gate reported FAIL" in ctl["detail"]


def test_the_control_is_unverified_when_the_engine_cannot_see_its_plant(clean_tree, tmp_path, monkeypatch):
    """A foundry rule whose specimen does not match its own pattern cannot arm: the scan is unverified, so the
    real row is UNVERIFIED and the control does not fire (no PASS bought by a blind engine)."""
    blind = {"schema": 1, "rules": [{"id": "foundry.blind", "patterns": ["NEVER-MATCHES-ANYTHING"],
                                     "specimen": "something else"}]}
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump(blind))
    monkeypatch.setenv(RULES_ENV, str(rules))
    _, out = both(clean_tree, tmp_path)
    row, ctl, _ = out[FOUNDRY_GATE]
    assert row["status"] == "UNVERIFIED" and ctl["status"] == "FAIL", (row, ctl)
    assert gates.rollup([row, ctl], "configuration")[gates.gates_for("configuration").index(FOUNDRY_GATE)][
        "verdict"] == "UNVERIFIED"


def test_the_arm_banner_still_fails_the_arm_gate_with_foundry_rules_on(at, tmp_path, engines):
    tree = Tree(at, tmp_path / "t", {"rtl/v.v": specimen("arm.banner") + "\nmodule v; endmodule\n",
                                     "lib/cells.lef": "invented\n"})
    _, out = both(tree, tmp_path)
    assert out[ARM_GATE][0]["status"] == "FAIL" and "arm.banner at rtl/v.v:1" in out[ARM_GATE][2]["ip"]["why"]
    assert out[FOUNDRY_GATE][0]["status"] == "WARN"
