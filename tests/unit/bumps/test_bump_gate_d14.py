"""D14 (David 2026-10-19, interactive): a NEW red (FAIL: green before, red after) for the same move in ANY
registered configuration of the component holds the bump, even when another configuration passes; the bump
proposal and `GET /api/v1/bump-check` name the configuration that broke. A WARN (already red before the bump, D12)
in one configuration and a PASS in another still lands. UNVERIFIED (the move could not be judged) in ANY
configuration holds it too, like a FAIL (David 2026-10-19): the proposal and the bump-check name that
configuration and say "could not be judged there"; a FAIL anywhere still answers first (interfaces.md 26.4, 26.7).
"""
from __future__ import annotations

import pytest

from bfix import world
from bfix import bump_gate_passes

from nanosoc_rm import contracts

AUTOUSE = (bump_gate_passes,)

pytestmark = [pytest.mark.requirement("E3"), pytest.mark.requirement("PR6")]

TO, ROOT, PATH = "a" * 40, "b" * 40, "sub/dep"


def _ev(cid, verdict, job=None):
    if verdict is None:
        return {"verdict": None, "job": None, "href": None, "finished_at": None, "note": None, "configuration": cid}
    note = {"WARN": "already red before the bump: x:lint is still red after it, so it does not hold the bump",
            "FAIL": "y:sim worse", "UNVERIFIED": "z:elab not-run"}.get(verdict)
    return {"verdict": verdict, "job": job, "href": "/capabilities/%s/bump?job=%s" % (cid, job),
            "finished_at": None, "note": note, "configuration": cid}


def _registry(monkeypatch, verdicts: dict, component="top", extra=()):
    """Configurations of `component` with these newest test-bump verdicts (None: never tested)."""
    from nanosoc_rm.capabilities import gate
    from nanosoc_rm.ci import configs
    confs = [{"id": cid, "component": component} for cid in verdicts] + [{"id": e, "component": "other"}
                                                                      for e in extra]
    monkeypatch.setattr(configs, "configurations", lambda config=None: confs)
    jobs = {cid: n for n, cid in enumerate(verdicts, 11)}
    seen = []

    def evidence(config, configuration, tree_paths, to, root_commit):
        seen.append(configuration)
        return _ev(configuration, verdicts.get(configuration), jobs.get(configuration))
    monkeypatch.setattr(gate, "evidence", evidence)
    return seen


# ----- GET /api/v1/bump-check -----------------------------------------------------------------------------------
@pytest.mark.parametrize("verdicts,want,named", [
    ({"top-asic": "PASS", "top-fpga": "FAIL"}, "FAIL", "top-fpga"),       # a new red holds even beside a PASS
    ({"top-asic": "FAIL", "top-fpga": "PASS"}, "FAIL", "top-asic"),       # whichever order they are registered
    ({"top-asic": "WARN", "top-fpga": "FAIL"}, "FAIL", "top-fpga"),       # an already-red never hides a new red
    ({"top-asic": None, "top-fpga": "FAIL"}, "FAIL", "top-fpga"),         # untested elsewhere: the red decides
    ({"top-asic": "WARN", "top-fpga": "PASS"}, "PASS", "top-fpga"),       # WARN + PASS lands
    ({"top-asic": "WARN", "top-fpga": None}, "WARN", "top-asic"),         # WARN alone lands (D12)
    ({"top-asic": "UNVERIFIED", "top-fpga": None}, "UNVERIFIED", "top-asic"),   # UNVERIFIED holds, as before
    ({"top-asic": "UNVERIFIED", "top-fpga": "PASS"}, "UNVERIFIED", "top-asic"),  # not judged there: holds
    ({"top-asic": "PASS", "top-fpga": "UNVERIFIED"}, "UNVERIFIED", "top-fpga"),  # whichever order
    ({"top-asic": "WARN", "top-fpga": "UNVERIFIED"}, "UNVERIFIED", "top-fpga"),  # a landing WARN never hides it
    ({"top-asic": "UNVERIFIED", "top-fpga": "FAIL"}, "FAIL", "top-fpga"),        # a new red answers first
])
def test_bump_check_holds_on_a_new_red_or_unjudged_in_any_configuration(monkeypatch, verdicts, want, named):
    from nanosoc_rm.capabilities import bumpcheck as BC
    from nanosoc_rm.capabilities import gate
    seen = _registry(monkeypatch, verdicts, extra=("else-main",))
    a = BC.answer(None, {"component": "top", "path": PATH, "to": TO, "root": ROOT})
    assert a["verdict"] == want and a["configuration"] == named, a
    assert a["configurations"] == list(verdicts)
    assert "else-main" not in seen, "only configurations of the asking component are read"
    assert gate.lands({"verdict": want}) is (want in ("PASS", "WARN"))
    if want in ("FAIL", "UNVERIFIED"):
        assert a["url"] == "/capabilities/%s/bump?job=%d" % (named, list(verdicts).index(named) + 11)
        assert a["command"] == "nanosoc-rm test-bump %s %s %s" % (named, PATH, TO)
        assert a["note"].startswith("%s: job " % named) and "D14" in a["note"]
        assert ("y:sim worse" if want == "FAIL" else "could not be judged there") in a["note"], a["note"]
        for other, v in verdicts.items():
            if other != named:
                assert "%s %s" % (other, v or "untested") in a["note"], "the note says what the others gave"


