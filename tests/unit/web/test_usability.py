"""The usability pass of 2026-10-19 (CR-WH-usability): the header's four places, More in three groups, a styled Not
found page for any address, a browser title per workspace tab, the "What needs me?" strip on Home, plain words for
spec codes, and empty states that say what to do next."""
from __future__ import annotations

import copy
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
