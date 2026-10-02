"""trigger(): RM_REQUEST, the stored run, the one write, and what a failed trigger leaves (interfaces.md section 10)."""
from __future__ import annotations

import json
import re
import urllib.parse
from urllib.parse import quote

import pytest

from ci_helpers import git, jsonl, mirror_commit
from nanosoc_rm import contracts, db
from nanosoc_rm.ci import configs
from nanosoc_rm.ci.client import FakeCI, GitLabCI
from nanosoc_rm.ci.trigger import RunPending, TriggerError, trigger

import ci_helpers

# fixtures (ci_helpers docstring)
ce, sw, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.sw, ci_helpers.ci_config, ci_helpers.scanner_clean

ETH = "SoC-Labs/NanoSoC-Ethernet-Chiplet"


def eth_mirror(cfg, ce, *, main=True, tag=True):
    """The eth component's mirror with GitHub main and tag v1.0.0 (section 3 layout). Returns {ref: sha}."""
    key = "refs/r/github/%s" % quote(ETH, safe="")
    out = {}
    if main:
        out["main"] = mirror_commit(cfg.mirrors_dir / "nanosoc-ethernet-chiplet.git", [key + "/heads/main"], "main")
    if tag:
        out["v1.0.0"] = mirror_commit(cfg.mirrors_dir / "nanosoc-ethernet-chiplet.git", [key + "/tags/v1.0.0"], "v1")
    return out


def test_frozen_configuration_is_pending_with_the_request_stored_and_sent(ce, ci_config):
    fake = FakeCI()
    doc = trigger("eth-rc4", by="schedule", kind="schedule", client=fake)
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"], f["commit"]) == ("pending", None, ce.RC4)
    assert f["run"]["pipeline_id"] == 1000 and doc["subject"] == "t0_result:eth-rc4:" + f["run"]["nonce"]
    stored = db.get_run(f["run"]["nonce"])
    (ref, variables), = fake.triggered
    assert ref == "main" and variables["RM_REQUEST"].encode() == stored["request"]     # verify check 2's premise
    req = json.loads(stored["request"])
    assert contracts.validate_rm_request(req) == []
    assert list(req) == ["schema", "nonce", "configuration", "repo", "commit", "upgrade_from", "canonical_urls"]
    assert req["repo"] == {"host": "github", "path": ETH} and req["upgrade_from"] is None
    assert re.fullmatch(r"[0-9a-f]{32}", req["nonce"]) and req["nonce"] == f["run"]["nonce"]
    assert stored["state"] == "pending" and stored["pipeline_id"] == 1000
    assert db.actions() == []                                  # a scheduled run logs no action
    assert [d["subject"] for d in db.latest("t0_result")] == [doc["subject"]]


def test_manual_run_logs_one_action_at_the_trigger_time(ce, ci_config, sw):
    doc = trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())
    (a,) = db.actions()
    assert (a["login"], a["action"], a["target"], a["at"]) == ("alice", "run_t0", "eth-rc4", doc["fact"]["run"]["triggered_at"])
    runs = [dict(r, request=None) for r in db.runs()]
    # AT-A2's own checker: the manual run has exactly its one actions row
    probs = sw.check_a2([], [], {}, ci_project="x", runs=runs, actions=db.actions())
    assert not [p for p in probs if "actions rows" in p], probs
    assert [p for p in sw.check_a2([], [], {}, ci_project="x", runs=runs, actions=[]) if "actions rows" in p]


def test_a_manual_run_while_one_is_pending_is_refused(ce, ci_config):
    trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())
    with pytest.raises(RunPending):
        trigger("eth-rc4", by="bob", kind="manual", client=FakeCI())
    assert len(db.runs("eth-rc4")) == 1
    trigger("eth-rc4", by="schedule", kind="schedule", client=FakeCI())     # the night is never skipped
    assert len(db.runs("eth-rc4")) == 2


@pytest.mark.parametrize("status, verdict", [(500, "INFRA"), (401, "UNVERIFIED"), ("timeout", "INFRA")])
def test_failed_trigger_is_recorded_no_bundle_and_not_pending(ce, ci_config, status, verdict):
    fake = FakeCI(fail_trigger_for={"eth-rc4": status})
    doc = trigger("eth-rc4", by="alice", kind="manual", client=fake)
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"], f["run"]["pipeline_id"]) == ("no-bundle", verdict, None)
    assert "trigger" in f["reason"] and (str(status) if status != "timeout" else "timeout") in f["reason"]
    assert db.get_run(f["run"]["nonce"])["state"] == "no-bundle"
    assert [a["result"] for a in db.actions()] == ["no-bundle"]
    trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())            # Run is enabled again


