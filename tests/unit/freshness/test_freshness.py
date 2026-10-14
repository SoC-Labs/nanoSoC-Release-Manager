"""The freshness engine (interfaces.md section 24; CR-WF-fresh): the rule (pure), the scan on a hermetic estate,
the stored-facts view the pages call, the job kind, the scheduler hook, the CLI and the page."""
from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from fresh_fx import TODAY, at_pins, build, synced_and_walked

from nanosoc_rm import contracts, db
from nanosoc_rm.freshness import rules

D = dt.timedelta


# ----- the rule, pure -----------------------------------------------------------------------------------------------
def _pin(behind=None, bumped_days=None, unpushed=False, frozen=False, depth=1, path="p", lower_bound=False):
    bump = None if bumped_days is None else {"date": "%sT12:00:00Z" % (TODAY - D(days=bumped_days)).isoformat()}
    return {"tree_path": path, "component": path, "depth": depth, "behind": behind, "unpushed": unpushed,
            "frozen": frozen, "bump": bump, "lower_bound": lower_bound, "pin_date": None}


@pytest.mark.parametrize("pin, state", [
    (_pin(0, 400), "current"),
    (_pin(9, 89), "behind"),                      # 1-9 behind, bumped within 90 d
    (_pin(10, 1), "outdated"),                    # 10+ behind
    (_pin(1, 90), "outdated"),                    # behind and not bumped for 90 d
    (_pin(1, None), "behind"),                    # bump date unknown: never guessed outdated
    (_pin(None, 1), "not_measured"),
    (_pin(None, 1, unpushed=True), "unpushed"),   # unpushed is decided before not measured
    (_pin(50, 500, unpushed=True), "unpushed"),
])
def test_the_pin_ladder(pin, state):
    assert rules.judge_pin(pin, TODAY)["state"] == state


def test_a_frozen_pin_says_what_it_would_be_and_is_never_lagging():
    j = rules.judge_pin(_pin(50, 500, frozen=True), TODAY)
    assert (j["state"], j["would_be"], j["live"], j["lagging"]) == ("frozen", "outdated", "outdated", False)


@pytest.mark.parametrize("days, word", [(0, "fresh"), (29, "fresh"), (30, "aging"), (89, "aging"), (90, "stale"),
                                        (None, None)])
def test_age_words(days, word):
    assert rules.age_word(days) == word


def test_days_count_utc_calendar_days():
    assert rules.days_since("2026-10-13T23:30:00-01:00", TODAY) == 0          # 00:30 UTC on the 14th
    assert rules.days_since("2026-10-14T00:30:00+02:00", TODAY) == 1          # 22:30 UTC on the 13th


def test_the_tree_verdict_is_the_worst_direct_pin_and_unmeasured_is_never_current():
    deep = _pin(40, 300, depth=2, path="a/deep")
    assert rules.judge_tree(False, [_pin(0, 5, path="a"), deep], TODAY)["state"] == "current"   # direct only
    j = rules.judge_tree(False, [_pin(0, 5, path="a"), _pin(None, 5, path="b"), deep], TODAY)
    assert j["state"] == "not_measured", "a direct pin nobody measured must not read as current"
    assert rules.judge_tree(False, [_pin(3, 5, path="a"), _pin(None, 5, path="b")], TODAY)["state"] == "behind"
    assert rules.judge_tree(False, [], TODAY)["state"] == "not_measured"
    assert (j["direct"], j["direct_lagging"], j["lagging"], j["pins"]) == (2, 0, 1, 3)
    assert j["newest_bump"]["days"] == 5 and j["age"] == "fresh" and j["stalest_lagging"]["tree_path"] == "a/deep"


def test_a_frozen_tree_counts_nothing_and_keeps_what_it_would_be():
    j = rules.judge_tree(True, [_pin(12, 5, path="a"), _pin(1, 5, path="b")], TODAY)
    assert (j["state"], j["would_be"], j["counted"]) == ("frozen", "outdated", False)
    assert (j["lagging"], j["direct_lagging"], j["would_be_lagging"], j["would_be_direct_lagging"]) == (0, 0, 2, 2)


