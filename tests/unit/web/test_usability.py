"""The usability pass of 2026-10-19 (CR-WH-usability): the header's four places, More in three groups, a styled Not
found page for any address, a browser title per workspace tab, the "What needs me?" strip on Home, plain words for
spec codes, and empty states that say what to do next."""
from __future__ import annotations

import copy
import html
import re

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.config import load_config
from nanosoc_rm.web import home, needs
from fpfix import estate, local_site, sign_in
from webfix import EXAMPLES


@pytest.fixture
def fp(tmp_path, monkeypatch, seeded_db):
    s = local_site(tmp_path, monkeypatch, seeded_db)
    c = sign_in(TestClient(s["app"]))
    c.get("/")                                            # reads the registry records into the store
    estate(s["cfg"])
    s["client"] = c
    s["config"] = load_config(s["cfg"])
    return s


def _strip(text: str) -> list[tuple[str, str, str]]:
    """[(key, text, link href)] of the What needs me? strip, in order; [] when the strip is not there."""
    sec = re.search(r'<section class="fp-needs"[^>]*data-needs>(.*?)</section>', text, re.S)
    if not sec:
        return []
    return [(k, re.sub(r"\s+", " ", t).strip(), h) for k, t, h in re.findall(
        r'<li data-need="([^"]+)"><span>(.*?)</span> <a href="([^"]+)"', sec.group(1), re.S)]


def _alert(cfg, n: int, message: str) -> None:
    for i in range(n):
        db.put({"schema": 1, "kind": "alert", "subject": "alert:mirror_failed:gitlab:soclabs/p%d" % i,
                "observed_at": clock.iso(), "sources": [{"kind": "derived", "command": "nanosoc-rm mirror sync"}],
                "fact": {"alert_kind": "mirror_failed", "about": "gitlab:soclabs/p%d" % i, "raised_at": clock.iso(),
                         "cleared_at": None, "cleared_by": None, "message": message}}, config=cfg)


def _publish(cfg, configuration: str) -> None:
    doc = next(d for _, d in contracts.docs_in(EXAMPLES / "observation.publication.example.jsonl")
               if d["fact"].get("kind") == "release")
    doc = copy.deepcopy(doc)
    doc["fact"]["configuration"] = configuration
    db.put(doc, config=cfg)


# ----- header ---------------------------------------------------------------------------------------------------
def test_an_unknown_address_gets_the_styled_not_found_page_with_a_next_step(fp):
    c = fp["client"]
    for url in ("/registry", "/jobs/99999", "/no/such/page"):
        r = c.get(url)
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html"), url
        assert '<nav aria-label="Main" class="topnav">' in r.text and "<h1>Not found</h1>" in r.text, url
        assert 'data-next>Next: open <a href="/">Projects</a>' in r.text, url
    api = c.get("/api/v1/no-such")
    assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")
    anon = TestClient(fp["app"]).get("/registry", follow_redirects=False)
    assert anon.status_code == 303 and anon.headers["location"].startswith("/auth/login")


def test_each_workspace_tab_has_its_own_browser_title(fp):
    c = fp["client"]
    titles = {}
    for tab in ("", "/tree", "/history", "/silicon"):
        r = c.get("/configurations/eth-rc4" + tab)
        assert r.status_code == 200, tab
        titles[tab] = re.search(r"<title>(.*?)</title>", r.text).group(1)
        assert "<h1>eth-rc4</h1>" in r.text, tab                       # the heading stays the configuration
    assert titles[""] == "eth-rc4 · nanoSoC Release Manager"
    assert titles["/tree"] == "Submodule tree · eth-rc4 · nanoSoC Release Manager"
    assert len(set(titles.values())) == 4, titles


# ----- What needs me? -------------------------------------------------------------------------------------------
def test_the_strip_names_a_silicon_configuration_with_no_published_release(fp):
    lines = _strip(fp["client"].get("/").text)
    assert ("unreleased", "eth-rc4 is taped out with no published release.", "/publish/") in lines, lines
    _publish(fp["cfg"], "eth-rc4")
    assert "unreleased" not in [k for k, _, _ in _strip(fp["client"].get("/").text)]


def test_the_strip_names_unreadable_private_repos_and_links_their_alert_group(fp):
    _alert(fp["cfg"], 2, "fatal: could not read Username for 'https://git.example': terminal prompts disabled")
    lines = dict((k, (t, h)) for k, t, h in _strip(fp["client"].get("/").text))
    assert lines["private"] == ("2 private repos cannot be read by the mirrors.", "/alerts#group-private-anon")
    assert 'id="group-private-anon"' in fp["client"].get("/alerts").text


