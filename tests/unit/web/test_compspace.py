"""The component workspace (Wave C spec section 4, unit K): Overview, Branches (build-first fix 4), Quality,
Versions, the /branches picker and its 303, and the local-only review POSTs."""
from __future__ import annotations

import copy
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db
from nanosoc_rm.web import compspace, create_app, routers
from webfix import EXAMPLES, make_registry, seed

pytestmark = [pytest.mark.requirement("H1")]

HASH = auth.hash_password("correct horse battery", salt=b"0123456789abcdef")


def _branch(comp, host, path, branch, verdict, *, ahead=0, behind=5, last="2026-05-02T09:00:00Z", tags=(),
            pins=(), cited=(), deletable=None, default="main"):
    if deletable is None:
        deletable = verdict in ("MERGED", "STALE") and not (tags or pins or cited)
    scope = "branch_state:%s:%s" % (host, path)
    return {"schema": 1, "kind": "branch_state", "subject": "%s:%s" % (scope, branch),
            "observed_at": "2026-10-10T12:00:00Z",
            "sources": [{"kind": "git", "command": "git for-each-ref refs/heads (fixture)"}],
            "snapshot": {"id": "%s@2026-10-10T12:00:00Z" % scope, "scope": scope},
            "fact": {"component": comp, "repo": {"host": host, "path": path}, "branch": branch,
                     "tip": "%040x" % (abs(hash(branch)) % (1 << 64)), "default_branch": default, "ahead": ahead,
                     "behind": behind, "merged": verdict == "MERGED", "last_commit_at": last, "tags": list(tags),
                     "keep_tags": [t for t in tags if t.startswith("keep/")], "pins": list(pins), "cited": list(cited),
                     "stale_days": 90, "verdict": verdict, "deletable": deletable}}


# tidelink: every row is on the gitlab COPY; the canonical github repo has none stored (build-first 4).
TIDELINK = [
    _branch("tidelink", "gitlab", "soclabs/tidelink", "exp/v1-route-a", "STALE", ahead=3, behind=291,
            last="2026-06-23T10:00:00Z"),
    _branch("tidelink", "gitlab", "soclabs/tidelink", "feat/kr260-port", "MERGED", behind=252),
    _branch("tidelink", "gitlab", "soclabs/tidelink", "wip/diea-starvation-repro", "MERGED", behind=253),
    _branch("tidelink", "gitlab", "soclabs/tidelink", "integ/freeze-2026-07-21", "MERGED", behind=4,
            tags=("freeze-2026-07-22",)),
    _branch("tidelink", "gitlab", "soclabs/tidelink", "fix/word-window", "PROTECTED", ahead=25, behind=291,
            tags=("archive/2026-07-cleanup/fix_word-window",)),
    _branch("tidelink", "gitlab", "soclabs/tidelink", "fix/credit-drain", "ACTIVE", ahead=1, behind=0,
            last="2026-10-01T10:00:00Z"),
]


def ws_config(path, data, registry):
    path.write_text("\n".join([
        "[service]", 'data_dir = "%s"' % data,
        "[registry]", 'repo = "soclabs/nanosoc-registry"', 'ref = "main"', 'path = "%s"' % registry,
        "[auth]", 'mode = "local"', "[[auth.users]]", 'login = "alice"', 'hash = "%s"' % HASH,
        "[[auth.users]]", 'login = "carol"', 'hash = "%s"' % HASH]) + "\n")
    return path


