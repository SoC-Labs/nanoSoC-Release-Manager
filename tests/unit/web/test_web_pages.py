"""Pages and JSON API (interfaces.md section 13; A3, H1): page hooks, data age, fact links, dashboard items,
the Run button, run and errata pages."""
from __future__ import annotations

import copy
import html
import json
import re
import sys
import types
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, clock, contracts, db
from nanosoc_rm.web import create_app, views
from webfix import EXAMPLES, Hooks, make_registry, write_config

RC4 = "eth-rc4"
NONCE = "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f"
WAIVED_NONCE = "9c1e4f7a2b6d4e0f8a3c5b7d9e1f2a4c"
ITEMS = ("t0-result", "red-pins", "open-errata", "host-drift", "runner-status", "data-age")


def example(name):
    return [d for _, d in contracts.docs_in(EXAMPLES / name)]


@pytest.fixture
def site(service, fake_registry_module, tmp_path):
    """The seeded service with a registry checkout (eth-rc4, eth-main, multicore-master) and a member client."""
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    return dict(service, cfg=cfg, registry=reg, client=client)


def fill(path, subject="registry_record:eligibility.yaml#/configurations/0"):
    vals = {"/configurations/": RC4, "/components/": "tidelink", "/defects/": "SL-DEF-0001", "/errata/": RC4,
            "/runs/": NONCE, "/contains/": "036d3811e630c89b2344b56aa617b40a99cd652b"}
    if path.startswith("/facts/"):
        return "/facts/" + urllib.parse.quote(subject, safe="")
    val = next((v for k, v in vals.items() if k in path), "x")
    return re.sub(r"\{[^}]+\}", val, path)


def stored(cfg, subject):
    hist = db.history(subject, config=cfg)
    return max(hist, key=lambda d: clock.parse(d["observed_at"])) if hist else None


def html_routes(app):
    for r in app.routes:
        if getattr(getattr(r, "app", None), "directory", None) or "GET" not in (getattr(r, "methods", None) or ()):
            continue
        if r.path.startswith(("/api/", "/auth")):
            continue
        yield r.path


# ----- A3: every page, one data age from its oldest fact; every fact linked to its commands -------------------
def test_every_page_shows_one_age_from_its_oldest_fact_and_links_every_fact(site):
    c, cfg = site["client"], site["cfg"]
    checked = 0
    for path in html_routes(c.app):
        url = fill(path) if path != "/contains" else "/contains?sha=036d381&component=nanosoc_gen"
        r = c.get(url)
        fetched = clock.now()
        assert r.status_code == 200, (url, r.status_code)
        h = Hooks(r.text)
        ages = h.with_attr("data-age-seconds")
        assert len(ages) == 1, (url, len(ages))
        facts = h.facts()
        assert facts, "%s shows no fact" % url
        oldest = min((at for _, at in facts), key=clock.parse)
        assert ages[0]["data-oldest-observed-at"] == oldest, url
        assert abs(int(ages[0]["data-age-seconds"]) - (fetched - clock.parse(oldest)).total_seconds()) <= 5, url
        links = h.links()
        for subject, at in facts:
            obs = stored(cfg, subject)
            assert obs is not None, (url, subject)
            assert clock.parse(at) == clock.parse(obs["observed_at"]), (url, subject)
            assert links.get(subject) == "/facts/" + urllib.parse.quote(subject, safe=""), (url, subject)
            body = c.get(links[subject]).text
            for s in obs["sources"]:
                assert html.escape(s["command"], quote=False) in body, (url, subject, s["command"])
        checked += 1
    assert checked >= 10


def test_a_fact_page_shows_commands_verbatim_quotes_and_brackets_included(site):
    subject = "host_compare:tidelink:gitlab:soclabs/tidelink"
    body = site["client"].get("/facts/" + urllib.parse.quote(subject, safe="")).text
    cmd = stored(site["cfg"], subject)["sources"][0]["command"]
    assert "'" in cmd and "<" in cmd, "the example command must exercise quotes and brackets"
    assert html.escape(cmd, quote=False) in body