def test_the_strip_counts_repos_that_need_a_group(fp):
    for _, d in contracts.docs_in(EXAMPLES / "observation.catalogue.example.jsonl"):
        db.put(d, config=fp["cfg"])
    chars_ask = sum(1 for ch in home._characterisations(fp["config"], [])[0].values() if ch.question)
    lines = dict((k, (t, h)) for k, t, h in _strip(fp["client"].get("/").text))
    n = 1 + chars_ask
    assert lines["catalogue"] == ("%d %s a group in the catalogue." % (n, "repo needs" if n == 1 else "repos need"),
                                  "/catalogue/")


def test_the_strip_shows_held_bumps_and_never_more_than_three_lines(fp, monkeypatch):
    from nanosoc_rm import bumps
    held = {"fact": {"id": "p1", "state": "blocked", "configuration": "eth-main",
                     "dependency": {"component": "tidelink"}, "consumer": {"component": "nanosoc-ethernet-chiplet"}}}
    decided = {"fact": {"id": "p2", "state": "blocked", "dependency": {"component": "x"}}}
    monkeypatch.setattr(bumps, "stored", lambda **kw: [held, decided])
    monkeypatch.setattr(bumps, "marks", lambda *a, **kw: [{"proposal": "p2"}])
    _alert(fp["cfg"], 1, "fatal: Authentication failed for 'https://git.example/p'")
    for _, d in contracts.docs_in(EXAMPLES / "observation.catalogue.example.jsonl"):
        db.put(d, config=fp["cfg"])
    lines = _strip(fp["client"].get("/").text)
    assert [k for k, _, _ in lines] == ["unreleased", "held", "private"], lines
    assert lines[1][1:] == ("1 bump proposal is held (tidelink in nanosoc-ethernet-chiplet).", "/bumps/")


def test_the_strip_is_hidden_when_nothing_needs_anyone_and_a_broken_source_gives_no_line(fp, monkeypatch):
    def boom(config, chars):
        raise RuntimeError("store down")
    monkeypatch.setattr(needs, "SOURCES", (("boom", boom), ("none", lambda c, ch: None)))
    r = fp["client"].get("/")
    assert r.status_code == 200 and "data-needs" not in r.text and "What needs me?" not in r.text
    assert needs.items(fp["config"]) == []


# ----- words, not codes -----------------------------------------------------------------------------------------
# Pages whose own text (not registry data a person wrote) must carry no spec or change-request code.
WORDS_PAGES = ("/", "/bumps/", "/bumps/ledger", "/matrix/", "/compat", "/configurations/eth-rc4",
               "/configurations/eth-rc4/history", "/configurations/eth-rc4/silicon", "/silicon-protection",
               "/notifications", "/inbox", "/releases/", "/releases/forks", "/versions/", "/alerts", "/catalogue/",
               "/quality/", "/publish/", "/store/", "/capabilities/", "/mirrors", "/hosts", "/branches",
               "/components/tidelink", "/configurations/eth-rc4/tree?path=tidelink")
CODE = re.compile(r"Phase [2-5]|\bCR-[0-9A-Z][\w-]*|\((?:[A-HQ]\d{1,2}|PR\d)(?:,\s*[A-HQ]\d{1,2})*\)|\bT[0-6]\b|"
                  r"\b(?:PROTECTED_BY_TAG|BRANCH_ONLY|ON_DEFAULT|PR_ONLY|NO_GITLINK|LOCAL_URL)\b|David 20\d\d")


def visible(text: str) -> str:
    """The words a reader sees in <main>: tags, scripts and tooltips (title attributes) left out."""
    m = re.search(r"<main.*?</main>", text, re.S)
    t = m.group(0) if m else text
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def test_no_page_shows_a_spec_or_change_request_code_as_its_text(fp):
    found = {}
    for url in WORDS_PAGES:
        r = fp["client"].get(url)
        assert r.status_code == 200, url
        hits = sorted(set(CODE.findall(visible(r.text))))
        if hits:
            found[url] = hits
    assert found == {}, found


def test_the_plain_filter_names_phases_and_drops_parenthesised_ids_into_a_tooltip():
    from nanosoc_rm.web import terms
    assert str(terms.plain("waits for the Phase 2 entry gate")) == \
        'waits for the <span title="Phase 2 entry gate">nightly-matrix sign-off</span>'
    assert str(terms.plain("never proposed (E3, E5)")) == 'never proposed<span class="term-code" data-code="E3 E5"></span>'
    assert str(terms.plain("see (CR-WH-usability)")) == 'see<span class="term-code" data-code="CR-WH-usability"></span>'
    assert str(terms.plain("Phase 4 automation")) == '<span title="Phase 4">bump-bot stage</span> automation'
    assert str(terms.plain("CR-15 stays")) == "CR-15 stays"                     # only a parenthesised id is dropped


