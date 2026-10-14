"""The configuration workspace (Wave C spec section 3, unit C): Overview, Submodule tree, What-if (Now vs After),
Matrix tab, Silicon and History, plus the pure helpers behind them (tree, flags, diff_gates, expectation)."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db, jobs
from nanosoc_rm.config import load_config
from nanosoc_rm.web import cfgspace, create_app, routers, verdict as V
from webfix import EXAMPLES, Hooks, make_registry, seed, write_config

RC4 = "eth-rc4"
NEW = "c22416c1c0e1244047d09c42857a0ba9501ac9e4"


def _no_ci(cfg: Path) -> Path:
    """The same service config without its [ci] section: a local deployment (local jobs, local POSTs)."""
    txt = cfg.read_text()
    i = txt.index("[ci]")
    j = txt.index("\n", txt.index("allowed_runner_ids", i))
    cfg.write_text(txt[:i] + txt[j + 1:])
    return cfg


def _site(service, tmp_path, *, local: bool):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    if local:
        _no_ci(cfg)
    seed(cfg, "observation.compat-result.example.jsonl")
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    return dict(service, cfg=cfg, client=client, config=load_config(cfg))


@pytest.fixture
def ws(service, registry_spy, tmp_path):
    return _site(service, tmp_path, local=True)


@pytest.fixture
def ci_ws(service, registry_spy, tmp_path):
    return _site(service, tmp_path, local=False)


def _example(name: str) -> list[dict]:
    return [json.loads(ln) for ln in (EXAMPLES / name).read_text().splitlines() if ln.strip().startswith("{")]


def _put_uncovered(cfg) -> dict:
    """A silicon_protection row for tidelink with no keep tag (the example rows are all covered)."""
    d = copy.deepcopy(next(x for x in _example("observation.example.jsonl") if x["kind"] == "silicon_protection"))
    sha = "5e8bdb5a2141ff5a82103813a326a2c9df2b43ed"
    d["subject"] = "silicon_protection:%s:tidelink:%s:github" % (RC4, sha)
    d["fact"].update(component="tidelink", commit=sha, tree_paths=["tidelink"], covered=False, covering_tags=[],
                     state="BRANCH_ONLY", repo={"host": "github", "path": "SoC-Labs/tidelink"})
    db.put(d, config=cfg)
    return d


def _tbody(html: str, heading_id: str) -> str:
    i = html.index('id="%s"' % heading_id)
    return html[html.index("<tbody>", i):html.index("</tbody>", i)]


# ----- pure helpers ---------------------------------------------------------------------------------------------
def _pin(path: str, comp: str | None = None, **f) -> dict:
    fact = {"tree_paths": [path], "child": {"component": comp or path.rsplit("/", 1)[-1], "commit": "%040x" % len(path)},
            "hosts": {}, "url_kind": "https", "split": False}
    fact.update(f)
    return {"subject": "pin_state:x:%s" % path, "observed_at": "2026-10-13T00:00:00Z", "fact": fact}


def test_tree_nests_each_pin_under_its_longest_tree_path_prefix():
    pins = [_pin("a"), _pin("a/b"), _pin("a/b/c"), _pin("a/bc"), _pin("x/y"), _pin("a/b/c/d/e")]
    root = cfgspace.tree(pins)
    top = {n.path: n for n in root.children}
    assert set(top) == {"a", "x/y"}                                   # x/y has no pinned parent: hangs off the root
    a = top["a"]
    assert [n.path for n in a.children] == ["a/b", "a/bc"]           # "a/bc" is not below "a/b"
    b = a.children[0]
    assert [n.path for n in b.children] == ["a/b/c"] and [n.path for n in b.children[0].children] == ["a/b/c/d/e"]
    assert b.children[0].children[0].depth == 4


def test_flags_name_the_gate_coverage_split_ssh_and_erratum():
    fc = cfgspace.FlagCtx(deciding_paths=frozenset({"tidelink"}), deciding_short="ssh pin",
                          uncovered={"tidelink": {}}, split_commits={"tidelink": 2}, errata={"tidelink": ["ERR-9"]},
                          frozen_tag="v1.0.0", frozen=True)
    n = cfgspace.Node("tidelink", _pin("tidelink", url_kind="ssh"))
    assert cfgspace.flags(n, fc) == [("GATE FAIL: ssh pin", "c-fail"), ("UNCOVERED", "c-fail"),
                                     ("SPLIT x2", "c-split"), ("ssh URL", "c-warn"), ("ERR-9", "c-warn")]
    assert cfgspace.flags(cfgspace.Node("other", _pin("other")), fc) == []
    assert cfgspace.flags(cfgspace.Node("", None), fc) == [("FROZEN v1.0.0", "c-ink")]


def _doc(gates: dict, tier=None) -> dict:
    f = {"gates": [{"id": g, "verdict": v, "detail": "d-" + g} for g, v in gates.items()]}
    if tier:
        f["tier"] = {"tier": "t1", "verdict": tier, "reason": "r"}
    return {"fact": f}


def test_diff_gates_puts_changed_rows_first_and_names_each_change():
    now = _doc({"t0.a": "FAIL", "t0.b": "PASS", "t0.c": "FAIL", "t0.d": "WARN"}, tier="INFRA")
    after = _doc({"t0.a": "PASS", "t0.b": "PASS", "t0.c": "FAIL", "t0.d": "FAIL", "t0.e": "PASS"})
    rows = cfgspace.diff_gates(now, after)
    assert [(r["gate"], r["change"]) for r in rows] == [
        ("t0.a", "better"), ("t0.d", "worse"), ("tier.t1", "gone"), ("t0.e", "new"), ("t0.b", "same"), ("t0.c", "same")]
    assert rows[0]["now_tone"] == "c-fail" and rows[0]["after_tone"] == "c-pass"
    assert all(r["change"] == "same" for r in cfgspace.diff_gates(now, now))


def test_expectation_says_an_ssh_pin_is_not_fixed_by_moving_its_commit():
    d = V.Decision("FAIL", "t0.pins.fetchable", "tidelink pinned by ssh URL", "tidelink", ("tidelink",),
                   ("t0.pins.fetchable", "t0.urls.no-ssh-public"), (), False, kind="ssh-pin", component="tidelink")
    assert cfgspace.expectation(d, "tidelink", "eth-main") == (
        "Moving tidelink alone will not turn eth-main green. Its ssh URL is the blocker, not its commit.")
    assert "is not where eth-main's deciding cause sits" in cfgspace.expectation(d, "ahb_qspi", "eth-main", ["ahb_qspi"])
    ok = V.deciding({"fact": {"verdict": "PASS", "gates": []}})
    assert "has no red gate now" in cfgspace.expectation(ok, "tidelink", "x")


# ----- Overview -------------------------------------------------------------------------------------------------
def test_overview_shows_the_deciding_row_first_and_keeps_the_section_13_hooks(ws):
    text = ws["client"].get("/configurations/%s" % RC4).text
    rows = re.findall(r"<tr ([^>]*data-gate=[^>]*)>", _tbody(text, "h-gates"))
    assert rows and 'class="is-deciding"' in rows[0], rows[:2]
    assert sum('is-deciding' in r for r in rows) == len(V.deciding(db.latest(
        "compat_result", "compat_result:%s:" % RC4, config=ws["cfg"])[0]).same_cause_gates)
    assert 'id="h-decide">The deciding gate' in text
    assert len(Hooks(text).with_attr("data-run-t0")) == 1                    # H1: exactly one Run form
    pins = {s for s, _ in Hooks(text).facts() if s.startswith("pin_state:")}
    assert pins == {d["subject"] for d in _example("pin-state.example.jsonl")}  # every pin is still a fact
    assert '<nav class="tabs" aria-label="Workspace tabs">' in text
    assert re.search(r'<a href="/configurations/eth-rc4" aria-current="page">Overview', text)
    crumbs = re.search(r'<nav class="crumbs" aria-label="Breadcrumb"><ol>(.*?)</ol>', text, re.S).group(1)
    assert "SoC configurations" in crumbs and '<li aria-current="page">Overview</li>' in crumbs


def test_overview_of_a_configuration_never_checked_says_so(ws):
    text = ws["client"].get("/configurations/eth-main").text
    assert "eth-main has never been checked" in text and "is-deciding" not in text
    assert len(Hooks(text).with_attr("data-run-t0")) == 1
    assert ws["client"].get("/configurations/no-such").status_code == 404


# ----- Submodule tree -------------------------------------------------------------------------------------------
def test_tree_opens_the_ancestors_of_the_chosen_node(ws):
    c = ws["client"]
    deep = "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen"
    parent_re = (r'<details id="(n-[0-9a-f]+)"( open)?>\s*<summary><span class="trow[^"]*" data-tree-path='
                 r'"nanosoc-multicore-system/nanosoc_arch_tech"')
    for extra in ({}, {"open": "none"}):                  # by default, and even under Collapse all
        text = c.get("/configurations/%s/tree" % RC4, params=dict(extra, node=deep)).text
        parent = re.search(parent_re, text)
        assert parent and parent.group(2) == " open", ("the chosen node's ancestor must render open", extra)
    other = re.search(parent_re, c.get("/configurations/%s/tree" % RC4, params={"node": "tidelink"}).text)
    assert other and other.group(2) is None, "no red pin and no selection below it: closed"
    sel = re.search(r'class="trow tsel[^"]*" data-tree-path="([^"]+)"', text)
    assert sel and sel.group(1) == deep
    assert '<aside id="pin-aside"' in text and "<code>%s</code></h2>" % deep in text
    closed = c.get("/configurations/%s/tree" % RC4, params={"open": "none", "node": "tidelink"}).text
    assert re.search(r'<details id="n-[0-9a-f]+">\s*<summary><span class="trow[^"]*" data-tree-path='
                     r'"nanosoc-multicore-system/nanosoc_arch_tech"', closed), "collapse all closes the others"


def test_tree_filter_uncovered_lists_only_uncovered_pins(ws):
    _put_uncovered(ws["cfg"])
    text = ws["client"].get("/configurations/%s/tree" % RC4, params={"show": "uncovered"}).text
    paths = re.findall(r'class="trow[^"]*" data-tree-path="([^"]+)"', text)
    assert paths == ["tidelink"]
    assert "UNCOVERED" in text and "<details" not in text.split('id="h-tree"')[1].split("</section>")[0]
    allp = re.findall(r'class="trow[^"]*" data-tree-path="([^"]+)"',
                      ws["client"].get("/configurations/%s/tree" % RC4).text)
    assert len(set(allp)) == 3


def test_tree_node_fragment_is_the_aside_alone(ws):
    r = ws["client"].get("/configurations/%s/tree/node" % RC4, params={"path": "tidelink"},
                         headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text.lstrip().startswith(("{#", "<aside")) is not None
    assert '<aside id="pin-aside"' in r.text and "<html" not in r.text and "<code>tidelink</code>" in r.text
    assert "ssh URL" in r.text
    assert 'data-subject="pin_state:eth-rc4:tidelink"' in r.text


# ----- What-if --------------------------------------------------------------------------------------------------
def _after(now: dict) -> dict:
    after = copy.deepcopy(now)
    f = after["fact"]
    f["overrides"] = [{"tree_path": "tidelink", "commit": NEW, "replaces": "5e8bdb5a2141ff5a82103813a326a2c9df2b43ed"}]
    for g in f["gates"]:
        if g["id"] == "t0.vendor.no-arm-ip":
            g["verdict"] = "PASS"
    return after


def test_whatif_job_opens_on_now_vs_after_with_the_baseline(ws):
    cfg = ws["config"]
    now = views_now(ws)
    job = jobs.submit(cfg, "compat", {"configuration": RC4, "pins": ["tidelink=%s" % NEW]}, "alice")
    jobs._update(cfg, job["id"], state="done", verdict="FAIL", started_at="2026-10-13T10:00:00Z",
                 finished_at="2026-10-13T10:00:40Z", result=_after(now))
    c = ws["client"]
    r = c.get("/compat", params={"job": job["id"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/configurations/%s/whatif?job=%d" % (RC4, job["id"])
    text = c.get(r.headers["location"]).text
    assert text.index("Now vs After, per gate") < text.index("Your question") < text.index("1. Pick the component")
    assert "stored result %s" % now["fact"]["nonce"][:12] in text
    rows = cfgspace.diff_gates(now, _after(now))
    same = sum(1 for x in rows if x["change"] == "same")
    assert "Nothing else moved: %d gates unchanged." % same in text
    first = re.search(r'<tr data-gate="([^"]+)" data-change="([^"]+)"', text)
    assert first.group(1) == "t0.vendor.no-arm-ip" and first.group(2) == "better"
    assert "<code>tidelink</code>" in text and NEW[:7] in text                  # the moved pin


def views_now(ws) -> dict:
    from nanosoc_rm.web import views
    return views.compat_results(ws["config"], RC4)[0]


def test_whatif_running_job_polls_and_no_baseline_asks_for_a_first_check(ws):
    cfg = ws["config"]
    job = jobs.submit(cfg, "compat", {"configuration": RC4, "pins": ["tidelink=%s" % NEW]}, "alice")
    jobs._update(cfg, job["id"], state="running", started_at="2026-10-13T10:00:00Z")
    text = ws["client"].get("/configurations/%s/whatif" % RC4, params={"job": job["id"]}).text
    assert 'hx-get="/jobs/%d/fragment" hx-trigger="every 2s"' % job["id"] in text
    assert "Now vs After" not in text
    main = ws["client"].get("/configurations/eth-main/whatif").text
    assert "eth-main has never been checked. Run a first check, then ask again." in main


def test_whatif_builds_the_pin_fields_from_component_target_and_paths(ws):
    text = ws["client"].get("/configurations/%s/whatif" % RC4,
                            params={"component": "tidelink", "target_other": NEW}).text
    form = text[text.index('<form method="post" action="/compat/jobs">'):]
    form = form[:form.index("</form>")]
    assert '<input type="hidden" name="pin" value="tidelink=%s">' % NEW in form
    assert '<input type="hidden" name="return" value="whatif">' in form
    assert "What if tidelink moves to %s in eth-rc4?" % NEW[:7] in text
    assert ws["client"].get("/compat", params={"configuration": "eth-main"}, follow_redirects=False) \
        .headers["location"] == "/configurations/eth-main/whatif"


def test_a_ci_deployment_whatif_offers_the_cli_and_no_local_form(ci_ws):
    text = ci_ws["client"].get("/configurations/%s/whatif" % RC4,
                               params={"component": "tidelink", "target_other": NEW}).text
    assert "/compat/jobs" not in text and "nanosoc-rm compat eth-rc4 --pin tidelink=%s" % NEW in text


# ----- Matrix, Silicon, History ---------------------------------------------------------------------------------
def test_matrix_tab_hands_over_to_the_plugin(ws):
    r = ws["client"].get("/configurations/%s/matrix" % RC4, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/matrix/%s" % RC4


def test_silicon_lists_uncovered_rows_first_and_keeps_the_errata_hooks(ws):
    _put_uncovered(ws["cfg"])
    text = ws["client"].get("/configurations/%s/silicon" % RC4).text
    covered = re.findall(r'<tr data-covered="(yes|no)">', text)
    assert covered[0] == "no" and covered.count("no") == 1 and len(covered) == 14
    assert "13 of 14 silicon commits are covered by keep/eth-rc4/* tags. 1 is not." in text
    assert "git tag keep/eth-rc4/tidelink 5e8bdb5a2141ff5a82103813a326a2c9df2b43ed" in text
    assert re.search(r'<tr data-erratum="ETH-R0P0-ERR-001" data-exposure="[^"]+" data-method="[^"]+"', text)
    assert 'id="h-waivers">T0 waivers (7)' in text


def test_history_lists_jobs_and_stored_results(ws):
    cfg = ws["config"]
    job = jobs.submit(cfg, "compat", {"configuration": RC4}, "alice")
    text = ws["client"].get("/configurations/%s/history" % RC4).text
    assert 'data-job="%d"' % job["id"] in text and "nanosoc-rm compat eth-rc4" in text
    assert "Stored compat results (1)" in text and "No CI is configured" in text


# ----- seams ----------------------------------------------------------------------------------------------------
def test_the_workspace_adds_get_routes_only_and_a_get_never_queues(ws, monkeypatch):
    app = ws["client"].app
    mine = {p for p, m, _ in routers.route_table(app) if p.startswith("/configurations/{id}/")}
    assert {"/configurations/{id}/tree", "/configurations/{id}/tree/node", "/configurations/{id}/whatif",
            "/configurations/{id}/matrix", "/configurations/{id}/silicon", "/configurations/{id}/history"} <= mine
    for r in cfgspace.router(app).routes:
        assert r.methods == {"GET"}, r.path
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a GET queued a job")))
    for path in ("", "/tree", "/tree/node?path=tidelink", "/whatif?component=tidelink", "/silicon", "/history"):
        assert ws["client"].get("/configurations/%s%s" % (RC4, path)).status_code == 200, path


# ----- review fixes (C-1 .. C-6) --------------------------------------------------------------------------------
def test_overview_puts_the_deciding_gate_first_even_when_another_fail_is_stored_first(ws):
    """C-1: same-verdict gates keep stored order only after the deciding rows."""
    doc = copy.deepcopy(views_now(ws))
    gates = doc["fact"]["gates"]
    i = next(k for k, g in enumerate(gates) if g["id"] == "t0.vendor.no-arm-ip")
    gates.insert(0, gates.pop(i))
    doc["observed_at"] = doc["fact"]["finished_at"] = "2099-01-01T00:00:00Z"
    db.put(doc, config=ws["cfg"])
    dec = V.deciding(doc)
    assert dec.gate and dec.gate != "t0.vendor.no-arm-ip"
    rows = re.findall(r"<tr ([^>]*data-gate=[^>]*)>",
                      _tbody(ws["client"].get("/configurations/%s" % RC4).text, "h-gates"))
    flags = ['class="is-deciding"' in r for r in rows]
    n = sum(flags)
    assert n and flags[:n] == [True] * n, rows


def test_hosts_paint_branch_only_red_on_a_frozen_tree_only():
    """C-2a."""
    p = _pin("tidelink", hosts={"github": {"state": "BRANCH_ONLY", "pinned": True}})
    assert cfgspace._hosts(p, True) == [("github* BRANCH_ONLY", "c-fail")]
    assert cfgspace._hosts(p, False)[0][1] != "c-fail"


def test_overview_silicon_tile_counts_only_covered_rows(ws):
    """C-2b: an uncovered row must not read as N of N."""
    _put_uncovered(ws["cfg"])
    text = ws["client"].get("/configurations/%s" % RC4).text
    m = re.search(r'<div class="lbl">Silicon protection</div><div class="disp"[^>]*>([^<]+)</div>', text)
    assert m and m.group(1) == "13 of 14", m and m.group(1)


def test_whatif_refuses_a_job_of_another_configuration(ws):
    """C-2c."""
    cfg = ws["config"]
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main", "pins": ["tidelink=%s" % NEW]}, "alice")
    jobs._update(cfg, job["id"], state="done", verdict="FAIL", started_at="2026-10-13T10:00:00Z",
                 finished_at="2026-10-13T10:00:40Z", result=_after(views_now(ws)))
    text = ws["client"].get("/configurations/%s/whatif" % RC4, params={"job": job["id"]}).text
    assert "job %d is not a compat job of %s" % (job["id"], RC4) in text
    assert "Now vs After, per gate" not in text


def test_expectation_names_the_component_that_holds_the_deciding_cause():
    """C-2d."""
    d = V.Decision("FAIL", "t0.pins.reachable", "ahb_qspi unreachable", "ahb_qspi", ("ahb_qspi",),
                   ("t0.pins.reachable",), (), False, kind="pin", component="ahb_qspi")
    assert cfgspace.expectation(d, "ahb_qspi", "eth-main", ["ahb_qspi"]) == (
        "ahb_qspi holds the deciding cause (ahb_qspi unreachable). A different commit may clear it; the run will tell.")


def test_diff_gates_calls_an_equal_severity_move_changed_not_worse():
    """C-3."""
    a = _doc({"g1": "NOT-RUN", "g2": "WARN"})
    b = _doc({"g1": "INFRA", "g2": "WAIVED"})
    for x, y in ((a, b), (b, a)):
        assert [(r["gate"], r["change"]) for r in cfgspace.diff_gates(x, y)] == [("g1", "changed"), ("g2", "changed")]
    assert cfgspace.CHANGE_TONE["changed"] != cfgspace.CHANGE_TONE["worse"]


def _primaries(html: str) -> int:
    return len(re.findall(r'class="(?:prim|primary)"', html))


def test_each_workspace_page_has_one_primary_action(ws):
    """C-4: spec 3.1, the single primary action."""
    c = ws["client"]
    assert _primaries(c.get("/configurations/%s" % RC4).text) == 1
    w = c.get("/configurations/%s/whatif" % RC4, params={"component": "tidelink", "target_other": NEW}).text
    assert _primaries(w) == 1 and re.search(r'class="prim"[^>]*>Run what-if', w)
    assert len(Hooks(w).with_attr("data-run-t0")) == 1


def test_an_oversized_job_number_is_not_a_server_error(ws):
    """C-5."""
    big = "99999999999999999999999"
    c = ws["client"]
    for url in ("/configurations/%s/whatif" % RC4, "/compat", "/configurations/%s" % RC4):
        assert c.get(url, params={"job": big}, follow_redirects=False).status_code < 500, url
    assert jobs.get(ws["config"], int(big)) is None


def test_picking_a_target_reloads_the_page_without_the_extra_button(ws):
    """C-6: with htmx, a target radio change is enough; the button stays for no-JS."""
    text = ws["client"].get("/configurations/%s/whatif" % RC4, params={"component": "tidelink"}).text
    form = text[text.index('id="wi-target"'):]
    form = form[:form.index("</form>")]
    assert 'hx-trigger="change"' in form and 'hx-select="#main"' in form and 'hx-push-url="true"' in form
