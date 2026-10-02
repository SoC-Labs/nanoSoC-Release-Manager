"""`nanosoc-rm runner` (D10) and `nanosoc-rm credentials check` (H3)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.ci.client import FakeCI
from nanosoc_rm.ci.runners import runner_settings
from nanosoc_rm.config import load_config
from nanosoc_rm.credentials import check

import ci_helpers

# fixtures (ci_helpers docstring)
ce, sw, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.sw, ci_helpers.ci_config, ci_helpers.scanner_clean

GOOD_RUNNER = {"id": 9001, "description": "rm-hardened", "active": True, "paused": False, "is_shared": False,
               "runner_type": "project_type", "run_untagged": False, "locked": True, "access_level": "ref_protected",
               "tag_list": ["rm-hardened"], "online": True}


@pytest.fixture
def runner_config(ce, tmp_path, monkeypatch):
    path = ce.write_config(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace(
        "fetch_timeout_minutes = 120\n", "fetch_timeout_minutes = 120\n"
        'runner_expected = {run_untagged = false, locked = true, access_level = "ref_protected", is_shared = false}\n'))
    return load_config(path)


def test_runner_settings_ok(ce, runner_config):
    (doc,) = runner_settings(FakeCI(runners={9001: GOOD_RUNNER}))
    f = doc["fact"]
    assert contracts.validate_observation(doc) == [] and db.check_observation(doc) == []
    assert (f["runner_id"], f["readable"], f["verdict"], f["drift"]) == (9001, True, "ok", [])
    for key, want in ce.EXPECTED_RUNNER_SETTINGS.items():
        assert f["settings"][key] == want
    assert f["expected"]["tag"] == "rm-hardened" and doc["sources"] == [{"kind": "ci_api", "command": "GET runners/9001"}]
    assert [d["subject"] for d in db.latest("runner_settings")] == ["runner_settings:9001"]
    assert db.alerts() == []


@pytest.mark.parametrize("change, words", [
    ({"run_untagged": True}, "run_untagged: true, expected false"),
    ({"access_level": "not_protected"}, "access_level: not_protected"),
    ({"locked": False}, "locked: false"),
    ({"is_shared": True}, "is_shared: true"),
    ({"tag_list": ["shared"]}, "lacks rm-hardened"),
])
def test_each_drift_is_named_and_alerted_once(ce, runner_config, change, words):
    fake = FakeCI(runners={9001: dict(GOOD_RUNNER, **change)})
    (doc,) = runner_settings(fake)
    assert doc["fact"]["verdict"] == "drift" and any(words in d for d in doc["fact"]["drift"]), doc["fact"]["drift"]
    runner_settings(fake)
    (alert,) = db.alerts()
    assert alert["fact"]["alert_kind"] == "runner_drift" and alert["fact"]["about"] == "runner_settings:9001"


def test_unreadable_runner_is_unverified(ce, runner_config):
    (doc,) = runner_settings(FakeCI(runners={9001: 403}))
    f = doc["fact"]
    assert (f["readable"], f["settings"], f["verdict"]) == (False, None, "unverified")
    assert contracts.validate_observation(doc) == []


@pytest.mark.parametrize("missing", ["locked", "run_untagged", "access_level", "is_shared"])
def test_a_partial_runner_record_is_unverified_not_drift(ce, runner_config, missing):
    """The token sees only part of the record: the setting is unknown, not wrong (review CI-T2)."""
    raw = {k: v for k, v in GOOD_RUNNER.items() if k != missing}
    (doc,) = runner_settings(FakeCI(runners={9001: raw}))
    assert (doc["fact"]["readable"], doc["fact"]["verdict"]) == (False, "unverified")
    assert db.alerts() == []


def test_runner_cli_exit_codes(ce, runner_config, monkeypatch):
    from nanosoc_rm.ci import runners
    for runner, code in ((GOOD_RUNNER, 0), (dict(GOOD_RUNNER, run_untagged=True), 1), (404, 2)):
        monkeypatch.setattr(runners, "make_client", lambda config, r=runner: FakeCI(runners={9001: r}))
        res = CliRunner().invoke(cli.main, ["runner", "--json"])
        assert res.exit_code == code, res.output
        (line,) = res.output.splitlines()
        assert json.loads(line)["kind"] == "runner_settings"


# ---- credentials (H3) -------------------------------------------------------------------------------------------
GITLAB_SELF = {"id": 1, "name": "poller", "scopes": ["read_api", "read_repository"], "expires_at": "2027-06-30",
               "active": True, "revoked": False}


def fake_hosts(gitlab=None, github_headers=None, github_status=200, gitlab_status=200):
    calls = []

    def get(url, token):
        calls.append((url, token))
        if url.endswith("/personal_access_tokens/self"):
            return gitlab_status, {}, dict(GITLAB_SELF, **(gitlab or {}))
        if url.endswith("/user"):
            return github_status, dict(github_headers or {}), {"login": "svc"}
        raise AssertionError(url)
    get.calls = calls
    return get


@pytest.fixture
def cred_config(sw, tmp_path, monkeypatch):
    """AT-H3's offline config (test_scope_web.write_config): the four v1 credentials, expiring 2027-06-30."""
    class Host:
        url = "http://127.0.0.1:9"
    path = sw.write_config(tmp_path / "config.toml", data=tmp_path / "data", registry=tmp_path / "registry",
                           github=Host, gitlab=Host)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(path))
    return path