# ----- the src links, the two clocks, the axes ------------------------------------------------------------------
def test_every_src_link_names_its_fact_for_a_screen_reader(fp):
    from nanosoc_rm.bumps.web import _fact as bump_fact
    obs = {"subject": "bump_proposal:eth-main:p1", "observed_at": "2026-10-18T12:00:00Z"}
    pages = {url: fp["client"].get(url).text for url in ("/configurations/eth-rc4", "/silicon-protection")}
    pages["bump fact"] = str(bump_fact(None, obs, "p1")) + str(bump_fact(None, obs))
    for url, text in pages.items():
        links = re.findall(r'<a class="src"[^>]*>src</a>', text)
        assert links, url
        for a in links:
            name = re.search(r'aria-label="How (.+?) was found"', a)
            assert name and name.group(1).strip(), (url, a)
    assert 'aria-label="How p1 was found"' in pages["bump fact"]
    assert 'aria-label="How bump_proposal:eth-main:p1 was found"' in pages["bump fact"]


def test_the_header_says_when_the_page_was_updated_and_links_its_oldest_fact(fp):
    text = fp["client"].get("/configurations/eth-rc4").text
    age = re.search(r'<p class="age" data-age-seconds="(\d+)" data-oldest-observed-at="([^"]+)"[^>]*>(.*?)</p>',
                    text, re.S)
    assert age, "one data-age element"
    shown = re.sub(r"\s+", " ", age.group(3))
    assert re.search(r"^Updated .+ ago · oldest fact <a href=\"/facts/[^\"]+\" data-oldest-fact><strong>", shown.strip())
    href = re.search(r'href="([^"]+)" data-oldest-fact', shown).group(1)
    oldest = fp["client"].get(html.unescape(href))
    assert oldest.status_code == 200 and age.group(2) in oldest.text


def test_the_workspace_names_both_axes_and_the_rail_says_its_chips_are_the_last_check(fp):
    text = fp["client"].get("/configurations/eth-main").text
    assert re.search(r'<span class="axis">Check</span> <span class="chip [^"]+">', text)
    fresh = re.search(r'<span class="axis">Freshness</span> <a class="chip c-neutral" href="([^"]+)" data-freshness>'
                      r'([^<]+)</a>', text)
    assert fresh and fresh.group(2) == "Outdated", fresh
    assert "sel=c%3Aeth-main" in fresh.group(1)
    assert re.search(r'data-freshness>Frozen</a>', fp["client"].get("/configurations/eth-rc4").text)
    assert re.search(r'data-freshness>not scanned</a>', fp["client"].get("/configurations/multicore-master").text)
    assert "SoC configurations <span class=\"rail-axis\">· last check</span>" in text


def test_projects_with_no_registered_configuration_say_how_to_register_one(fp):
    text = fp["client"].get("/?v=1&o=grp:none&sel=grp:none").text
    rows = re.findall(r'data-row="([^"]+)"', text)
    assert "grp:none" in rows and "grp:unk" in rows
    inside = rows[rows.index("grp:none") + 1:]
    assert inside and all(r.startswith("p:") for r in inside)             # the unregistered projects, under it
    assert re.search(r"No configuration registered \(%d\)" % len(inside), text)
    assert "eligibility.yaml" in text and "merge request" in text
    assert "not scanned yet" in fp["client"].get("/?sel=grp:unk").text


def test_the_card_view_says_why_its_count_differs_from_the_bell(fp):
    for _, d in contracts.docs_in(EXAMPLES / "observation.catalogue.example.jsonl"):
        db.put(d, config=fp["cfg"])                         # a new-repos item: on the bell, not a card
    text = fp["client"].get("/inbox").text
    bell = int(re.search(r'data-bell="(\d+)"', text).group(1))
    open_ = int(re.search(r"Cards · (\d+) open", text).group(1))
    assert bell == open_ + 1
    assert "data-count-note>The bell counts %d unread" % bell in text


# ----- empty states ---------------------------------------------------------------------------------------------
# A fresh install on the example registry with an empty store: each list page says what to do next.
EMPTY_NEXT = {
    "/": "nanosoc-rm freshness scan --store",
    "/compat": "Choose a configuration above and press Run",
    "/branches": "press Walk now",
    "/hosts": "Press Compare now",
    "/contains": "press <strong>Ask</strong>",
    "/silicon-protection": "then run <code>nanosoc-rm silicon-protection</code>",
    "/mirrors": "Press Sync",
    "/matrix/": "and press Scan now",
    "/releases/": "and press <strong>Draft</strong>",
    "/releases/forks": "press List forks now",
    "/versions/": "Press Refresh",
    "/bumps/": "Press Scan now",
    "/bumps/ledger": 'Press Scan now on <a href="/bumps/">Bump proposals</a>',
    "/publish/": 'make one on the <a href="/releases/">Releases</a> page',
    "/catalogue/": "nanosoc-rm catalogue org-scan --store",
}


