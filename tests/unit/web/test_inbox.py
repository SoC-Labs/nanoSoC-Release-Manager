"""The inbox cards (Wave C spec section 2, 7.1 unit I; GET /inbox since CR-WF-gui, the board on the projects
home): one test per card kind (built from stored facts, absent
without them), the ranking table, done / snooze / undo per login, the board under the inbox, and no job from a GET.
Fixtures follow the SHAPES of the facts.md world (synthetic where it matters, example.invalid URLs)."""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, clock, contracts, db, jobs
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app, inbox, uistate
from webfix import EXAMPLES, Hooks, make_registry, seed, write_config

NOW = clock.now()


def example(name, kind=None):
    return [d for _, d in contracts.docs_in(EXAMPLES / name) if kind is None or d["kind"] == kind]


def _no_ci(cfg):
    """A config with no [ci] project: Run is local, so the local-only POSTs (section 13a) are registered."""
    text = cfg.read_text()
    text = re.sub(r'\[ci\]\nproject = "[^"]*"\nallowed_runner_ids = \[\d+\]\n', "", text)
    text += '[hosts.gitlab]\nweb = "https://gitlab.example.invalid"\napi = "https://gitlab.example.invalid/api/v4"\n'
    cfg.write_text(text)
    return cfg


def _site(service, tmp_path, *, empty=False, local=True):
    """A registry and a config of its own under `tmp_path/<name>`; the data dir is the service's seeded copy of
    the contract examples, or an empty one."""
    import shutil
    root = tmp_path / ("empty" if empty else "seeded") / ("local" if local else "ci")
    root.mkdir(parents=True)
    reg = make_registry(root / "registry")
    data = root / "data"
    data.mkdir()
    if not empty:
        shutil.copy(tmp_path / "data" / "rm.sqlite", data / "rm.sqlite")
    cfg = write_config(root / "rm.toml", data=data, github_url=service["github"].url, registry=reg)
    if local:
        _no_ci(cfg)
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    client.get("/")                                   # reads the registry records into the store
    return {"cfg": cfg, "registry": reg, "client": client, "config": load_config(cfg), "tmp": tmp_path}


@pytest.fixture
def site(service, registry_spy, tmp_path):
    return _site(service, tmp_path)


@pytest.fixture
def empty(service, registry_spy, tmp_path):
    return _site(service, tmp_path, empty=True)


# ----- the facts.md world -----------------------------------------------------------------------------------
def _pin(path, comp=None, url_kind="https", **items):
    base = {"fetchable": "pass", "reachable": "pass", "no_ssh": "pass", "vendor": "pass"}
    return {"tree_path": path, "tree_paths": [path], "component": comp or path.rsplit("/", 1)[-1],
            "commit": "%040x" % (sum(map(ord, path)) * 7919), "overridden": False, "replaces": None,
            "url_kind": url_kind, "state": "ON_DEFAULT", "split": False, "items": dict(base, **items)}


def _gate(gid, verdict, detail="", **kw):
    control = "not-applicable" if verdict == "NOT-RUN" else "fired"
    return dict({"id": gid, "verdict": verdict, "control": control, "detail": detail or verdict}, **kw)


BELOW = ["tidelink/deps/axi-chiplet-controller", "tidelink/deps/tidelink-gpio-phy", "tidelink/deps/tidelink-phy",
         "tidelink/deps/a", "tidelink/deps/b"]


def compat_doc(cid, nonce, gates, pins, verdict="FAIL", tier=None, at=None):
    at = at or clock.iso(NOW - dt.timedelta(minutes=38))
    ex = example("observation.compat-result.example.jsonl")
    d = copy.deepcopy(ex[1] if tier is not None else ex[0])
    f = d["fact"]
    f.update(configuration=cid, nonce=nonce, gates=gates, pins=pins, verdict=verdict, overrides=[], splits=[],
             not_run=sum(1 for g in gates if g["verdict"] == "NOT-RUN"), started_at=at, finished_at=at)
    if tier is not None:
        f["tier"].update(tier)
        f["run_verdict"] = "FAIL" if verdict == "FAIL" else tier["verdict"]
    d["subject"] = "compat_result:%s:%s" % (cid, nonce)
    d["observed_at"] = at
    assert db.check_observation(d) == [], db.check_observation(d)
    return d