def rows(docs):
    return [d["fact"] for d in docs]


TODAY = dt.date(2026, 10, 1)


def test_clean_credentials_pass_the_h3_checker(sw, cred_config):
    get = fake_hosts()
    docs = check(today=TODAY, http_get=get)
    assert [d["fact"]["name"] for d in docs] == ["github_read", "gitlab_read", "ci_trigger", "github_oauth"]
    assert all(contracts.validate_observation(d) == [] for d in docs)
    v = {f["name"]: f for f in rows(docs)}
    assert v["gitlab_read"]["verdict"] == "ok" and v["gitlab_read"]["live_scope"] == ["read_api", "read_repository"]
    assert v["github_read"]["verdict"] == "unverified" and "fine-grained" in v["github_read"]["reason"]
    assert v["ci_trigger"]["verdict"] == "unverified" and v["ci_trigger"]["project"] == sw.CI_PROJECT
    assert v["github_oauth"]["verdict"] == "ok" and v["github_oauth"]["expires"] is None
    cfg = load_config()
    assert sw.check_h3(rows(docs), today=TODAY, ci_project=sw.CI_PROJECT, gitlab_self=GITLAB_SELF, github_headers={},
                       oauth_scope="read:org", config=cfg) == []
    assert {t for _u, t in get.calls} == {"fake-gitlab_read", "fake-github_read"}
    text = json.dumps(docs)
    assert not [t for t in ("fake-gitlab_read", "fake-github_read", "fake-ci_trigger", "fake-github_oauth") if t in text]
    assert [d["subject"] for d in db.latest("credential_status")] == [d["subject"] for d in docs]


def test_h3_control_gitlab_reports_api(sw, cred_config, monkeypatch):
    """AT-H3's system-level control, without the web app: GitLab says `api`, the record says read_api."""
    res = CliRunner().invoke(cli.main, ["credentials", "check", "--json"])
    assert res.exit_code == 2                                 # no host reachable: unverified, never ok
    import nanosoc_rm.credentials as cred
    monkeypatch.setattr(cred, "_http_get", fake_hosts(gitlab={"scopes": ["api"]}))
    res = CliRunner().invoke(cli.main, ["credentials", "check", "--json"])
    assert res.exit_code == 1, res.output
    facts = [json.loads(line)["fact"] for line in res.output.splitlines()]
    g = {f["name"]: f for f in facts}["gitlab_read"]
    assert g["verdict"] == "finding" and g["live_scope"] == ["api"]
    probs = sw.check_h3(facts, today=TODAY, ci_project=sw.CI_PROJECT, gitlab_self={"scopes": ["api"]})
    assert any(p.startswith("gitlab_read: live scope ['api'] differs") for p in probs), probs


