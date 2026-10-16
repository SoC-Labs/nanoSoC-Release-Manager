"""web.verdict: the deciding gate (Wave C spec 2.2). Fixtures follow the SHAPES of the eth-rc4 and eth-main compat
results (synthetic commits, example.invalid URLs)."""
from __future__ import annotations

import copy

from nanosoc_rm.web import verdict as V

ITEMS_OK = {"fetchable": "pass", "reachable": "pass", "no_ssh": "pass", "vendor": "pass"}
BELOW = ("tidelink/deps/axi-chiplet-controller", "tidelink/deps/axi-chiplet-controller/soctools_flow",
         "tidelink/deps/axi-chiplet-controller/asic_flow", "tidelink/deps/tidelink-gpio-phy", "tidelink/deps/tidelink-phy")


def pin(path, comp=None, url_kind="https", **items):
    return {"tree_path": path, "tree_paths": [path], "component": comp or path.rsplit("/", 1)[-1],
            "commit": "%040x" % (abs(hash(path)) % (1 << 64)), "overridden": False, "replaces": None,
            "url_kind": url_kind, "state": "ON_DEFAULT", "split": False, "items": dict(ITEMS_OK, **items)}


def gate(gid, verdict, detail="", **kw):
    return dict({"id": gid, "verdict": verdict, "control": "fired", "detail": detail}, **kw)


def doc(cid, gates, pins, verdict="FAIL", tier=None):
    f = {"configuration": cid, "gates": gates, "pins": pins, "verdict": verdict}
    if tier is not None:
        f["tier"] = tier
        f["run_verdict"] = "FAIL" if verdict == "FAIL" else tier["verdict"]
    return {"kind": "compat_result", "subject": "compat_result:%s:n1" % cid, "fact": f}


def eth_main():
    pins = [pin("tidelink", url_kind="ssh", fetchable="fail", no_ssh="fail", vendor="not-reached"),
            pin("nanosoc-multicore-system", vendor="fail"), pin("tidechart")]
    pins += [pin(p, fetchable="not-reached", reachable="pass", no_ssh="pass", vendor="not-reached") for p in BELOW]
    gates = [gate("t0.infra.probe", "PASS"), gate("t0.runner.hardened", "NOT-RUN"),
             gate("t0.pins.fetchable", "FAIL", "37 of 43 fetched; not fetched: tidelink; 5 below not reached"),
             gate("t0.pins.reachable", "PASS"),
             gate("t0.urls.no-ssh-public", "FAIL", "pins by ssh URL: tidelink"),
             gate("t0.manifest.valid", "NOT-RUN"),
             gate("t0.clone.upgrade-path", "FAIL", "anonymous recursive clone of upgrade_from ab73e40 does not "
                                                   "resolve (auth): ASIC/asic-toolkit, tidelink"),
             gate("t0.vendor.no-arm-ip", "FAIL", "1 with findings: nanosoc-multicore-system")]
    tier = {"tier": "t1", "verdict": "INFRA", "reason": "make lint: the tool does not exist; tier not run",
            "forced": True, "rows": []}
    return doc("eth-main", gates, pins, tier=tier)


