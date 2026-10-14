"""Coverage angle (rob/coverage): the D9 comparison and the D7/D8 helpers of nanosoc_rm.matrix.cells as pure
functions (no compat run, no store).

compare() labels each gate same / new-failure / fixed / changed against the same night's pinned run. "fixed" must
mean the cell checked the gate and it no longer fails; a gate the cell did not answer (INFRA, NOT-RUN) or did not
report at all is not evidence that anything was fixed."""
from __future__ import annotations

import datetime as dt

import pytest

from nanosoc_rm import clock
from nanosoc_rm.matrix import cells as M

T0 = "2026-10-14T01:00:00Z"
CC = "c" * 40


def _fact(column="in-range", verdict="PASS", gates=None, started=T0, tier=None, commit=CC, moved=None, nonce="n1"):
    f = {"configuration": "eth", "component": "ip", "column": column, "nonce": nonce, "verdict": verdict,
         "started_at": started, "finished_at": started, "configuration_commit": commit,
         "attempts": [{"gates": [{"id": k, "verdict": v} for k, v in (gates or {}).items()]}] if gates is not None
         else [], "provenance": {"moved": moved or []}}
    if tier:
        f["tier"] = tier
    return f


def _doc(fact, subject="matrix_cell:eth:-:pinned:n0"):
    return {"subject": subject, "fact": fact}


def _shift(hours):
    return clock.iso(clock.parse(T0) + dt.timedelta(hours=hours))


def _changes(cell_gates, base_gates):
    base = _doc(_fact("pinned", gates=base_gates, nonce="b" * 16))
    out = M.compare(_fact(gates=cell_gates), base)
    assert out["note"] is None and out["baseline"]["nonce"] == "b" * 16
    return {r["gate"]: r["change"] for r in out["rows"]}


def test_compare_labels_each_gate():
    assert _changes({"a": "PASS", "b": "FAIL", "c": "PASS", "d": "WARN", "e": "UNVERIFIED"},
                    {"a": "PASS", "b": "PASS", "c": "FAIL", "d": "PASS", "e": "FAIL"}) == \
        {"a": "same", "b": "new-failure", "c": "fixed", "d": "changed", "e": "changed"}
    assert _changes({"new": "FAIL"}, {"old": "PASS"}) == {"new": "new-failure", "old": "changed"}   # added, dropped


def test_compare_marks_quarantined_gates_and_explains_no_baseline():
    base = _doc(_fact("pinned", gates={"a": "PASS"}))
    rows = M.compare(_fact(gates={"a": "FAIL"}), base, quarantined={"a"})["rows"]
    assert rows == [{"gate": "a", "baseline": "PASS", "cell": "FAIL", "change": "new-failure", "quarantined": True}]
    none = M.compare(_fact(gates={"a": "FAIL"}), None)
    assert none["baseline"] is None and none["rows"] == [] and "12 h" in none["note"]


def test_compare_refuses_a_baseline_without_gates_or_with_moved_pins():
    for base in (_fact("pinned", verdict="INFRA", gates=None), _fact("pinned", gates={"a": "PASS"}, moved=["x"])):
        out = M.compare(_fact(gates={"a": "FAIL"}), _doc(base))
        assert out["rows"] == [] and "cannot be compared" in out["note"] and out["baseline"]["verdict"]


@pytest.mark.xfail(strict=True, reason="ROB-coverage-6: compare() reports a gate the cell did not answer (INFRA, "
                                       "NOT-RUN) or did not report as 'fixed' when the baseline FAILed "
                                       "(matrix/cells.py compare: 'fixed' if b in bad and c not in bad)")
@pytest.mark.parametrize("cell", ["INFRA", "NOT-RUN", None])
def test_an_unanswered_gate_is_never_fixed(cell):
    cell_gates = {"a": cell} if cell else {}
    assert _changes(cell_gates, {"a": "FAIL"})["a"] != "fixed"


