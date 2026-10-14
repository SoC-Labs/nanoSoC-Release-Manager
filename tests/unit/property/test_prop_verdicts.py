"""Property tests: verdicts.tsv rows, the T0 roll-up, the run verdict ladder and T0 waivers (README section 6).

Rows are a wire format between the runner, the verifier and the CLI: what the runner writes, both readers must
read back field for field. The roll-up is a pure function of a SET of rows (order never matters, rows of
undeclared gates never matter, PASS needs exactly one PASS row and one fired control). The run verdict is
monotonic in the ladder FAIL > UNVERIFIED > INFRA > WAIVED > WARN > PASS and order-free. Waivers are
order-free, monotonic (another waiver never un-waives a gate), pure, and only ever turn FAIL/UNVERIFIED with a
fired control into WAIVED.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from prop_helpers import example, given, st  # loads the profile; skips without hypothesis
from click.testing import CliRunner

from nanosoc_rm import cli, contracts
from nanosoc_rm.ci import verify
from nanosoc_rm.t0 import gates as G

NONCE = "0123456789abcdef0123456789abcdef"
UTC = "2026-10-14T09:00:00Z"

# ---------------------------------------------------------------------------------------------------------
# verdicts.tsv rows
# ---------------------------------------------------------------------------------------------------------
FIELD = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\t\n"), max_size=12)
ANY_DETAIL = st.text(st.characters(blacklist_categories=("Cs",)), max_size=40)


@given(st.tuples(FIELD, FIELD, FIELD, FIELD, FIELD))
def test_parse_verdict_row_round_trips_five_fields(fields):
    row = contracts.parse_verdict_row("\t".join(fields))
    assert tuple(row[c] for c in contracts.VERDICT_COLUMNS) == fields


@given(st.lists(FIELD, max_size=8).filter(lambda f: len(f) != 5))
def test_any_other_field_count_is_refused(fields):
    with pytest.raises(ValueError):
        contracts.parse_verdict_row("\t".join(fields))


@given(ANY_DETAIL)
def test_clean_detail_has_no_tab_or_newline_and_is_idempotent(detail):
    d = G.clean_detail(detail)
    assert "\t" not in d and "\n" not in d and "\r" not in d
    assert G.clean_detail(d) == d
    assert contracts.validate_verdict_row({"utc": UTC, "status": "PASS", "gate": "t0.infra.probe", "detail": d,
                                           "nonce": NONCE}) == []


def _write_rows(bundle: Path, details: list[str]) -> list[dict]:
    rows = [{"utc": UTC, "status": "PASS", "gate": "t0.infra.probe", "detail": G.clean_detail(d), "nonce": NONCE}
            for d in details]
    with open(bundle / "verdicts.tsv", "w") as f:                        # exactly as t0/runner.py:173 writes
        for r in rows:
            f.write("\t".join(r[k] for k in ("utc", "status", "gate", "detail", "nonce")) + "\n")
    return rows


@given(st.lists(ANY_DETAIL, min_size=1, max_size=4))
def test_the_verifier_reads_back_what_the_runner_wrote(tmp_path_factory, details):
    bundle = tmp_path_factory.mktemp("bundle")
    written = _write_rows(bundle, details)
    assert verify.read_rows(bundle) == written


# ---------------------------------------------------------------------------------------------------------
# Roll-up
# ---------------------------------------------------------------------------------------------------------
CONFIG = G.gates_for("configuration")
STATUSES = ["PASS", "FAIL", "UNVERIFIED", "WARN", "SKIP", "INFRA", "BOGUS"]
GATE_NAMES = st.sampled_from(CONFIG + [g + ".negctl" for g in CONFIG] + ["t0.not.declared", "t0.registry.schema"])
ROW = st.builds(lambda g, s, d: {"utc": UTC, "status": s, "gate": g, "detail": d, "nonce": NONCE},
                GATE_NAMES, st.sampled_from(STATUSES), st.sampled_from(["", " ", "why", "local run"]))
ROWS = st.lists(ROW, max_size=14)


@given(ROWS, st.randoms(), st.sampled_from(G.MODES))
def test_rollup_is_one_item_per_declared_gate_and_order_free(rows, rnd, mode):
    out = G.rollup(rows, "configuration", mode)
    assert [g["id"] for g in out] == CONFIG
    shuffled = list(rows)
    rnd.shuffle(shuffled)
    again = G.rollup(shuffled, "configuration", mode)
    if mode == "ci":
        assert again == out
    else:   # local mode's UNVERIFIED detail lists the statuses in row order: only the text may differ
        assert [(g["id"], g["verdict"], g["control"]) for g in again] == \
               [(g["id"], g["verdict"], g["control"]) for g in out]


@given(ROWS, st.lists(ROW.map(lambda r: dict(r, gate="t0.undeclared." + r["gate"])), max_size=4))
def test_rows_of_undeclared_gates_change_nothing(rows, junk):
    assert G.rollup(rows + junk, "configuration") == G.rollup(rows, "configuration")


@given(ROWS)
def test_a_gate_passes_only_on_one_pass_row_and_one_fired_control(rows):
    for g in G.rollup(rows, "configuration"):
        main = [r for r in rows if r["gate"] == g["id"]]
        ctl = [r for r in rows if r["gate"] == g["id"] + ".negctl"]
        if g["verdict"] in ("PASS", "WARN"):
            assert [r["status"] for r in main] == [g["verdict"]] and [r["status"] for r in ctl] == ["PASS"]
            assert g["control"] == "fired"
        if g["verdict"] == "FAIL":
            assert [r["status"] for r in main] == ["FAIL"]
        if g["verdict"] == "NOT-RUN":
            assert G.GATES[g["id"]].may_skip and len(main) == 1 and main[0]["status"] == "SKIP"
            assert main[0]["detail"].strip() and g["control"] == "not-applicable"
        if len(main) != 1:
            assert g["verdict"] == "UNVERIFIED"


@given(ROWS)
def test_local_mode_never_vouches_for_the_hardened_runner(rows):
    out = {g["id"]: g for g in G.rollup(rows, "configuration", "local")}
    assert out["t0.runner.hardened"]["verdict"] in ("NOT-RUN", "UNVERIFIED")


# ---------------------------------------------------------------------------------------------------------
# Run verdict
# ---------------------------------------------------------------------------------------------------------
LADDER = ["PASS", "WARN", "WAIVED", "INFRA", "UNVERIFIED", "FAIL"]          # better to worse
RANK = {v: i for i, v in enumerate(LADDER)}
VERDICTS = st.lists(st.sampled_from(LADDER + ["NOT-RUN"]), min_size=1, max_size=8)


def run(vs: list[str]) -> str:
    return G.run_verdict([{"id": "g%d" % i, "verdict": v} for i, v in enumerate(vs)])[0]


@given(VERDICTS, st.randoms())
def test_run_verdict_is_order_free_and_counts_not_run(vs, rnd):
    shuffled = list(vs)
    rnd.shuffle(shuffled)
    assert run(shuffled) == run(vs)
    assert G.run_verdict([{"verdict": v} for v in vs])[1] == vs.count("NOT-RUN")


@given(VERDICTS, st.data())
def test_a_worse_gate_never_gives_a_better_run(vs, data):
    i = data.draw(st.sampled_from([n for n, v in enumerate(vs) if v != "NOT-RUN"] or [None]))
    if i is None:
        assert run(vs) == "UNVERIFIED"                 # nothing was checked
        return
    worse = data.draw(st.sampled_from(LADDER[RANK[vs[i]]:]))
    assert RANK[run(vs[:i] + [worse] + vs[i + 1:])] >= RANK[run(vs)]


@given(VERDICTS)
def test_the_run_is_never_better_than_its_worst_gate(vs):
    ranked = [RANK[v] for v in vs if v != "NOT-RUN"]
    assert RANK[run(vs)] >= (max(ranked) if ranked else RANK["UNVERIFIED"])


# ---------------------------------------------------------------------------------------------------------
# Waivers
# ---------------------------------------------------------------------------------------------------------
TREE = st.sampled_from([".", "a", "a/b", "a/b/c", "ab", "c"])
RESULT = st.sampled_from(["pass", "fail", "unverified", "not-reached"])
ITEM = st.one_of(st.builds(lambda t, r: {"tree_path": t, "result": r, "why": "w"}, TREE, RESULT),
                 st.just({"file": "catalogue.yaml", "pointer": "/x", "result": "fail", "why": "w"}))
ITEMS = st.dictionaries(st.sampled_from(CONFIG), st.lists(ITEM, max_size=4), max_size=4)
WAIVER = st.builds(lambda g, t, r, who: {"gate": g, "tree_path": t, "result": r, "reason": "r-%s" % who,
                                         "approved_by": who, "approved_on": "2026-10-01"},
                   st.sampled_from(CONFIG), TREE, st.sampled_from(["fail", "unverified", "not-reached"]),
                   st.sampled_from(["ann", "bob"]))
WAIVERS = st.lists(WAIVER, max_size=6)
GATE = st.builds(lambda g, v, c: {"id": g, "verdict": v, "control": c, "detail": "d"},
                 st.sampled_from(CONFIG), st.sampled_from(LADDER[:2] + LADDER[3:] + ["NOT-RUN"]),
                 st.sampled_from(["fired", "not-fired", "missing", "not-applicable"]))
GATES = st.lists(GATE, min_size=1, max_size=6, unique_by=lambda g: g["id"])


def summary(out: list[dict]) -> list:
    return [(g["id"], g["verdict"], g.get("raw_verdict"),
             [(w["tree_path"], w["result"], w["covered_by"]) for w in g.get("waived", [])]) for g in out]


@given(GATES, ITEMS, WAIVERS)
def test_waivers_are_pure_and_only_waive_a_red_gate_with_a_fired_control(gates, items, waivers):
    before = copy.deepcopy((gates, items, waivers))
    out, unused = G.apply_waivers(gates, items, waivers)
    assert (gates, items, waivers) == before
    for g, o in zip(gates, out):
        if o["verdict"] == "WAIVED":
            assert g["verdict"] in ("FAIL", "UNVERIFIED") and g["control"] == "fired"
            bad = [i for i in items.get(g["id"], []) if i["result"] != "pass"]
            assert bad and len(o["waived"]) == len(bad)
        else:
            assert o == g
    assert all(u in waivers for u in unused)


@given(st.sampled_from(CONFIG), st.sampled_from(["FAIL", "UNVERIFIED", "PASS", "WARN", "INFRA", "NOT-RUN"]),
       st.sampled_from(["fired", "not-fired", "missing", "not-applicable"]),
       st.lists(st.tuples(TREE, st.sampled_from(["fail", "unverified"])), min_size=1, max_size=4, unique=True))
def test_fully_covered_items_waive_exactly_a_red_gate_with_a_fired_control(gid, verdict, control, bad):
    """Every non-pass item has its own waiver: WAIVED iff the gate is FAIL/UNVERIFIED and its control fired."""
    gate = {"id": gid, "verdict": verdict, "control": control, "detail": "d"}
    items = {gid: [{"tree_path": t, "result": r, "why": "w"} for t, r in bad]}
    waivers = [{"gate": gid, "tree_path": t, "result": r, "reason": "r", "approved_by": "ann",
                "approved_on": "2026-10-01"} for t, r in bad]
    out, unused = G.apply_waivers([gate], items, waivers)
    want = verdict in ("FAIL", "UNVERIFIED") and control == "fired"
    assert (out[0]["verdict"] == "WAIVED") == want and unused == []
    if want:
        assert out[0]["raw_verdict"] == verdict and len(out[0]["waived"]) == len(bad)
        out2, unused2 = G.apply_waivers([gate], items, waivers[1:])        # one item left uncovered
        assert out2[0]["verdict"] == verdict and unused2 == []          # the rest still matched items


@given(GATES, ITEMS, WAIVERS, st.randoms())
def test_waivers_are_order_free(gates, items, waivers, rnd):
    shuffled = list(waivers)
    rnd.shuffle(shuffled)
    a, ua = G.apply_waivers(gates, items, waivers)
    b, ub = G.apply_waivers(gates, items, shuffled)
    assert summary(a) == summary(b)
    key = lambda w: sorted(w.items())   # noqa: E731
    assert sorted(map(key, ua)) == sorted(map(key, ub))


@given(GATES, ITEMS, WAIVERS, WAIVERS)
def test_another_waiver_never_unwaives_and_never_worsens_the_run(gates, items, waivers, more):
    a, _ = G.apply_waivers(gates, items, waivers)
    b, _ = G.apply_waivers(gates, items, waivers + more)
    for x, y in zip(a, b):
        if x["verdict"] == "WAIVED":
            assert y["verdict"] == "WAIVED"
    assert RANK.get(G.run_verdict(b)[0], 0) <= RANK.get(G.run_verdict(a)[0], 0)
    assert RANK.get(G.run_verdict(a)[0], 0) <= RANK.get(G.run_verdict(gates)[0], 0)


@given(GATES, ITEMS, WAIVERS)
def test_an_uncovered_fail_item_keeps_its_gate_red(gates, items, waivers):
    """A `fail` or `unverified` item is covered only by a waiver naming its own gate, tree path and result."""
    out, _ = G.apply_waivers(gates, items, waivers)
    for o in out:
        for i in items.get(o["id"], []):
            if i["result"] in ("fail", "unverified") and not any(
                    w["gate"] == o["id"] and w["tree_path"] == i.get("tree_path") and w["result"] == i["result"]
                    for w in waivers):
                assert o["verdict"] != "WAIVED"


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
def _t0_run_cli(detail: str):
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "rm-bundle")

        def fake_run(o, url_map=None):
            Path(o).mkdir(parents=True, exist_ok=True)
            _write_rows(Path(o), [detail])
            return 0
        with mock.patch("nanosoc_rm.t0.runner.run", fake_run):
            return CliRunner().invoke(cli.main, ["t0", "run", "--out", str(out), "--json"])


@given(st.text(st.characters(blacklist_categories=("Cs", "Zl", "Zp", "Cc")), max_size=20))
def test_t0_run_prints_what_the_runner_wrote(detail):
    """The harness of ROB-property-10 is not vacuous: an ordinary detail goes through."""
    res = _t0_run_cli(detail)
    assert res.exit_code == 0 and res.exception is None, res.output


@example("vendor/x\u2028y.v")
@given(st.sampled_from(["a\x0bb", "a\x0cb", "a\x1cb", "a\x85b", "a\u2028b", "a\u2029b"]))
def test_rob_property_10_t0_run_prints_what_the_runner_wrote(detail):
    res = _t0_run_cli(detail)
    assert res.exception is None and res.exit_code == 0, res.output[-200:]


@given(st.text(max_size=40))
def test_a_clean_detail_is_one_line_however_it_is_split(text):
    """ROB-property-10, the writer's half: no character str.splitlines() breaks at survives clean_detail."""
    from nanosoc_rm.t0.gates import clean_detail
    d = clean_detail(text)
    assert len(d.splitlines()) <= 1 and "\t" not in d