def test_branch_configuration_resolves_ref_and_upgrade_from_in_the_mirror(ce, ci_config):
    shas = eth_mirror(ci_config, ce)
    fake = FakeCI()
    f = trigger("eth-main", by="schedule", kind="schedule", client=fake)["fact"]
    req = json.loads(fake.triggered[0][1]["RM_REQUEST"])
    assert f["commit"] == req["commit"] == shas["main"]
    assert req["upgrade_from"] == shas["v1.0.0"]               # eth-main: upgrade_from {tag: v1.0.0}
    assert any(s["kind"] == "git" and "rev-parse" in s["command"] for s in
               db.latest("t0_result")[0]["sources"])


def test_unresolvable_ref_stores_nothing(ce, ci_config):
    eth_mirror(ci_config, ce, main=False)
    with pytest.raises(TriggerError, match="branch main"):
        trigger("eth-main", by="schedule", kind="schedule", client=FakeCI())
    assert db.runs() == [] and db.latest("t0_result") == []


def test_canonical_urls_come_from_the_stored_walk(ce, ci_config):
    for doc in jsonl("pin-state.example.jsonl"):
        db.put(doc)
    fake = FakeCI()
    trigger("eth-rc4", by="schedule", kind="schedule", client=fake)
    req = json.loads(fake.triggered[0][1]["RM_REQUEST"])
    # the arch_tech pin names the GitLab copy; its canonical repo is GitHub (pin-state example)
    assert req["canonical_urls"] == {"https://<gitlab-host>/soclabs/nanosoc-m0/nanosoc_arch_tech.git":
                                     "https://github.com/SoC-Labs/nanoSoC-Arch-Tech.git"}
    pinned_is_canonical = [d["fact"]["url"] for d in jsonl("pin-state.example.jsonl")
                           if any(h["role"] == "canonical" and h["pinned"] for h in d["fact"]["hosts"].values())]
    assert not set(pinned_is_canonical) & set(req["canonical_urls"])


def test_registry_job_asks_for_the_registry_head(ce, ci_config):
    reg = ci_config.require("registry", "path")
    git("init", "-q", "-b", "main", reg)
    git("add", "-A", cwd=reg)
    git("commit", "-q", "-m", "registry", cwd=reg)
    head = git("rev-parse", "HEAD", cwd=reg)
    fake = FakeCI()
    f = trigger(configs.REGISTRY_JOB, by="schedule", kind="schedule", client=fake)["fact"]
    req = json.loads(fake.triggered[0][1]["RM_REQUEST"])
    assert f["configuration"] == "@registry" and req["commit"] == head == f["commit"]
    assert req["repo"] == {"host": "gitlab", "path": "soclabs/nanosoc-registry"}


def test_unknown_configuration_and_bad_arguments(ce, ci_config):
    with pytest.raises(configs.UnknownConfiguration):
        trigger("no-such-config", by="alice", kind="manual", client=FakeCI())
    for by, kind in (("alice", "schedule"), ("schedule", "manual"), ("", "manual"), ("x", "nightly")):
        with pytest.raises(ValueError):
            trigger("eth-rc4", by=by, kind=kind, client=FakeCI())
    assert db.runs() == []


# ---- the real client against a local fake GitLab (AT-A4's FakeHost) ------------------------------------------------
def gitlab_config(tmp_path, monkeypatch, ce, host, *, trigger_project=None):
    from nanosoc_rm.config import load_config
    path = ce.write_config(tmp_path, monkeypatch)
    ce.example_registry(tmp_path / "registry")
    tok = tmp_path / "tokens"
    tok.mkdir()
    (tok / "read").write_text("fake-read-token\n")
    (tok / "trigger").write_text("fake-trigger-token\n")
    text = path.read_text().replace('api = "https://gitlab.invalid/api/v4"', 'api = "%s/api/v4"' % host.url)
    text += ('[credentials.gitlab_read]\ntoken_file = "%s"\nscope = ["read_api", "read_repository"]\n'
             '[credentials.ci_trigger]\ntoken_file = "%s"\nscope = ["trigger"]\nproject = "%s"\n'
             % (tok / "read", tok / "trigger", trigger_project or ce.CI_PROJECT))
    path.write_text(text)
    return load_config(path)