def test_gate_new_red_and_siblings(monkeypatch):
    from nanosoc_rm.capabilities import gate
    _registry(monkeypatch, {"top-asic": "PASS", "top-fpga": "FAIL", "top-sim": "UNVERIFIED"}, extra=("else",))
    assert gate.siblings(None, "top-fpga") == ["top-fpga", "top-asic", "top-sim"]
    assert gate.siblings(None, "unknown") == ["unknown"]
    cid, ev = gate.new_red(None, ["top-asic", "top-sim", "top-fpga"], [PATH], TO, ROOT)
    assert cid == "top-fpga" and ev["verdict"] == "FAIL" and ev["configuration"] == "top-fpga"
    assert gate.new_red(None, ["top-asic", "top-sim"], [PATH], TO, ROOT) is None
    # held_in: a FAIL first wherever it is, else the first UNVERIFIED, else nothing holds
    assert gate.held_in(None, ["top-sim", "top-asic", "top-fpga"], [PATH], TO, ROOT)[0] == "top-fpga"
    cid_u, ev_u = gate.held_in(None, ["top-asic", "top-sim"], [PATH], TO, ROOT)
    assert cid_u == "top-sim" and ev_u["verdict"] == "UNVERIFIED" and ev_u["configuration"] == "top-sim"
    assert gate.held_in(None, ["top-asic"], [PATH], TO, ROOT) is None
    u = gate.broke_in(ev_u, "top-sim", "top-asic", {"verdict": "PASS"})
    assert "could not be judged there" in u["note"] and "although top-asic is PASS" in u["note"]
    assert "top-sim" in gate.blocker(u, "top-asic", PATH, TO)
    # the blocker of a sibling's red names the sibling's command
    b = gate.blocker(ev, "top-asic", PATH, TO)
    assert "nanosoc-rm test-bump top-fpga %s %s" % (PATH, TO[:12]) in b and "in top-fpga (job" in b


# ----- the bump engine --------------------------------------------------------------------------------------------
def _scan(cid="eth-main"):
    from nanosoc_rm import bumps
    res = bumps.proposals(cid)
    return res[0], res[1:]


def _siblings(monkeypatch, own: str, sib: str):
    """eth-main plus a second configuration of the same component, `eth-fpga`, with these verdicts."""
    from nanosoc_rm.capabilities import gate
    from nanosoc_rm.ci import configs
    real = configs.configurations()
    comp = next(c["component"] for c in real if c["id"] == "eth-main")
    monkeypatch.setattr(configs, "configurations",
                        lambda config=None: real + [{"id": "eth-fpga", "component": comp}])
    v = {"eth-main": own, "eth-fpga": sib}
    monkeypatch.setattr(gate, "evidence", lambda config, configuration, tree_paths, to, root_commit:
                        _ev(configuration, v.get(configuration), 40 if configuration == "eth-main" else 41))


@pytest.mark.parametrize("own,sib,would,named", [
    ("PASS", "FAIL", "hold", "eth-fpga"),                  # a new red elsewhere holds a passing proposal
    ("WARN", "FAIL", "hold", "eth-fpga"),
    (None, "FAIL", "hold", "eth-fpga"),
    ("FAIL", "PASS", "hold", "eth-main"),                  # its own red holds, as before
    ("WARN", "PASS", "open-pull-request", "eth-main"),     # WARN here, PASS there: lands
    ("PASS", "UNVERIFIED", "hold", "eth-fpga"),            # not judged there: holds like a FAIL
    ("WARN", "UNVERIFIED", "hold", "eth-fpga"),
    ("UNVERIFIED", "PASS", "hold", "eth-main"),            # its own UNVERIFIED still holds, as before
    ("UNVERIFIED", "UNVERIFIED", "hold", "eth-main"),      # its own hold already says what to run
    (None, "UNVERIFIED", "hold", "eth-main"),
    ("UNVERIFIED", "FAIL", "hold", "eth-fpga"),            # a new red elsewhere names that configuration
])
def test_bump_engine_holds_on_a_new_red_or_unjudged_in_another_configuration(tmp_path, monkeypatch, own, sib, would, named):
    world(tmp_path, monkeypatch)
    _siblings(monkeypatch, own, sib)
    _, props = _scan()
    proposed = [d["fact"] for d in props if d["fact"]["state"] == "proposed"]
    assert proposed, "the world has at least one proposal the gate alone decides"
    for d in props:
        assert contracts.validate_observation(d) == [], d["subject"]
    for f in proposed:
        assert f["decision"]["would"] == would, f["decision"]
        assert f["test_bump"]["configuration"] == named
        if would == "hold":
            gate_line = f["decision"]["blockers"][1]
            assert gate_line.startswith("bump gate:") and "nanosoc-rm test-bump %s " % named in gate_line
        if named == "eth-fpga":
            want = ("a new red in configuration eth-fpga" if sib == "FAIL" else
                    "configuration eth-fpga is UNVERIFIED: the move could not be judged there")
            assert f["test_bump"]["job"] == 41 and want in f["test_bump"]["note"], f["test_bump"]["note"]
            assert "could not be judged there" in f["decision"]["blockers"][1] or sib == "FAIL"
            assert "although eth-main is %s" % (own or "untested") in f["test_bump"]["note"]
