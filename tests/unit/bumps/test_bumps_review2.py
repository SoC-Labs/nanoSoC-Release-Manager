"""Second review of wave-b/bumps (findings BUMPS-R1..R8): the shadow ledger judges each gitlink by the FIRST move a
person made on the branch after the proposal (so an agreement lasts and one move is one decision), a held
proposal is never scored, a consumer without a declared range is never offered a move across a major version or a
breaking change, a frozen-only configuration is refused, the mark POST is local-only, and the proposal page knows
about compat jobs that finished after the scan."""
from __future__ import annotations

import pytest

from bfix import GL_ARCH, GL_GEN, GL_MC, world
from bfix import bump_gate_passes

AUTOUSE = (bump_gate_passes,)          # the autouse fixture: a passing test-bump exists (Wave G bump gate)

pytestmark = pytest.mark.requirement("E2")


def _by_dep(props):
    return {p["fact"]["dependency"]["component"]: p["fact"] for p in props}


def _scan_gen(cid="eth-main", **kw):
    from nanosoc_rm import bumps
    return _by_dep(bumps.proposals(cid, **kw)[1:])


def _ledger():
    from nanosoc_rm import bumps
    led = bumps.ledger()["fact"]
    return {e["id"]: e for e in led["entries"]}, led["counts"]


def _pin(w, parent, gen_commit, msg):
    a = w["arch"].commit(parent, links={"nanosoc_gen": gen_commit}, msg=msg)
    w["arch"].set(GL_ARCH, "heads", "main", a)
    return a