# ----- the scan on a hermetic estate --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def estate(tmp_path_factory):
    h = at_pins().LocalHosts(tmp_path_factory.mktemp("fresh"))
    fx = build(h)
    env = synced_and_walked(h, fx)
    with env.active():
        from nanosoc_rm.freshness import scan
        docs = scan.scan(today=TODAY)                    # stored
    yield {"h": h, "fx": fx, "env": env, "docs": docs}
    h.api.close()


def _by(docs):
    return {(d["fact"]["configuration"], d["fact"].get("tree_path")): d["fact"] for d in docs}


def test_every_document_is_a_valid_stored_observation(estate):
    docs = estate["docs"]
    assert docs and all(contracts.validate_observation(d) == [] for d in docs)
    assert all(db.check_observation(d) == [] for d in docs)
    with estate["env"].active():
        stored = db.latest("freshness")
    assert sorted(d["subject"] for d in stored) == sorted(d["subject"] for d in docs)
    assert {d["snapshot"]["scope"] for d in stored} == {"freshness:%s" % c for c in estate["fx"]["configurations"]}


def test_pin_facts_match_the_estate(estate):
    by, fx = _by(estate["docs"]), estate["fx"]
    lib = by[("top-main", "lib")]
    assert (lib["behind"], lib["ahead"], lib["on_default"], lib["unpushed"], lib["lower_bound"]) == (1, 0, True, False, False)
    assert lib["pinned"] == fx["lib"][1] and lib["upstream"]["head"] == fx["lib"][2]
    assert lib["upstream"]["default_branch"] == "main" and lib["branches"] == ["main"]
    assert lib["bump"]["commit"] == fx["c2"] and lib["bump"]["date"] == "2026-10-04T12:00:00Z"
    assert lib["bump"]["total"] == 2 and lib["bump"]["author"] == "rm-fixture"
    assert lib["pin_date"] == "2026-05-18T12:00:00Z"            # lib[1]: 150 - 1 days before TODAY
    assert lib["state"] == "behind"
    old = by[("top-main", "old")]
    assert (old["behind"], old["state"], old["bump"]["commit"]) == (2, "outdated", fx["c1"])
    leaf = by[("top-main", "mid/deps/leaf")]
    assert (leaf["behind"], leaf["depth"], leaf["state"]) == (11, 2, "outdated")
    assert leaf["parent"] == {"component": "mid", "commit": fx["mid"]}
    assert leaf["bump"]["date"] == "2026-02-06T12:00:00Z"
    assert by[("top-main", "mid")]["state"] == by[("top-main", "cur")]["state"] == "current"
    gone = by[("edge-main", "gone")]
    assert (gone["unpushed"], gone["reachable"], gone["behind"], gone["state"]) == (True, False, None, "unpushed")
    u = by[("edge-main", "u")]
    assert (u["state"], u["not_measured"]) == ("not_measured", "the URL matches no catalogued repo")
    # every measured pin names the git commands that produced it (A3)
    assert any("rev-list --count %s..%s" % (fx["lib"][1], fx["lib"][2]) in s["command"]
               for s in estate["docs"][[d["subject"] for d in estate["docs"]].index("freshness:top-main:lib")]["sources"])


