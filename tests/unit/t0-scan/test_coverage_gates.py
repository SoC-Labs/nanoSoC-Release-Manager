"""Coverage angle (rob/coverage): the T0 gate machinery's error paths (t0/gates.py), with a patched gate table.

A check that crashes or answers nonsense is UNVERIFIED, never an answer; a control that cannot be built is a
control that did not fire; local mode never vouches for the hardened runner; an unknown status or mode is refused;
a registry item is never waived; the credential scrub names variables, never values."""
from __future__ import annotations

import dataclasses

import pytest

from nanosoc_rm.t0 import gates as G

NONCE = "ab" * 16


class _Ctx:
    nonce = NONCE


def _gate(check, control=lambda ctx: ctx, may_skip=False, gid="t0.infra.probe"):
    return dataclasses.replace(G.GATES[gid], check=check, control=control, may_skip=may_skip)


@pytest.fixture
def one_gate(monkeypatch):
    """Replace t0.infra.probe's check and control; returns a setter."""
    def put(check, control=lambda ctx: ctx, may_skip=False):
        monkeypatch.setitem(G.GATES, "t0.infra.probe", _gate(check, control, may_skip))
    return put


def test_a_crashing_or_nonsense_check_is_unverified(one_gate):
    def boom(ctx):
        raise RuntimeError("line one\nline two")
    one_gate(boom)
    row, ctl = G.run_with_control("t0.infra.probe", _Ctx())
    assert row["status"] == "UNVERIFIED" and row["detail"] == "check raised RuntimeError: line one"
    assert ctl["status"] == "FAIL" and "did not fire" in ctl["detail"]          # its control cannot fire either
    for answer, words in ((("OK", "fine"), "invalid status 'OK'"), (("PASS",), "check raised ValueError"),
                          (None, "check raised TypeError")):
        one_gate(lambda ctx, a=answer: a)
        assert words in G.run_with_control("t0.infra.probe", _Ctx())[0]["detail"]


def test_raised_without_a_message_still_names_the_exception(one_gate):
    def boom(ctx):
        raise KeyError()
    one_gate(boom)
    assert G.run_with_control("t0.infra.probe", _Ctx())[0]["detail"] == "check raised KeyError:"


def test_a_control_that_cannot_be_built_did_not_fire(one_gate):
    def no_control(ctx):
        raise OSError("disk full")
    one_gate(lambda ctx: ("PASS", "ok"), control=no_control)
    row, ctl = G.run_with_control("t0.infra.probe", _Ctx())
    assert row["status"] == "PASS" and ctl["gate"] == "t0.infra.probe.negctl"
    assert ctl["status"] == "FAIL" and "could not be built (OSError: disk full): not fired" in ctl["detail"]


def test_the_control_fires_only_on_fail(one_gate):
    broken = object()

    def check(ctx):
        return ("FAIL", "broken\tinput\nhere") if ctx is broken else ("PASS", "real")
    one_gate(check, control=lambda ctx: broken)
    row, ctl = G.run_with_control("t0.infra.probe", _Ctx())
    assert row["status"] == "PASS" and ctl["status"] == "PASS" and ctl["nonce"] == NONCE
    assert "\t" not in ctl["detail"] and "\n" not in ctl["detail"] and "fired" in ctl["detail"]
    one_gate(lambda ctx: ("WARN", "w"), control=lambda ctx: broken)             # the broken input is not FAILed
    assert G.run_with_control("t0.infra.probe", _Ctx())[1]["status"] == "FAIL"


def _row(gate, status, detail="d"):
    return {"gate": gate, "status": status, "detail": detail, "utc": "2026-10-14T00:00:00Z", "nonce": NONCE}


def _item(rows, gid, scope="configuration", mode="ci"):
    return next(i for i in G.rollup(rows, scope, mode) if i["id"] == gid)


def test_rollup_refuses_unknown_scope_and_mode():
    with pytest.raises(ValueError, match="scope"):
        G.rollup([], "nope")
    with pytest.raises(ValueError, match="run mode"):
        G.rollup([], "configuration", "laptop")
    with pytest.raises(ValueError):
        G.gates_for("everything")


def test_rollup_local_mode_never_vouches_for_the_hardened_runner():
    gid = "t0.runner.hardened"
    reason = G.LOCAL_NOT_RUN[gid]
    assert _item([_row(gid, "SKIP", reason)], gid, mode="local")["verdict"] == "NOT-RUN"
    for rows in ([_row(gid, "PASS")], [_row(gid, "SKIP", "other")], [],
                 [_row(gid, "SKIP", reason), _row(gid + ".negctl", "PASS")]):
        it = _item(rows, gid, mode="local")
        assert it["verdict"] == "UNVERIFIED" and "must be recorded NOT-RUN" in it["detail"]
    assert _item([_row(gid, "SKIP", reason)], gid, mode="ci")["verdict"] == "UNVERIFIED"   # never skips in CI