def eth_rc4():
    waived = lambda tp: {"tree_path": tp, "result": "fail", "covered_by": tp, "reason": "ssh in frozen tree",
                         "approved_by": "member-a"}
    pins = [pin("ASIC/asic-toolkit", url_kind="ssh", fetchable="fail", reachable="unverified", no_ssh="fail",
                vendor="not-reached"),
            pin("tidelink", url_kind="ssh", fetchable="fail", no_ssh="fail", vendor="not-reached"),
            pin("nanosoc-multicore-system", vendor="fail")]
    pins += [pin(p, fetchable="not-reached", vendor="not-reached") for p in BELOW]
    gates = [gate("t0.infra.probe", "PASS"),
             gate("t0.pins.fetchable", "WAIVED", raw_verdict="FAIL",
                  waived=[waived("tidelink"), waived("ASIC/asic-toolkit")]),
             gate("t0.pins.reachable", "WAIVED", raw_verdict="UNVERIFIED", waived=[waived("ASIC/asic-toolkit")]),
             gate("t0.urls.no-ssh-public", "WAIVED", raw_verdict="FAIL",
                  waived=[waived("tidelink"), waived("ASIC/asic-toolkit")]),
             gate("t0.manifest.valid", "NOT-RUN"), gate("t0.clone.upgrade-path", "NOT-RUN"),
             gate("t0.vendor.no-arm-ip", "FAIL", "38 of 45 trees scanned; 1 with findings: nanosoc-multicore-system")]
    return doc("eth-rc4", gates, pins)


def test_eth_rc4_is_decided_by_the_vendor_scan_and_waived_gates_never_decide():
    d = V.deciding(eth_rc4())
    assert (d.verdict, d.gate, d.kind) == ("FAIL", "t0.vendor.no-arm-ip", "vendor")
    assert d.label == "Arm source in nanosoc-multicore-system"
    assert d.sentence == "FAIL: Arm source in nanosoc-multicore-system"
    assert not {"t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public"} & set(d.same_cause_gates + d.separate_gates)
    assert d.infra is False
    a = V.next_action(d, "eth-rc4")
    assert a["label"] == "Show that tree" and a["href"] == "/configurations/eth-rc4/tree?node=nanosoc-multicore-system"


def test_eth_main_is_one_ssh_pin_cause_covering_fetchable_and_no_ssh():
    d = V.deciding(eth_main())
    assert d.kind == "ssh-pin" and d.label == "tidelink pinned by ssh URL" and d.component == "tidelink"
    assert d.same_cause_gates == ("t0.pins.fetchable", "t0.urls.no-ssh-public") and d.gate == "t0.pins.fetchable"
    assert d.not_reached == 5
    assert set(d.separate_gates) == {"t0.clone.upgrade-path", "t0.vendor.no-arm-ip"}
    assert d.infra is True and d.infra_text.startswith("This host cannot run T1. Nothing to fix in the design.")
    labels = [c.label for c in V.causes(eth_main())]
    assert labels[0] == "tidelink pinned by ssh URL"
    assert "upgrade path from ab73e40 does not clone anonymously" in labels
    a = V.next_action(d, "eth-main", https_url="https://example.invalid/org/tidelink.git", doc=eth_main())
    assert a["label"] == "Try the fix in What-if" and a["href"] == "/configurations/eth-main/whatif?component=tidelink"
    assert a["code"] == ("git config -f .gitmodules submodule.tidelink.url https://example.invalid/org/tidelink.git\n"
                         "git submodule sync tidelink")


def test_a_pass_has_no_deciding_gate_and_no_action():
    d = V.deciding(doc("x", [gate("t0.pins.fetchable", "PASS")], [pin("a")], verdict="PASS"))
    assert (d.gate, d.label, d.kind) == (None, "PASS", "none") and V.next_action(d, "x") is None
    w = V.deciding(doc("x", [gate("t0.pins.fetchable", "WAIVED", raw_verdict="FAIL")], [pin("a")], verdict="WAIVED"))
    assert w.gate is None and w.label == "WAIVED"


def test_no_result_asks_for_a_first_check():
    d = V.deciding(None)
    assert d.verdict == V.NO_RESULT and d.label == "never checked"
    assert V.next_action(d, "multicore-master") == {"label": "Run first check", "method": "post", "code": None,
                                                     "href": "/configurations/multicore-master/runs"}