def test_tree_facts_match_the_estate(estate):
    by = _by(estate["docs"])
    top = by[("top-main", None)]
    assert (top["state"], top["direct"], top["direct_lagging"], top["pins"], top["lagging"]) == ("outdated", 4, 2, 5, 3)
    assert top["counts"] == {"unpushed": 0, "not_measured": 0, "current": 2, "outdated": 2, "behind": 1}
    assert (top["updated_days"], top["age"], top["newest_bump"]["tree_path"]) == (10, "fresh", "lib")
    assert top["worst_lagging"]["tree_path"] == "mid/deps/leaf" and top["total_behind"] == 14
    assert top["root"]["commit"] == estate["fx"]["c2"] and top["root"]["date"] == "2026-10-04T12:00:00Z"
    v1 = by[("top-v1", None)]
    assert (v1["state"], v1["would_be"], v1["lagging"], v1["would_be_lagging"], v1["silicon"]) == \
        ("frozen", "outdated", 0, 3, True)
    assert all(by[("top-v1", p)]["state"] == "frozen" for p in ("lib", "old", "mid", "cur", "mid/deps/leaf"))
    assert by[("top-v1", "lib")]["would_be"] == "behind"
    assert by[("edge-main", None)]["state"] == "unpushed"
    assert by[("calm-main", None)]["state"] == "behind"


def test_a_failed_sync_makes_the_count_a_lower_bound(estate, tmp_path):
    shadow = at_pins().make_shadow(estate["env"], tmp_path / "shadow")
    with shadow.active():
        from nanosoc_rm.freshness import scan
        (st,) = db.latest("mirror_status", "mirror_status:github:SoC-Labs/lib")
        failed = json.loads(json.dumps(st))
        failed["fact"].update(ok=False, access="unreadable", cause="network", error="fatal: unable to access")
        failed["snapshot"] = {"id": db.new_snapshot_id(st["snapshot"]["scope"]), "scope": st["snapshot"]["scope"]}
        db.put_snapshot(st["snapshot"]["scope"], [failed])
        by = _by(scan.scan(["top-main"], store=False, today=TODAY))
    assert by[("top-main", "lib")]["lower_bound"] is True and by[("top-main", "old")]["lower_bound"] is False
    assert by[("top-main", None)]["any_lower_bound"] is True


def test_a_configuration_never_walked_is_not_measured_and_an_unknown_one_is_refused(estate, tmp_path):
    from nanosoc_rm.freshness import scan
    shadow = at_pins().make_shadow(estate["env"], tmp_path / "shadow")
    with shadow.active():
        with db.connect() as con, con:
            con.execute("DELETE FROM observations WHERE kind = 'pin_state' AND subject LIKE 'pin_state:calm-main:%'")
        (tree,) = scan.scan(["calm-main"], store=False, today=TODAY)
        with pytest.raises(scan.FreshnessUsage):
            scan.scan(["no-such"], today=TODAY)
    assert tree["fact"]["state"] == "not_measured" and "walk calm-main" in tree["fact"]["not_measured"]


# ----- the view: stored facts only, re-judged on the day -----------------------------------------------------------
def test_pages_re_judge_stored_facts_on_their_own_day(estate):
    from nanosoc_rm.freshness import view
    with estate["env"].active():
        now = {s["configuration"]: s for s in view.trees(today=TODAY)}
        later = {s["configuration"]: s for s in view.trees(today=TODAY + D(days=80))}
    assert [c for c in now] == ["calm-main", "edge-main", "top-main", "top-v1"]          # live first
    assert now["calm-main"]["state"] == "behind" and later["calm-main"]["state"] == "outdated"   # lib: 10 + 80 d
    assert now["top-main"]["age_word"] == "Fresh" and later["top-main"]["age_word"] == "Stale"
    assert now["top-v1"]["word"] == "Frozen" and now["top-v1"]["would_be_word"] == "Outdated"


def test_the_tree_view_orders_nodes_and_rolls_up_the_worst_beneath(estate):
    from nanosoc_rm.freshness import view
    with estate["env"].active():
        t = view.tree("top-main", today=TODAY)
        assert view.tree("no-such") is None
    paths = [n["tree_path"] for n in t["nodes"]]
    assert paths == ["cur", "lib", "mid", "mid/deps/leaf", "old"]
    mid = t["nodes"][2]
    assert (mid["state"], mid["rollup"], mid["rollup_word"], mid["rollup_behind"]) == ("current", "outdated", "Outdated", 11)
    assert mid["children"] == ["mid/deps/leaf"] and t["nodes"][3]["display_depth"] == 2
    assert all(n["subject"].startswith("freshness:top-main:") and n["observed_at"] for n in t["nodes"])