def test_rollup_status_table():
    gid, skippable = "t0.infra.probe", "t0.manifest.valid"
    fired = [_row(gid + ".negctl", "PASS")]
    assert _item([_row(gid, "WARN")] + fired, gid)["verdict"] == "WARN"
    w = _item([_row(gid, "WARN")], gid)
    assert w["verdict"] == "UNVERIFIED" and w["control"] == "missing" and "WARN without a fired control" in w["detail"]
    nf = _item([_row(gid, "PASS"), _row(gid + ".negctl", "PASS"), _row(gid + ".negctl", "PASS")], gid)
    assert nf["control"] == "not-fired" and nf["verdict"] == "UNVERIFIED"         # two control rows: not trusted
    assert _item([_row(gid, "INFRA")], gid)["verdict"] == "INFRA"
    assert _item([_row(gid, "BOGUS")], gid)["detail"].startswith("unknown status 'BOGUS'")
    assert "may never skip" in _item([_row(gid, "SKIP", "why")], gid)["detail"]
    assert _item([_row(skippable, "SKIP", "  ")], skippable)["detail"] == "SKIP without a reason"
    nr = _item([_row(skippable, "SKIP", "no manifest")], skippable)
    assert (nr["verdict"], nr["control"]) == ("NOT-RUN", "not-applicable")
    assert _item([_row(gid, "PASS"), _row(gid, "PASS")] + fired, gid)["detail"] == "2 rows for this gate"


def test_apply_waivers_never_waives_a_registry_item_or_an_unfired_gate():
    gid = "t0.registry.schema"
    g = [{"id": gid, "verdict": "FAIL", "control": "fired", "detail": "d"}]
    items = {gid: [{"result": "fail", "tree_path": None}]}
    out, unused = G.apply_waivers(g, items, [{"gate": gid, "tree_path": None, "result": "fail", "reason": "r",
                                             "approved_by": "D"}])
    assert out[0]["verdict"] == "FAIL" and len(unused) == 1
    cg = "t0.pins.reachable"
    w = [{"gate": cg, "tree_path": "a", "result": "fail", "reason": "r", "approved_by": "D"}]
    items = {cg: [{"result": "fail", "tree_path": "a"}, {"result": "pass", "tree_path": "b"}]}
    ok, _ = G.apply_waivers([{"id": cg, "verdict": "FAIL", "control": "fired"}], items, w)
    assert ok[0]["verdict"] == "WAIVED" and ok[0]["raw_verdict"] == "FAIL" and ok[0]["waived"][0]["covered_by"] == "a"
    nf, _ = G.apply_waivers([{"id": cg, "verdict": "FAIL", "control": "not-fired"}], items, w)
    assert nf[0]["verdict"] == "FAIL"
    src = [{"id": cg, "verdict": "FAIL", "control": "fired"}]
    G.apply_waivers(src, items, w)
    assert "waived" not in src[0]                                               # pure: inputs unchanged


def test_credential_vars_names_never_values():
    env = {"CI_JOB_TOKEN": "tok-12345678", "CI_REPOSITORY_URL": "https://gitlab-ci-token:tok-12345678@h/x.git",
           "MY_URL": "https://user:pw@host/repo", "COPY": "prefix tok-12345678 suffix", "CI_JOB_JWT_V2": "j",
           "CI_REGISTRY_PASSWORD": "p", "PLAIN": "https://host/repo", "CI_PROJECT": "x", "N": 5}
    out = G.credential_vars(env)
    assert out == sorted(["CI_JOB_TOKEN", "CI_REPOSITORY_URL", "MY_URL", "COPY", "CI_JOB_JWT_V2",
                          "CI_REGISTRY_PASSWORD"])
    assert "COPY" not in G.credential_vars({"CI_JOB_TOKEN": "short", "COPY": "short"})   # < 8 chars: not matched
    assert G.clean_detail("a\tb\r\nc\n") == "a b  c"
    assert G.synthetic_commit(NONCE, "x") != G.synthetic_commit(NONCE, "y") and len(G.synthetic_commit("n", "x")) == 40


def test_run_verdict_with_nothing_checked():
    assert G.run_verdict([]) == ("UNVERIFIED", 0)
    assert G.run_verdict([{"verdict": "NOT-RUN"}, {"verdict": "WAIVED"}]) == ("UNVERIFIED", 1)
    assert G.run_verdict([{"verdict": "PASS"}, {"verdict": "INFRA"}]) == ("INFRA", 0)