def eth_main(nonce="96a4b07e5ea4" + "0" * 20, at=None):
    pins = [_pin("tidelink", url_kind="ssh", fetchable="fail", no_ssh="fail", vendor="not-reached"),
            _pin("nanosoc-multicore-system", vendor="fail"), _pin("tidechart")]
    pins += [_pin(p, fetchable="not-reached", vendor="not-reached") for p in BELOW]
    gates = [_gate("t0.infra.probe", "PASS"), _gate("t0.runner.hardened", "NOT-RUN", "local run"),
             _gate("t0.pins.fetchable", "FAIL", "37 of 43 fetched; not fetched: tidelink; 5 below not reached"),
             _gate("t0.pins.reachable", "PASS"), _gate("t0.urls.no-ssh-public", "FAIL", "pins by ssh URL: tidelink"),
             _gate("t0.manifest.valid", "NOT-RUN"),
             _gate("t0.clone.upgrade-path", "FAIL", "anonymous recursive clone of upgrade_from ab73e40 does not resolve"),
             _gate("t0.vendor.no-arm-ip", "FAIL", "1 with findings: nanosoc-multicore-system")]
    return compat_doc("eth-main", nonce, gates, pins, tier={"tier": "t1", "verdict": "INFRA",
                                                            "reason": "--force-tier: make lint: the tool is missing"}, at=at)


def eth_rc4(nonce="2eba94f36a86" + "0" * 20):
    w = lambda tp: {"tree_path": tp, "result": "fail", "covered_by": tp, "reason": "ssh in frozen tree",
                    "approved_by": "member-a"}
    pins = [_pin("ASIC/asic-toolkit", url_kind="ssh", fetchable="fail", reachable="unverified", no_ssh="fail",
                 vendor="not-reached"),
            _pin("tidelink", url_kind="ssh", fetchable="fail", no_ssh="fail", vendor="not-reached"),
            _pin("nanosoc-multicore-system", vendor="fail")]
    gates = [_gate("t0.infra.probe", "PASS"), _gate("t0.runner.hardened", "NOT-RUN", "local run"),
             _gate("t0.pins.fetchable", "WAIVED", raw_verdict="FAIL", waived=[w("tidelink"), w("ASIC/asic-toolkit")]),
             _gate("t0.pins.reachable", "WAIVED", raw_verdict="UNVERIFIED", waived=[w("ASIC/asic-toolkit")]),
             _gate("t0.urls.no-ssh-public", "WAIVED", raw_verdict="FAIL", waived=[w("tidelink"), w("ASIC/asic-toolkit")]),
             _gate("t0.manifest.valid", "NOT-RUN"), _gate("t0.clone.upgrade-path", "NOT-RUN"),
             _gate("t0.vendor.no-arm-ip", "FAIL", "38 of 45 trees scanned; 1 with findings: nanosoc-multicore-system")]
    return compat_doc("eth-rc4", nonce, gates, pins)


def uncover_asic_toolkit(cfg, at=None, component="asic-toolkit"):
    """The rc4 silicon table with asic-toolkit 4de7860 uncovered and UNREADABLE (facts.md section 4)."""
    at = at or clock.iso(NOW - dt.timedelta(hours=1))
    docs = copy.deepcopy([d for d in example("observation.example.jsonl", "silicon_protection")
                          if d["fact"]["configuration"] == "eth-rc4"])
    snap = {"id": "silicon_protection:eth-rc4@%s" % at, "scope": "silicon_protection:eth-rc4"}
    for d in docs:
        d["snapshot"], d["observed_at"] = snap, at
        if d["fact"]["component"] == component:
            d["fact"].update(covered=False, covering_tags=[], state="UNREADABLE")
    db.put_snapshot("silicon_protection:eth-rc4", docs, config=cfg)


def world(cfg):
    db.put(eth_rc4(), config=cfg)
    db.put(eth_main(), config=cfg)
    uncover_asic_toolkit(cfg)


def put_examples(cfg, *names):
    """The example observations of the store's kinds (an example file may also hold a view, e.g. matrix_view)."""
    for name in names:
        for d in example(name):
            if d["kind"] in db.SET_KINDS or d["kind"] in db.EVENT_KINDS:
                db.put(d, config=cfg)


def local_client(site):
    """A signed-in client of a LOCAL sign-in deployment over the same store (FakeMembership skips CSRF)."""
    from test_web_local import HASHES, sign_in
    cfg = site["cfg"].parent / "rm-local.toml"
    cfg.write_text(site["cfg"].read_text() + '[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\n'
                   'hash = "%s"\n' % HASHES["alice"])
    c = TestClient(create_app(cfg))
    assert sign_in(c).status_code == 303
    return c


def kinds(cards):
    return [c.kind for c in cards]


def of(cards, kind):
    return [c for c in cards if c.kind == kind]


# ----- one test per card kind (spec 2.3) -------------------------------------------------------------------
def test_an_empty_store_gives_only_the_registry_cards(empty):
    cs = inbox.cards(empty["config"])
    assert set(kinds(cs)) == {"never_checked", "errata_exposure"}, kinds(cs)