@pytest.fixture
def empty_site(tmp_path, monkeypatch):
    from nanosoc_rm import auth
    from nanosoc_rm.web import create_app
    from fpfix import PASSWORD
    from webfix import make_registry
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    h = auth.hash_password(PASSWORD, salt=b"0123456789abcdef")
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[registry]\nrepo = "soclabs/nanosoc-registry"\nref = "main"\n'
                   'path = "%s"\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\nhash = "%s"\n'
                   % (tmp_path / "data", reg, h))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    return sign_in(TestClient(create_app(cfg)))


def test_every_list_page_with_nothing_stored_says_what_to_do_next(empty_site):
    missing = {}
    for url, step in EMPTY_NEXT.items():
        r = empty_site.get(url)
        assert r.status_code == 200, url
        if step not in r.text:
            missing[url] = step
    assert missing == {}, missing
    # the pages this pass gave an empty state mark it, so a later change cannot drop it unnoticed
    for url in ("/branches", "/contains", "/silicon-protection", "/releases/", "/bumps/ledger"):
        assert "data-empty" in empty_site.get(url).text, url


# ----- a tier this host could not run ---------------------------------------------------------------------------
def _result(tier: str, verdict: str, reason: str, at: str) -> dict:
    return {"fact": {"finished_at": at, "tier": {"tier": tier, "verdict": verdict, "reason": reason}}}


def test_the_newest_run_of_each_tier_decides_whether_the_form_warns(fp, monkeypatch):
    from nanosoc_rm.ci import configs as cimod
    from nanosoc_rm.web import views
    newest_first = [_result("t1", "INFRA", "tool not on this host: VERILATOR=/opt/v does not exist; the tier was not "
                            "run", "2026-10-18T10:00:00Z"),
                    _result("t2", "PASS", "", "2026-10-18T09:00:00Z"),
                    _result("t2", "INFRA", "the vcs-seat lock is held", "2026-10-17T09:00:00Z")]
    assert views.tiers_this_host_could_not_run(newest_first) == {
        "t1": "tool not on this host: VERILATOR=/opt/v does not exist"}         # t2 ran since: no warning
    blocked = views.tiers_this_host_could_not_run(newest_first)
    monkeypatch.setattr(views, "tiers_this_host_could_not_run", lambda results: blocked)
    spec = {"command": "make lint", "budget_s": 600}
    monkeypatch.setattr(cimod, "tiers", lambda cid, config=None: {"t1": spec, "t2": dict(spec, command="make sim")})
    text = fp["client"].get("/configurations/eth-rc4/whatif?component=tidelink&target=" + "a" * 40).text
    assert re.search(r'<option value="t1"><span title="T1">static and unit tests</span> \(budget 600 s\), could not '
                     r'run here last time</option>', text), text[text.find("wi-tier"):][:900]
    assert not re.search(r'<option value="t2">.{0,80}could not run', text)
    note = re.search(r'<p class="note" data-tier-blocked="t1">(.*?)</p>', text, re.S).group(1)
    assert "could not run the" in note and "VERILATOR=/opt/v does not exist. Pick it again once that is fixed." in note


def test_the_infra_sentence_is_one_short_sentence():
    from nanosoc_rm.web import verdict as V
    long = "tool not on this host: " + "X=/a/very/long/path " * 20 + "; the tier was not run"
    ok, text = V._infra({"tier": {"tier": "t2", "verdict": "INFRA", "reason": long}})
    assert ok and text.startswith("This host cannot run the smoke simulation (t2): tool not on this host: X=")
    assert text.endswith("…. Nothing to fix in the design.") and len(text) < 260
    assert "the tier was not run" not in text


def test_a_what_if_for_a_component_the_tree_lacks_says_so(fp):
    text = fp["client"].get("/configurations/eth-rc4/whatif?component=nope").text
    assert "eth-rc4 has no component nope in its stored tree: pick one from the list below" in text
    assert "has no component" not in fp["client"].get("/configurations/eth-rc4/whatif?component=tidelink").text


def test_an_error_page_names_the_service_directories_never_their_paths(fp):
    from nanosoc_rm.web.plugins import public_text
    data = str(fp["config"].get("service")["data_dir"])
    reg = str(fp["config"].get("registry")["path"])
    msg = "component x has no mirror at %s/mirrors/x.git; registry %s/catalogue.yaml" % (data, reg)
    assert public_text(fp["config"], msg) == ("component x has no mirror at <data dir>/mirrors/x.git; registry "
                                              "<registry>/catalogue.yaml")
    r = fp["client"].get("/quality/tidelink")
    assert data not in r.text and reg not in r.text