def test_the_detail_panel_names_other_live_trees_lagging_on_the_same_pin(estate):
    from nanosoc_rm.freshness import view
    with estate["env"].active():
        lib = view.pin("top-main", "lib", today=TODAY)
        old = view.pin("top-main", "old", today=TODAY)
        assert view.pin("top-main", "nope") is None
    assert lib["same_pin_lags_in"] == ["calm-main"], "top-v1 pins it too, but a frozen tree is never counted"
    assert old["same_pin_lags_in"] == [] and lib["bump"]["author"] == "rm-fixture"


def test_the_grid_is_component_by_configuration_with_frozen_cells_not_counted(estate):
    from nanosoc_rm.freshness import view
    with estate["env"].active():
        g = view.grid(today=TODAY)
        live = view.grid(today=TODAY, live_only=True)
    assert g["configurations"] == ["calm-main", "edge-main", "top-main", "top-v1"]
    assert "top-v1" not in live["configurations"]
    lib = g["cells"]["lib"]
    assert lib["top-main"]["state"] == lib["calm-main"]["state"] == "behind"
    assert (lib["top-v1"]["state"], lib["top-v1"]["would_be"], lib["top-v1"]["counted"]) == ("frozen", "behind", False)
    assert g["components"][0] == "lib", "the component lagging in the most live trees comes first"