def test_gitlab_client_posts_the_trigger_form_without_the_token_in_url_or_record(ce, sw, tmp_path, monkeypatch):
    host = sw.FakeHost()
    try:
        sw.gitlab_ci(host)
        cfg = gitlab_config(tmp_path, monkeypatch, ce, host)
        doc = trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(cfg))
        (post,) = [e for e in host.log if e["method"] == "POST"]
        assert post["path"] == "/api/v4/projects/%s/trigger/pipeline" % quote(ce.CI_PROJECT, safe="")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(post["body"].decode()).items()}
        assert form["ref"] == "main" and form["token"] == "fake-trigger-token"
        assert form["variables[RM_REQUEST]"].encode() == db.get_run(doc["fact"]["run"]["nonce"])["request"]
        assert "fake-trigger-token" not in json.dumps(doc) and "fake-trigger-token" not in post["path"]
        assert doc["fact"]["acceptance"] == "pending" and doc["fact"]["run"]["pipeline_id"] == 1000
        assert doc["sources"][0] == {"kind": "ci_api", "command": "POST projects/%s/trigger/pipeline"
                                     % quote(ce.CI_PROJECT, safe="")}
    finally:
        host.close()


def test_gitlab_client_401_is_recorded(ce, sw, tmp_path, monkeypatch):
    host = sw.FakeHost()
    try:
        sw.gitlab_ci(host, trigger_status=401)
        cfg = gitlab_config(tmp_path, monkeypatch, ce, host)
        f = trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(cfg))["fact"]
        assert (f["acceptance"], f["verdict"]) == ("no-bundle", "UNVERIFIED") and "HTTP 401" in f["reason"]
        assert sw.check_a4({"fact": f}, None, [], [], configuration="eth-rc4", commit=ce.RC4, login="alice")
    finally:
        host.close()


def test_trigger_guard_refuses_a_token_recorded_for_another_project(ce, sw, tmp_path, monkeypatch):
    host = sw.FakeHost()
    try:
        sw.gitlab_ci(host)
        cfg = gitlab_config(tmp_path, monkeypatch, ce, host, trigger_project="soclabs/nanosoc-registry")
        f = trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(cfg))["fact"]
        assert f["acceptance"] == "no-bundle" and "not sent" in f["reason"] and "nanosoc-registry" in f["reason"]
        assert host.writes() == []                             # nothing reached the host
    finally:
        host.close()


def test_without_trigger_credentials_the_run_is_recorded_not_lost(ce, ci_config):
    """AT-D5 Then 3: a Run POST in a config with no trigger credential still records a run (no-bundle)."""
    f = trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(ci_config))["fact"]
    assert f["acceptance"] == "no-bundle" and f["run"]["pipeline_id"] is None and "not sent" in f["reason"]


# ---- review CI-R1: no trigger path leaves a run pending for good --------------------------------------------------
def test_a_config_error_before_the_call_stores_nothing(ce, tmp_path, monkeypatch):
    """No [ci] project: the trigger cannot name its call. ConfigError (exit 3), and no pending run is left behind
    to disable Run for that configuration."""
    from nanosoc_rm.config import ConfigError, load_config
    path = ce.write_config(tmp_path, monkeypatch)
    ce.example_registry(tmp_path / "registry")
    good = path.read_text()
    path.write_text(good.replace('project = "%s"\n' % ce.CI_PROJECT, ""))
    cfg = load_config(path)
    with pytest.raises(ConfigError, match="project"):
        trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(cfg), config=cfg)
    assert db.runs() == [] and db.actions() == [] and db.latest("t0_result") == []
    path.write_text(good)
    assert trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())["fact"]["acceptance"] == "pending"


def test_an_unexpected_error_in_the_call_still_makes_the_run_final(ce, ci_config):
    class Broken(FakeCI):
        def trigger(self, ref, variables):
            raise RuntimeError("a bug in the client")

    with pytest.raises(RuntimeError, match="a bug"):
        trigger("eth-rc4", by="alice", kind="manual", client=Broken())
    (run,) = db.runs()
    assert (run["state"], run["pipeline_id"]) == ("no-bundle", None)
    (doc,) = db.latest("t0_result")
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"]) == ("no-bundle", "UNVERIFIED") and "RuntimeError" in f["reason"]
    assert [a["result"] for a in db.actions()] == ["no-bundle"]
    trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())            # Run is enabled again