def test_1_compat_fail_names_the_deciding_cause_and_its_fix(site):
    cfg = site["config"]
    assert not [c for c in inbox.cards(cfg) if c.id == "compat_fail:eth-main"]
    db.put(eth_main(), config=cfg)
    c = [c for c in inbox.cards(cfg) if c.id == "compat_fail:eth-main"][0]
    assert c.title == "eth-main is FAIL: tidelink pinned by ssh URL."
    assert c.why.startswith("That one cause fails 2 gates and hides 5 trees from the vendor scan.")
    assert c.primary["label"] == "Try the fix in What-if"
    assert c.code.startswith("git config -f .gitmodules submodule.tidelink.url ")
    assert any(e["tag"] == "INFRA" for e in c.evidence)
    assert any("upgrade path from v1.0.0" in e["say"] for e in c.evidence)       # eligibility: {tag: v1.0.0}


def test_2_never_checked_is_one_card_with_a_run_form_per_configuration(site, empty):
    c = of(inbox.cards(empty["config"]), "never_checked")[0]
    assert c.title == "3 configurations have never been checked: eth-rc4, eth-main, multicore-master."
    assert [f["action"] for f in c.forms] == ["/configurations/eth-rc4/runs", "/configurations/eth-main/runs",
                                              "/configurations/multicore-master/runs"]
    assert c.dest == "queues one check per press (nanosoc-rm compat CONFIG --store)"
    c = of(inbox.cards(site["config"]), "never_checked")[0]                     # eth-rc4 has stored T0 runs
    assert c.title == "2 configurations have never been checked: eth-main, multicore-master."
    world(site["cfg"])
    c = of(inbox.cards(site["config"]), "never_checked")[0]
    assert c.title == "1 configuration has never been checked: multicore-master."


def test_3_silicon_uncovered(site):
    assert not of(inbox.cards(site["config"]), "silicon_uncovered")
    uncover_asic_toolkit(site["cfg"])
    c = of(inbox.cards(site["config"]), "silicon_uncovered")[0]
    assert c.title == "1 eth-rc4 silicon commit has no keep tag: asic-toolkit 4de7860."
    assert "git tag keep/eth-rc4/asic-toolkit 4de7860af95cbb003265f94e6c2b7b4a1618877f" in c.code
    assert "git push origin keep/eth-rc4/asic-toolkit" in c.code
    assert c.note == "UNREADABLE: run from a checkout that can read it."
    assert c.weight == 95


def _red(cfg, cid, state):
    docs = []
    for d in example("pin-state.example.jsonl"):
        d = copy.deepcopy(d)
        d["fact"]["configuration"] = cid
        d["subject"] = "pin_state:%s:%s" % (cid, d["fact"]["tree_paths"][0])
        d["snapshot"] = {"id": "pin_state:%s@test" % cid, "scope": "pin_state:%s" % cid}
        for h in d["fact"]["hosts"].values():
            if h["pinned"]:
                h["state"] = state
                h["tags"], h["lag"] = [], None
        docs.append(d)
    db.put_snapshot("pin_state:%s" % cid, docs[:1], config=cfg)
    return docs[0]["fact"]["tree_paths"][0]


def test_4_red_pin_not_already_named_by_a_compat_or_silicon_card(site):
    assert not of(inbox.cards(site["config"]), "red_pin")
    path = _red(site["cfg"], "eth-main", "MISSING")
    c = of(inbox.cards(site["config"]), "red_pin")[0]
    assert c.title == "eth-main: %s is MISSING." % path and c.weight == 40
    path = _red(site["cfg"], "eth-rc4", "MISSING")
    rc4 = [c for c in of(inbox.cards(site["config"]), "red_pin") if c.id.startswith("red_pin:eth-rc4:")]
    assert rc4 and rc4[0].weight == 70                                            # frozen
    assert path == "nanosoc-multicore-system/nanosoc_arch_tech"
    uncover_asic_toolkit(site["cfg"], component="nanosoc_arch_tech")              # the silicon card names it now
    cs = inbox.cards(site["config"])
    assert "silicon_uncovered:eth-rc4" in [c.id for c in cs]
    assert not [c for c in of(cs, "red_pin") if c.id.startswith("red_pin:eth-rc4:")]


def test_5_branches_deletable_from_stored_rows_only(site, monkeypatch):
    import nanosoc_rm.analysis.branches as br
    monkeypatch.setattr(br, "branches", lambda *a, **k: pytest.fail("the inbox computed branches in a request"))
    assert not of(inbox.cards(site["config"]), "branches_deletable")
    seed(site["cfg"], "observation.branch-state.example.jsonl")
    c = of(inbox.cards(site["config"]), "branches_deletable")[0]
    assert c.title == "1 nanosoc-ethernet-chiplet branch can be deleted."
    assert c.why.startswith("0 MERGED plus 1 STALE, minus 0 kept") and c.weight == 21


def test_6_host_drift_diverged_own_cards_and_one_summary(site, empty):
    assert not of(inbox.cards(empty["config"]), "host_drift")
    cs = of(inbox.cards(site["config"]), "host_drift")
    titles = sorted(c.title for c in cs)
    assert "ahb_qspi copies have diverged (1 / 5)." in titles and "asic-flow copies have diverged (4 / 1)." in titles
    assert "The gitlab copy of tidelink lacks 298 commits." in titles
    w = {c.title.split()[0]: c.weight for c in cs}
    assert w["asic-flow"] == 60 and w["ahb_qspi"] == 50 and w["The"] == 15         # +10 default-branch mismatch


