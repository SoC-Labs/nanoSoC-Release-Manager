"""CR-6a: the catalogue `vendor_allowlist` in the no-arm-ip gate and the scan module (David's U1 decision).

allow -> clean (recorded as allowed in the item's why); warn -> row WARN with the item listed; uncovered -> FAIL.
Mutation: with the matcher disabled (VendorAllow.covers always False) the allow and warn cases both go FAIL, so
each test here is red against a gate that ignores the allowlist."""
from __future__ import annotations

import subprocess

import pytest
import yaml

from nanosoc_rm.scan import ScanResult, apply_allowlist
from nanosoc_rm.scan.allowlist import VendorAllow, vendor_allowlist
from nanosoc_rm.scan.rules import specimen
from nanosoc_rm.t0 import gates

WEB = "https://github.com/SoC-Labs/%s.git"
BANNER = specimen("arm.banner") + "\nmodule vendored; endmodule\n"


class ArmTree:
    """rm-va-super (github, main) pins rm-va-arm, whose tree holds the arm.banner specimen at rtl/vendored/pl.v."""

    def __init__(self, at, root):
        bare = lambda n: root / ("%s.git" % n)
        self.arm = at.fx_commit(bare("rm-va-arm"), files={"rtl/vendored/pl.v": BANNER, "rtl/ok.v": "module ok; endmodule\n"},
                                ref="refs/heads/main")
        self.super = at.fx_commit(bare("rm-va-super"), files={"README": "super\n"}, gitlinks={"arm": self.arm},
                                  gitmodules={"arm": WEB % "rm-va-arm"}, ref="refs/heads/main")
        self.url_map = {WEB % n: str(bare(n)) for n in ("rm-va-arm", "rm-va-super")}
        self.request = {"configuration": "rm-va", "repo": {"host": "github", "path": "SoC-Labs/rm-va-super"},
                        "commit": self.super, "upgrade_from": None, "canonical_urls": {}}
        self.banner_sha = subprocess.run(["git", "hash-object", "--stdin"], input=BANNER, capture_output=True,
                                         text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def arm_tree(at, tmp_path_factory):
    return ArmTree(at, tmp_path_factory.mktemp("va"))


def entry(severity, **match):
    return {"rule": "arm.banner", "match": match, "severity": severity, "reason": "unit: %s" % severity,
            "approved_by": "David", "approved_on": "2026-10-10"}


def registry(tmp_path, entries, name):
    reg = tmp_path / ("registry-" + name)
    reg.mkdir()
    (reg / "catalogue.yaml").write_text(yaml.safe_dump({
        "schema": 1,
        "components": [{"id": "rm-va-super", "class": "soc_ref", "canonical": {"host": "github", "path": "SoC-Labs/rm-va-super"}},
                       {"id": "rm-va-arm", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/RM-VA-ARM"}}],
        "vendor_allowlist": entries}))
    return reg


def run(arm_tree, tmp_path, entries, name="w"):
    ctx = gates.Context.for_tests(nonce="ab" * 16, workdir=tmp_path / name, request=arm_tree.request,
                                  url_map=arm_tree.url_map, registry_dir=registry(tmp_path, entries, name))
    row, ctl = gates.run_with_control("t0.vendor.no-arm-ip", ctx)
    return row, ctl, {i["tree_path"]: i for i in ctx.items("t0.vendor.no-arm-ip")}


def test_uncovered_finding_is_fail(arm_tree, tmp_path):
    row, ctl, items = run(arm_tree, tmp_path, [])
    assert (row["status"], ctl["status"]) == ("FAIL", "PASS"), (row, ctl)
    assert items["arm"]["result"] == "fail" and "rtl/vendored/pl.v" in items["arm"]["why"]
    assert items["arm"]["component"] == "rm-va-arm"            # the catalogue names the pin (case-insensitive path)
    assert items["."]["component"] == "rm-va-super" and items["."]["result"] == "pass"


def test_allow_entry_makes_the_finding_clean_and_records_it(arm_tree, tmp_path):
    row, ctl, items = run(arm_tree, tmp_path, [entry("allow", component="rm-va-arm")])
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (row, ctl)
    assert items["arm"]["result"] == "pass"
    assert "1 allowed by vendor_allowlist" in items["arm"]["why"] and "unit: allow" in items["arm"]["why"]
    assert "1 finding(s) allowed by vendor_allowlist" in row["detail"]


def test_warn_entry_makes_the_row_warn_and_lists_the_item(arm_tree, tmp_path):
    row, ctl, items = run(arm_tree, tmp_path, [entry("warn", component="rm-va-arm", path_glob="rtl/vendored/*")])
    assert (row["status"], ctl["status"]) == ("WARN", "PASS"), (row, ctl)
    assert items["arm"]["result"] == "pass" and items["arm"]["why"].startswith("WARN:")
    assert "rtl/vendored/pl.v" in items["arm"]["why"]
    assert "1 with warn-covered findings only (vendor_allowlist): arm" in row["detail"]
    # README section 6 today: a WARN row rolls up UNVERIFIED (amber), never PASS and never FAIL.
    (gate,) = [g for g in gates.rollup([row, ctl], "configuration") if g["id"] == "t0.vendor.no-arm-ip"]
    assert gate["verdict"] == "UNVERIFIED" and gate["detail"].startswith("WARN:") and gate["control"] == "fired"


def test_entry_for_another_component_or_path_does_not_cover(arm_tree, tmp_path):
    row, _, items = run(arm_tree, tmp_path, [entry("allow", component="rm-va-super"),
                                             entry("warn", component="rm-va-arm", path_glob="docs/*")], "w1")
    assert row["status"] == "FAIL" and items["arm"]["result"] == "fail"


def test_file_sha_covers_exactly_that_blob(arm_tree, tmp_path):
    row, ctl, _ = run(arm_tree, tmp_path, [entry("allow", file_sha=arm_tree.banner_sha)], "sha-hit")
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), row
    row, _, _ = run(arm_tree, tmp_path, [entry("allow", file_sha="0" * 40)], "sha-miss")
    assert row["status"] == "FAIL"


def test_mutation_matcher_disabled_turns_allow_and_warn_red(arm_tree, tmp_path, monkeypatch):
    """The allow and warn tests above depend on the matcher: with it disabled, both rows are FAIL."""
    monkeypatch.setattr(VendorAllow, "covers", lambda self, *a: False)
    row, _, _ = run(arm_tree, tmp_path, [entry("allow", component="rm-va-arm")], "m1")
    assert row["status"] == "FAIL"
    row, _, _ = run(arm_tree, tmp_path, [entry("warn", component="rm-va-arm")], "m2")
    assert row["status"] == "FAIL"


def test_path_only_allow_wide_enough_for_the_control_disarms_it(arm_tree, tmp_path):
    """The control's scratch superproject names no catalogue component, so a `component` entry never covers its
    planted banner; a path-only entry wide enough to (`path_glob: "*"`) does, the control does not fire, and the
    gate is UNVERIFIED: never a PASS the allowlist bought."""
    row, ctl, _ = run(arm_tree, tmp_path, [entry("allow", component="rm-va-arm"), entry("allow", component="rm-va-super")], "comp")
    assert (row["status"], ctl["status"]) == ("PASS", "PASS")           # component entries: control still fires
    row, ctl, _ = run(arm_tree, tmp_path, [entry("allow", path_glob="*")], "wide")
    assert (row["status"], ctl["status"]) == ("PASS", "FAIL"), (row, ctl)
    (gate,) = [g for g in gates.rollup([row, ctl], "configuration") if g["id"] == "t0.vendor.no-arm-ip"]
    assert gate["verdict"] == "UNVERIFIED"


# -- the pure part ------------------------------------------------------------------------------------------
def res(*findings, verdict="finding"):
    return ScanResult(verdict, [{"rule": "arm.banner", "file": f, "line": 1} for f in findings], 3, {"arm.banner": True})


def test_apply_allowlist_allow_wins_over_warn_and_unverified_is_kept():
    entries = (VendorAllow("arm.banner", "warn", "w", "D", "2026-10-10", path_glob="a/*"),
               VendorAllow("arm.banner", "allow", "a", "D", "2026-10-10", path_glob="a/x.v"))
    out = apply_allowlist(res("a/x.v", "a/y.v", "b/z.v"), entries, None, {})
    assert out.verdict == "finding" and [f["file"] for f in out.findings] == ["b/z.v"]
    assert [f["file"] for f in out.allowed] == ["a/x.v"] and [f["file"] for f in out.warnings] == ["a/y.v"]
    assert out.flagged_files == ["b/z.v"] and "by" in out.warnings[0]
    assert apply_allowlist(res("a/y.v"), entries, None, {}).verdict == "warn"
    assert apply_allowlist(res("a/x.v"), entries, None, {}).verdict == "clean"
    assert apply_allowlist(res("a/x.v", verdict="unverified"), entries, None, {}).verdict == "unverified"
    unsc = ScanResult("finding", [{"rule": "scan.unscannable", "file": "a/x.v", "line": None}], 1, {"arm.banner": True})
    assert apply_allowlist(unsc, entries, None, {}).verdict == "finding"      # unscannable is never covered


def test_vendor_allowlist_reads_the_catalogue_example():
    from pathlib import Path
    cat = yaml.safe_load((Path(__file__).resolve().parents[3] / "schemas/examples/component-catalogue.example.yaml").read_text())
    entries = vendor_allowlist(cat)
    assert {e.severity for e in entries} == {"allow", "warn"}
    assert [e.component for e in entries if e.severity == "allow"] == ["sldma230_tech", "nanosoc-multicore-system"]
    assert next(e for e in entries if e.component == "nanosoc-multicore-system").covers(
        "arm.banner", "nanosoc-multicore-system", "src/rtl/vendored/dma_230/pl230_dma.v", None)
    assert vendor_allowlist({"vendor_allowlist": [{"rule": "arm.banner", "match": {}, "severity": "block"}]}) == ()
