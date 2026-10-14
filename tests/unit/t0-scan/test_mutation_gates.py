"""Mutation-killing tests for t0.gates roll-up, waivers and the gate/control runner (robustness angle: mutation).

Scripted AST mutation over rollup, apply_waivers, _waiver_gates, run_verdict, _safe_check and run_with_control
(2026-10-14) left 29 of 116 mutants alive against test_t0_rollup.py, test_t0_review_fixes.py and
test_vendor_allowlist.py. The local-mode (CR-11) branch of rollup was the largest hole: no test there fed it
anything but the one correct SKIP row. Each test names the mutant it kills."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanosoc_rm.t0 import gates as G

HARD = "t0.runner.hardened"


def _row(gate, status, detail=""):
    return {"utc": "2026-10-14T00:00:00Z", "status": status, "gate": gate, "detail": detail, "nonce": "n"}


def _item(rows, gid, mode="ci"):
    return next(i for i in G.rollup(rows, "configuration", mode) if i["id"] == gid)


def test_unknown_mode_is_refused():
    """Kills `Raise -> pass` on the mode check."""
    with pytest.raises(ValueError, match="unknown run mode"):
        G.rollup([], "configuration", "laptop")


def test_local_mode_exact_skip_is_not_run():
    it = _item([_row(HARD, "SKIP", "local run")], HARD, "local")
    assert it == {"id": HARD, "verdict": "NOT-RUN", "control": "not-applicable", "detail": "local run"}


@pytest.mark.parametrize("label, rows, control, got", [
    ("a PASS can never vouch for the runner", [_row(HARD, "PASS", "local run")], "missing", "got PASS"),
    ("a SKIP with another reason", [_row(HARD, "SKIP", "laptop")], "missing", "got SKIP"),
    ("two SKIP rows", [_row(HARD, "SKIP", "local run")] * 2, "missing", "got SKIP, SKIP"),
    ("a SKIP plus a control row", [_row(HARD, "SKIP", "local run"), _row(HARD + ".negctl", "PASS")],
     "not-fired", "got SKIP"),
    ("no row", [], "missing", "got no row"),
])
def test_local_mode_anything_else_is_unverified(label, rows, control, got):
    """Every condition of the local-mode NOT-RUN test. Kills the negate / and<->or / ==<->!= / `1 -> 2` / drop-not
    mutants of rollup lines 1281-1287 and `continue -> pass`."""
    it = _item(rows, HARD, "local")
    assert (it["verdict"], it["control"]) == ("UNVERIFIED", control), (label, it)
    assert it["detail"] == "local mode: %s must be recorded NOT-RUN (local run), %s" % (HARD, got), (label, it)


def test_ci_mode_does_not_apply_the_local_rule():
    """In ci mode the hardened gate rolls up like any other. Kills `in -> not in` on LOCAL_NOT_RUN."""
    it = _item([_row(HARD, "PASS", "ok"), _row(HARD + ".negctl", "PASS")], HARD)
    assert (it["verdict"], it["control"]) == ("PASS", "fired")
    it = _item([_row(HARD, "SKIP", "local run")], HARD)
    assert it["verdict"] == "UNVERIFIED" and it["detail"].startswith("SKIP on a gate that may never skip")
    # and the local rule does not touch the other gates
    it = _item([_row("t0.pins.fetchable", "PASS", "ok"), _row("t0.pins.fetchable.negctl", "PASS")],
               "t0.pins.fetchable", "local")
    assert it["verdict"] == "PASS"


def test_row_count_details():
    """Kills drop-not on `"no row ..." if not main`."""
    assert _item([], "t0.pins.fetchable")["detail"] == "no row for this gate"
    two = [_row("t0.pins.fetchable", "PASS")] * 2
    assert _item(two, "t0.pins.fetchable")["detail"] == "2 rows for this gate"


def test_skip_details_on_may_skip_and_never_skip_gates():
    """`SKIP without a reason` vs `may never skip` (drop not at 1315), and a reasoned SKIP on a may-skip gate is
    NOT-RUN with control not-applicable even when a control row exists (kills `== -> !=` at the NOT-RUN line)."""
    it = _item([_row("t0.manifest.valid", "SKIP", "  ")], "t0.manifest.valid")
    assert (it["verdict"], it["detail"]) == ("UNVERIFIED", "SKIP without a reason")
    it = _item([_row("t0.pins.fetchable", "SKIP", "why")], "t0.pins.fetchable")
    assert it["detail"] == "SKIP on a gate that may never skip: why"
    it = _item([_row("t0.manifest.valid", "SKIP", "no manifest"), _row("t0.manifest.valid.negctl", "FAIL")],
               "t0.manifest.valid")
    assert (it["verdict"], it["control"]) == ("NOT-RUN", "not-applicable")
    it = _item([_row("t0.manifest.valid", "FAIL", "bad"), _row("t0.manifest.valid.negctl", "FAIL")],
               "t0.manifest.valid")
    assert (it["verdict"], it["control"]) == ("FAIL", "not-fired")


# ---- waivers --------------------------------------------------------------------------------------------------
def _gate(gid, verdict="FAIL"):
    return {"id": gid, "verdict": verdict, "control": "fired", "detail": ""}


def _w(gate, tp, result, who="member-a"):
    return {"gate": gate, "tree_path": tp, "result": result, "reason": "r", "approved_by": who}


ARM, FOUNDRY = "t0.vendor.no-arm-ip", "t0.vendor.no-foundry-data"


def test_an_arm_fail_waiver_never_waives_the_foundry_gate():
    """Only a `not-reached` arm waiver extends to the foundry gate (VS-3). Kills `== -> !=` in _waiver_gates."""
    items = {ARM: [{"tree_path": "x", "result": "fail"}], FOUNDRY: [{"tree_path": "x", "result": "fail"}]}
    out, unused = G.apply_waivers([_gate(ARM), _gate(FOUNDRY)], items, [_w(ARM, "x", "fail")])
    assert [g["verdict"] for g in out] == ["WAIVED", "FAIL"] and unused == []
    items = {ARM: [{"tree_path": "x", "result": "not-reached"}], FOUNDRY: [{"tree_path": "x", "result": "not-reached"}]}
    out, _ = G.apply_waivers([_gate(ARM, "UNVERIFIED"), _gate(FOUNDRY, "UNVERIFIED")], items,
                             [_w(ARM, "x", "not-reached")])
    assert [g["verdict"] for g in out] == ["WAIVED", "WAIVED"]


def test_a_registry_item_stops_the_waiver_even_when_dot_is_covered():
    """An item with no tree_path is never waived. Kills `break -> pass`, which would let the next branch cover a
    not-reached registry item from '.' (or crash on None.startswith)."""
    items = {"t0.pins.fetchable": [{"tree_path": ".", "result": "fail"}, {"tree_path": None, "result": "not-reached"}]}
    out, _ = G.apply_waivers([_gate("t0.pins.fetchable")], items, [_w("t0.pins.fetchable", ".", "fail")])
    assert out[0]["verdict"] == "FAIL"


def test_a_not_reached_item_is_covered_by_its_nearest_covered_ancestor():
    """sorted(..., reverse=True): the LONGEST covering path names `covered_by`. Kills `True -> False`."""
    gid = "t0.pins.fetchable"
    items = {gid: [{"tree_path": "top", "result": "fail"}, {"tree_path": "top/mid", "result": "fail"},
                   {"tree_path": "top/mid/leaf", "result": "not-reached"}]}
    ws = [_w(gid, "top", "fail", "member-a"), _w(gid, "top/mid", "fail", "member-b")]
    out, _ = G.apply_waivers([_gate(gid)], items, ws)
    leaf = out[0]["waived"][2]
    assert (leaf["covered_by"], leaf["approved_by"]) == ("top/mid", "member-b")


# ---- the gate/control runner ----------------------------------------------------------------------------------
def _fake_gate(check, control=lambda ctx: ctx):
    return G.Gate("t0.pins.fetchable", "configuration", ("D2",), False, check, control)


CTX = SimpleNamespace(nonce="n")


def test_safe_check_turns_every_bad_answer_into_unverified():
    """Kills `return (...) -> None` on both branches, `0 -> 1` on splitlines()[0] and the negated `if str(e)`."""
    def boom(ctx):
        raise RuntimeError("first line\nsecond line")
    assert G._safe_check(_fake_gate(boom), CTX) == ("UNVERIFIED", "check raised RuntimeError: first line")

    assert G._safe_check(_fake_gate(lambda c: (_ for _ in ()).throw(ValueError())), CTX) == \
        ("UNVERIFIED", "check raised ValueError: ")
    assert G._safe_check(_fake_gate(lambda c: ("MAYBE", "x")), CTX) == \
        ("UNVERIFIED", "check returned an invalid status 'MAYBE'")
    assert G._safe_check(_fake_gate(lambda c: ("PASS", 7)), CTX) == ("PASS", "7")


def test_run_with_control_names_the_negctl_row_and_cuts_detail_at_300(monkeypatch):
    """Kills `return (row, _row(gate_id + '.negctl' ...)) -> None`, `+ -> -` and `300 -> 301` (both branches)."""
    long = "x" * 400
    calls = []

    def check(ctx):
        calls.append(ctx)
        return ("PASS", "ok") if ctx is CTX else ("FAIL", long)
    monkeypatch.setitem(G.GATES, "t0.pins.fetchable", _fake_gate(check, lambda ctx: SimpleNamespace(nonce="n")))
    row, crow = G.run_with_control("t0.pins.fetchable", CTX)
    assert (row["status"], crow["gate"], crow["status"]) == ("PASS", "t0.pins.fetchable.negctl", "PASS")
    assert crow["detail"] == "fired: on its broken input the gate reported FAIL: " + "x" * 300

    monkeypatch.setitem(G.GATES, "t0.pins.fetchable",
                        _fake_gate(lambda c: ("PASS", long), lambda ctx: SimpleNamespace(nonce="n")))
    _row_, crow = G.run_with_control("t0.pins.fetchable", CTX)
    assert crow["detail"] == "did not fire: on its broken input the gate reported PASS: " + "x" * 300

    def no_control(ctx):
        raise OSError("disk")
    monkeypatch.setitem(G.GATES, "t0.pins.fetchable", _fake_gate(lambda c: ("PASS", "ok"), no_control))
    row, crow = G.run_with_control("t0.pins.fetchable", CTX)
    assert row["status"] == "PASS" and (crow["gate"], crow["status"]) == ("t0.pins.fetchable.negctl", "FAIL")
    assert crow["detail"] == "control input could not be built (OSError: disk): not fired"


def test_local_mode_gives_one_item_per_gate():
    """The local branch ends with `continue`. Kills `continue -> pass` (a second, ci-mode item for the gate)."""
    for rows in ([_row(HARD, "SKIP", "local run")], [_row(HARD, "PASS")]):
        ids = [i["id"] for i in G.rollup(rows, "configuration", "local")]
        assert ids == G.gates_for("configuration")