def test_7_alert_group(site, empty):
    assert not of(inbox.cards(empty["config"]), "alert_group")
    c = of(inbox.cards(site["config"]), "alert_group")[0]                          # the example rewrite alert
    assert c.title.startswith("1 alert has one cause: branches moved without descent")
    assert c.primary["href"] == "/alerts/clear-group" and c.primary["fields"]["group"] == "rewrite"
    assert c.weight == 30


def test_8_quality_top_ranks_the_findings_that_matter(site):
    assert not of(inbox.cards(site["config"]), "quality_top")
    seed(site["cfg"], "observation.quality.example.jsonl")
    c = of(inbox.cards(site["config"]), "quality_top")[0]
    assert c.title == "demo has 2 comment findings; 1 are decoration (one rule). Review the 1 that matter first."
    assert c.why == "Top one: src/a.py:2 commented_out_code." and c.weight == 11


def test_9_waiver_unused_from_the_registry_and_from_an_alert(site):
    from webfix import git
    cfg = site["cfg"]
    db.put(eth_rc4(), config=cfg)                                    # every registry waiver's path is used
    assert not of(inbox.cards(site["config"]), "waiver_unused")
    elig = site["registry"] / "eligibility.yaml"
    doc = json.loads(elig.read_text())
    rc4 = next(c for c in doc["configurations"] if c["id"] == "eth-rc4")
    rc4["t0_waivers"].append(dict(rc4["t0_waivers"][0], gate="t0.manifest.valid", tree_path="gone/path"))
    elig.write_text(json.dumps(doc, indent=1) + "\n")
    git("commit", "-q", "-am", "a waiver for a path no gate fails", cwd=site["registry"])
    site["client"].get("/")                                          # the registry read stores the new record
    c = of(inbox.cards(site["config"]), "waiver_unused")[0]
    assert c.title == "1 waiver on eth-rc4 covers nothing."
    assert "{gate: t0.manifest.valid, tree_path: gone/path}" in c.code and c.weight == 35
    db.raise_alert("waiver_unused", "t0_result:eth-main:n1", "1 t0_waivers of eth-main covered nothing in run n1",
                   [{"kind": "derived", "command": "nanosoc-rm t0 runs eth-main --json"}], config=cfg)
    cs = of(inbox.cards(site["config"]), "waiver_unused")
    assert {c.id for c in cs} == {"waiver_unused:eth-rc4", "waiver_unused:eth-main"}
    assert not [c for c in of(inbox.cards(site["config"]), "alert_group") if "waiver" in c.title]


def test_10_errata_exposure_by_defect(site):
    c = of(inbox.cards(site["config"]), "errata_exposure")[0]
    assert c.title == "Defect SL-DEF-0001 is probably-exposed in eth-rc4." and c.weight == 45
    assert "ETH-R0P0-ERR-001 permanent" in c.why


def test_11_matrix_flaky_and_quarantine(site):
    assert not of(inbox.cards(site["config"]), "matrix_flaky")
    put_examples(site["cfg"], "observation.matrix.example.jsonl")
    cs = of(inbox.cards(site["config"]), "matrix_flaky")
    q = [c for c in cs if ":quarantine:" in c.id]
    assert q and q[0].title.startswith("eth-rc4: t0.pins.reachable is quarantined until ")
    cell = copy.deepcopy([d for d in example("observation.matrix.example.jsonl", "matrix_cell")][1])
    cell["fact"]["verdict"] = "FLAKY"
    cell["fact"]["attempts"][-1]["verdict"] = "PASS"
    cell["fact"]["flaky_gates"] = ["t0.pins.reachable"]
    cell["fact"]["nonce"] = "f" * 32
    cell["subject"] = db.subject_of(cell)
    db.put(cell, config=site["cfg"])
    cs = of(inbox.cards(site["config"]), "matrix_flaky")
    flaky = [c for c in cs if ":quarantine:" not in c.id]
    assert flaky and flaky[0].title == "eth-rc4: tidelink at latest-tag is FLAKY." and flaky[0].weight == 40
    assert flaky[0].primary["href"] == "/matrix/eth-rc4/cell?component=tidelink&column=latest-tag"


def test_11_a_quarantine_expiring_within_3_days_weighs_more(site):
    put_examples(site["cfg"], "observation.matrix.example.jsonl")
    q = [d for d in example("observation.matrix.example.jsonl", "matrix_quarantine")][0]
    soon = clock.parse(q["fact"]["expires_at"]) - dt.timedelta(days=2)
    c = [c for c in inbox.cards(site["config"], now=soon) if ":quarantine:" in c.id][0]
    assert c.weight == 55
    later = clock.parse(q["fact"]["expires_at"]) - dt.timedelta(days=10)
    assert [c for c in inbox.cards(site["config"], now=later) if ":quarantine:" in c.id][0].weight == 40


