"""The Wave F front pages (CR-WF-gui; David 2026-10-14: front page approved): the projects home at `/`, the
freshness matrix at /matrix/, the Catalogue page, Notifications behind the bell, and the header.

The estate (fpfix.estate): eth-main (live) and eth-rc4 (frozen silicon), both rooted at nanosoc-ethernet-chiplet
in the example registry; multicore-master is registered but has no stored scan."""
from __future__ import annotations

import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, jobs
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app, home, uistate
from fpfix import csrf, estate, local_site, post_form, put_tree, sign_in
from webfix import Hooks, make_registry, seed, write_config


@pytest.fixture
def fp(tmp_path, monkeypatch, seeded_db):
    s = local_site(tmp_path, monkeypatch, seeded_db)
    c = sign_in(TestClient(s["app"]))
    c.get("/")                                            # reads the registry records into the store
    estate(s["cfg"])
    s["client"] = c
    s["config"] = load_config(s["cfg"])
    return s


def rows(text):
    """[(row key, state, the state word shown, the age text)] in page order."""
    out = []
    for m in re.finditer(r'<li class="fp-li[^"]*" id="[^"]+" data-row="([^"]+)" data-state="([^"]+)"(.*?)</li>', text, re.S):
        word = re.search(r'<span class="fs fs-\w+"><span class="mk mk-\w+" aria-hidden="true"></span>([^<]+)</span>',
                         m.group(3))
        ago = re.search(r'<span class="fp-ago">([^<]*)</span>', m.group(3))
        out.append((m.group(1), m.group(2), word.group(1) if word else None, ago.group(1) if ago else None))
    return out


def keys(text):
    return [r[0] for r in rows(text)]


def detail(text):
    m = re.search(r'<aside aria-label="Details"[^>]*>(.*?)</aside>', text, re.S)
    return m.group(1) if m else ""


def facts(text):
    return dict(re.findall(r"<dt>([^<]+)</dt><dd[^>]*>([^<]*)</dd>", detail(text)))


# ----- Home: the tree ------------------------------------------------------------------------------------------
def test_home_is_the_projects_tree_with_the_most_outdated_project_open(fp):
    text = fp["client"].get("/").text
    assert 'id="inbox"' not in text and "<title>Projects ·" in text
    k = keys(text)
    # the default view: the outdated project and its live tree open on its direct pins, frozen tree closed
    assert k[:2] == ["p:nanosoc-ethernet-chiplet", "c:eth-main"]
    pins = [x for x in k if x.startswith("n:eth-main:")]
    # sibling order: SoC, custom peripherals, generator, flows (David: SoC, custom, subsystems, arch, generator,
    # libraries, toolkits/flows); deeper pins stay closed
    assert pins == ["n:eth-main:nanosoc-multicore-system", "n:eth-main:tidelink", "n:eth-main:nanosoc_gen",
                    "n:eth-main:asic-flow"]
    assert "c:eth-rc4" in k and not any(x.startswith("n:eth-rc4:") for x in k)
    # projects with no stored scan fold into one group (multicore-master is registered but not scanned)
    assert k[-1] == "grp:unk" and "Not measured yet (2)" in text


def test_every_row_says_its_state_in_words_beside_a_shape(fp):
    text = fp["client"].get("/?v=1&o=p:nanosoc-ethernet-chiplet&o=c:eth-main&o=c:eth-rc4").text
    got = rows(text)
    assert got and all(w for _, _, w, _ in got), got
    words = {"outdated": "Outdated", "behind": "Behind", "current": "Current", "frozen": "Frozen",
             "not_measured": "Not measured", "unpushed": "Unpushed"}
    assert all(w == words[s] for _, s, w, _ in got), got
    by = {k: (s, a) for k, s, _, a in got}
    assert by["n:eth-main:tidelink"] == ("behind", "20 d ago")
    assert by["n:eth-main:asic-flow"] == ("current", "5 d ago")
    assert by["n:eth-main:nanosoc-multicore-system"][0] == "outdated"


