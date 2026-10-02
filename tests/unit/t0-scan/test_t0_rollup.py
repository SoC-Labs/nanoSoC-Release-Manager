"""README section 6: rollup, apply_waivers and run_verdict, checked row by row and against the acceptance
file's reference oracles (which share no tool code)."""
from __future__ import annotations

import copy

import pytest

from nanosoc_rm.contracts import validate_observation
from nanosoc_rm.t0 import gates

N = "9c1e4f7a2b6d4e0f8a3c5b7d9e1f2a4c"


def rows(*spec):
    return [{"utc": "2026-10-01T02:00:00Z", "status": st, "gate": g, "detail": d, "nonce": N} for g, st, d in spec]


def one(rs, gid="t0.infra.probe", scope="configuration"):
    return {g["id"]: g for g in gates.rollup(rs, scope)}[gid]


@pytest.mark.parametrize("main, ctl, verdict, control", [
    ("PASS", "PASS", "PASS", "fired"),
    ("PASS", "FAIL", "UNVERIFIED", "not-fired"),
    ("PASS", None, "UNVERIFIED", "missing"),
    ("FAIL", "PASS", "FAIL", "fired"),
    ("UNVERIFIED", "PASS", "UNVERIFIED", "fired"),
    ("WARN", "PASS", "UNVERIFIED", "fired"),
    ("INFRA", None, "INFRA", "missing"),
    ("SKIP", "PASS", "UNVERIFIED", "fired"),          # t0.infra.probe may never skip
])
def test_readme_table(main, ctl, verdict, control):
    spec = [("t0.infra.probe", main, "why")] + ([("t0.infra.probe.negctl", ctl, "c")] if ctl else [])
    g = one(rows(*spec))
    assert (g["verdict"], g["control"]) == (verdict, control)


def test_skip_on_a_gate_that_may_skip_is_not_run_with_its_reason():
    g = one(rows(("t0.manifest.valid", "SKIP", "no manifest anywhere")), "t0.manifest.valid")
    assert (g["verdict"], g["control"], g["detail"]) == ("NOT-RUN", "not-applicable", "no manifest anywhere")
    g = one(rows(("t0.manifest.valid", "SKIP", "")), "t0.manifest.valid")
    assert g["verdict"] == "UNVERIFIED"                     # a SKIP must say why


def test_missing_and_doubled_rows_are_unverified():
    assert one([])["verdict"] == "UNVERIFIED"
    doubled = rows(("t0.infra.probe", "PASS", "a"), ("t0.infra.probe", "PASS", "b"), ("t0.infra.probe.negctl", "PASS", ""))
    assert one(doubled)["verdict"] == "UNVERIFIED"
    two_ctl = rows(("t0.infra.probe", "PASS", "a"), ("t0.infra.probe.negctl", "PASS", ""),
                   ("t0.infra.probe.negctl", "PASS", ""))
    assert (one(two_ctl)["verdict"], one(two_ctl)["control"]) == ("UNVERIFIED", "not-fired")


def test_rollup_equals_the_reference_oracle_on_the_golden_and_its_variants(at):
    golden = at.parse_rows(at.example_verdicts())
    variants = [golden, [r for r in golden if r["gate"] != "t0.infra.probe.negctl"], golden + [dict(golden[0])],
                [dict(r, status="SKIP") for r in golden], [], [r for r in golden if "negctl" not in r["gate"]]]
    for v in variants:
        ours = {g["id"]: (g["verdict"], g["control"]) for g in gates.rollup(v, "configuration")}
        assert ours == at.reference_rollup(v, at.CONFIG_GATES)
        assert gates.run_verdict(gates.rollup(v, "configuration")) == at.reference_run_verdict(ours)
    assert {g["id"]: (g["verdict"], g["control"]) for g in gates.rollup(golden, "configuration")} == at.EXPECTED_RC4_ROLLUP


def test_registry_scope_declares_only_the_registry_gates():
    rolled = gates.rollup(rows(("t0.infra.probe", "PASS", "")), "registry")
    assert [g["id"] for g in rolled] == ["t0.registry.schema", "t0.registry.refs-resolve"]
    assert all(g["verdict"] == "UNVERIFIED" for g in rolled)


def test_run_verdict_ladder():
    g = lambda *vs: [{"verdict": v} for v in vs]
    assert gates.run_verdict(g("PASS", "FAIL", "INFRA")) == ("FAIL", 0)
    assert gates.run_verdict(g("PASS", "UNVERIFIED", "INFRA")) == ("UNVERIFIED", 0)
    assert gates.run_verdict(g("PASS", "INFRA", "WAIVED")) == ("INFRA", 0)
    assert gates.run_verdict(g("PASS", "WAIVED", "NOT-RUN")) == ("WAIVED", 1)
    assert gates.run_verdict(g("PASS", "NOT-RUN")) == ("PASS", 1)
    assert gates.run_verdict(g("NOT-RUN", "NOT-RUN")) == ("UNVERIFIED", 2)    # nothing checked
    assert gates.run_verdict(g("WAIVED")) == ("UNVERIFIED", 0)
    assert gates.run_verdict([]) == ("UNVERIFIED", 0)


