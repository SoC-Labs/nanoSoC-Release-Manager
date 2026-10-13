"""Review items 1-7 on the bump engine (wave-b/bumps): each E3 condition has a test that blocks, the matrix cell is
only a T0-T2 PASS on the walked tree, the decisive blocker and ledger rules are pinned, the pages read the stored
ledger (no git in a request), a proposal kept for two configurations keeps both, and a defect introduced and fixed
inside the moved range is neither."""
from __future__ import annotations

import time

import pytest

from bfix import GH_ETH, GH_TL, GL_ARCH, GL_GEN, GL_MC, Mirror, pin, store_walk, world
from nanosoc_rm import contracts

pytestmark = pytest.mark.requirement("E3")


def _scan(cid="eth-main", **kw):
    from nanosoc_rm import bumps
    res = bumps.proposals(cid, **kw)
    return res[0], res[1:]


def _by_dep(props):
    return {p["fact"]["dependency"]["component"]: p["fact"] for p in props}


def _has(blockers, text):
    return any(text in b for b in blockers)


def _gen(**kw):
    return _by_dep(_scan(**kw)[1])["nanosoc_gen"]


# ----- review item 1: the matrix cell is T0-T2 on the walked tree ------------------------------------------------
def _compat(cfg, w, tier, verdict, commit=None, pins=None):
    from nanosoc_rm import jobs
    pins = pins or ["nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=%s" % w["g"][3]]
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main", "pins": pins, "tier": tier}, "alice")
    result = {"schema": 1, "kind": "compat_result", "fact": {"configuration": "eth-main", "commit": commit or w["r1"]}}
    jobs._update(cfg, job["id"], state="done", verdict=verdict, finished_at="2026-10-02T10:00:00Z", result=result)
    return job