@pytest.fixture
def ws(tmp_path, monkeypatch, seeded_db):
    reg = make_registry(tmp_path / "registry")
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(seeded_db, data / "rm.sqlite")
    cfg = ws_config(tmp_path / "rm.toml", data, reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    seed(cfg, "observation.branch-state.example.jsonl", "observation.compat-result.example.jsonl")
    for d in TIDELINK:
        db.put(d, config=cfg)
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    client = TestClient(app)
    client.get("/")                                              # registry records into the store
    return {"cfg": cfg, "client": client, "app": app, "data": data, "registry": reg, "tmp": tmp_path}


# ----- pure helpers ----------------------------------------------------------------------------------------
def test_branch_why_names_the_reason_from_the_stored_row():
    f = lambda d: d["fact"]
    assert compspace.branch_why(f(TIDELINK[1])) == "Every commit is on main (0 ahead)."
    assert compspace.branch_why(f(TIDELINK[3])) == "Kept: tag freeze-2026-07-22 sits on its tip."
    assert compspace.branch_why(f(TIDELINK[0])) == "No commit since 2026-06-23 and 291 behind main."
    pinned = _branch("x", "github", "a/b", "p", "PINNED", pins=({"configuration": "eth-rc4", "tree_path": "tidelink"},))
    assert compspace.branch_why(pinned["fact"]) == "Kept: pinned by eth-rc4:tidelink."


def test_branch_view_counts_every_copy_and_ticks_every_deletable_row_on_first_load():
    v = compspace.branch_view(TIDELINK, canonical=("github", "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"))
    assert (v["total"], v["deletable"], v["selected"]) == (6, 3, 3)
    assert v["delete_text"].count("git push origin --delete") == 3
    assert "exp/v1-route-a   # STALE" in v["delete_text"]
    assert v["warnings"] == ["exp/v1-route-a has 3 commits not on main; they go with it."]
    assert [r["key"] for r in v["rows"] if r["deletable"]] == v["all_keys"]
    # unticking: `picked` with a subset re-renders the list from the ticked rows only
    keep = "gitlab:soclabs/tidelink:feat/kr260-port"
    v2 = compspace.branch_view(TIDELINK, picked=True, selected=[keep, "gitlab:soclabs/tidelink:fix/word-window"])
    assert v2["selected"] == 1 and "feat/kr260-port" in v2["delete_text"] and "exp/v1" not in v2["delete_text"]
    assert v2["warnings"] == []                                  # the STALE row is no longer ticked
    assert compspace.branch_view(TIDELINK, picked=True)["delete_text"] == ""
    # only the canonical repo: none of the copy's rows, and the page says so
    v3 = compspace.branch_view(TIDELINK, canonical=("github", "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"),
                               only_canonical=True)
    assert v3["total"] == 0 and v3["canonical_empty"] and v3["every"] == 6


def test_branch_view_filters_keep_the_selection_of_hidden_rows():
    v = compspace.branch_view(TIDELINK, verdict="MERGED")
    assert {r["doc"]["fact"]["branch"] for r in v["rows"]} == {"feat/kr260-port", "wip/diea-starvation-repro",
                                                              "integ/freeze-2026-07-21"}
    assert v["hidden"] == ["gitlab:soclabs/tidelink:exp/v1-route-a"]    # ticked, filtered out: kept in the form
    assert {r["doc"]["fact"]["branch"] for r in compspace.branch_view(TIDELINK, verdict="kept")["rows"]} == \
        {"integ/freeze-2026-07-21", "fix/word-window"}
    assert [r["doc"]["fact"]["branch"] for r in compspace.branch_view(TIDELINK, q="KR260")["rows"]] == ["feat/kr260-port"]


def test_sentence_states_only_stored_counts():
    s = compspace.sentence("tidelink", {"branches_total": 15, "deletable": 7, "analysed": True, "worth": 440,
                                        "banner": 4880, "hosts": [{"relation": "AHEAD", "canonical": "github",
                                                                  "copy_host": "gitlab", "canonical_only": 462}]})
    assert s == ("7 of 15 branches can be deleted, 440 comment findings are worth a look (4880 more are banners), "
                 "and the gitlab copy lacks 462 commits that github has.")
    assert compspace.sentence("x", {}).startswith("Nothing is stored for x yet")


# ----- pages ---------------------------------------------------------------------------------------------
def test_branches_defaults_to_every_copy_and_shows_the_delete_list_before_the_table(ws):
    c = ws["client"]
    r = c.get("/components/tidelink/branches")
    assert r.status_code == 200
    t = r.text
    assert "mirror may be missing" not in t and "No branch inventory" not in t
    assert t.index('id="delete-list"') < t.index("<table")
    assert "Delete list: 3 of 3 branches selected · 6 on every copy" in t
    boxes = re.findall(r'<tr data-verdict="([A-Z]+)" data-branch="[^"]+" data-deletable="(yes|no)">\s*<td>(<input)?', t)
    assert len(boxes) == 6
    assert all((d == "yes") == bool(inp) for _, d, inp in boxes), "a checkbox on a row that is not deletable"
    assert re.search(r'<a class="tab[^"]*"|aria-current="page">Branches', t)
    assert 'aria-label="Breadcrumb"' in t and t.count("data-age-seconds") == 1


def test_unticking_a_row_re_renders_the_delete_list_server_side(ws):
    c = ws["client"]
    r = c.get("/components/tidelink/branches", params=[("picked", "1"), ("part", "list"),
                                                        ("del", "gitlab:soclabs/tidelink:feat/kr260-port")],
              headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text            # the fragment only
    assert "Delete list: 1 of 3 branches selected" in r.text
    pre = re.search(r'<pre class="code" id="delete-list"[^>]*>(.*?)</pre>', r.text, re.S).group(1)
    assert "feat/kr260-port" in pre and "exp/v1-route-a" not in pre


def test_canonical_only_with_rows_on_a_copy_says_so(ws):
    t = ws["client"].get("/components/tidelink/branches?canonical=1").text
    assert "The canonical repo has no stored branch; 6 rows are on other copies" in t


def test_branches_picker_and_its_redirect(ws):
    c = ws["client"]
    r = c.get("/branches?component=tidelink&verdict=STALE", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/components/tidelink/branches?verdict=STALE"
    p = c.get("/branches")
    assert p.status_code == 200 and "In my configurations" in p.text and ">Show<" not in p.text
    assert 'hx-get="/branches" hx-trigger="change"' in p.text


def test_a_get_never_reads_a_mirror(ws, monkeypatch):
    """Spec rule: never walk, fetch or compare inside a request. The Branches tab reads stored rows only."""
    from nanosoc_rm.analysis import branches as br
    monkeypatch.setattr(br, "branches", lambda *a, **k: pytest.fail("a GET read the mirror"))
    r = ws["client"].get("/components/ahb_qspi/branches")
    assert r.status_code == 200 and "No branch inventory stored" in r.text


def test_overview_lists_needs_and_where_it_is_pinned(ws):
    t = ws["client"].get("/components/tidelink").text
    assert 'data-need="branches"' in t and 'data-need="quality"' in t and 'data-need="hosts"' in t
    assert "3 DELETABLE" in t and "NOT ANALYSED" in t
    assert 'data-pin="eth-rc4:tidelink"' in t
    assert "never walked" in t.lower()                     # configurations with no stored walk
    assert "Review delete list" in t and 'data-primary' in t
    assert "the gitlab copy lacks 298 commits that github has" in t          # the stored host_compare
    assert "Canonical github has 298 commits the gitlab copy lacks" in t
    assert t.count("data-age-seconds") == 1


def test_overview_marks_the_pin_holding_the_deciding_cause(ws):
    """The example eth-rc4 result decides on `t0.pins.reachable` at ASIC/asic-toolkit: plant that pin in the
    stored walk (same snapshot), and asic-toolkit's Overview names it; tidelink's does not."""
    from nanosoc_rm import contracts
    tl = next(d for _, d in contracts.docs_in(EXAMPLES / "pin-state.example.jsonl")
              if d["subject"] == "pin_state:eth-rc4:tidelink")
    at = copy.deepcopy(tl)
    at["subject"] = "pin_state:eth-rc4:ASIC/asic-toolkit"
    at["fact"].update(path="ASIC/asic-toolkit", tree_paths=["ASIC/asic-toolkit"], depth=1)
    at["fact"]["child"]["component"] = "asic-toolkit"
    db.put(at, config=ws["cfg"])
    t = ws["client"].get("/components/asic-toolkit").text
    row = re.search(r'<tr data-pin="eth-rc4:ASIC/asic-toolkit" class="is-deciding">(.*?)</tr>', t, re.S)
    assert row, t[t.find("Where asic-toolkit"):][:1500]
    assert "DECIDING GATE" in row.group(1) and "Holds the deciding cause of the eth-rc4 FAIL" in row.group(1)
    assert "DECIDING GATE" not in ws["client"].get("/components/tidelink").text


def test_versions_tab_redirects_to_the_plugin(ws):
    r = ws["client"].get("/components/tidelink/versions", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/versions/tidelink"


def test_unknown_and_invalid_components_are_404(ws):
    c = ws["client"]
    assert c.get("/components/no-such-thing").status_code == 404
    assert c.get("/components/no-such-thing/branches").status_code == 404
    assert c.get("/components/bad..id/quality").status_code == 404


def test_quality_tab_without_a_stored_result_offers_the_job_and_queues_nothing(ws):
    from nanosoc_rm import jobs
    from nanosoc_rm.config import load_config
    t = ws["client"].get("/components/tidelink/quality").text
    assert "Not analysed yet" in t and 'href="/quality/tidelink"' in t
    assert jobs.jobs(load_config(ws["cfg"]), limit=50) == []


def test_review_posts_exist_only_in_a_local_deployment(ws, tmp_path, fake_github, monkeypatch):
    local = routers.writes(ws["app"])
    for route in (("POST", "/components/{id}/review/decide"), ("POST", "/components/{id}/review/basket")):
        assert route in local
        assert route in routers.WAVE_C_POSTS
    from webfix import write_config
    ci = write_config(tmp_path / "ci.toml", data=ws["data"], github_url=fake_github.url, registry=ws["registry"])
    ci_app = create_app(ci, auth=auth.FakeMembership(login="alice"))
    assert not {w for w in routers.writes(ci_app) if w[1].startswith("/components/")}
    r = TestClient(ci_app).post("/components/tidelink/review/decide", data={"key": "x", "decision": "skip"})
    assert r.status_code in (404, 405)


def test_review_post_needs_the_csrf_token(tmp_path, monkeypatch, seeded_db):
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    cfg = ws_config(tmp_path / "rm.toml", tmp_path / "data", reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    c = TestClient(create_app(cfg))
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/auth/login").text).group(1)
    r = c.post("/auth/login", data={"csrf_token": tok, "login": "alice", "password": "correct horse battery",
                                    "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/components/tidelink/review/decide", data={"key": "x", "decision": "skip"})
    assert r.status_code == 403 and "CSRF" in r.text