# ---- review CI-T2: request bytes, the pipeline-id answer, the action's time ---------------------------------------
def test_the_request_is_stored_and_sent_as_compact_json(ce, ci_config):
    fake = FakeCI()
    doc = trigger("eth-rc4", by="schedule", kind="schedule", client=fake)
    stored = db.get_run(doc["fact"]["run"]["nonce"])["request"]
    assert stored == json.dumps(json.loads(stored), separators=(",", ":")).encode()       # section 9 Input
    assert b" " not in stored and fake.triggered[0][1]["RM_REQUEST"].encode() == stored


@pytest.mark.parametrize("answer", [{}, {"id": 0}, {"id": True}, {"id": "1000"}, []])
def test_an_answer_without_a_pipeline_id_is_no_bundle(ce, ci_config, answer):
    class Odd(FakeCI):
        def trigger(self, ref, variables):
            return answer

    f = trigger("eth-rc4", by="alice", kind="manual", client=Odd())["fact"]
    assert (f["acceptance"], f["run"]["pipeline_id"]) == ("no-bundle", None) and "no pipeline id" in f["reason"]


def test_the_action_row_carries_the_trigger_time_not_a_later_one(ce, ci_config, monkeypatch):
    import datetime as dt
    from nanosoc_rm import clock
    ticks = iter(dt.datetime(2026, 10, 1, 9, 0, s, tzinfo=dt.timezone.utc) for s in range(60))
    monkeypatch.setattr(clock, "now", lambda: next(ticks))
    doc = trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())
    (a,) = db.actions()
    assert a["at"] == doc["fact"]["run"]["triggered_at"] == db.get_run(doc["fact"]["run"]["nonce"])["triggered_at"]


# ---- review CI-T1: no credential in a URL, a query or a header other than the read token's ------------------------
def _assert_tokens_confined(host):
    for e in host.log:
        where = json.dumps({"path": e["path"], "query": e["query"]})
        headers = {k.lower(): v for k, v in e["headers"].items()}
        body = e["body"].decode(errors="replace") if isinstance(e["body"], bytes) else str(e["body"] or "")
        for tok in ("fake-trigger-token", "fake-read-token"):
            assert tok not in where, (tok, e["method"], where)
        assert "fake-trigger-token" not in json.dumps(headers), e["method"]
        if e["method"] == "POST":
            assert "fake-read-token" not in json.dumps(headers) and "fake-read-token" not in body
        else:
            assert headers.get("authorization") == "Bearer fake-read-token"
            assert "fake-read-token" not in json.dumps({k: v for k, v in headers.items() if k != "authorization"})
            assert "fake-trigger-token" not in body


def test_no_token_travels_in_a_url_or_a_query(ce, sw, tmp_path, monkeypatch):
    host = sw.FakeHost()
    try:
        sw.gitlab_ci(host)
        cfg = gitlab_config(tmp_path, monkeypatch, ce, host)
        client = GitLabCI(cfg)
        doc = trigger("eth-rc4", by="alice", kind="manual", client=client)
        pid = doc["fact"]["run"]["pipeline_id"]
        client.pipeline(pid)
        (job,) = client.jobs(pid)
        client.variables(pid)
        client.artifacts(job["id"])
        assert [e["method"] for e in host.log] == ["POST", "GET", "GET", "GET", "GET"]
        _assert_tokens_confined(host)
    finally:
        host.close()


def test_a_redirected_trigger_is_not_followed(ce, sw, tmp_path, monkeypatch):
    """One write (A2): a 307 on the trigger is an answer, never a second POST carrying the token elsewhere."""
    host = sw.FakeHost()
    try:
        host.route("POST", r"/api/v4/projects/([^/]+)/trigger/pipeline",
                   lambda m, q, hd, body: (307, {"Location": "/elsewhere"}, {"message": "moved"}))
        host.route("POST", r"/elsewhere", lambda m, q, hd, body: (201, {}, {"id": 1000}))
        cfg = gitlab_config(tmp_path, monkeypatch, ce, host)
        f = trigger("eth-rc4", by="alice", kind="manual", client=GitLabCI(cfg))["fact"]
        assert [e["path"] for e in host.log if e["method"] == "POST"] == [
            "/api/v4/projects/%s/trigger/pipeline" % quote(ce.CI_PROJECT, safe="")]
        assert f["acceptance"] == "no-bundle" and f["run"]["pipeline_id"] is None
    finally:
        host.close()