def test_a_frozen_tree_reads_frozen_and_not_counted_never_outdated(fp):
    text = fp["client"].get("/?v=1&o=p:nanosoc-ethernet-chiplet&o=c:eth-rc4").text
    by = {k: s for k, s, _, _ in rows(text)}
    assert by["c:eth-rc4"] == "frozen" and by["n:eth-rc4:tidelink"] == "frozen"     # 327 behind, still frozen
    row = re.search(r'data-row="c:eth-rc4".*?</li>', text, re.S).group(0)
    assert "not counted" in row and "frozen 46 d ago" in row
    pills = dict(re.findall(r'data-fresh-pill="(\w+)">.*?<span class="mono">(\d+)</span>', text, re.S))
    assert pills["outdated"] == "1" and pills["frozen"] == "1"       # eth-main is the one outdated tree


def _pills(text):
    return {k: int(n) for k, n in re.findall(r'data-fresh-pill="(\w+)">.*?<span class="mono">(\d+)</span>', text, re.S)}


def test_a_scanned_tree_that_reads_not_measured_is_in_the_not_measured_pill_and_filter(fp):
    c = fp["client"]
    before = _pills(c.get("/").text)
    put_tree(fp["cfg"], "eth-main", "nanosoc-ethernet-chiplet", [
        {"tree_path": "nanosoc-multicore-system", "component": "nanosoc-multicore-system", "behind": 0},
        {"tree_path": "tidelink", "component": "tidelink", "behind": None}])          # a direct pin not counted
    text = c.get("/?v=1&o=p:nanosoc-ethernet-chiplet").text
    assert {k: s for k, s, _, _ in rows(text)}["c:eth-main"] == "not_measured"
    after = _pills(text)
    assert after["outdated"] == before["outdated"] - 1 and after["not_measured"] == before["not_measured"] + 1
    assert sum(after.values()) == sum(before.values())                      # every tree is in exactly one pill
    text = c.get("/?f=not_measured&v=1&o=p:nanosoc-ethernet-chiplet").text
    assert "p:nanosoc-ethernet-chiplet" in keys(text) and "c:eth-main" in keys(text)


def test_a_configuration_is_judged_on_its_direct_pins_a_pin_row_on_what_is_beneath(fp):
    put_tree(fp["cfg"], "eth-main", "nanosoc-ethernet-chiplet", [
        {"tree_path": "nanosoc-multicore-system", "component": "nanosoc-multicore-system", "behind": 0},
        {"tree_path": "nanosoc-multicore-system/ethernet-subsystem-ahb", "component": "ethernet-subsystem-ahb",
         "behind": 34, "bump_days": 105}])
    text = fp["client"].get("/?v=1&o=p:nanosoc-ethernet-chiplet&o=c:eth-main").text
    by = {k: s for k, s, _, _ in rows(text)}
    assert by["c:eth-main"] == "current"                               # every DIRECT pin is current
    assert by["n:eth-main:nanosoc-multicore-system"] == "outdated"    # its own pin is current; beneath it is not
    text = fp["client"].get("/?v=1&o=p:nanosoc-ethernet-chiplet&o=c:eth-main"
                            "&sel=n:eth-main:nanosoc-multicore-system").text
    assert "its own pin; worst beneath it: outdated" in detail(text)


def test_rows_are_links_that_open_close_and_select(fp):
    c = fp["client"]
    text = c.get("/").text
    href = re.search(r'data-row="n:eth-main:nanosoc-multicore-system".*?<a class="fp-row" href="([^"]+)"', text,
                     re.S).group(1).replace("&amp;", "&")
    text = c.get(href).text
    k = keys(text)
    assert "n:eth-main:nanosoc-multicore-system/ethernet-subsystem-ahb" in k          # opened
    assert 'aria-current="true"' in re.search(r'data-row="n:eth-main:nanosoc-multicore-system".*?</li>', text,
                                              re.S).group(0)                           # and selected
    href = re.search(r'data-row="n:eth-main:nanosoc-multicore-system".*?<a class="fp-row" href="([^"]+)"', text,
                     re.S).group(1).replace("&amp;", "&")
    assert "n:eth-main:nanosoc-multicore-system/ethernet-subsystem-ahb" not in keys(c.get(href).text)   # closed


def test_the_pin_panel_gives_behind_bump_sha_and_where_the_same_pin_lags(fp):
    put_tree(fp["cfg"], "multicore-master", "nanosoc-ethernet-chiplet", [
        {"tree_path": "tidelink", "component": "tidelink", "behind": 3, "bump_days": 20,
         "pinned": "%040x" % (abs(hash(("eth-main", "tidelink"))) & ((1 << 160) - 1))}])
    text = fp["client"].get("/?v=1&sel=n:eth-main:tidelink").text
    f = facts(text)
    assert f["Commits behind"] == "3 behind main"
    assert f["Last bump"].startswith(tuple("0123456789")) and "by rm-fixture · 20 d ago · Fresh" in f["Last bump"]
    assert len(f["Pinned commit"]) == 7 and f["Path"] == "tidelink"
    assert f["Same pin also lags in"] == "multicore-master"
    assert 'data-source-for="freshness:eth-main:tidelink"' in detail(text)          # the fact and its src link