def test_a_t0_only_pass_never_clears_the_t0_t2_blocker(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    _compat(load_config(), w, None, "PASS")
    gen = _gen()
    assert gen["matrix"]["verdict"] != "PASS"
    assert gen["matrix"]["tiers"]["t0"]["verdict"] == "PASS" and gen["matrix"]["tiers"]["t1"] is None
    assert _has(gen["decision"]["blockers"], "T0-T2 on the exact commits")


def test_t1_and_t2_pass_on_the_walked_tree_is_the_t0_t2_cell(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    cfg = load_config()
    _compat(cfg, w, "t1", "PASS")
    _compat(cfg, w, "t2", "PASS")
    gen = _gen()
    assert (gen["matrix"]["verdict"], gen["matrix"]["source"]) == ("PASS", "compat-job")
    assert not _has(gen["decision"]["blockers"], "T0-T2 on the exact commits")
    _compat(cfg, w, "t2", "FAIL")                                  # a newer t2 run fails: the newest per tier counts
    gen = _gen()
    assert gen["matrix"]["verdict"] == "FAIL" and _has(gen["decision"]["blockers"], "T0-T2 on the exact commits: FAIL")


def test_a_pass_on_a_tree_that_has_moved_on_is_stale_and_not_used(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    cfg = load_config()
    old = "1" * 40
    _compat(cfg, w, "t1", "PASS", commit=old)
    _compat(cfg, w, "t2", "PASS", commit=old)
    gen = _gen()
    assert gen["matrix"]["verdict"] is None and "2 what-if" in (gen["matrix"]["note"] or "")
    assert _has(gen["decision"]["blockers"], "T0-T2 on the exact commits")


def test_a_consumer_branch_past_the_configured_commit_blocks_exact_commits(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    cfg = load_config()
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][0]}, msg="arch moved on")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    _compat(cfg, w, "t1", "PASS")
    _compat(cfg, w, "t2", "PASS")
    gen = _gen()
    assert gen["consumer"]["at_configured"] is False
    assert _has(gen["decision"]["blockers"], "not the exact commits")


def test_the_run_buttons_queue_the_missing_tiers(tmp_path, monkeypatch):
    from test_bumps_cli_web import _local, _signed_in
    from nanosoc_rm import bumps
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    c = _signed_in(create_app(w["cfg"]))
    gen = next(d["fact"] for d in bumps.stored("eth-main") if d["fact"]["dependency"]["component"] == "nanosoc_gen")
    t = c.get("/bumps/p/%s?configuration=eth-main" % gen["id"]).text
    assert 'name="tier" value="t1"' in t and 'name="tier" value="t2"' in t
    assert 'name="tier" value=""' not in t, "a T0-only what-if is never offered as the E3 cell"


# ----- review item 2: every E3 / E5 condition blocks ------------------------------------------------------------
def test_conditions_the_tool_cannot_yet_test_always_block(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    b = _gen()["decision"]["blockers"]
    assert _has(b, "controls fired") and _has(b, "green to red") and _has(b, "build-affecting")


def test_a_split_pin_blocks(tmp_path, monkeypatch):
    from nanosoc_rm import db
    world(tmp_path, monkeypatch)
    assert not _has(_gen()["decision"]["blockers"], "split")
    docs = db.latest("pin_state", "pin_state:eth-main:")
    for d in docs:
        d.pop("snapshot")
        if d["fact"]["child"]["component"] == "nanosoc_gen":
            d["fact"]["split"] = True
    store_walk("eth-main", docs)
    assert _has(_gen()["decision"]["blockers"], "split")


def test_part_of_a_train_blocks(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    by = _by_dep(_scan()[1])
    assert _has(by["nanosoc_gen"]["decision"]["blockers"], "part of a train")
    assert _has(by["nanosoc_arch_tech"]["decision"]["blockers"], "part of a train")


def _tidelink(w, default="main"):
    tl = Mirror(w["data"], "tidelink")
    t2 = tl.commit(w["t1"], msg="t2 release")
    tl.set(GH_TL, "tags", "v1.0.0", t2)
    tl.set(GH_TL, "heads", "main", t2)
    w["eth"].head(GH_ETH, default)
    return t2


def test_a_target_that_is_not_the_default_branch_blocks(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    _tidelink(w, default="main")
    tl = _by_dep(_scan()[1])["tidelink"]
    assert tl["consumer"]["branch"] == "main" and not _has(tl["decision"]["blockers"], "default branch")
    w["eth"].head(GH_ETH, "develop")
    tl = _by_dep(_scan()[1])["tidelink"]
    assert _has(tl["decision"]["blockers"], "not the default branch")


def _inventory(cfg, repo, visibility):
    from nanosoc_rm import db
    scope = "repo_inventory:%s" % repo["host"]
    snap = {"id": db.new_snapshot_id(scope), "scope": scope}
    doc = {"schema": 1, "kind": "repo_inventory", "subject": "repo_inventory:%s:%s" % (repo["host"], repo["path"]),
           "observed_at": "2026-10-01T10:00:00Z", "sources": [{"kind": "derived", "command": "unit fixture"}],
           "snapshot": snap, "fact": {"repo": repo, "component": "nanosoc-ethernet-chiplet", "role": "canonical",
                                      "visibility": visibility, "default_branch": "main"}}
    assert contracts.validate_observation(doc) == []
    db.put_snapshot(scope, [doc], config=cfg)


def test_a_private_or_unknown_github_consumer_blocks_e5(tmp_path, monkeypatch):
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    _tidelink(w)
    cfg = load_config()
    tl = _by_dep(_scan()[1])["tidelink"]
    assert _has(tl["decision"]["blockers"], "visibility of github:%s is not known" % GH_ETH["path"])
    _inventory(cfg, GH_ETH, "private")
    tl = _by_dep(_scan()[1])["tidelink"]
    assert _has(tl["decision"]["blockers"], "private GitHub repo")
    _inventory(cfg, GH_ETH, "public")
    tl = _by_dep(_scan()[1])["tidelink"]
    assert not _has(tl["decision"]["blockers"], "private GitHub") and not _has(tl["decision"]["blockers"], "visibility")
    gen = _gen()
    assert not _has(gen["decision"]["blockers"], "visibility"), "E5 names private GitHub repos only"


def test_a_bump_that_retires_a_workaround_blocks(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    manifest = ("schema: 1\nid: nanosoc_arch_tech\nclass: arch_tech\nversion: \"0.5.0\"\n"
                "canonical: https://github.com/SoC-Labs/nanoSoC-Arch-Tech.git\nprovides: []\n"
                "requires:\n  - {id: nanosoc_gen, path: nanosoc_gen, range: \"*\"}\nreexports: [nanosoc_gen]\n"
                "variants:\n  sim: {flist: \"make flist\"}\ntests:\n  t0: {run: \"make t0\", control: \"make t0-control\"}\n"
                "workarounds:\n  - {path: local/fix.v, retires_when: {component: nanosoc_gen, commit: \"%s\"}}\n")
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][0]},
                          files={"nanosoc-component.yaml": manifest % w["g"][2]}, msg="workaround")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    assert _has(_gen()["decision"]["blockers"], "retires the workaround local/fix.v")
    a4 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][0]},
                          files={"nanosoc-component.yaml": manifest % w["g"][4]}, msg="workaround, later fix")
    w["arch"].set(GL_ARCH, "heads", "main", a4)
    assert not _has(_gen()["decision"]["blockers"], "retires the workaround")