def test_a_tie_goes_to_the_cause_hiding_more_pins_then_to_gate_order():
    pins = [pin("a", reachable="fail"), pin("b", manifest="fail"), pin("b/c", vendor="not-reached"),
            pin("b/d", vendor="not-reached")]
    d = V.deciding(doc("x", [gate("t0.pins.reachable", "FAIL"), gate("t0.manifest.valid", "FAIL")], pins))
    assert d.gate == "t0.manifest.valid" and d.not_reached == 2 and d.label == "manifest invalid in b"
    pins = [pin("a", reachable="fail"), pin("b", manifest="fail")]
    d = V.deciding(doc("x", [gate("t0.manifest.valid", "FAIL"), gate("t0.pins.reachable", "FAIL")], pins))
    assert d.gate == "t0.pins.reachable" and d.label == "1 pins not reachable"


def test_same_failing_paths_without_an_ssh_pin_stay_separate_causes():
    pins = [pin("a", fetchable="fail", no_ssh="fail")]             # an https pin: no "pinned by ssh" cause
    d = V.deciding(doc("x", [gate("t0.pins.fetchable", "FAIL"), gate("t0.urls.no-ssh-public", "FAIL")], pins))
    assert d.kind != "ssh-pin" and d.same_cause_gates == ("t0.pins.fetchable",)


def test_unverified_counts_after_fail_and_the_tier_only_when_t0_is_green():
    pins = [pin("a", reachable="unverified"), pin("b", vendor="fail")]
    d = V.deciding(doc("x", [gate("t0.pins.reachable", "UNVERIFIED"), gate("t0.vendor.no-arm-ip", "FAIL")], pins))
    assert d.gate == "t0.vendor.no-arm-ip"
    t = {"tier": "t2", "verdict": "FAIL", "reason": "exit 2", "forced": False, "rows": []}
    green = doc("x", [gate("t0.pins.fetchable", "PASS")], [pin("a")], verdict="PASS", tier=t)
    green["fact"]["run_verdict"] = "FAIL"
    d = V.deciding(green)
    assert d.gate == "tier.t2" and d.verdict == "FAIL"


def test_an_unknown_gate_falls_back_to_its_id():
    d = V.deciding(doc("x", [gate("t0.future.gate", "FAIL")], [pin("a")]))
    assert d.label == "t0.future.gate" and d.gate == "t0.future.gate"


def test_an_infra_only_run_is_not_a_design_failure():
    t = {"tier": "t1", "verdict": "INFRA", "reason": "no tool", "forced": False, "rows": []}
    d = V.deciding(doc("x", [gate("t0.pins.fetchable", "PASS")], [pin("a")], verdict="PASS", tier=t))
    assert d.verdict == "INFRA" and d.infra and d.kind == "infra" and "Nothing to fix in the design" in d.infra_text


def test_unused_waivers_are_those_no_gate_used():
    # A t0_waiver always names its gate (eligibility schema); unused is keyed on (gate, tree_path) (ROB-mutation-2).
    rec = {"fact": {"record": {"frozen": True, "t0_waivers": [{"gate": "t0.pins.fetchable", "tree_path": "tidelink"},
                                                             {"gate": "t0.pins.fetchable", "tree_path": "gone/path"}]}}}
    assert V.unused_waivers(eth_rc4(), rec) == [{"gate": "t0.pins.fetchable", "tree_path": "gone/path"}]
    unfrozen = copy.deepcopy(rec)
    unfrozen["fact"]["record"]["frozen"] = False
    assert V.unused_waivers(eth_rc4(), unfrozen) == []


def test_a_nested_ssh_pin_names_its_submodule_relative_to_its_parent():
    pins = [pin("top"), pin("top/sub", comp="sub", url_kind="ssh", fetchable="fail", no_ssh="fail")]
    dd = doc("x", [gate("t0.pins.fetchable", "FAIL"), gate("t0.urls.no-ssh-public", "FAIL")], pins)
    a = V.next_action(V.deciding(dd), "x", doc=dd)
    assert "submodule.sub.url <https URL of sub>" in a["code"] and a["code"].endswith("git submodule sync sub")