def test_the_configuration_and_project_panels(fp):
    f = facts(fp["client"].get("/?sel=c:eth-rc4").text)
    assert f["Direct pins lagging"] == "1 of 2 (if live)" and f["Frozen"] == "46 d ago"
    # its own counts are 0 (never lagging); the panel reads the would-be numbers (CR-WF-fresh-r1)
    assert f["Most behind"] != "nothing behind" and "(0 outdated" not in f["Whole tree lagging"]
    assert f["Waiting longest"] != "nothing lagging"
    d = detail(fp["client"].get("/?sel=c:eth-rc4").text)
    assert "frozen silicon, not counted; would be outdated if live" in d and "never count as outdated" in d
    f = facts(fp["client"].get("/?sel=c:eth-main").text)
    assert f["Direct pins lagging"] == "2 of 4" and f["Most behind"] == "ethernet-subsystem-ahb, 34 commits"
    d = detail(fp["client"].get("/?sel=p:nanosoc-multicore-system").text)
    assert "Type: SoC" in d and "Implementation ?" in d and "No forkable project" in d        # guessed role
    assert "multicore-master is registered but has no stored scan yet" in d


def test_filters_fold_and_count(fp):
    c = fp["client"]
    text = c.get("/?f=frozen").text
    assert [k for k in keys(text) if k.startswith("p:")] == ["p:nanosoc-ethernet-chiplet"]
    text = c.get("/?gap=1&filters=1&v=1&o=grp:unk").text
    assert [k for k in keys(text) if k.startswith("p:")] == ["p:nanosoc-multicore-system"]   # the SoC no forkable
    assert re.search(r'data-gap-filter>No forkable project <span class="mono">1</span>', text)  # project wraps
    text = c.get("/?lv=subsystem&filters=1&v=1&o=grp:unk").text
    assert [k for k in keys(text) if k.startswith("p:")] == ["p:ethernet-subsystem-ahb"]
    assert "Filters (1)" in text