def test_12_bump_proposals_waiting_minus_marked(site):
    from nanosoc_rm.bumps import store
    assert not of(inbox.cards(site["config"]), "bump_proposal")
    p = example("observation.bumps.example.jsonl", "bump_proposal")[0]
    store.record(site["config"], [p], "eth-main")
    c = of(inbox.cards(site["config"]), "bump_proposal")[0]
    assert c.title == "1 bump proposal is waiting: nanosoc_arch_tech nanosoc_gen v0.1.0 to v0.2.0."
    assert c.primary["href"] == "/bumps/p/bp-baf82b05fd03" and c.weight == 25
    store.mark(site["config"], p["fact"]["id"], "alice", "done")
    assert not of(inbox.cards(site["config"]), "bump_proposal")


def test_13_release_draft_that_no_longer_verifies(site):
    v = copy.deepcopy(example("observation.release.example.jsonl", "release_verify")[0])
    db.put(v, config=site["cfg"])
    assert not of(inbox.cards(site["config"]), "release_draft")                  # PASS: nothing to do
    v["fact"]["verdict"] = "FAIL"
    v["fact"]["checks"][1]["verdict"] = "FAIL"
    v["observed_at"] = clock.iso()
    db.put(v, config=site["cfg"])
    c = of(inbox.cards(site["config"]), "release_draft")[0]
    assert c.title == "Release eth-rc4 1.0.0 no longer verifies." and c.weight == 60
    assert c.primary["href"] == "/releases/eth-rc4/1.0.0"


def test_the_wave_b_kinds_are_skipped_when_their_package_is_absent(site, monkeypatch):
    put_examples(site["cfg"], "observation.matrix.example.jsonl", "observation.release.example.jsonl")
    for name in ("nanosoc_rm.matrix.cells", "nanosoc_rm.bumps", "nanosoc_rm.releases"):
        monkeypatch.setitem(sys.modules, name, None)                             # import raises ImportError
    problems = []
    cs = inbox.cards(site["config"], problems=problems)
    assert not {"matrix_flaky", "bump_proposal", "release_draft"} & set(kinds(cs)) and problems == []


# ----- ranking (spec 2.3 Ranking) --------------------------------------------------------------------------
def test_the_ranking_of_the_facts_md_world(site):
    world(site["cfg"])
    cs = inbox.cards(site["config"])
    assert [c.id for c in cs[:3]] == ["compat_fail:eth-main", "compat_fail:eth-rc4", "silicon_uncovered:eth-rc4"]
    assert [c.weight for c in cs[:3]] == [165, 110, 95]
    order = [c.kind for c in cs]
    assert order.index("errata_exposure") < order.index("alert_group")
    assert inbox.headline(cs) == "Next: eth-main is FAIL: tidelink pinned by ssh URL."
    assert inbox.headline([]) == "Inbox clear. Nothing needs you right now."
    red = [c for c in cs if c.kind == "red_pin" and c.id.startswith("red_pin:eth-rc4:ASIC/asic-toolkit")]
    assert not red, "a pin the silicon card already names gets no red-pin card"


def test_a_fact_older_than_7_days_ranks_at_0_8(site):
    old = clock.iso(NOW - dt.timedelta(days=8))
    db.put(eth_main(at=old), config=site["cfg"])
    c = of(inbox.cards(site["config"]), "compat_fail")[0]
    assert c.weight == 165 and c.rank == pytest.approx(132.0)


def test_ties_go_to_frozen_then_the_older_fact():
    a = inbox.Card(id="a:1", kind="red_pin", title="a", why="", chip=("x", "c-fail"), weight=40,
                   fact_at=clock.iso(NOW))
    b = inbox.Card(id="b:1", kind="red_pin", title="b", why="", chip=("x", "c-fail"), weight=40,
                   fact_at=clock.iso(NOW), frozen=True)
    c = inbox.Card(id="c:1", kind="red_pin", title="c", why="", chip=("x", "c-fail"), weight=40,
                   fact_at=clock.iso(NOW - dt.timedelta(hours=1)))
    assert [x.id for x in inbox._rank([a, b, c], NOW)] == ["b:1", "c:1", "a:1"]


def test_the_infra_only_rule_and_the_fixable_bonus():
    w = inbox.WEIGHTS["compat_fail"]
    assert (w["base"], w["per_gate"], w["per_not_reached"], w["fixable"], w["infra_only"]) == (100, 10, 5, 20, -40)
    assert set(inbox.WEIGHTS) == set(inbox.CARD_KINDS)


# ----- done, snooze, undo (spec 2.4) -----------------------------------------------------------------------
def _csrf(text):
    return re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)


def _cards_on(text):
    return [e["data-card"] for e in Hooks(text).with_attr("data-card")]