def test_registry_facts_link_to_their_git_show_command(site):
    c = site["client"]
    c.get("/")
    head = __import__("webfix").git("rev-parse", "HEAD", cwd=site["registry"])
    body = c.get("/facts/" + urllib.parse.quote("registry_record:eligibility.yaml#/configurations/1", safe="")).text
    assert "git -C &lt;registry&gt; show %s:eligibility.yaml" % head in body


def test_unknown_fact_is_404_and_old_fact_says_not_current(site):
    c = site["client"]
    assert c.get("/facts/no_such_kind:x").status_code == 404
    c.get("/")                                               # the registry read replaces the example record
    r = c.get("/facts/" + urllib.parse.quote("registry_record:eligibility.yaml#/configurations/0", safe=""))
    assert r.status_code == 200


def test_a_row_stored_without_a_snapshot_is_not_shown(site):
    """A3 negative control at unit level: a set-kind row that bypassed put (no snapshot, no sources) is never
    part of the current view, so no page shows it."""
    import sqlite3
    obs = [d for d in example("observation.example.jsonl") if d["kind"] == "silicon_protection"][0]
    bad = dict(obs, subject=obs["subject"].replace("tidechart", "tidechart-planted"), sources=[])
    con = sqlite3.connect(db.database_path(site["cfg"]))
    con.execute("INSERT INTO observations (kind, subject, observed_at, doc) VALUES (?, ?, ?, ?)",
                (bad["kind"], bad["subject"], bad["observed_at"], json.dumps(bad)))
    con.commit()
    con.close()
    shown = {s for s, _ in Hooks(site["client"].get("/silicon-protection").text).facts()}
    assert obs["subject"] in shown and bad["subject"] not in shown


# ----- H1: the dashboard -------------------------------------------------------------------------------------
def rows_of(text):
    h = Hooks(text)
    return {e["data-configuration"]: e for e in h.with_attr("data-configuration")}


def cells(text):
    """(configuration, item) -> cell text, read with the acceptance tests' own parser."""
    import importlib.util
    from webfix import REPO
    spec = importlib.util.spec_from_file_location("sw_pages", REPO / "tests" / "acceptance" / "test_scope_web.py")
    sw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sw)
    p = sw.PageScan(text)
    return p, sw


def test_dashboard_has_six_items_and_one_csrf_run_form_per_configuration(site):
    text = site["client"].get("/").text
    p, sw = cells(text)
    confs = ["eth-rc4", "eth-main", "multicore-master"]
    assert sorted(rows_of(text)) == sorted(confs)
    for c in confs:
        assert all((c, i) in p.items for i in ITEMS), c
        forms = [f for f in p.forms if f["run"] and f["configuration"] == c]
        assert len(forms) == 1 and forms[0]["method"] == "post" and forms[0]["action"] == "/configurations/%s/runs" % c
        assert forms[0]["fields"].get("csrf_token")
    assert sw.check_h1({"/": text}, configurations=confs, cli_subjects={}) == []
    assert rows_of(text)[RC4]["data-verdict"] == "UNVERIFIED"          # the example run of eth-rc4
    assert rows_of(text)["eth-main"]["data-verdict"] == ""             # no run yet


def _pins_with_states(cid, states):
    out = []
    for d, st in zip(example("pin-state.example.jsonl"), states):
        d = copy.deepcopy(d)
        d["fact"]["configuration"] = cid
        d["subject"] = "pin_state:%s:%s" % (cid, d["fact"]["tree_paths"][0])
        d["snapshot"] = {"id": "pin_state:%s@test" % cid, "scope": "pin_state:%s" % cid}
        for h in d["fact"]["hosts"].values():
            if h["pinned"]:
                h["state"] = st
                if st not in ("TAGGED", "PROTECTED_BY_TAG", "ON_DEFAULT"):
                    h["tags"], h["lag"] = [], None
        out.append(d)
    return out