def test_no_view_function_runs_git(estate, monkeypatch):
    import nanosoc_rm.gitrun as G
    from nanosoc_rm.freshness import view
    import subprocess

    def boom(*a, **k):
        raise AssertionError("a page read ran a subprocess")
    monkeypatch.setattr(G, "run_git", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    with estate["env"].active():
        assert view.trees(today=TODAY) and view.tree("top-main", today=TODAY)
        assert view.pin("top-main", "lib", today=TODAY) and view.grid(today=TODAY)["cells"]


# ----- CLI, job kind, scheduler, page -------------------------------------------------------------------------------
def test_cli_scan_and_show(estate):
    env = estate["env"]
    p = env.cli("freshness", "scan", "top-main", "--today", TODAY.isoformat(), "--json")
    assert p.returncode == 0, p.stderr
    docs = [json.loads(x) for x in p.stdout.splitlines()]
    assert docs[0]["fact"]["level"] == "tree" and len(docs) == 6
    assert env.cli("freshness", "scan", "no-such").returncode == 3
    assert env.cli("freshness", "scan", "--today", "14/10/2026").returncode == 3
    p = env.cli("freshness", "show", "--today", TODAY.isoformat())
    assert p.returncode == 0 and re.search(r"top-v1\s+Frozen \(would be Outdated\)", p.stdout), p.stdout
    p = env.cli("freshness", "show", "top-main", "--tree", "--today", TODAY.isoformat())
    assert p.returncode == 0 and re.search(r"\n  leaf\s+Outdated\s+11\s+250\s+Outdated", p.stdout), p.stdout


def test_job_kind_params_args_and_outcome():
    from nanosoc_rm.freshness import jobkind
    assert jobkind.params({}) == {"configuration": None}
    with pytest.raises(ValueError):
        jobkind.params({"configuration": "Bad Id"})
    with pytest.raises(ValueError):
        jobkind.params({"other": 1})
    assert jobkind.cli_args({"configuration": "eth-main"}) == ["freshness", "scan", "eth-main", "--store", "--json"]
    tree = {"kind": "freshness", "fact": {"level": "tree", "configuration": "a", "state": "behind", "not_measured": None}}
    pin = {"kind": "freshness", "fact": {"level": "pin", "behind": None}}
    o = jobkind.outcome([tree, pin], "")
    assert o["verdict"] == "PASS" and o["result"] == {"trees": {"a": "behind"}, "pins": 1, "not_measured": 1}
    unwalked = dict(tree, fact=dict(tree["fact"], not_measured="no stored walk"))
    assert jobkind.outcome([unwalked], "")["verdict"] == "UNVERIFIED"
    assert jobkind.outcome([], "boom")["verdict"] == "UNVERIFIED"


def test_the_scheduler_queues_one_scan_after_poll(rm_config):
    from nanosoc_rm import jobs, schedule
    from nanosoc_rm.config import load_config
    cfg = load_config(rm_config)
    job = next(j for j in schedule.JOBS if j.name == "freshness_scan")
    names = [j.name for j in schedule.JOBS]
    assert names.index("freshness_scan") == names.index("poll") + 1 and job.every == "poll" and job.enabled
    job.run(cfg)
    job.run(cfg)                                          # one is queued already: no second
    queued = jobs.jobs(cfg, kind="freshness-scan")
    assert len(queued) == 1 and queued[0]["params"] == {"configuration": None} and queued[0]["by"] == "schedule"


def _signed_in_client(env, monkeypatch):
    from fastapi.testclient import TestClient

    from nanosoc_rm import auth
    from nanosoc_rm.web import create_app
    pw = "correct horse battery staple"
    h = auth.hash_password(pw, salt=b"0123456789abcdef")
    env.config.write_text(env.config.read_text() + '\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\n'
                          'hash = "%s"\n' % h)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(env.config))
    c = TestClient(create_app(env.config))
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/auth/login").text).group(1)
    r = c.post("/auth/login", data={"csrf_token": tok, "login": "alice", "password": pw, "next": "/"},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def test_the_page_shows_words_and_the_post_queues_a_job(estate, tmp_path, monkeypatch):
    from nanosoc_rm import jobs
    shadow = at_pins().make_shadow(estate["env"], tmp_path / "web")
    for k, v in shadow.env.items():
        if k != "NANOSOC_RM_CONFIG":
            monkeypatch.setenv(k, v)
    c = _signed_in_client(shadow, monkeypatch)
    page = c.get("/freshness/")
    assert page.status_code == 200
    row = re.search(r'<tr data-configuration="top-v1" data-verdict="frozen">.*?</tr>', page.text, re.S).group(0)
    assert ">Frozen<" in row and "would be outdated if live; not counted" in row
    assert len(re.findall(r'class="pill ', row)) == 1, "a frozen tree's age gets no Fresh/Aging/Stale pill"
    assert re.search(r"Frozen \d+ d ago", row), "a frozen tree's age is how long ago it was frozen"
    head = page.text[:page.text.index("<table data-freshness")]
    head = head[head.index('data-freshness-scan'):]
    assert 'class="note"' not in head and 'class="panel"' not in head.split("<details")[0], \
        "one compact toolbar; the explanations live behind How is this judged?"
    assert 'data-subject="freshness:top-main"' in page.text and "data-age-seconds" in page.text
    for m in re.finditer(r'<span class="pill (state-\w+)">([^<]*)<', page.text):
        assert m.group(2).strip(), "a state pill without its word"
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = c.post("/freshness/scan", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/freshness/?job=")
    r2 = c.post("/freshness/scan", data={"csrf_token": tok, "configuration": "top-main"}, follow_redirects=False)
    assert r2.headers["location"] == r.headers["location"], "a queued scan of every tree covers one tree"
    assert c.post("/freshness/scan", data={"csrf_token": tok, "configuration": "no-such"}).status_code == 400
    assert c.post("/freshness/scan", data={"configuration": "top-main"}).status_code == 403     # no CSRF token
    from nanosoc_rm.config import load_config
    q = jobs.jobs(load_config(shadow.config), kind="freshness-scan")
    assert [j["params"] for j in q] == [{"configuration": None}]


# ----- a frozen tree never reads as lagging; the registry decides what is listed (review F1-F4) --------------------
def test_a_frozen_tree_keeps_its_lag_only_under_would_be():
    pins = [_pin(12, 200, path="a"), _pin(1, 5, path="b"), _pin(30, 300, depth=2, path="a/deep")]
    j = rules.judge_tree(True, pins, TODAY)
    assert set(j["counts"].values()) == {0}, "a frozen tree counts no pin in any state"
    assert j["would_be_counts"]["outdated"] == 2 and j["would_be_counts"]["behind"] == 1
    assert j["worst_lagging"] is None and j["stalest_lagging"] is None
    assert j["would_be_worst_lagging"]["tree_path"] == "a/deep" and j["would_be_stalest_lagging"]["days"] == 300
    assert j["age"] is None and j["updated_days"] == 5, "frozen on purpose: no Fresh/Aging/Stale grade"
    live = rules.judge_tree(False, pins, TODAY)
    assert live["would_be_counts"] is None and live["would_be_worst_lagging"] is None and live["age"] == "fresh"


def test_every_node_of_a_frozen_tree_rolls_up_frozen(estate):
    from nanosoc_rm.freshness import view
    with estate["env"].active():
        t = view.tree("top-v1", today=TODAY)
        later = {s["configuration"]: s for s in view.trees(today=TODAY + D(days=400))}
    assert {n["rollup"] for n in t["nodes"]} == {"frozen"} and {n["rollup_word"] for n in t["nodes"]} == {"Frozen"}
    mid = next(n for n in t["nodes"] if n["tree_path"] == "mid")
    assert mid["would_be_rollup"] == "outdated" and mid["rollup_behind"] == 11
    assert later["top-v1"]["age"] is None and later["top-v1"]["age_word"] is None
    assert later["top-main"]["age_word"] == "Stale"
    p = estate["env"].cli("freshness", "show", "top-v1", "--tree", "--today", TODAY.isoformat())
    assert p.returncode == 0, p.stderr
    assert "Outdated" not in p.stdout and "Behind" not in p.stdout, p.stdout


def test_the_registry_decides_which_trees_are_listed_and_which_are_frozen(estate, tmp_path):
    import shutil
    import subprocess
    import yaml
    from nanosoc_rm.freshness import view
    reg = tmp_path / "registry"
    shutil.copytree(estate["fx"]["registry"], reg)
    el = yaml.safe_load((reg / "eligibility.yaml").read_text())
    el["configurations"] = [c for c in el["configurations"] if c["id"] != "calm-main"]
    for c in el["configurations"]:
        if c["id"] == "top-main":
            c.update(frozen=True, commit=estate["fx"]["c2"])
    (reg / "eligibility.yaml").write_text(yaml.safe_dump(el, sort_keys=False))
    subprocess.run(["git", "init", "-q", "-b", "main", str(reg)], check=True)
    subprocess.run(["git", "-C", str(reg), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(reg), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm",
                    "registry"], check=True)
    shadow = at_pins().make_shadow(estate["env"], tmp_path / "shadow")
    env = at_pins().ToolEnv(tmp_path / "env", at_pins().service_config(registry=reg, data_dir=shadow.data_dir,
                                                                      base=shadow.cfg), shadow.data_dir, reg)
    with env.active():
        from nanosoc_rm.config import load_config
        from nanosoc_rm.web import registry_facts
        before = [s["configuration"] for s in view.trees(today=TODAY)]       # the registry never read: as stored
        assert registry_facts.refresh(load_config()) == []                    # what every page GET does first
        names = [s["configuration"] for s in view.trees(today=TODAY)]
        g = view.grid(today=TODAY)
        top = view.tree("top-main", today=TODAY)
        gone = view.tree("calm-main", today=TODAY)
    assert "calm-main" in before
    assert "calm-main" not in names and "calm-main" not in g["configurations"] and gone is None
    assert top["tree"]["state"] == "frozen" and top["tree"]["would_be"] == "outdated"
    assert {n["state"] for n in top["nodes"]} == {"frozen"} and g["cells"]["lib"]["top-main"]["counted"] is False