def test_the_upgrade_path_label_names_the_registry_tag_not_a_dict():
    rec = {"fact": {"record": {"upgrade_from": {"tag": "v1.0.0"}}}}
    labels = [c.label for c in V.causes(eth_main(), rec)]
    assert "upgrade path from v1.0.0 does not clone anonymously" in labels, labels
    rec = {"fact": {"record": {"upgrade_from": {"commit": "ab73e40924317e45e7b3b941f52a7ed35f20832e"}}}}
    assert "upgrade path from ab73e40 does not clone anonymously" in [c.label for c in V.causes(eth_main(), rec)]


def test_a_vendor_gate_red_only_for_unfetched_trees_does_not_claim_a_finding():
    """Review VS-1: an UNVERIFIED vendor gate whose items are all pass or not-reached found nothing. Its label
    names the trees it could not scan, never "Arm source in the tree"."""
    pins = [pin("."), pin("tidelink", url_kind="ssh", fetchable="fail", no_ssh="fail", vendor="not-reached",
                          foundry="not-reached")]
    pins += [pin(p, fetchable="not-reached", vendor="not-reached", foundry="not-reached") for p in BELOW]
    for gid in ("t0.vendor.no-arm-ip", "t0.vendor.no-foundry-data"):
        d = doc("eth-main", [gate(gid, "UNVERIFIED", "38 of 45 trees scanned; 6 not fetched")], pins,
                verdict="UNVERIFIED")
        (c,) = V.causes(d)
        assert c.verdict == "UNVERIFIED" and c.kind == "vendor"
        assert "Arm source" not in c.label and "marked blocking" not in c.label, c.label
        assert c.label == "%d trees not scanned (not fetched)" % (1 + len(BELOW)), c.label


def test_a_vendor_gate_label_says_unverified_when_no_item_failed():
    pins = [pin("."), pin("nanosoc-multicore-system", vendor="unverified")]
    (c,) = V.causes(doc("x", [gate("t0.vendor.no-arm-ip", "UNVERIFIED")], pins, verdict="UNVERIFIED"))
    assert c.label == "Arm source scan unverified in nanosoc-multicore-system", c.label
    pins = [pin("."), pin("a", vendor="fail"), pin("b", vendor="unverified")]
    (c,) = V.causes(doc("x", [gate("t0.vendor.no-arm-ip", "FAIL")], pins))
    assert c.label == "Arm source in a", c.label


def test_an_unverified_run_has_a_deciding_cause_and_a_red_dot():
    """Evaluation 2026-10-16 (mutant M5: UNVERIFIED added to GREEN survived 873 tests). deciding() on a doc whose
    verdict is UNVERIFIED must name the gate that left it unverified, never answer "no red gate", and the rail
    must give it the red dot."""
    from nanosoc_rm.web import shell
    pins = [pin("."), pin("nanosoc-multicore-system", vendor="unverified")]
    d = doc("x", [gate("t0.pins.fetchable", "PASS"), gate("t0.vendor.no-arm-ip", "UNVERIFIED")], pins,
            verdict="UNVERIFIED")
    dec = V.deciding(d)
    assert dec.verdict == "UNVERIFIED" and dec.gate == "t0.vendor.no-arm-ip", dec
    assert dec.label == "Arm source scan unverified in nanosoc-multicore-system", dec.label
    assert shell.rail_dot(dec.verdict) == "d-fail"
    assert not set(V.GREEN) & {"UNVERIFIED", "FAIL", "INFRA", V.NO_RESULT}
    # the control: a green verdict has no cause and a green dot; no result is grey
    ok = V.deciding(doc("x", [gate("t0.pins.fetchable", "PASS")], [pin(".")], verdict="PASS"))
    assert ok.gate is None and shell.rail_dot(ok.verdict) == "d-pass"
    assert shell.rail_dot(V.NO_RESULT) == "d-notrun" and shell.rail_dot("INFRA") == "d-notrun"