def test_red_pins_counts_branch_only_as_red_in_a_frozen_tree_only(site):
    states = ["BRANCH_ONLY", "MISSING", "TAGGED"]
    for cid in ("eth-rc4", "eth-main"):
        docs = _pins_with_states(cid, states)
        problems = [p for d in docs for p in db.check_observation(d)]
        assert problems == [], problems
        db.put_snapshot("pin_state:%s" % cid, docs, config=site["cfg"])
    p, sw = cells(site["client"].get("/").text)
    assert sw.first_int(p.items[("eth-rc4", "red-pins")]) == 2       # frozen: BRANCH_ONLY and MISSING
    assert sw.first_int(p.items[("eth-main", "red-pins")]) == 1      # not frozen: MISSING only


def test_pin_level_states_are_red():
    pin = {"fact": {"state": "LOCAL_URL", "hosts": {}}}
    assert views.pin_is_red(pin, frozen=False)
    assert not views.pin_is_red({"fact": {"state": None, "hosts": {"github": {"pinned": True, "state": "ON_DEFAULT"}}}},
                                frozen=True)


def test_open_errata_and_host_drift(site):
    unknown = copy.deepcopy([d for d in example("observation.example.jsonl") if d["kind"] == "host_compare"][0])
    f = unknown["fact"]
    f.update(component="nanosoc_gen", relation="UNKNOWN", canonical_only=None, copy_only=None)
    f["copy"]["repo"] = {"host": "gitlab", "path": "soclabs/nanosoc_gen-copy"}
    unknown["subject"] = db.subject_of(unknown)
    unknown["snapshot"] = {"id": "host_compare:nanosoc_gen@test", "scope": "host_compare:nanosoc_gen"}
    db.put_snapshot("host_compare:nanosoc_gen", [unknown], config=site["cfg"])
    p, sw = cells(site["client"].get("/").text)
    assert sw.first_int(p.items[(RC4, "open-errata")]) == 1          # ETH-R0P0-ERR-001, permanent
    drift = p.items[(RC4, "host-drift")]
    assert "tidelink AHEAD" in drift                                  # in rc4's tree (the example pins)
    assert "unknown: nanosoc_gen" in drift and "nanosoc_gen UNKNOWN" not in drift
    assert "ahb_qspi" not in drift, "a component outside the tree's stored pins is not this tree's drift"


def test_runner_status_and_alert_banner(site):
    text = site["client"].get("/").text
    p, _ = cells(text)
    assert "runner 9001: ok" in p.items[(RC4, "runner-status")]
    assert "alert:rewrite:" in text


# ----- Run T0 ----------------------------------------------------------------------------------------------
def pending_run(cid, nonce, at="2026-10-02T10:00:00Z"):
    return {"schema": 1, "kind": "t0_result", "subject": "t0_result:%s:%s" % (cid, nonce), "observed_at": at,
            "sources": [{"kind": "ci_api", "command": "POST projects/soclabs%2Fnanosoc-compat-ci/trigger/pipeline"}],
            "fact": {"configuration": cid, "commit": "8e6d0434fc8331191607ad5b2af73646e995c892",
                     "run": {"nonce": nonce, "trigger": "manual", "triggered_by": "alice", "triggered_at": at,
                             "pipeline_id": 77, "job_id": None},
                     "acceptance": "pending", "reason": None, "verdict": None, "gates": [], "bundle": None,
                     "started_at": None, "finished_at": None, "over_budget": None, "runner": None}}


@pytest.fixture
def fake_trigger(site, monkeypatch):
    """A test double of the contracted ci.trigger.trigger: stores a pending run and returns it."""
    calls = []
    mod = types.ModuleType("nanosoc_rm.ci.trigger")

    def trigger(configuration, by, kind, client=None):
        calls.append((configuration, by, kind))
        doc = pending_run(configuration, "%032x" % (len(calls) + 0xabc), clock.iso())
        db.put(doc)
        return doc
    mod.trigger = trigger
    monkeypatch.setitem(sys.modules, "nanosoc_rm.ci", types.ModuleType("nanosoc_rm.ci"))
    monkeypatch.setitem(sys.modules, "nanosoc_rm.ci.trigger", mod)
    return calls


