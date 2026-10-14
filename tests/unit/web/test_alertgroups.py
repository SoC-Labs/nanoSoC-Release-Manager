"""Alerts grouped by cause (Wave C spec 5.1, build-first fix 1; CR-W3): 50 `mirror_failed` alerts with one cause
across two hosts are one group (40/10); clear-group clears all 50 and logs 50 `clear_alert` actions; a URL in a
message never reaches a page."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db
from nanosoc_rm.config import load_config
from nanosoc_rm.web import alertgroups, create_app, routers
from webfix import make_registry, write_config

SECRET_HOST = "gitlab.example.invalid"
MSG = ("mirror fetch of %s:%s failed: fatal: could not read Username for 'https://%s': terminal prompts disabled")
SRC = [{"kind": "git", "command": "git fetch"}]


def _config(service, tmp_path, local=True):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    text = cfg.read_text()
    if local:
        text = re.sub(r'\[ci\]\nproject = "[^"]*"\nallowed_runner_ids = \[\d+\]\n', "", text)
    text += '[hosts.gitlab]\nweb = "https://%s"\napi = "https://%s/api/v4"\n' % (SECRET_HOST, SECRET_HOST)
    cfg.write_text(text)
    return cfg


def raise_fifty(cfg):
    for i in range(50):
        host = "gitlab" if i < 40 else "github"
        path = "soclabs/private-%02d" % i
        db.raise_alert("mirror_failed", "mirror_status:%s:%s" % (host, path),
                       MSG % (host, path, SECRET_HOST if host == "gitlab" else "github.example.invalid"), SRC,
                       config=cfg)


@pytest.fixture
def site(service, registry_spy, tmp_path):
    cfg = _config(service, tmp_path)
    raise_fifty(cfg)
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    return {"cfg": cfg, "config": load_config(cfg), "client": client}


def test_normalise_takes_out_what_differs_between_alerts_of_one_cause():
    a = alertgroups.normalise(MSG % ("gitlab", "soclabs/a", SECRET_HOST))
    b = alertgroups.normalise(MSG % ("github", "SoC-Labs/B-repo", "github.example.invalid"))
    assert a == b and SECRET_HOST not in a and "soclabs" not in a
    assert alertgroups.normalise("moved 1111111 -> 2222222abc") == "moved <sha> -> <sha>"


def test_fifty_mirror_failures_on_two_hosts_are_one_group(site):
    gs = alertgroups.groups(site["config"])
    mirror = [g for g in gs if g["kind"] == "mirror_failed"]
    assert len(mirror) == 1
    g = mirror[0]
    assert (g["key"], g["count"], g["by_host"]) == ("private-anon", 50, {"gitlab": 40, "github": 10})
    assert g["label"] == "private repos cannot be mirrored anonymously" and g["hosts_text"] == "40 gitlab, 10 github"
    assert len(g["sample"]) == 3 and len(g["subjects"]) == 50
    assert gs[0] is g                                                  # the biggest group first
    rewrite = [x for x in gs if x["kind"] == "rewrite"]               # the example rewrite alert: its own group
    assert len(rewrite) == 1 and rewrite[0]["count"] == 1


def test_an_unmatched_message_is_its_own_group_keyed_by_its_text():
    doc = lambda m: {"subject": "alert:x:%s" % m, "observed_at": "2026-10-13T00:00:00Z",
                     "fact": {"alert_kind": "mystery", "message": m, "about": m, "raised_at": "2026-10-13T00:00:00Z",
                              "cleared_at": None}}
    k1, label, _ = alertgroups.cause_of(doc("disk full on repo gitlab:a/b at 1234567"))
    k2, _, _ = alertgroups.cause_of(doc("disk full on repo github:c/d at 89abcde"))
    k3, _, _ = alertgroups.cause_of(doc("something else entirely"))
    assert k1 == k2 != k3 and k1.startswith("x-") and label.startswith("mystery: disk full on repo <repo>")


def test_clear_group_clears_all_fifty_and_logs_each(site):
    c = site["client"]
    page = c.get("/alerts").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    from test_inbox import local_client
    lc = local_client(site)
    assert lc.post("/alerts/clear-group", data={"group": "private-anon"}).status_code == 403    # no CSRF token
    seen = re.search(r'name="group" value="private-anon"><input type="hidden" name="seen" value="([0-9a-f]+)"',
                     page).group(1)
    r = c.post("/alerts/clear-group", data={"csrf_token": token, "group": "private-anon", "seen": seen},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/alerts"
    open_ = db.alerts(open_only=True, config=site["cfg"])
    assert not [a for a in open_ if a["fact"]["alert_kind"] == "mirror_failed"]
    acts = [a for a in db.actions(config=site["cfg"]) if a["action"] == "clear_alert"]
    assert len(acts) == 50 and {a["login"] for a in acts} == {"alice"}
    assert c.post("/alerts/clear-group", data={"csrf_token": token, "group": "private-anon", "seen": seen}
                  ).status_code == 404


def test_no_url_from_a_message_reaches_a_page(site):
    c = site["client"]
    for path in ("/", "/alerts", "/mirrors"):
        text = c.get(path).text
        assert SECRET_HOST not in text and "github.example.invalid" not in text, path
    text = c.get("/alerts").text
    assert "could not read Username for &#39;&lt;gitlab&gt;&#39;" in text              # redacted to the host name
    assert '<tr id="group-private-anon"' in text and "Clear all 50" in text


def test_the_board_and_the_rail_show_one_line_per_cause(site):
    text = site["client"].get("/").text
    assert text.count('data-alert-group="private-anon"') == 1
    assert "50 private repos cannot be mirrored anonymously" in text
    rail = re.search(r'id="rail-h-alerts">Alerts</p>(.*?)</div>', text, re.S).group(1)
    assert rail.count('class="rail-item"') == 2 and "50 private repos cannot be mirrored anonymously" in rail


def test_clear_group_is_not_a_route_in_a_github_ci_deployment(service, registry_spy, tmp_path):
    cfg = _config(service, tmp_path, local=False)
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    assert ("POST", "/alerts/clear-group") not in routers.writes(app)
    assert 'action="/alerts/clear-group"' not in TestClient(app).get("/alerts").text


def test_clear_group_refuses_when_the_group_changed_since_the_page(site):
    """I-R3: a clear covers exactly the alerts the person saw; one raised since then answers 409, clears nothing."""
    c = site["client"]
    page = c.get("/alerts").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    seen = re.search(r'name="group" value="rewrite"><input type="hidden" name="seen" value="([0-9a-f]+)"',
                     page).group(1)
    assert "Clear all 1" in page
    old = next(a for a in db.alerts(open_only=True, config=site["cfg"]) if a["fact"]["alert_kind"] == "rewrite")
    db.raise_alert("rewrite", "branch_state:gitlab:soclabs/other:main", old["fact"]["message"], SRC, config=site["cfg"])
    r = c.post("/alerts/clear-group", data={"csrf_token": token, "group": "rewrite", "seen": seen})
    assert r.status_code == 409
    assert not [a for a in db.actions(config=site["cfg"]) if a["action"] == "clear_alert"]
    assert len([a for a in db.alerts(open_only=True, config=site["cfg"]) if a["fact"]["alert_kind"] == "rewrite"]) == 2
    assert c.post("/alerts/clear-group", data={"csrf_token": token, "group": "rewrite"}).status_code == 400