def test_the_timeline_toggle_is_saved_per_login_and_a_get_never_creates_gui_state(fp, tmp_path, monkeypatch):
    c = fp["client"]
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: pytest.fail("a GET submitted a job"))
    for path in ("/", "/?tl=on", "/matrix/", "/catalogue/", "/notifications", "/inbox"):
        assert c.get(path).status_code == 200, path
    assert not uistate.path(fp["config"]).exists()
    text = c.get("/").text
    assert 'data-timeline="off"' in text and 'class="fp-bar' not in text
    assert 'class="fp-bar' in c.get("/?tl=on").text                    # a URL may override for one view
    r = c.post("/home/timeline", data={"csrf_token": csrf(text), "timeline": "on", "return": "/?v=1&sel=c:eth-main"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?v=1&sel=c:eth-main"
    text = c.get("/").text
    assert 'data-timeline="on"' in text and 'class="fp-bar' in text and "Dashed line = eth-rc4 frozen" in text
    assert uistate.prefs(fp["config"], "alice") == {"timeline": "on"}
    carol = sign_in(TestClient(fp["app"]), "carol")
    assert 'data-timeline="off"' in carol.get("/").text                # per login
    assert c.post("/home/timeline", data={"csrf_token": csrf(text), "timeline": "maybe"}).status_code == 400
    assert c.post("/home/timeline", data={"timeline": "on"}).status_code == 403          # no CSRF token
    r = c.post("/home/timeline", data={"csrf_token": csrf(text), "timeline": "off", "return": "https://x.example"},
               follow_redirects=False)
    assert r.headers["location"] == "/"                                 # never an open redirect


def test_the_section_13_board_stays_on_home_for_at_h1(fp):
    from test_web_pages import cells
    text = fp["client"].get("/").text
    assert re.search(r'<details id="home-checks" class="fp-checks">', text)
    p, sw = cells(text)
    assert sw.check_h1({"/": text}, configurations=["eth-rc4", "eth-main", "multicore-master"], cli_subjects={}) == []
    assert text.count("data-age-seconds=") == 1


def test_no_github_ci_deployment_gets_a_wave_f_post(service, registry_spy, tmp_path):
    from nanosoc_rm.web import routers
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    assert not (routers.WAVE_F_POSTS | {("POST", "/catalogue/patch")}) & routers.writes(app)
    c = TestClient(app)
    text = c.get("/").text
    assert 'action="/home/timeline"' not in text and 'data-timeline="off"' in text
    for path in ("/notifications", "/catalogue/"):
        body = c.get(path).text
        assert 'action="/notifications/' not in body and 'action="/catalogue/patch"' not in body, path


# ----- the header ------------------------------------------------------------------------------------------------
def test_the_header_has_the_five_entries_more_search_and_the_bell(fp):
    text = fp["client"].get("/matrix/").text
    nav = re.search(r'<nav aria-label="Main" class="topnav">(.*?)</nav>', text, re.S).group(1)
    main = re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav.split("<details")[0])
    assert [x[1] for x in main] == ["Projects", "Matrix", "Releases", "Silicon", "Catalogue"]
    assert re.search(r'<a href="/matrix/" aria-current="page">Matrix</a>', nav)
    assert "<summary>More</summary>" in nav and ">Freshness</a>" in nav and ">Compat</a>" in nav
    assert 'id="cmd" type="search"' in text
    bell = re.search(r'<a class="bell" href="/notifications" data-bell="(\d+)" aria-label="Notifications, (\d+) unread"',
                     text)
    assert bell and bell.group(1) == bell.group(2)


# ----- Matrix ----------------------------------------------------------------------------------------------------
def test_the_matrix_defaults_to_freshness_and_keeps_the_compatibility_tab(fp):
    c = fp["client"]
    text = c.get("/matrix/").text
    assert re.search(r'id="panel-freshness" aria-labelledby="tab-freshness">', text)
    assert re.search(r'id="panel-compat" aria-labelledby="tab-compat" hidden>', text)
    assert 'data-nightly="disabled"' in text and 'href="/matrix/eth-rc4"' in text      # the D3 index, unchanged
    heads = dict(re.findall(r'data-column="([^"]+)"><span class="colid">[^<]+</span><span class="colsub">([^<]+)<', text))
    assert list(heads) == ["eth-main", "eth-rc4"] and heads["eth-rc4"] == "frozen silicon · not counted"
    shown = re.findall(r'data-matrix-row="([^"]+)"', text)
    assert "tidelink" in shown and "asic-flow" not in shown       # current in every live tree: hidden by default
    assert "1 repo is current in every live tree and hidden" in text or "repos are current" in text
    assert "asic-flow" in re.findall(r'data-matrix-row="([^"]+)"', c.get("/matrix/?rows=all").text)
    cell = re.search(r'data-matrix-row="tidelink".*?</tr>', text, re.S).group(0)
    assert 'data-cell-state="beh"' in cell and ">Behind<" in cell and 'data-cell-state="frz"' in cell
    compat = c.get("/matrix/?tab=compat").text
    assert re.search(r'id="panel-compat" aria-labelledby="tab-compat">', compat)


def test_a_matrix_cell_opens_one_detail_line_linking_to_the_pin_on_home(fp):
    c = fp["client"]
    text = c.get("/matrix/?comp=ethernet-subsystem-ahb&cfg=eth-main&rows=all").text
    d = re.search(r'<tr class="detail" data-detail="ethernet-subsystem-ahb">(.*?)</tr>', text, re.S).group(1)
    assert "Outdated, 34 behind main" in d
    href = re.search(r'<a href="([^"]+)">Open in the project tree</a>', d).group(1).replace("&amp;", "&")
    home_text = c.get(href).text
    k = keys(home_text)
    assert "n:eth-main:nanosoc-multicore-system/ethernet-subsystem-ahb" in k                 # its parents opened
    assert "Pin in eth-main" in detail(home_text) and "ethernet-subsystem-ahb" in detail(home_text)


# ----- Catalogue -------------------------------------------------------------------------------------------------
def test_the_catalogue_page_lists_the_queue_the_questions_the_groups_and_the_ignored(fp):
    seed(fp["cfg"], "observation.catalogue.example.jsonl")
    text = fp["client"].get("/catalogue/").text
    assert 'data-new-repo="github:SoC-Labs/example-new-repo"' in text
    assert 'data-ask="tidechart"' in text and "Component, or toolkit (it is firmware + host tools)?" in text
    assert "SoC-Labs/demo-repository" in text and re.search(r'data-group="soc">.*?nanosoc-multicore-system ?', text, re.S)
    assert text.count("data-age-seconds=") == 1 and 'data-subject="repo_seen:github:SoC-Labs/example-new-repo"' in text


def test_save_builds_a_patch_and_writes_nothing(fp):
    seed(fp["cfg"], "observation.catalogue.example.jsonl")
    c = fp["client"]
    reg = fp["registry"]
    before = (reg / "catalogue.yaml").read_text()
    page = c.get("/catalogue/").text
    form = {"csrf_token": csrf(page), "new.0.repo": "github:SoC-Labs/example-new-repo", "new.0.group": "ignore",
            "ask.tidechart": "toolkit", "do": "preview"}
    r = c.post("/catalogue/patch", data=form)
    assert r.status_code == 200
    patch = re.search(r'<textarea id="patch"[^>]*>(.*?)</textarea>', r.text, re.S).group(1)
    assert "+++ b/catalogue.yaml" in patch and "example-new-repo" in patch and "not nanoSoC" in patch
    assert re.search(r'^\+.*"id": "tidechart".*"group": "toolkit"', patch.replace("&#34;", '"'), re.M), patch
    assert 'data-preview-valid="true"' in r.text
    dl = c.post("/catalogue/patch", data=dict(form, do="download"))
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("text/x-diff")
    assert 'filename="catalogue.patch"' in dl.headers["content-disposition"] and "+++ b/catalogue.yaml" in dl.text
    assert (reg / "catalogue.yaml").read_text() == before
    assert subprocess.run(["git", "status", "--porcelain"], cwd=reg, capture_output=True, text=True).stdout == ""
    # confirming a guessed group without changing it confirms it
    r = c.post("/catalogue/patch", data={"csrf_token": csrf(page), "confirm.tidechart": "1", "do": "preview"})
    assert "tidechart: confirm group" in r.text
    assert c.post("/catalogue/patch", data={"new.0.group": "ignore"}).status_code == 403                # no CSRF
    assert c.post("/catalogue/patch", data={"csrf_token": csrf(page), "do": "download"}).status_code == 400


def _seen(cfg, stamp, *repos):
    """Store one complete repo_seen snapshot of `repos` [(path, mentions_nanosoc)], shaped like the example."""
    import copy, json
    from nanosoc_rm import db
    from webfix import EXAMPLES
    base = json.loads((EXAMPLES / "observation.catalogue.example.jsonl").read_text().splitlines()[0])
    for path, mentions in repos:
        d = copy.deepcopy(base)
        d["subject"] = "repo_seen:github:" + path
        d["observed_at"] = d["fact"]["first_seen"] = stamp
        d["snapshot"]["id"] = "repo_seen:github@%s#%08x" % (stamp, abs(hash(stamp)) & 0xffffffff)
        d["fact"]["repo"]["path"] = path
        d["fact"]["mentions_nanosoc"] = mentions
        d["fact"]["description"] = "a nanoSoC thing" if mentions else "something else"
        db.put(d, config=cfg)


def _as_browser(text, **extra):
    """The form a browser submits for /catalogue/ untouched: every hidden input, every checked radio and each
    select's selected (else first) option, plus `extra`."""
    body = re.search(r'<form[^>]*action="/catalogue/patch".*?</form>', text, re.S).group(0)
    pairs = []
    for m in re.finditer(r"<input ([^>]*)>", body):
        a = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', m.group(1)))
        if "name" not in a or "disabled" in m.group(1):
            continue
        if a.get("type") == "hidden" or (a.get("type") == "radio" and " checked" in m.group(1)):
            pairs.append((a["name"], a.get("value", "")))
    for m in re.finditer(r'<select [^>]*name="([^"]+)"[^>]*>(.*?)</select>', body, re.S):
        sel = re.search(r'<option value="([^"]*)"[^>]* selected', m.group(2)) or re.search(r'<option value="([^"]*)"', m.group(2))
        pairs.append((m.group(1), sel.group(1)))
    return pairs + list(extra.items())


def test_a_new_repo_starts_on_decide_later_so_an_untouched_save_adds_nothing(fp):
    _seen(fp["cfg"], "2026-10-14T10:00:00Z", ("SoC-Labs/nanoSoC-Gen", True), ("SoC-Labs/other-thing", False))
    c = fp["client"]
    text = c.get("/catalogue/").text
    assert "Nothing is checked until you decide." in text
    assert not re.search(r'name="new\.\d+\.group" value="(?!")[^"]+" checked', text)        # no group pre-picked
    assert len(re.findall(r'name="new\.\d+\.group" value="" checked', text)) == 2           # Decide later
    assert 'data-suggest="project"' in text and "Suggested: Project" in text                # a hint only
    r = post_form(c, "/catalogue/patch", _as_browser(text, do="download"))
    assert r.status_code == 400 and "no change chosen" in r.text, r.text[:400]


def test_a_new_repo_choice_follows_its_repo_not_its_row(fp):
    _seen(fp["cfg"], "2026-10-14T10:00:00Z", ("SoC-Labs/nanoSoC-Gen", True), ("SoC-Labs/other-thing", False))
    c = fp["client"]
    text = c.get("/catalogue/").text
    i = re.search(r'name="new\.(\d+)\.repo" value="github:SoC-Labs/nanoSoC-Gen"', text).group(1)
    # a newer scan lands between the GET and the Save, and sorts ahead of nanoSoC-Gen
    _seen(fp["cfg"], "2026-10-14T11:00:00Z", ("SoC-Labs/aaa-nanosoc-new-chip", True),
          ("SoC-Labs/nanoSoC-Gen", True), ("SoC-Labs/other-thing", False))
    assert 'value="github:SoC-Labs/aaa-nanosoc-new-chip"' in c.get("/catalogue/").text
    form = [("csrf_token", csrf(text)), ("new.%s.repo" % i, "github:SoC-Labs/nanoSoC-Gen"),
            ("new.%s.group" % i, "ignore"), ("do", "download")]
    patch = post_form(c, "/catalogue/patch", form).text
    assert "SoC-Labs/nanoSoC-Gen" in patch and "aaa-nanosoc-new-chip" not in patch, patch
    # a choice for a repo that left the queue is skipped and said so, never moved onto another row
    gone = [("csrf_token", csrf(text)), ("new.0.repo", "github:SoC-Labs/vanished"), ("new.0.group", "ignore"),
            ("do", "preview")]
    r = post_form(c, "/catalogue/patch", gone)
    assert "SoC-Labs/vanished is no longer in the queue" in r.text
    patch = re.search(r'<textarea id="patch"[^>]*>(.*?)</textarea>', r.text, re.S)
    assert patch is None or "nanosoc" not in patch.group(1).lower()


# ----- Notifications ---------------------------------------------------------------------------------------------
def _items(text):
    return [(e["data-item"], e["data-read"]) for e in Hooks(text).with_attr("data-read")]


def _bell(text):
    return int(re.search(r'data-bell="(\d+)"', text).group(1))


def test_notifications_are_one_line_each_with_one_action_and_the_bell_counts_them(fp):
    seed(fp["cfg"], "observation.catalogue.example.jsonl")
    c = fp["client"]
    text = c.get("/notifications").text
    items = _items(text)
    assert items and all(r == "false" for _, r in items) and _bell(text) == len(items)
    assert ("catalogue:new-repos", "false") in items and "1 new repo to characterise" in text
    li = re.findall(r'<li class="fp-note-row[^"]*"(.*?)</li>', text, re.S)
    assert all(x.count('class="fp-note-act"') == 1 for x in li)                 # one action each
    first, fp_ = items[0][0], re.search(r'name="item" value="%s"><input type="hidden" name="fp" value="([0-9a-f]+)"'
                                        % re.escape(items[0][0]), text).group(1)
    r = c.post("/notifications/read", data={"csrf_token": csrf(text), "item": first, "fp": fp_}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notifications"
    text2 = c.get("/notifications").text
    assert first not in [i for i, _ in _items(text2)] and _bell(text2) == len(items) - 1
    assert _bell(c.get("/catalogue/").text) == len(items) - 1          # the bell on any other page counts too
    assert (first, "true") in _items(c.get("/notifications?show=all").text)
    # the card view shares the read state (inbox done)
    assert first not in [e["data-card"] for e in Hooks(c.get("/inbox").text).with_attr("data-card")]
    c.post("/notifications/read", data={"csrf_token": csrf(text), "item": first, "fp": fp_, "read": "0"})
    assert _bell(c.get("/notifications").text) == len(items)


def test_mark_all_read_covers_only_what_the_page_showed(fp):
    c = fp["client"]
    text = c.get("/notifications").text
    shown = [i for i, _ in _items(text)]
    form = [("csrf_token", csrf(text))]
    for m in re.finditer(r'action="/notifications/read-all".*?</form>', text, re.S):
        form += re.findall(r'name="(item|fp)" value="([^"]+)"', m.group(0))
    assert len([x for x in form if x[0] == "item"]) == len(shown)
    seed(fp["cfg"], "observation.catalogue.example.jsonl")             # a new item after the page was drawn
    r = post_form(c, "/notifications/read-all", form, follow_redirects=False)
    assert r.status_code == 303
    assert [i for i, _ in _items(c.get("/notifications").text)] == ["catalogue:new-repos"]
    assert c.post("/notifications/read-all", data={"csrf_token": csrf(text)}).status_code == 400


def test_notify_me_about_mutes_kinds_and_projects_per_login(fp):
    seed(fp["cfg"], "observation.catalogue.example.jsonl")
    c = fp["client"]
    text = c.get("/notifications").text
    listed = re.findall(r'name="listed" value="([^"]+)"', text)
    assert "notify.kind.catalogue" in listed and "notify.project.nanosoc-ethernet-chiplet" in listed
    on = [x for x in listed if x != "notify.kind.catalogue"]
    r = post_form(c, "/notifications/prefs", [("csrf_token", csrf(text))] + [("listed", x) for x in listed]
                  + [("on", x) for x in on], follow_redirects=False)
    assert r.status_code == 303
    after = c.get("/notifications").text
    assert "catalogue:new-repos" not in [i for i, _ in _items(after)] and _bell(after) == _bell(text) - 1
    assert re.search(r'id="nk-catalogue" name="on" value="notify.kind.catalogue">', after)        # unticked
    carol = sign_in(TestClient(fp["app"]), "carol")
    assert "catalogue:new-repos" in [i for i, _ in _items(carol.get("/notifications").text)]
    bad = c.post("/notifications/prefs", data={"csrf_token": csrf(text), "listed": "notify.kind.nonsense"})
    assert bad.status_code == 400
    # muting a project hides the items about its configurations
    projects = [x for x in listed if x.startswith("notify.project.")]
    r = post_form(c, "/notifications/prefs", [("csrf_token", csrf(text))] + [("listed", x) for x in listed])
    assert not [i for i, _ in _items(c.get("/notifications").text)
                if i.split(":", 1)[1].split(":")[0] in ("eth-rc4", "eth-main", "multicore-master", "quickstart-main")]
    assert projects


def test_muting_a_project_hides_every_item_only_about_it(fp):
    c = fp["client"]
    text = c.get("/notifications").text
    shown = [i for i, _ in _items(text)]
    assert {"errata_exposure:SL-DEF-0001", "compat_fail:eth-rc4", "never_checked:all"} <= set(shown), shown
    listed = re.findall(r'name="listed" value="([^"]+)"', text)
    mute = "notify.project.nanosoc-ethernet-chiplet"
    post_form(c, "/notifications/prefs", [("csrf_token", csrf(text))] + [("listed", x) for x in listed]
              + [("on", x) for x in listed if x != mute])
    after = c.get("/notifications?show=all").text
    left = [i for i, _ in _items(after)]
    assert "errata_exposure:SL-DEF-0001" not in left           # an erratum exposed only in eth-rc4
    assert "compat_fail:eth-rc4" not in left
    assert "never_checked:all" in left                         # it also names multicore-master (another project)
    assert "host_drift:summary" in left                        # about no project: muted only by its kind
    assert _bell(after) == len([i for i, r in _items(c.get("/notifications").text)])


def test_preferences_are_validated_names():
    for good in ("timeline", "notify.kind.compat", "notify.project.nanosoc-ethernet-chiplet"):
        assert uistate.PREF_NAME.match(good)
    for bad in ("", "notify.kind.", "theme", "notify.project.a b", "timeline2"):
        assert not uistate.PREF_NAME.match(bad), bad


def test_sibling_rank_follows_the_ladder():
    r = home.sibling_rank
    assert [r("soc", True), r("component", True), r("subsystem", False), r("arch", False), r("generator", False),
            r("lib", False), r("toolkit", True), r("flow", False)] == [0, 1, 2, 3, 4, 5, 6, 6]
    assert r(None, True) == 1 and r(None, False) == 2 and r("project", True) == 0