def test_baseline_is_the_nearest_usable_pinned_run_of_the_same_tier_and_commit():
    cell = _fact()
    near_unusable = _doc(_fact("pinned", verdict="INFRA", gates=None, started=_shift(0.5)), "near-unusable")
    usable = _doc(_fact("pinned", gates={"a": "PASS"}, started=_shift(-3)), "usable")
    far = _doc(_fact("pinned", gates={"a": "PASS"}, started=_shift(-13)), "far")
    other_tier = _doc(_fact("pinned", gates={"a": "PASS"}, started=_shift(0.1), tier="t1"), "t1")
    other_commit = _doc(_fact("pinned", gates={"a": "PASS"}, started=_shift(0.1), commit="d" * 40), "commit")
    not_run = _doc(_fact("pinned", verdict="NOT-RUN", gates={"a": "PASS"}, started=_shift(0.1)), "not-run")
    in_range = _doc(_fact("in-range", gates={"a": "PASS"}, started=_shift(0.1)), "in-range")
    stored = [near_unusable, usable, far, other_tier, other_commit, not_run, in_range]
    assert M.baseline_for(cell, stored)["subject"] == "usable"
    assert M.baseline_for(cell, [near_unusable, far])["subject"] == "near-unusable"   # named, then not compared
    assert M.baseline_for(cell, [far, other_commit, in_range]) is None
    assert M.baseline_for(_fact(tier="t1"), stored)["subject"] == "t1"
    assert M.baseline_for(_fact("pinned"), stored) is None                           # a pinned cell has none


def test_classify_infra_needs_every_failing_gate_to_be_the_hosts():
    def res(verdict, *details):
        return {"fact": {"verdict": verdict, "gates": [{"id": "g%d" % i, "verdict": "FAIL", "detail": d}
                                                       for i, d in enumerate(details)]
                         + [{"id": "ok", "verdict": "PASS", "detail": "fine"}]}}
    assert M.classify_infra(res("PASS", "timed out")) is None
    assert "INFRA, not FAIL" in M.classify_infra(res("FAIL", "git timed out after 60 s", "Out of memory"))
    assert M.classify_infra(res("FAIL", "git timed out", "rtl/watchdog_timeout.v holds arm text")) is None
    assert M.classify_infra({"fact": {"verdict": "FAIL", "gates": []}}) is None      # no failing gate: not INFRA
    assert M.classify_infra(res("FAIL", None)) is None


def test_active_quarantine_and_exit_codes():
    now = clock.parse(T0)
    e = [{"fact": {"test": "a", "expires_at": _shift(1)}}, {"fact": {"test": "b", "expires_at": T0}}]
    assert set(M.active_quarantine(e, now)) == {"a"}                                  # expiry is exclusive
    assert [M.exit_for(v) for v in ("PASS", "WARN", "WAIVED", "FAIL", "FLAKY", "UNVERIFIED", "INFRA", "NOT-RUN")] \
        == [0, 0, 0, 1, 1, 2, 2, 2]
    assert M.subject_of_cell({"configuration": "eth", "component": None, "column": "head", "nonce": "x"}) \
        == "matrix_cell:eth:-:head:x"
    assert M.tier_of({"tier": ""}) is None and M.tier_of({"tier": "t1"}) == "t1"


# ----- columns: which pins a cell moves ----------------------------------------------------------------------
from nanosoc_rm.matrix import columns as COL  # noqa: E402


def _row(*pins):
    return COL.Row("ip", [{"tree_path": tps[0], "tree_paths": list(tps), "commit": c} for c, tps in pins], False, True)


def test_overrides_move_only_outer_paths_not_already_at_the_commit():
    new = "n" * 40
    row = _row(("o" * 40, ["a/ip", "b/ip"]), ("o" * 40, ["a/ip/sub/ip"]), (new, ["c/ip"]), ("p" * 40, ["a/ip2"]))
    assert COL.overrides_for(row, new) == {"a/ip": new, "b/ip": new, "a/ip2": new}   # a/ip/sub/ip rides on a/ip
    assert COL.overrides_for(_row((new, ["x"])), new) == {}
    assert row.tree_paths == ["a/ip", "b/ip", "a/ip/sub/ip", "c/ip", "a/ip2"]


def test_same_as_pinned_and_root_commit():
    one = _row(("o" * 40, ["a"]), ("o" * 40, ["b"]))
    assert COL.same_as_pinned(one, "o" * 40) and not COL.same_as_pinned(one, None)
    assert not COL.same_as_pinned(_row(("o" * 40, ["a"]), ("p" * 40, ["b"])), "o" * 40)       # split: not the same
    tree = [{"fact": {"depth": 2, "parent": {"commit": "x"}}}, {"fact": {"depth": 1, "parent": {"commit": "r"}}}]
    assert COL.root_commit(tree, {"commit": "frozen"}) == "frozen"
    assert COL.root_commit(tree, {}) == "r" and COL.root_commit([], {}) is None