# ----- review item 3: the decisive blockers and the ledger rules -------------------------------------------------
def test_a_diverged_pin_is_diverged_and_blocks(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    assert not _has(_gen()["decision"]["blockers"], "diverged")
    side = w["gen"].commit(w["g"][0], msg="side work on top of v0.1.0, never released")
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": side}, msg="pin side work")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    gen = _gen()
    assert gen["from"]["commit"] == side and gen["change"]["diverged"] is True
    assert _has(gen["decision"]["blockers"], "diverged")


@pytest.mark.parametrize("cls,blocks", [("fix", False), ("feature", False), ("none", True), ("breaking", True),
                                        ("unknown", True)])
def test_only_fix_or_feature_passes_the_class_condition(tmp_path, monkeypatch, cls, blocks):
    from nanosoc_rm.bumps import interfaces
    world(tmp_path, monkeypatch)
    real = interfaces.provider

    def fake(spec):
        if spec == interfaces.CLASSIFIER and cls != "unknown":
            return lambda *a, **k: {"class": cls, "detail": "fixture", "sources": []}
        return None if spec == interfaces.CLASSIFIER else real(spec)
    monkeypatch.setattr(interfaces, "provider", fake)
    if cls == "unknown":                                        # the fixture changelog leaves 0.1.1 unclassified
        monkeypatch.setattr("nanosoc_rm.bumps.changelog.declared_class", lambda s: None)
    gen = _gen()
    assert gen["change"]["class"] == cls
    assert _has(gen["decision"]["blockers"], "change class %s" % cls) is blocks


def test_a_person_moving_to_a_newer_tag_without_the_proposal_disagrees(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    gen = _gen(store=True)
    g6 = w["gen"].commit(w["g"][0], msg="a branch off v0.1.0, released without the v0.2.0 work")
    w["gen"].set(GL_GEN, "tags", "v0.3.0", g6)
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": g6}, msg="person pins v0.3.0")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    out = {e["id"]: e for e in bumps.ledger()["fact"]["entries"]}
    assert out[gen["id"]]["outcome"] == "disagreed"
    g7 = w["gen"].commit(w["g"][3], msg="v0.4.0 contains v0.2.0")
    w["gen"].set(GL_GEN, "tags", "v0.4.0", g7)
    a4 = w["arch"].commit(a3, links={"nanosoc_gen": g7}, msg="person pins v0.4.0")
    w["arch"].set(GL_ARCH, "heads", "main", a4)
    out = {e["id"]: e for e in bumps.ledger()["fact"]["entries"]}
    assert out[gen["id"]]["outcome"] == "superseded"


@pytest.mark.parametrize("weeks,decided,met", [(4, 20, True), (10, 19, False), (3.9, 100, False), (0, 0, False)])
def test_the_e2_threshold_needs_both_four_weeks_and_twenty_bumps(weeks, decided, met):
    from nanosoc_rm.bumps.ledger import threshold_met
    assert threshold_met(weeks, decided) is met


def test_five_weeks_with_no_decided_bump_does_not_meet_the_threshold(tmp_path, monkeypatch):
    from nanosoc_rm import bumps, clock
    world(tmp_path, monkeypatch)
    _scan(store=True)
    later = clock.iso(clock.parse(clock.iso()) + __import__("datetime").timedelta(weeks=5))
    th = bumps.ledger(now=later)["fact"]["threshold"]
    assert th["met"] is False and "5.0 of 4 weeks" in th["reason"]


# ----- review item 4: no ledger and no git inside a request ------------------------------------------------------
def test_pages_read_the_stored_ledger_and_run_no_git(tmp_path, monkeypatch):
    from test_bumps_cli_web import _local, _signed_in
    from nanosoc_rm import bumps, gitrun
    import nanosoc_rm.analysis._inputs as inp
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    c = _signed_in(create_app(w["cfg"]))
    gen = next(d["fact"] for d in bumps.stored("eth-main") if d["fact"]["dependency"]["component"] == "nanosoc_gen")
    seen = []

    def spy(*args, **kw):
        seen.append(args)
        raise AssertionError("git inside a request")

    def no_ledger(*a, **k):
        raise AssertionError("the ledger is computed inside a request")
    monkeypatch.setattr(gitrun, "run_git", spy)
    monkeypatch.setattr(inp, "run_git", spy)
    import sys
    monkeypatch.setattr(sys.modules["nanosoc_rm.bumps.ledger"], "ledger", no_ledger)
    monkeypatch.setattr(bumps, "ledger", no_ledger)
    for path in ("/bumps/", "/bumps/p/%s" % gen["id"], "/bumps/ledger", "/bumps/facts/bump_ledger:all"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert "could not be computed" not in r.text
    assert 'class="pill state-grey">pending<' in c.get("/bumps/ledger").text
    assert not seen


def test_a_mark_queues_a_ledger_refresh_job(tmp_path, monkeypatch):
    from test_bumps_cli_web import _csrf, _local, _signed_in
    from nanosoc_rm import bumps, jobs
    from nanosoc_rm.config import load_config
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    c = _signed_in(create_app(w["cfg"]))
    pid = bumps.stored("eth-main")[0]["fact"]["id"]
    token = _csrf(c, "/bumps/")
    r = c.post("/bumps/p/%s/mark" % pid, data={"csrf_token": token, "decision": "declined"}, follow_redirects=False)
    assert r.status_code == 303
    q = jobs.jobs(load_config(), kind="bumps")
    assert len(q) == 1 and q[0]["command"] == "nanosoc-rm bumps ledger --store"


def test_two_hundred_kept_proposals_render_quickly(tmp_path, monkeypatch):
    import copy
    from test_bumps_cli_web import _local, _signed_in
    from nanosoc_rm import bumps
    from nanosoc_rm.bumps import store
    from nanosoc_rm.config import load_config
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    cfg = load_config()
    base = bumps.stored("eth-main")[0]
    docs = []
    for n in range(200):
        d = copy.deepcopy(base)
        d["fact"]["id"] = "bp-%012x" % n
        d["subject"] = "bump_proposal:eth-main:%s" % d["fact"]["id"]
        docs.append(d)
    store.record(cfg, docs, "eth-main")
    store.record_ledger(cfg, bumps.ledger(config=cfg))
    c = _signed_in(create_app(w["cfg"]))
    for path in ("/bumps/", "/bumps/p/%s" % docs[0]["fact"]["id"], "/bumps/ledger"):
        t0 = time.monotonic()
        assert c.get(path).status_code == 200
        assert time.monotonic() - t0 < 3.0, path


# ----- review item 6: one proposal kept for two configurations ---------------------------------------------------
def _multicore_walk(w):
    root = {"component": "nanosoc-multicore-system", "repo": GL_MC, "commit": w["m1"]}
    archp = {"component": "nanosoc_arch_tech", "repo": GL_ARCH, "commit": w["a"][0]}
    store_walk("multicore-master", [
        pin("multicore-master", "nanosoc_arch_tech", "nanosoc_arch_tech", 1, root, "nanosoc_arch_tech", GL_ARCH,
            w["a"][0], role="copy"),
        pin("multicore-master", "nanosoc_arch_tech/nanosoc_gen", "nanosoc_gen", 2, archp, "nanosoc_gen", GL_GEN,
            w["g"][0]),
    ])


def test_a_proposal_shared_by_two_configurations_keeps_each_ones_document(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    from nanosoc_rm.web import create_app
    w = world(tmp_path, monkeypatch)
    _multicore_walk(w)
    eth = _gen(store=True)
    mc = _by_dep(_scan("multicore-master", store=True)[1])["nanosoc_gen"]
    assert eth["id"] == mc["id"]
    kept = [d for d in bumps.stored("eth-main") if d["fact"]["id"] == eth["id"]]
    assert [d["fact"]["configuration"] for d in kept] == ["eth-main"]
    assert kept[0]["fact"]["matrix"]["pins"] == ["nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=%s" % w["g"][3]]
    assert bumps.get(eth["id"], configuration="eth-main")["subject"] == "bump_proposal:eth-main:%s" % eth["id"]
    assert bumps.get(eth["id"], configuration="multicore-master")["fact"]["matrix"]["pins"] == [
        "nanosoc_arch_tech/nanosoc_gen=%s" % w["g"][3]]
    from test_bumps_cli_web import PASSWORD, _signed_in
    from nanosoc_rm import auth
    h = auth.hash_password(PASSWORD, salt=b"0123456789abcdef")
    w["cfg"].write_text(w["cfg"].read_text() + '\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\nhash = "%s"\n' % h)
    c = _signed_in(create_app(w["cfg"]))
    assert c.get("/bumps/facts/bump_proposal:eth-main:%s" % eth["id"]).status_code == 200
    assert c.get("/bumps/facts/bump_proposal:multicore-master:%s" % eth["id"]).status_code == 200
    page = c.get("/bumps/p/%s?configuration=eth-main" % eth["id"]).text
    assert "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=" in page
    led = bumps.ledger()["fact"]
    assert [e["id"] for e in led["entries"]].count(eth["id"]) == 1, "one move is judged once"


# ----- review item 7: introduced and fixed inside the range ------------------------------------------------------
def test_a_defect_introduced_and_fixed_inside_the_range_is_neither(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    world(tmp_path, monkeypatch)
    gen = _gen()
    assert gen["defects"]["fixed"] == [] and gen["defects"]["introduced"] == []
    assert gen["defects"]["within_range"][0]["id"] == "SL-DEF-0042"
    assert not _has(gen["decision"]["blockers"], "introduces SL-DEF-0042")
    msg = bumps.commit_message(gen)
    assert "INTRODUCES" not in msg and "Fixes SL-DEF-0042" not in msg


def test_a_defect_introduced_and_not_yet_fixed_is_introduced(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch, manifest_range="~0.1")
    gen = _gen()
    assert gen["to"]["tag"] == "v0.1.1"
    assert [d["id"] for d in gen["defects"]["introduced"]] == ["SL-DEF-0042"] and gen["defects"]["fixed"] == []
    assert _has(gen["decision"]["blockers"], "introduces SL-DEF-0042")