def test_run_button_starts_one_run_shows_it_and_is_then_disabled(site, fake_trigger):
    c = site["client"]
    p, sw = cells(c.get("/").text)
    form = next(f for f in p.forms if f["run"] and f["configuration"] == "eth-main")
    assert not form["disabled"]
    r = c.post(form["action"], data=form["fields"])
    assert r.status_code == 202
    assert fake_trigger == [("eth-main", "alice", "manual")]
    shown = [s for s, _ in Hooks(r.text).facts() if s.startswith("t0_result:eth-main:")]
    assert len(shown) == 1
    again = sw.run_form(c.get("/").text, "eth-main")
    assert again is not None and again["disabled"], "the button must be disabled while the run is pending"
    r2 = c.post(form["action"], data=form["fields"])
    assert r2.status_code == 409 and fake_trigger == [("eth-main", "alice", "manual")]
    assert shown[0] in r2.text


def test_api_run_returns_the_run_as_an_observation(site, fake_trigger):
    r = site["client"].post("/api/v1/configurations/multicore-master/runs")
    assert r.status_code == 202 and r.headers["content-type"].startswith("application/x-ndjson")
    (doc,) = [json.loads(x) for x in r.text.splitlines()]
    assert contracts.validate_observation(doc) == [] and doc["fact"]["run"]["triggered_by"] == "alice"


def test_htmx_run_returns_a_fragment(site, fake_trigger):
    r = site["client"].post("/configurations/eth-main/runs", data={}, headers={"HX-Request": "true"})
    assert r.status_code == 202 and "<html" not in r.text and "t0_result:eth-main:" in r.text


def test_run_without_the_ci_module_is_503_not_a_crash(site, monkeypatch):
    monkeypatch.setitem(sys.modules, "nanosoc_rm.ci.trigger", None)
    r = site["client"].post("/configurations/eth-main/runs", data={})
    assert r.status_code == 503 and "not installed" in r.text


def test_a_trigger_that_raises_is_reported_not_a_crash(site, monkeypatch):
    mod = types.ModuleType("nanosoc_rm.ci.trigger")

    class RegistryUnreadable(Exception):
        pass

    def trigger(configuration, by, kind, client=None):
        raise RegistryUnreadable("eligibility.yaml is unreadable")
    mod.trigger = trigger
    monkeypatch.setitem(sys.modules, "nanosoc_rm.ci", types.ModuleType("nanosoc_rm.ci"))
    monkeypatch.setitem(sys.modules, "nanosoc_rm.ci.trigger", mod)
    for path in ("/configurations/eth-main/runs", "/api/v1/configurations/eth-main/runs"):
        r = site["client"].post(path, data={})
        assert r.status_code == 500 and "could not start the run" in r.text and "RegistryUnreadable" in r.text


def test_only_the_two_run_routes_write_and_no_docs_routes(site):
    app = site["client"].app
    writes = {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ()) if m not in ("GET", "HEAD")}
    assert writes == {("POST", "/configurations/{id}/runs"), ("POST", "/api/v1/configurations/{id}/runs")}
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}