@pytest.mark.parametrize("kw, name, words", [
    (dict(github_headers={"X-OAuth-Scopes": "repo, read:org"}), "github_read", "classic token"),
    (dict(gitlab={"revoked": True}), "gitlab_read", "revoked"),
    (dict(gitlab={"scopes": ["read_api"]}), "gitlab_read", "live scope ['read_api'] differs from the recorded"),
    (dict(gitlab={"expires_at": "2027-01-01"}), "gitlab_read", "recorded expiry 2027-06-30, GitLab says 2027-01-01"),
    (dict(gitlab_status=401), "gitlab_read", "HTTP 401"),
    (dict(github_headers={"github-authentication-token-expiration": "2026-12-01 00:00:00 UTC"}), "github_read",
     "GitHub says 2026-12-01"),
])
def test_host_answers_that_are_findings(sw, cred_config, kw, name, words):
    hd = kw.pop("github_headers", None)
    get = fake_hosts(github_headers={k.lower(): v for k, v in (hd or {}).items()}, **kw)
    f = {r["name"]: r for r in rows(check(today=TODAY, http_get=get))}[name]
    assert f["verdict"] == "finding" and words in f["reason"], f


def edit_config(sw, path, fn):
    import tomllib
    doc = tomllib.loads(path.read_text())
    fn(doc)
    path.write_text(sw.to_toml(doc))


def test_record_problems_are_findings(sw, cred_config):
    def broken(doc):
        doc["credentials"]["gitlab_read"]["scope"] = ["api"]
        doc["credentials"]["ci_trigger"]["project"] = "soclabs/nanosoc-registry"
        doc["credentials"]["bump_writer"] = {"token_file": doc["credentials"]["ci_trigger"]["token_file"],
                                             "scope": ["write_repository"], "expires": "2027-01-01"}
        del doc["credentials"]["github_oauth"]
        doc["credentials"]["github_read"]["expires"] = ""
    edit_config(sw, cred_config, broken)
    v = {r["name"]: r for r in rows(check(today=TODAY, http_get=fake_hosts(gitlab={"scopes": ["api"]})))}
    assert "beyond its role" in v["gitlab_read"]["reason"]
    assert "nanosoc-registry" in v["ci_trigger"]["reason"] and v["ci_trigger"]["verdict"] == "finding"
    assert v["bump_writer"]["verdict"] == "finding" and "Phase 4" in v["bump_writer"]["reason"]
    assert v["github_oauth"]["verdict"] == "finding" and "not configured" in v["github_oauth"]["reason"]
    assert v["github_read"]["verdict"] == "finding" and "expiry not recorded" in v["github_read"]["reason"]


def test_scope_beyond_the_role_is_named(sw, cred_config):
    """github_oauth must be exactly read:org; GitLab naming a write scope is called out as beyond read access."""
    def wide(doc):
        doc["credentials"]["github_oauth"]["scope"] = ["read:org", "repo"]
    edit_config(sw, cred_config, wide)
    get = fake_hosts(gitlab={"scopes": ["read_api", "read_repository", "write_repository"]})
    v = {r["name"]: r for r in rows(check(today=TODAY, http_get=get))}
    assert v["github_oauth"]["verdict"] == "finding" and "beyond its role" in v["github_oauth"]["reason"]
    assert v["gitlab_read"]["verdict"] == "finding" and "beyond read access" in v["gitlab_read"]["reason"]


def test_expiry_within_14_days_raises_one_alert_and_expired_is_a_finding(sw, cred_config):
    def soon(doc):
        doc["credentials"]["ci_trigger"]["expires"] = "2026-10-11"
        doc["credentials"]["gitlab_read"]["expires"] = "2026-09-30"
    edit_config(sw, cred_config, soon)
    get = fake_hosts(gitlab={"expires_at": "2026-09-30"})
    docs = check(today=TODAY, http_get=get)
    v = {r["name"]: r for r in rows(docs)}
    assert v["gitlab_read"]["verdict"] == "finding" and "expired on 2026-09-30" in v["gitlab_read"]["reason"]
    alerts = {a["fact"]["about"]: a for a in db.alerts()}
    assert set(alerts) == {"credential_status:ci_trigger", "credential_status:gitlab_read"}
    assert "10 days left" in alerts["credential_status:ci_trigger"]["fact"]["message"]
    check(today=TODAY, http_get=get)
    assert len(db.alerts()) == 2                              # raised once while open
    table = [{"kind": a["fact"]["alert_kind"], "subject": a["subject"], "cleared_at": None} for a in db.alerts()]
    assert not [p for p in sw.check_h3(rows(docs), today=TODAY, ci_project=sw.CI_PROJECT, alerts=table)
                if "no expiry alert" in p]