def test_waivers_turn_rc4_waived_and_match_the_stored_example(at):
    base = gates.rollup(at.parse_rows(at.example_verdicts()), "configuration")
    before = copy.deepcopy(base)
    items, waivers = at.example_items(), at.example_waivers()
    items_before, waivers_before = copy.deepcopy(items), copy.deepcopy(waivers)
    out, unused = gates.apply_waivers(base, items, waivers)
    assert (base, items, waivers) == (before, items_before, waivers_before)      # pure
    assert unused == []
    import json
    stored = json.loads((at.EX / "observation.t0-result-waived.example.jsonl").read_text())
    assert out == stored["fact"]["gates"]                    # item for item, reasons and covered_by included
    assert gates.run_verdict(out) == ("WAIVED", 2)
    ref, ref_unused = at.reference_apply_waivers(at.reference_rollup(at.parse_rows(at.example_verdicts()),
                                                                     at.CONFIG_GATES), items, waivers)
    assert {g["id"]: (g["verdict"], g["control"]) for g in out} == ref and ref_unused == unused


def test_waivers_never_hide_something_new(at):
    base = gates.rollup(at.parse_rows(at.example_verdicts()), "configuration")
    W = at.example_waivers()
    new = copy.deepcopy(at.example_items())
    new["t0.pins.reachable"].append(dict(at.NEW_FAILURE))
    out = {g["id"]: g for g in gates.apply_waivers(base, new, W)[0]}
    assert out["t0.pins.reachable"]["verdict"] == "FAIL" and "waived" not in out["t0.pins.reachable"]
    orphan = copy.deepcopy(at.example_items())               # a not-reached item with nothing waived above it
    orphan["t0.pins.fetchable"].append({"tree_path": "tidechart/deps/x", "result": "not-reached", "why": "w"})
    assert {g["id"]: g for g in gates.apply_waivers(base, orphan, W)[0]}["t0.pins.fetchable"]["verdict"] == "FAIL"
    wrong_result = [dict(w, result="unverified") if w["tree_path"] == "tidelink" and w["gate"] == "t0.pins.fetchable"
                    else w for w in W]
    out, unused = gates.apply_waivers(base, at.example_items(), wrong_result)
    assert {g["id"]: g for g in out}["t0.pins.fetchable"]["verdict"] == "FAIL"
    assert any(u["tree_path"] == "tidelink" and u["gate"] == "t0.pins.fetchable" for u in unused)


def test_infra_not_run_and_pass_are_never_waived(at):
    W = at.example_waivers()
    items = at.example_items()
    for verdict in ("INFRA", "NOT-RUN", "PASS"):
        g = [{"id": "t0.pins.fetchable", "verdict": verdict, "control": "fired" if verdict != "NOT-RUN" else
              "not-applicable", "detail": "x"}]
        assert gates.apply_waivers(g, items, W)[0][0]["verdict"] == verdict


def test_a_waived_run_is_a_valid_t0_result(at):
    import json
    stored = json.loads((at.EX / "observation.t0-result-waived.example.jsonl").read_text())
    base = gates.rollup(at.parse_rows(at.example_verdicts()), "configuration")
    out, _ = gates.apply_waivers(base, at.example_items(), at.example_waivers())
    doc = copy.deepcopy(stored)
    doc["fact"]["gates"], doc["fact"]["verdict"] = out, gates.run_verdict(out)[0]
    assert validate_observation(doc) == []
    doc["fact"]["gates"] = base
    doc["fact"]["verdict"] = gates.run_verdict(base)[0]
    assert doc["fact"]["verdict"] == "FAIL" and validate_observation(doc) == []


def test_a_not_reached_item_rides_only_on_a_covered_item_above_it_not_on_a_name_prefix(at):
    base = gates.rollup(at.parse_rows(at.example_verdicts()), "configuration")
    sibling = copy.deepcopy(at.example_items())             # "tidelink-extra" is not below "tidelink"
    sibling["t0.pins.fetchable"].append({"tree_path": "tidelink-extra/x", "result": "not-reached", "why": "w"})
    out = {g["id"]: g for g in gates.apply_waivers(base, sibling, at.example_waivers())[0]}
    assert out["t0.pins.fetchable"]["verdict"] == "FAIL"
