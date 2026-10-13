"""The matrix's three front ends over one function (PR4): `matrix()` / the CLI / the /matrix pages; cells queued
as local jobs (never run in a request); the nightly schedule shipped disabled (CR-15, Phase 2 gate); B7 rows."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nanosoc_rm import auth, contracts, db, jobs, schedule
from nanosoc_rm import matrix as M
from nanosoc_rm.cli import main
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app

pytestmark = [pytest.mark.requirement("D3"), pytest.mark.requirement("D7"), pytest.mark.requirement("D8"),
              pytest.mark.requirement("B7")]
CELL_CLI = [sys.executable, str(Path(__file__).resolve().parent / "cell_cli.py")]


def view(scene, **kw):
    with scene.env.active():
        doc = M.matrix("top-main", **kw)
    assert contracts.validate_observation(doc) == [], contracts.validate_observation(doc)
    return doc["fact"]


def cells_of(v):
    return {(r["component"], c): cv for r in v["rows"] for c, cv in r["cells"].items()}


def test_view_before_any_run(clean):
    v = view(clean)
    c = cells_of(v)
    assert v["columns"] == ["pinned", "in-range", "latest-tag", "head"] and v["commit"] == clean.top
    assert v["pinned"]["status"] == "not-run" and c[("alpha", "in-range")]["status"] == "not-run"
    assert c[("alpha", "head")]["target"]["commit"] == clean.a[3]
    assert c[("beta", "latest-tag")]["status"] == "no-target"
    assert c[("beta", "head")]["status"] == "same-as-pinned" and c[("beta", "head")]["same_as_pinned"]
    assert v["nightly"]["enabled"] is False and "Phase 2" in v["nightly"]["gate"]


def test_cells_queue_as_jobs_pinned_first_and_the_worker_runs_them(clean, monkeypatch):
    monkeypatch.setenv("MATRIX_TEST_URL_MAP", json.dumps(clean.url_map))
    with clean.env.active():
        cfg = load_config()
        queued = M.queue("top-main", by="alice", config=cfg)
        assert [(j["params"]["component"], j["params"]["column"]) for j in queued] == [
            (None, "pinned"), ("alpha", "in-range"), ("alpha", "latest-tag"), ("alpha", "head")]
        assert queued[0]["command"] == "nanosoc-rm matrix cell top-main --column pinned --store"
        assert [a["action"] for a in db.actions(config=cfg)][-4:] == ["run_t0"] * 4     # H2: logged as Run
        v = M.matrix("top-main", config=cfg)["fact"]
        assert v["pinned"]["status"] == "queued" and cells_of(v)[("alpha", "head")]["status"] == "queued"
        assert M.queue("top-main", by="alice", config=cfg) == []                  # already queued: not twice
        w = jobs.Worker(cfg.path, argv_prefix=CELL_CLI)
        for _ in range(2):
            w.step()
        done = [jobs.get(cfg, j["id"]) for j in queued[:2]]
        assert [(j["state"], j["verdict"]) for j in done] == [("done", "PASS"), ("done", "PASS")], \
            [jobs.log_text(cfg, j["id"]) for j in done]
        cell = db.latest("matrix_cell", "matrix_cell:top-main:alpha:in-range:")[0]["fact"]
        assert cell["by"] == "alice" and cell["job"] == queued[1]["id"]          # the job's login, not the worker's
        assert cell["baseline"]["nonce"] == db.latest("matrix_cell", "matrix_cell:top-main:-:")[0]["fact"]["nonce"]
        v = M.matrix("top-main", config=cfg)["fact"]
        c = cells_of(v)
        assert v["pinned"]["verdict"] == "PASS" and c[("alpha", "in-range")]["verdict"] == "PASS"
        assert c[("beta", "head")]["verdict"] == "PASS"                       # same as pinned shows the pinned run
        assert c[("alpha", "head")]["status"] == "queued"


def test_job_kind_refuses_what_the_cli_would(clean):
    with clean.env.active():
        cfg = load_config()
        for bad in ({"configuration": "top-main", "column": "nightly"},
                    {"configuration": "top-main", "column": "nightly", "component": "alpha"},
                    {"configuration": "top-main", "column": "pinned", "component": "alpha"},
                    {"configuration": "top-main", "column": "head"},
                    {"configuration": "../x", "column": "pinned"}):
            with pytest.raises(jobs.JobUsage):
                jobs.submit(cfg, "matrix-cell", bad, "alice")
        assert "matrix-cell" in jobs.KINDS


def test_a_lost_job_shows_infra(clean):
    with clean.env.active():
        cfg = load_config()
        j = jobs.submit(cfg, "matrix-cell", {"configuration": "top-main", "column": "head", "component": "alpha"},
                        "alice")
        jobs._update(cfg, j["id"], state="infra", verdict="INFRA", reason="the service restarted")
        c = cells_of(M.matrix("top-main", config=cfg)["fact"])[("alpha", "head")]
        assert (c["status"], c["verdict"]) == ("job-infra", "INFRA")            # D7: never FAIL


def _pass(scene):
    import secrets
    return {"subject": "compat_result:top-main:%s" % secrets.token_hex(16),
            "fact": {"commit": scene.top, "verdict": "PASS", "reason": None, "not_run": 1, "over_budget": False,
                     "unjudged_overrides": [], "gates": [{"id": "t0.infra.probe", "verdict": "PASS", "detail": "ok"}],
                     "pins": [{"tree_paths": ["a"], "commit": scene.a[3]}, {"tree_paths": ["b"], "commit": scene.b1}]}}


def _same(run_dir, root, requested, overrides, scratch):
    return {"superproject_commit": root, "pins": [dict(r) for r in requested]}


def test_stale_when_the_column_commit_moved_since_the_result(clean):
    docs = clean.run_cell("head", "alpha", compat_fn=lambda *a, **k: _pass(clean), capture_fn=_same, store=True)
    with clean.env.active():
        cfg = load_config()
        assert cells_of(M.matrix("top-main", config=cfg)["fact"])[("alpha", "head")]["stale"] is False
        doc = json.loads(json.dumps(docs[0]))
        doc["fact"]["target"]["commit"] = clean.a[2]
        doc["fact"]["nonce"] = "e" * 32
        doc["subject"] = "matrix_cell:top-main:alpha:head:" + "e" * 32
        db.put(doc, config=cfg)                                                  # newest: run at an older head
        c = cells_of(M.matrix("top-main", config=cfg)["fact"])[("alpha", "head")]
        assert c["subject"] == doc["subject"] and c["stale"] is True


# ---------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------
def cli(scene, *args):
    with scene.env.active():
        return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_cli_view_json_text_and_usage(clean):
    r = cli(clean, "matrix", "top-main", "--json")
    docs = [json.loads(x) for x in r.output.splitlines() if x.startswith("{")]
    assert len(docs) == 1 and docs[0]["kind"] == "matrix_view" and contracts.validate_observation(docs[0]) == []
    assert r.exit_code == 2                                                    # nothing has a result yet
    r = cli(clean, "matrix", "top-main", "--component", "beta", "--columns", "pinned,head")
    assert "beta" in r.output and "= pinned" in r.output and "alpha" not in r.output
    assert "nightly matrix: DISABLED" in r.output
    assert cli(clean, "matrix", "top-main", "--columns", "bogus").exit_code == 3
    assert cli(clean, "matrix", "top-main", "--component", "gamma").exit_code == 3
    assert cli(clean, "matrix", "nope").exit_code == 3
    assert cli(clean, "matrix", "cell", "top-main", "--column", "pinned", "--component", "alpha").exit_code == 3
    assert cli(clean, "matrix", "quarantine", "top-main").exit_code == 0
    r = cli(clean, "matrix", "nightly", "top-main", "--json")
    n = json.loads(r.output)
    assert n["enabled"] is False and {"component": None, "column": "pinned"} in n["would_queue"]


def test_the_nightly_matrix_ships_disabled_and_unscheduled():
    assert M.NIGHTLY.enabled is False and "Phase 2 entry gate" in M.NIGHTLY.gate
    assert M.NIGHTLY.name not in {j.name for j in schedule.JOBS}


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def web_client(scene, tmp_path, ci=False):
    cfg = tmp_path / "web.toml"
    text = scene.env.config.read_text() + '[roles]\ndefault = ["member"]\n'
    if ci:
        text += '[ci]\nproject = "soclabs/nanosoc-compat-ci"\nproject_id = 1\nref = "main"\njob = "t0"\n' \
                'allowed_refs = ["main"]\nallowed_runner_ids = []\n'
    cfg.write_text(text)
    return TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice"))), cfg


def test_pages_grid_cell_and_run_buttons_queue_jobs(clean, tmp_path):
    c, cfg = web_client(clean, tmp_path)
    with clean.env.active():
        r = c.get("/matrix/")
        assert r.status_code == 200 and 'href="/matrix/top-main"' in r.text and 'data-nightly="disabled"' in r.text
        assert re.search(r'<a href="/matrix/"[^>]*>Matrix</a>', r.text)           # the navigation entry
        r = c.get("/matrix/top-main")
        assert r.status_code == 200, r.text
        assert 'data-cell="alpha/in-range"' in r.text and 'data-cell="beta/head"' in r.text
        assert 'data-status="same-as-pinned"' in r.text and 'data-age-seconds' in r.text
        assert 'data-matrix-run="all"' in r.text and 'data-matrix-run="cell"' in r.text
        r = c.post("/matrix/top-main/run", data={"scope": "cell", "column": "pinned"}, follow_redirects=False)
        assert r.status_code == 303 and "queued=1" in r.headers["location"]
        r = c.post("/matrix/top-main/run", data={"scope": "row", "component": "alpha"}, follow_redirects=False)
        assert r.status_code == 303 and "queued=3" in r.headers["location"]
        got = [(j["params"]["component"], j["params"]["column"]) for j in jobs.jobs(load_config(cfg), kind="matrix-cell")]
        assert sorted(got, key=str) == sorted([(None, "pinned"), ("alpha", "in-range"), ("alpha", "latest-tag"),
                                               ("alpha", "head")], key=str)
        r = c.get("/matrix/top-main")
        assert r.text.count('<span class="pill state-info">queued</span>') >= 4
        assert c.post("/matrix/top-main/run", data={"scope": "everything"}).status_code == 400
        assert c.get("/matrix/top-main/cell?component=alpha&column=in-range").status_code == 200
        assert c.get("/matrix/top-main/cell?column=bogus").status_code == 404
        assert c.get("/matrix/nope").status_code == 404


def test_a_ci_deployment_shows_the_command_and_refuses_the_post(clean, tmp_path):
    c, _ = web_client(clean, tmp_path, ci=True)
    with clean.env.active():
        r = c.get("/matrix/top-main")
        assert r.status_code == 200 and "data-matrix-run" not in r.text and "--run --wait" in r.text
        assert c.post("/matrix/top-main/run", data={"scope": "all"}).status_code == 404


# ---------------------------------------------------------------------------------------------------------
# Review fixes (2026-10-13)
# ---------------------------------------------------------------------------------------------------------
def _view_doc(*verdicts, pinned=None):
    return {"fact": {"pinned": {"verdict": pinned},
                     "rows": [{"cells": {"c%d" % i: {"verdict": v} for i, v in enumerate(verdicts)}}]}}


@pytest.mark.parametrize("verdicts,pinned,code", [
    (("PASS", "FAIL"), "PASS", 1), (("FLAKY",), "PASS", 1), ((None,), "FAIL", 1), (("PASS", "INFRA"), "PASS", 2),
    (("UNVERIFIED", "UNVERIFIED"), "UNVERIFIED", 2), (("INFRA",), "INFRA", 2), (("PASS", "NOT-RUN"), "PASS", 0),
    (("UNVERIFIED", "FAIL"), "PASS", 1), ((None, None), None, 2)])
def test_view_exit_follows_the_cli_contract(verdicts, pinned, code):
    """interfaces section 18 and 0.2: a FAIL or FLAKY cell exits 1; else any UNVERIFIED or INFRA cell (could not
    answer) 2; else a green result 0; no result at all 2. NOT-RUN (no commit to run) is not an answer either way."""
    from nanosoc_rm.matrix.commands import _view_exit
    assert _view_exit(_view_doc(*verdicts, pinned=pinned)) == code


def test_nightly_plan_runs_head_weekly_only(clean):
    import datetime as dt
    with clean.env.active():
        cfg = load_config()
        wed = dt.datetime(2026, 10, 14, 2, 0, tzinfo=dt.timezone.utc)
        sun = dt.datetime(2026, 10, 18, 2, 0, tzinfo=dt.timezone.utc)
        assert wed.weekday() == 2 and sun.weekday() == 6
        weekday = M.nightly_plan("top-main", wed, config=cfg)
        sunday = M.nightly_plan("top-main", sun, config=cfg)
    assert ("alpha", "head") not in weekday and ("alpha", "in-range") in weekday and (None, "pinned") in weekday
    assert ("alpha", "head") in sunday


def test_run_column_honours_the_component_filter(clean, tmp_path):
    c, cfg = web_client(clean, tmp_path)
    with clean.env.active():
        r = c.post("/matrix/top-main/run", data={"scope": "column", "column": "head", "filter_component": "beta"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        got = [(j["params"]["component"], j["params"]["column"]) for j in jobs.jobs(load_config(cfg),
                                                                                     kind="matrix-cell")]
        assert got == [(None, "pinned")]                                  # beta's head is the pinned tree


def test_cell_page_needs_a_component_except_for_pinned(clean, tmp_path):
    c, cfgp = web_client(clean, tmp_path)
    with clean.env.active():
        r = c.get("/matrix/top-main/cell?column=head")
        assert r.status_code == 400 and "component" in r.text
        assert c.get("/matrix/top-main/cell?column=pinned&component=alpha").status_code == 400
        assert c.get("/matrix/top-main/cell?column=pinned").status_code == 200
        r = c.post("/matrix/top-main/run", data={"scope": "cell", "column": "head"}, follow_redirects=False)
        assert r.status_code == 400 and "component" in r.text, (r.status_code, r.text)
        assert jobs.jobs(load_config(cfgp), kind="matrix-cell") == []


def test_a_run_that_queues_nothing_says_so(clean, tmp_path):
    c, _ = web_client(clean, tmp_path)
    with clean.env.active():
        c.post("/matrix/top-main/run", data={"scope": "cell", "column": "pinned"}, follow_redirects=False)
        r = c.post("/matrix/top-main/run", data={"scope": "row", "component": "beta"}, follow_redirects=False)
        assert r.status_code == 303 and "queued=0" in r.headers["location"], r.headers["location"]
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "Nothing was queued" in page.text


def test_matrix_cli_exits_2_when_every_cell_is_unverified(clean):
    """Review item 2 (the reviewer's repro): four cells whose capture moved a pin are UNVERIFIED; the grid must
    not exit 0 (green)."""
    from test_matrix_cells import FakeCompat, fake_result

    def moved(run_dir, root, requested, overrides, scratch):
        pins = [{"tree_path": r["tree_path"], "commit": r["commit"]} for r in requested]
        pins[0]["commit"] = "f" * 40
        return {"superproject_commit": root, "pins": pins}
    for col, comp in (("pinned", None), ("head", "alpha"), ("in-range", "alpha"), ("latest-tag", "alpha")):
        f = clean.run_cell(col, comp, compat_fn=FakeCompat(fake_result(clean, "PASS")), capture_fn=moved,
                           store=True)[0]["fact"]
        assert f["verdict"] == "UNVERIFIED", f["reason"]
    with clean.env.active():
        r = CliRunner().invoke(main, ["matrix", "top-main"])
    assert "UNVERIF" in r.output and r.exit_code == 2, (r.exit_code, r.output)