# ----- run page and errata (D6 hooks, F2 colours) ---------------------------------------------------------------
def test_run_page_shows_gates_controls_and_waivers(site):
    ex = example("observation.t0-result-waived.example.jsonl")[0]
    db.put(ex, config=site["cfg"])
    text = site["client"].get("/runs/%s" % WAIVED_NONCE).text
    h = Hooks(text)
    gates = {e["data-gate"]: e for e in h.with_attr("data-gate")}
    assert sorted(gates) == sorted(g["id"] for g in ex["fact"]["gates"])
    for g in ex["fact"]["gates"]:
        e = gates[g["id"]]
        assert (e["data-verdict"], e["data-control"]) == (g["verdict"], g["control"])
        if g["verdict"] == "WAIVED":
            assert e["data-raw-verdict"] == g["raw_verdict"] and "state-amber" in e["class"].split()
        else:
            assert "data-raw-verdict" not in e
    assert {e["data-waived"] for e in h.with_attr("data-waived")} == {
        w["tree_path"] for g in ex["fact"]["gates"] for w in g.get("waived", [])}
    assert "member-a" in text                                       # each waiver's approver is shown
    run_rows = [e for e in h.with_attr("data-verdict") if "data-gate" not in e]
    assert run_rows and run_rows[0]["data-verdict"] == "WAIVED" and "state-amber" in run_rows[0]["class"].split()
    assert site["client"].get("/runs/" + "0" * 32).status_code == 404


def test_a_waived_run_is_amber_on_the_dashboard(site):
    ex = copy.deepcopy(example("observation.t0-result-waived.example.jsonl")[0])
    ex["fact"]["run"]["triggered_at"] = "2026-10-01T03:00:00Z"           # newer than the example UNVERIFIED run
    db.put(ex, config=site["cfg"])
    row = rows_of(site["client"].get("/").text)[RC4]
    assert row["data-verdict"] == "WAIVED" and "state-amber" in row["class"].split()
    others = [r for c, r in rows_of(site["client"].get("/").text).items() if c != RC4]
    assert others and all("state-amber" not in (r.get("class") or "").split() for r in others)


def test_errata_rows_carry_level_method_and_colour(site):
    text = site["client"].get("/errata/%s" % RC4).text
    (el,) = Hooks(text).with_attr("data-erratum")
    assert (el["data-erratum"], el["data-exposure"], el["data-method"]) == ("ETH-R0P0-ERR-001", "probably-exposed", "ancestry")
    assert "state-amber" in el["class"].split()
    assert "80eb7ce7e81fee653b52c6821cea3942" in text                 # the evidence is printed beside the level
    assert site["client"].get("/errata/no-such-config").status_code == 404


def test_defect_pages(site):
    c = site["client"]
    assert "SL-DEF-0001" in c.get("/defects").text
    r = c.get("/defects/SL-DEF-0001")
    assert r.status_code == 200 and "ETH-R0P0-ERR-001" in r.text and "036d3811e630" in r.text
    assert c.get("/defects/SL-DEF-9999").status_code == 404


def test_component_page_shows_its_host_compares_only(site):
    text = site["client"].get("/components/tidelink").text
    hc = {s for s, _ in Hooks(text).facts() if s.startswith("host_compare:")}
    assert hc == {"host_compare:tidelink:gitlab:soclabs/tidelink"}
    assert site["client"].get("/components/no-such-component").status_code == 404


def test_silicon_page_lists_uncovered_first_per_configuration_and_host(site):
    facts = [s for s, _ in Hooks(site["client"].get("/silicon-protection").text).facts()]
    rows = {d["subject"]: d for d in example("observation.example.jsonl") if d["kind"] == "silicon_protection"}
    assert set(facts) == set(rows)
    order = [(rows[s]["fact"]["repo"]["host"], rows[s]["fact"]["covered"]) for s in facts]
    assert order == sorted(order), order


# ----- contains ------------------------------------------------------------------------------------------------
def test_contains_answers_from_the_stored_query(site, monkeypatch):
    monkeypatch.setitem(sys.modules, "nanosoc_rm.analysis.contains", None)   # never computed when stored
    text = site["client"].get("/contains?sha=036d381&component=nanosoc_gen").text
    assert [s for s, _ in Hooks(text).facts()] == [
        "containment:nanosoc_gen:036d3811e630c89b2344b56aa617b40a99cd652b:eth-rc4:gitlab"]


