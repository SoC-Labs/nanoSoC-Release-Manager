"""Wave B integration: the bump engine reads its T0 cell from the matrix (interfaces.md section 19.6)."""
from __future__ import annotations

import pytest


@pytest.mark.requirement("D3")
@pytest.mark.requirement("E1")
def test_bump_cell_answers_exactly_one_stored_move_and_the_bump_engine_uses_it(clean):
    from nanosoc_rm import bumps, matrix
    s = clean
    with s.env.active():
        assert matrix.bump_cell("top-main", {"a": s.a[1]}) is None, "nothing stored yet"
    s.run_cell("pinned", store=True)
    cell = s.run_cell("in-range", "alpha", store=True)[0]
    f = cell["fact"]
    assert {o["tree_path"]: o["commit"] for o in f["overrides"]} == {"a": s.a[1]}
    with s.env.active():
        got = matrix.bump_cell("top-main", {"a": s.a[1]})
        assert got is not None and got["verdict"] == ("UNVERIFIED" if f["verdict"] == "FLAKY" else f["verdict"])
        assert got["href"] == "/matrix/top-main/cell?component=alpha&column=in-range"
        assert got["observed_at"] == cell["observed_at"] and cell["subject"] in got["detail"]
        # exactly that move: another commit, an extra pin, or no pins at all is not this cell
        assert matrix.bump_cell("top-main", {"a": s.a[2]}) is None
        assert matrix.bump_cell("top-main", {"a": s.a[1], "b": s.b1}) is None
        assert matrix.bump_cell("top-main", {}) is None
        # the bump engine's proposal for alpha -> v1.0.1 (in its declared range ~1.0) reads this cell
        ans = bumps.proposals("top-main")
    props = [d for d in ans if d["kind"] == "bump_proposal" and d["fact"]["dependency"]["component"] == "alpha"]
    assert props, [d["fact"] for d in ans if d["kind"] == "bump_scan"]
    m = props[0]["fact"]["matrix"]
    assert props[0]["fact"]["to"]["commit"] == s.a[1], props[0]["fact"]["to"]
    assert m["source"] == "matrix" and m["verdict"] == got["verdict"] and m["href"] == got["href"], m


def test_a_flaky_cell_never_reads_as_a_green_bump(clean, monkeypatch):
    from nanosoc_rm import matrix
    from nanosoc_rm.matrix import cells as K
    s = clean
    cell = s.run_cell("in-range", "alpha", store=True)[0]
    flaky = dict(cell, fact=dict(cell["fact"], verdict="FLAKY"))
    monkeypatch.setattr(K, "cells", lambda configuration, config: [flaky])
    with s.env.active():
        got = matrix.bump_cell("top-main", {"a": s.a[1]})
    assert got["verdict"] == "UNVERIFIED" and "FLAKY" in got["detail"]