# ----- BUMPS-R1: an agreement lasts when the branch moves on ----------------------------------------------------
def test_an_agreement_lasts_when_the_next_release_is_applied_later(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    gen = _scan_gen(store=True)["nanosoc_gen"]
    a3 = _pin(w, w["a"][0], w["g"][3], "person applies v0.2.0")
    out, counts = _ledger()
    assert out[gen["id"]]["outcome"] == "agreed" and counts["agreed"] == 1
    g6 = w["gen"].commit(w["g"][4], msg="g6")
    w["gen"].set(GL_GEN, "tags", "v0.3.0", g6)
    _scan_gen(store=True)
    _pin(w, a3, g6, "person applies v0.3.0")
    out, counts = _ledger()
    assert out[gen["id"]]["outcome"] == "agreed", out[gen["id"]]["note"]
    assert out[gen["id"]]["person"]["commit"] == a3
    assert counts["agreed"] == 2 and counts["disagreed"] == 0 and counts["decided"] == 2


def test_an_agreement_lasts_when_a_later_untagged_commit_is_pinned(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    gen = _scan_gen(store=True)["nanosoc_gen"]
    a3 = _pin(w, w["a"][0], w["g"][3], "person applies v0.2.0")
    _pin(w, a3, w["g"][4], "person moves on to an untagged commit")
    out, counts = _ledger()
    assert out[gen["id"]]["outcome"] == "agreed", out[gen["id"]]["note"]
    assert counts["agreed"] == 1 and counts["disagreed"] == 0


def test_a_first_move_that_disagreed_stays_a_disagreement(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    gen = _scan_gen(store=True)["nanosoc_gen"]
    a3 = _pin(w, w["a"][0], w["g"][1], "person picks v0.1.1 instead")
    _pin(w, a3, w["g"][3], "and later v0.2.0")
    out, counts = _ledger()
    assert out[gen["id"]]["outcome"] == "disagreed" and out[gen["id"]]["person"]["commit"] == a3
    assert counts["decided"] == 1


# ----- BUMPS-R2: one person action is one decided bump -----------------------------------------------------------
def test_one_move_is_one_decision_across_two_proposals_for_the_same_gitlink(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    old = _scan_gen(store=True)["nanosoc_gen"]
    g6 = w["gen"].commit(w["g"][4], msg="g6")
    w["gen"].set(GL_GEN, "tags", "v0.3.0", g6)
    new = _scan_gen(store=True)["nanosoc_gen"]
    assert new["id"] != old["id"] and new["to"]["tag"] == "v0.3.0"
    _pin(w, w["a"][0], w["g"][3], "person applies v0.2.0")
    out, counts = _ledger()
    assert out[old["id"]]["outcome"] == "agreed"
    assert out[new["id"]]["outcome"] == "superseded", out[new["id"]]["note"]
    assert counts["decided"] == 1 and counts["disagreed"] == 0


# ----- BUMPS-R3: a held proposal is not scored -------------------------------------------------------------------
def test_a_proposal_the_bot_would_hold_is_never_scored(tmp_path, monkeypatch):
    from bfix import pin, store_walk
    w = world(tmp_path, monkeypatch, arch_tag_on="github")
    root = {"component": "nanosoc-multicore-system", "repo": GL_MC, "commit": w["m1"]}
    store_walk("multicore-master", [pin("multicore-master", "nanosoc_arch_tech", "nanosoc_arch_tech", 1, root,
                                        "nanosoc_arch_tech", GL_ARCH, w["a"][0], role="copy")])
    arch = _scan_gen("multicore-master", store=True)["nanosoc_arch_tech"]
    assert arch["state"] == "blocked" and arch["decision"]["would"] == "hold"
    m2 = w["mc"].commit(w["m1"], links={"nanosoc_arch_tech": w["a"][1]}, msg="person pins a2")
    w["mc"].set(GL_MC, "heads", "master", m2)
    out, counts = _ledger()
    assert out[arch["id"]]["outcome"] == "held", out[arch["id"]]
    assert counts["agreed"] == 0 and counts["decided"] == 0 and counts["held"] == 1


# ----- BUMPS-R4: no declared range: never across a major version, never a breaking change -------------------------
def test_without_a_declared_range_a_major_release_is_not_proposed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    g6 = w["gen"].commit(w["g"][4], files={"CHANGELOG.md": "# Changelog\n\n## [1.0.0]\nClass: breaking\n- gone\n"},
                         msg="g6 1.0.0")
    w["gen"].set(GL_GEN, "tags", "v1.0.0", g6)
    gen = _scan_gen()["nanosoc_gen"]
    assert gen["to"]["tag"] == "v0.2.0" and gen["newest"]["tag"] == "v1.0.0"
    assert gen["range"]["declared"] is None and "same major" in gen["range"]["assumed"]
    assert gen["decision"]["would"] == "open-pull-request"


def test_without_a_declared_range_only_a_major_release_is_held(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    # pin v0.2.0 already: the only newer release is v1.0.0
    _pin(w, w["a"][0], w["g"][3], "at v0.2.0")
    g6 = w["gen"].commit(w["g"][4], msg="g6 1.0.0")
    w["gen"].set(GL_GEN, "tags", "v1.0.0", g6)
    scan = bumps.proposals("eth-main")[0]
    assert "nanosoc_gen" not in _by_dep(bumps.proposals("eth-main")[1:])
    row = next(r for r in scan["fact"]["rows"] if r["component"] == "nanosoc_gen")
    assert row["status"] == "held-by-range" and "no range" in row["note"] and row["tag"] == "v1.0.0"


def test_without_a_declared_range_a_breaking_change_is_held(tmp_path, monkeypatch):
    from nanosoc_rm.bumps import interfaces
    world(tmp_path, monkeypatch)
    real = interfaces.provider
    monkeypatch.setattr(interfaces, "provider", lambda spec: (lambda *a, **k: {"class": "breaking", "detail": "x",
                        "sources": []}) if spec == interfaces.CLASSIFIER else real(spec))
    gen = _scan_gen()["nanosoc_gen"]
    assert gen["change"]["class"] == "breaking"
    assert gen["state"] == "blocked" and gen["decision"]["would"] == "hold" and "breaking" in gen["reason"]


def test_a_declared_range_lets_a_breaking_change_through_as_proposed(tmp_path, monkeypatch):
    from nanosoc_rm.bumps import interfaces
    world(tmp_path, monkeypatch, manifest_range=">=0.1.0, <1.0.0")
    real = interfaces.provider
    monkeypatch.setattr(interfaces, "provider", lambda spec: (lambda *a, **k: {"class": "breaking", "detail": "x",
                        "sources": []}) if spec == interfaces.CLASSIFIER else real(spec))
    gen = _scan_gen()["nanosoc_gen"]
    assert gen["state"] == "proposed" and gen["range"]["declared"] and gen["range"].get("assumed") is None


# ----- BUMPS-R5: a frozen configuration that is not silicon is refused -------------------------------------------
def test_a_frozen_configuration_that_is_not_silicon_is_refused(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    p = w["registry"] / "eligibility.yaml"
    p.write_text(p.read_text().replace("    ref: {branch: main}\n", "    ref: {branch: main}\n    frozen: true\n    commit: \"%s\"\n" % w["r1"], 1))
    res = bumps.proposals("eth-main")
    assert res[1:] == [] and res[0]["fact"]["eligible"] is False and "frozen" in res[0]["fact"]["refused"]


# ----- BUMPS-R6: the mark POST is local-only ---------------------------------------------------------------------
def test_a_ci_deployment_refuses_a_mark(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_bumps_cli_web import _local
    from nanosoc_rm import bumps
    from nanosoc_rm.auth import FakeMembership
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch, local=False)
    c = TestClient(create_app(w["cfg"], auth=FakeMembership(login="alice")))
    pid = bumps.stored("eth-main")[0]["fact"]["id"]
    # D5 (CR-WC-seams): a GitHub + CI deployment registers no plug-in POST, so a mark cannot even be routed
    r = c.post("/bumps/p/%s/mark" % pid, data={"decision": "done"})
    assert r.status_code in (404, 405)
    assert bumps.marks(pid) == []


# ----- BUMPS-R8: the page sees compat jobs that finished after the scan; Scan now checks the id ------------------
def test_the_page_names_compat_jobs_after_the_scan_and_offers_no_duplicate_run(tmp_path, monkeypatch):
    from test_bumps_cli_web import _csrf, _local, _signed_in
    from test_bumps_review import _compat
    from nanosoc_rm import bumps
    from nanosoc_rm.config import load_config
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    cfg = load_config()
    gen = _by_dep(bumps.stored("eth-main"))["nanosoc_gen"]
    c = _signed_in(create_app(w["cfg"]))
    page = c.get("/bumps/p/%s?configuration=eth-main" % gen["id"]).text
    assert 'Run <span title="T0">resolve check</span> + t1' in page and 'Run <span title="T0">resolve check</span> + t2' in page
    _compat(cfg, w, "t1", "PASS")
    _compat(cfg, w, "t2", "PASS")
    page = c.get("/bumps/p/%s?configuration=eth-main" % gen["id"]).text
    assert "Run T0 + t1" not in page and "Run T0 + t2" not in page
    assert 'data-bumps="rescan"' in page and "Scan again" in page
    token = _csrf(c, "/bumps/")
    r = c.post("/bumps/scan", data={"csrf_token": token, "configuration": "nope"}, follow_redirects=False)
    assert r.status_code == 400 and "nope" in r.text