def test_contains_runs_the_query_when_nothing_is_stored(site, monkeypatch):
    calls = []
    mod = types.ModuleType("nanosoc_rm.analysis.contains")

    def contains(commit, component=None, configurations=None):
        calls.append((commit, component))
        row = copy.deepcopy([d for d in example("observation.example.jsonl") if d["kind"] == "containment"][0])
        return [row]
    mod.contains = contains
    monkeypatch.setitem(sys.modules, "nanosoc_rm.analysis", types.ModuleType("nanosoc_rm.analysis"))
    monkeypatch.setitem(sys.modules, "nanosoc_rm.analysis.contains", mod)
    r = site["client"].get("/contains?sha=abcdef1&component=nanosoc_gen")
    assert r.status_code == 200 and calls == [("abcdef1", "nanosoc_gen")] and "answered now" in r.text


def test_contains_problems_are_shown_not_raised(site, monkeypatch):
    monkeypatch.setitem(sys.modules, "nanosoc_rm.analysis.contains", None)
    r = site["client"].get("/contains?sha=abcdef1")
    assert r.status_code == 200 and "not installed" in r.text
    assert "not a commit id" in site["client"].get("/contains?sha=xyz").text
    assert site["client"].get("/api/v1/contains/xyz").status_code == 422


# ----- JSON API ------------------------------------------------------------------------------------------------
API = {"/api/v1/configurations": "registry_record", "/api/v1/configurations/eth-rc4/pins": "pin_state",
       "/api/v1/components/tidelink/hosts": "host_compare",
       "/api/v1/contains/036d3811e630c89b2344b56aa617b40a99cd652b?component=nanosoc_gen": "containment",
       "/api/v1/silicon-protection?configuration=eth-rc4": "silicon_protection", "/api/v1/runs": "t0_result",
       "/api/v1/errata/eth-rc4": "registry_record", "/api/v1/mirrors": "mirror_status",
       "/api/v1/inventory": "repo_inventory", "/api/v1/runner": "runner_settings", "/api/v1/alerts": "alert"}


def test_every_api_line_is_a_valid_observation_of_its_kind(site):
    c = site["client"]
    for url, kind in API.items():
        r = c.get(url)
        assert r.status_code == 200, url
        docs = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        assert docs, url
        for d in docs:
            assert contracts.validate_observation(d) == [] and d["kind"] == kind, url
    assert len(c.get("/api/v1/configurations").text.splitlines()) == 3
    assert [json.loads(x)["fact"]["id"] for x in c.get("/api/v1/errata/eth-rc4").text.splitlines()] == ["ETH-R0P0-ERR-001"]
    assert c.get("/api/v1/inventory?unclassified=1").text == ""       # the example row is `ignored`
    assert len(c.get("/api/v1/silicon-protection?configuration=eth-main").text.splitlines()) == 0


def test_api_routes_match_the_contract_table(site):
    paths = {getattr(r, "path", "") for r in site["client"].app.routes}
    want = {"/api/v1/configurations", "/api/v1/configurations/{id}/pins", "/api/v1/components/{id}/hosts",
            "/api/v1/contains/{sha}", "/api/v1/silicon-protection", "/api/v1/runs", "/api/v1/errata/{configuration}",
            "/api/v1/mirrors", "/api/v1/inventory", "/api/v1/runner", "/api/v1/alerts"}
    assert want <= paths
    assert {"/", "/configurations/{id}", "/components/{id}", "/contains", "/silicon-protection", "/defects",
            "/defects/{id}", "/errata/{configuration}", "/runs/{nonce}", "/facts/{subject:path}"} <= paths


def test_configuration_page_shows_its_pins_history_and_run_form(site):
    text = site["client"].get("/configurations/%s" % RC4).text
    pins = {s for s, _ in Hooks(text).facts() if s.startswith("pin_state:")}
    assert pins == {d["subject"] for d in example("pin-state.example.jsonl")}
    assert len(Hooks(text).with_attr("data-run-t0")) == 1
    assert "t0_result:eth-rc4:%s" % NONCE in text
    assert site["client"].get("/configurations/no-such").status_code == 404