def test_done_hides_until_the_facts_change_and_undo_restores(site):
    world(site["cfg"])
    c = site["client"]
    page = c.get("/inbox").text
    assert "compat_fail:eth-main" in _cards_on(page)
    fp = re.search(r'data-card="compat_fail:eth-main".*?name="fp" value="([0-9a-f]+)"', page, re.S).group(1)
    r = c.post("/inbox/done", data={"csrf_token": _csrf(page), "card": "compat_fail:eth-main", "fp": fp},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    page = c.get("/inbox").text
    assert "compat_fail:eth-main" not in _cards_on(page) and "Done (1) today" in page
    # a new compat result changes the card's facts: it reopens
    db.put(eth_main(nonce="cfde584f53c4" + "0" * 20, at=clock.iso(NOW - dt.timedelta(minutes=5))), config=site["cfg"])
    page = c.get("/inbox").text
    assert "compat_fail:eth-main" in _cards_on(page) and "Done (0) today" in page
    # done again, then undo
    fp = re.search(r'data-card="compat_fail:eth-main".*?name="fp" value="([0-9a-f]+)"', page, re.S).group(1)
    c.post("/inbox/done", data={"csrf_token": _csrf(page), "card": "compat_fail:eth-main", "fp": fp})
    assert "compat_fail:eth-main" not in _cards_on(c.get("/inbox").text)
    r = c.post("/inbox/undo", data={"csrf_token": _csrf(page), "card": "compat_fail:eth-main"},
               headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text.lstrip().startswith('<section id="inbox"')
    assert "compat_fail:eth-main" in _cards_on(r.text)


def test_snooze_hides_until_the_date(site):
    world(site["cfg"])
    c = site["client"]
    page = c.get("/inbox").text
    r = c.post("/inbox/snooze", data={"csrf_token": _csrf(page), "card": "silicon_uncovered:eth-rc4", "days": "7",
                                      "cat": "silicon"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox?cat=silicon"
    assert "silicon_uncovered:eth-rc4" not in _cards_on(c.get("/inbox").text)
    v = inbox.inbox_view(site["config"], "alice", now=NOW + dt.timedelta(days=8))
    assert "silicon_uncovered:eth-rc4" in [x.id for x in v["cards"]]
    assert c.post("/inbox/snooze", data={"csrf_token": _csrf(page), "card": "x:y", "days": "3"}).status_code == 400


def test_two_logins_do_not_see_each_others_state(site):
    world(site["cfg"])
    page = site["client"].get("/inbox").text
    fp = re.search(r'data-card="compat_fail:eth-rc4".*?name="fp" value="([0-9a-f]+)"', page, re.S).group(1)
    site["client"].post("/inbox/done", data={"csrf_token": _csrf(page), "card": "compat_fail:eth-rc4", "fp": fp})
    carol = TestClient(create_app(site["cfg"], auth=auth.FakeMembership(login="carol")))
    assert "compat_fail:eth-rc4" in _cards_on(carol.get("/inbox").text)
    assert "compat_fail:eth-rc4" not in _cards_on(site["client"].get("/inbox").text)
    assert set(uistate.card_state(site["config"], "carol")) == set()


def test_the_inbox_posts_need_csrf_and_a_local_deployment(site, service, registry_spy, tmp_path):
    lc = local_client(site)
    assert lc.post("/inbox/done", data={"card": "x:y", "fp": "ab"}).status_code == 403          # no CSRF token
    assert lc.post("/inbox/done", data={"csrf_token": _csrf(lc.get("/inbox").text), "card": "x:y", "fp": "ab"},
                   follow_redirects=False).status_code == 303
    ci = _site(service, tmp_path, local=False)
    page = ci["client"].get("/inbox").text
    assert ci["client"].post("/inbox/done", data={"csrf_token": _csrf(page), "card": "x:y", "fp": "ab"}
                             ).status_code in (404, 405)
    assert 'action="/inbox/done"' not in page                                    # no buttons for a missing route


# ----- the page --------------------------------------------------------------------------------------------
def test_a_get_never_submits_a_job_or_creates_gui_state(site, monkeypatch):
    world(site["cfg"])
    put_examples(site["cfg"], "observation.matrix.example.jsonl", "observation.release.example.jsonl",
                 "observation.quality.example.jsonl", "observation.branch-state.example.jsonl")
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: pytest.fail("a GET submitted a job"))
    import nanosoc_rm.matrix as matrix
    for name in ("queue", "nightly_plan"):
        if hasattr(matrix, name):
            monkeypatch.setattr(matrix, name, lambda *a, **k: pytest.fail("a GET reached the matrix queue"))
    for path in ("/", "/inbox", "/inbox?cat=compat", "/inbox?cat=silicon", "/notifications", "/alerts",
                 "/configurations/eth-rc4"):
        assert site["client"].get(path).status_code == 200, path
    assert not uistate.path(site["config"]).exists()


def test_filters_show_their_counts_and_press_state(site):
    world(site["cfg"])
    text = site["client"].get("/inbox?cat=silicon").text
    pressed = re.findall(r'name="cat" value="([a-z]+)" class="seg" aria-pressed="(true|false)">([^<]+)<span class="mono">(\d+)',
                         text)
    assert [p[0] for p in pressed] == [k for k, _ in inbox.CATS]
    assert [p[0] for p in pressed if p[1] == "true"] == ["silicon"]
    on = {p[0]: int(p[3]) for p in pressed}
    assert on["silicon"] == len([x for x in _cards_on(text)]) and on["all"] >= on["silicon"] + on["compat"]
    assert '<li aria-current="page">Silicon</li>' in text


def test_every_status_chip_on_the_inbox_carries_text(site):
    world(site["cfg"])
    text = site["client"].get("/inbox").text
    chips = re.findall(r'<span class="chip (c-[a-z]+)">([^<]*)</span>', text)
    assert chips and all(t.strip() for _, t in chips)
    assert text.count("data-age-seconds") == 1


def test_the_board_names_the_deciding_gate_and_hides_uniform_columns(empty):
    world(empty["cfg"])
    text = empty["client"].get("/").text
    row = re.search(r'<tr data-configuration="eth-main".*?</tr>', text, re.S).group(0)
    cell = re.search(r'data-item="compat-result"[^>]*>(.*?)</td>', row, re.S).group(1)
    assert ">FAIL<" in cell and "tidelink pinned by ssh URL" in cell and "Try the fix in What-if" in cell
    assert re.search(r'<td data-item="t0-result" data-label="T0" hidden>', row)        # no CI here on every row
    assert re.search(r'<td data-item="runner-status" data-label="Runner" hidden>', row)
    assert 'so the <span title="T0">resolve check</span> (CI) column is hidden' in text
    from test_web_pages import cells
    p, sw = cells(text)
    assert sw.check_h1({"/": text}, configurations=["eth-rc4", "eth-main", "multicore-master"], cli_subjects={}) == []


def test_the_board_keeps_the_t0_column_when_ci_runs_exist(service, registry_spy, tmp_path):
    s = _site(service, tmp_path, local=False)
    text = s["client"].get("/").text
    row = re.search(r'<tr data-configuration="eth-rc4".*?</tr>', text, re.S).group(0)
    assert re.search(r'<td data-item="t0-result" data-label="T0">', row)


# ----- review fixes (I-R1, I-R2, I-R4, I-R5) ------------------------------------------------------------------
def _fp_of(text, card):
    return re.search(r'data-card="%s".*?name="fp" value="([0-9a-f]+)"' % re.escape(card), text, re.S).group(1)


def _restore_host_compares(cfg, change=None, minutes_ago=2):
    """Store every host_compare scope again, as a new producer run would: same facts, a new snapshot and a new
    observed_at (`change` may edit one fact first)."""
    at = clock.iso(clock.now() - dt.timedelta(minutes=minutes_ago))       # one snapshot id per producer run
    scopes: dict[str, list] = {}
    for d in db.latest("host_compare", "host_compare:", config=cfg):
        scopes.setdefault(d["snapshot"]["scope"], []).append(copy.deepcopy(d))
    for scope, docs in scopes.items():
        for d in docs:
            d["observed_at"] = at
            d["snapshot"] = {"id": "%s@%s" % (scope, at), "scope": scope}
            if change is not None:
                change(d)
        db.put_snapshot(scope, docs, config=cfg)


def test_done_holds_across_a_producer_run_that_changed_nothing(site):
    card = "host_drift:asic-flow:gitlab:soclabs/asic_flow"
    c = site["client"]
    page = c.get("/inbox").text
    assert card in _cards_on(page)
    r = c.post("/inbox/done", data={"csrf_token": _csrf(page), "card": card, "fp": _fp_of(page, card)},
               follow_redirects=False)
    assert r.status_code == 303 and card not in _cards_on(c.get("/inbox").text)
    _restore_host_compares(site["cfg"])                                   # the next mirror sync: same facts
    assert card not in _cards_on(c.get("/inbox").text), "a done card reopened though no fact changed"

    def more(d):
        if d["fact"]["component"] == "asic-flow":
            d["fact"]["copy_only"] = (d["fact"].get("copy_only") or 0) + 1
    _restore_host_compares(site["cfg"], more, minutes_ago=1)                             # a fact changed: it reopens
    assert card in _cards_on(c.get("/inbox").text)


def _t0(cid, nonce, verdict, acceptance="rejected", reason="captured commit differs from the requested one",
        at=None):
    at = at or clock.iso(NOW - dt.timedelta(minutes=10))
    d = copy.deepcopy(next(x for x in example("observation.example.jsonl", "t0_result")
                           if x["fact"]["acceptance"] == "rejected"))
    d["fact"].update(configuration=cid, verdict=verdict, acceptance=acceptance, reason=reason)
    d["fact"]["run"].update(nonce=nonce, triggered_at=at)
    d["subject"], d["observed_at"] = "t0_result:%s:%s" % (cid, nonce), at
    assert db.check_observation(d) == [], db.check_observation(d)
    return d


def test_a_stored_t0_result_is_a_verdict_in_a_ci_deployment(service, registry_spy, tmp_path):
    s = _site(service, tmp_path, local=False)
    nonce = "a" * 32
    db.put(_t0("eth-rc4", nonce, "UNVERIFIED"), config=s["cfg"])
    cs = inbox.cards(s["config"])
    never = of(cs, "never_checked")[0]
    assert "eth-rc4" not in never.title and never.title.startswith("2 configurations have never been checked")
    assert "CI" in never.dest and "nanosoc-rm compat" not in never.dest
    red = [c for c in cs if c.id == "compat_fail:eth-rc4"]
    assert red and red[0].chip[0] == "UNVERIFIED" and "latest CI resolve check" in red[0].title
    assert red[0].primary["href"] == "/runs/%s" % nonce and "captured commit differs" in red[0].why
    head = re.search(r'<h2[^>]*id="h-next"[^>]*>(.*?)</h2>', s["client"].get("/inbox").text, re.S).group(1)
    assert "eth-rc4" in head and "never been checked: eth-rc4" not in head


def test_an_unverified_compat_result_is_a_card(site):
    gates = [_gate(g["id"], "UNVERIFIED", "1 pin not verified") if g["id"] == "t0.pins.reachable"
             else g if g["verdict"] != "FAIL" else _gate(g["id"], "PASS") for g in eth_main()["fact"]["gates"]]
    pins = [_pin("tidechart", reachable="unverified")]
    db.put(compat_doc("eth-main", "b" * 32, gates, pins, verdict="UNVERIFIED"), config=site["cfg"])
    c = [x for x in inbox.cards(site["config"]) if x.id == "compat_fail:eth-main"]
    assert c and c[0].chip[0] == "UNVERIFIED" and c[0].title.startswith("eth-main is UNVERIFIED: ")


def test_every_card_button_names_its_card(site):
    world(site["cfg"])
    text = site["client"].get("/inbox").text
    heads = set(re.findall(r'<h3 id="(card-h-\d+)"', text))
    assert heads
    for label in ("Mark done", "Snooze", "1 day", "7 days", "30 days"):
        ctrls = re.findall(r'<(?:button|summary)([^>]*)>%s</(?:button|summary)>' % label, text)
        assert len(ctrls) == len(heads), label
        refs = [re.search(r'aria-describedby="([^"]+)"', a) for a in ctrls]
        assert all(refs) and {m.group(1) for m in refs} == heads, label


def test_11_a_tier_cell_neither_hides_nor_poses_as_the_t0_cell(site):
    """A newer t1 cell of the same (component, column) keeps the T0 FLAKY card; its own FLAKY card says t1."""
    put_examples(site["cfg"], "observation.matrix.example.jsonl")
    base = [d for d in example("observation.matrix.example.jsonl", "matrix_cell")][1]
    t0 = copy.deepcopy(base)
    t0["fact"].pop("tier", None)
    t0["fact"]["verdict"] = "FLAKY"
    t0["fact"]["attempts"][-1]["verdict"] = "PASS"
    t0["fact"]["flaky_gates"] = ["t0.pins.reachable"]
    t0["fact"]["nonce"] = "f" * 32
    t0["subject"] = db.subject_of(t0)
    db.put(t0, config=site["cfg"])
    t1 = copy.deepcopy(t0)
    t1["fact"].update(tier="t1", force_tier=False, verdict="PASS", flaky_gates=[], nonce="e" * 32,
                      finished_at="2099-01-01T00:00:00Z", attempts=t1["fact"]["attempts"][-1:])
    t1["subject"] = db.subject_of(t1)
    db.put(t1, config=site["cfg"])
    flaky = [c for c in of(inbox.cards(site["config"]), "matrix_flaky") if ":quarantine:" not in c.id]
    assert [c.primary["href"] for c in flaky] == ["/matrix/eth-rc4/cell?component=tidelink&column=latest-tag"]
    t1["fact"].update(verdict="FLAKY", flaky_gates=["t0.pins.reachable"], attempts=t0["fact"]["attempts"])
    db.put(t1, config=site["cfg"])
    flaky = [c for c in of(inbox.cards(site["config"]), "matrix_flaky") if ":quarantine:" not in c.id]
    hrefs = sorted(c.primary["href"] for c in flaky)
    assert hrefs == ["/matrix/eth-rc4/cell?component=tidelink&column=latest-tag",
                     "/matrix/eth-rc4/cell?component=tidelink&column=latest-tag&tier=t1"], hrefs
    assert len({c.id for c in flaky}) == 2 and any("(t1)" in c.title for c in flaky)
