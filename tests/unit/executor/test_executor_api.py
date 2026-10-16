"""The executor's server half (interfaces.md section 27.2, 26.3, 26.5): the task contract, the queue's leases,
the POST-only bearer-token API behind the web app's Guard, the [executor] config, and the client's path guard."""
from __future__ import annotations

import json

import pytest

from execfix import Rig
from nanosoc_rm.config import ConfigError, load_config
from nanosoc_rm.executor import queue as Q
from nanosoc_rm.executor import task as T
from nanosoc_rm.executor import tokens
from nanosoc_rm.executor.client import check_path


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _result(claim, verdict="PASS", rows=None, executor="eda-1"):
    rows = rows if rows is not None else [{"utc": "2026-10-16T10:00:00Z", "status": "PASS", "gate": "tier.t1",
                                           "detail": "exit 0", "nonce": claim["nonce"]}]
    return {"schema": 1, "task_id": claim["task_id"], "nonce": claim["nonce"], "executor": executor,
            "sandbox": "bwrap", "started_at": "2026-10-16T10:00:00Z", "finished_at": "2026-10-16T10:00:01Z",
            "verdict": verdict, "reason": None, "exit_code": 0, "killed": False, "seconds": 1.0,
            "over_budget": False, "rows": rows, "log": {"bytes": 0, "sha256": None, "truncated": False,
                                                       "uploaded": False},
            "log_tail": [], "checkout": {"laid_out": [""], "missing": []}}


# --- the task contract ----------------------------------------------------------------------------------
def test_task_contract_accepts_the_fixture_and_refuses_what_it_must(rig):
    assert T.check_task(rig.task()) == []
    bad = rig.task(command="source ./set_env.sh && make lint")
    assert any("set_env" in p for p in T.check_task(bad))
    bad = rig.task(env={"HOME": "/users/david"})
    assert any("HOME is set by the sandbox" in p for p in T.check_task(bad))
    bad = rig.task(env={"NANOSOC_RM_NONCE": "x"})
    assert any("NANOSOC_RM_NONCE" in p for p in T.check_task(bad))
    t = rig.task()
    t["pins"] = [{"path": "a", "mirror": "fixture-pin", "commit": rig.pin_commit}] * 2
    assert any("given twice" in p for p in T.check_task(t))
    t = rig.task()
    t["pins"] = [{"path": "../escape", "mirror": "fixture-pin", "commit": rig.pin_commit}]
    assert T.check_task(t)
    with pytest.raises(Q.TaskRefused):
        Q.enqueue(rig.config, bad)


def test_result_contract_binds_rows_to_the_nonce(rig):
    tid = rig.enqueue()
    claim = Q.claim(rig.config, "eda-1", ["verilator"])
    assert claim["task_id"] == tid
    good = _result(claim)
    assert T.check_result(good, claim["nonce"], tid) == []
    forged = _result(claim, rows=[dict(good["rows"][0], nonce="0" * 32)])
    assert any("nonce" in p for p in T.check_result(forged, claim["nonce"], tid))
    hollow = _result(claim, rows=[dict(good["rows"][0], status="FAIL")])
    assert any("PASS over a non-pass row" in p or "PASS with no PASS row" in p
               for p in T.check_result(hollow, claim["nonce"], tid))
    assert any("/nonce" in p for p in T.check_result(dict(good, nonce="1" * 32), claim["nonce"], tid))


# --- the queue ------------------------------------------------------------------------------------------
def test_claim_needs_every_label_and_leases_to_one_agent(rig):
    t = rig.task()
    t["labels"] = ["vcs", "verilator"]
    tid = Q.enqueue(rig.config, t)
    assert Q.claim(rig.config, "eda-2", ["verilator"]) is None          # lacks vcs
    claim = Q.claim(rig.config, "eda-1", ["verilator", "vcs"])
    assert claim["task_id"] == tid and len(claim["lease"]) == 32
    assert Q.claim(rig.config, "eda-1", ["verilator", "vcs"]) is None   # already leased
    with pytest.raises(Q.LeaseLost):
        Q.heartbeat(rig.config, tid, "eda-2", claim["lease"])           # right lease, wrong agent
    with pytest.raises(Q.LeaseLost):
        Q.heartbeat(rig.config, tid, "eda-1", "f" * 32)                 # right agent, wrong lease
    assert Q.heartbeat(rig.config, tid, "eda-1", claim["lease"]) == {"cancel": False, "lease_seconds": 60}
    row = Q.complete(rig.config, tid, "eda-1", claim["lease"], _result(claim))
    assert row["state"] == "done" and row["verdict"] == "PASS"
    assert "lease" not in row
    with pytest.raises(Q.LeaseLost):                                    # a result is accepted once
        Q.complete(rig.config, tid, "eda-1", claim["lease"], _result(claim))


def test_an_expired_lease_requeues_then_loses_the_task(rig):
    tid = rig.enqueue()
    c1 = Q.claim(rig.config, "eda-1", ["verilator"], now=1000)
    assert Q.claim(rig.config, "eda-1", ["verilator"], now=1059) is None
    c2 = Q.claim(rig.config, "eda-2", ["verilator"], now=1061)          # first lease ran out: queued again
    assert c2["task_id"] == tid and c2["lease"] != c1["lease"]
    with pytest.raises(Q.LeaseLost):                                    # the first agent's late result is refused
        Q.complete(rig.config, tid, "eda-1", c1["lease"], _result(c1))
    assert Q.expire(rig.config, now=1200) == [tid]                      # second claim lost too: max_attempts 2
    t = Q.get(rig.config, tid)
    assert t["state"] == "lost" and t["verdict"] == "INFRA" and "stopped reporting" in t["note"]


def test_a_forged_result_is_refused_and_the_task_stays_claimed(rig):
    tid = rig.enqueue()
    claim = Q.claim(rig.config, "eda-1", ["verilator"])
    with pytest.raises(Q.TaskRefused):
        Q.complete(rig.config, tid, "eda-1", claim["lease"], _result(claim, executor="eda-2"))
    with pytest.raises(Q.TaskRefused):
        Q.complete(rig.config, tid, "eda-1", claim["lease"], dict(_result(claim), nonce="2" * 32))
    assert Q.get(rig.config, tid)["state"] == "claimed"


def test_cancel_a_queued_task_and_ask_a_claimed_one_to_stop(rig):
    a, b = rig.enqueue(), rig.enqueue()
    assert Q.cancel(rig.config, a, "david")["state"] == "cancelled"
    claim = Q.claim(rig.config, "eda-1", ["verilator"])
    assert claim["task_id"] == b
    assert Q.cancel(rig.config, b, "david")["cancel_requested"] is True
    assert Q.heartbeat(rig.config, b, "eda-1", claim["lease"])["cancel"] is True


def test_the_log_is_kept_0600_and_capped(rig):
    rig.enqueue()
    claim = Q.claim(rig.config, "eda-1", ["verilator"])
    p = Q.store_log(rig.config, claim["task_id"], "eda-1", claim["lease"], b"x" * (Q.MAX_LOG_BYTES + 10))
    assert p.stat().st_size == Q.MAX_LOG_BYTES and (p.stat().st_mode & 0o077) == 0
    assert (Q.path(rig.config).stat().st_mode & 0o077) == 0


# --- the config -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry,want", [
    ('name = "eda-1"\ntoken_sha256 = "abc"\n', "token_sha256 is required"),
    ('name = "EDA"\ntoken_sha256 = "%s"\n' % ("a" * 64), "name is required"),
    ('name = "eda-1"\ntoken_sha256 = "%s"\nshell = "x"\n' % ("a" * 64), "unknown key 'shell'"),
    ('name = "eda-1"\ntoken_sha256 = "%s"\nlabels = ["VCS"]\n' % ("a" * 64), "is not a label"),
])
def test_executor_config_is_checked(tmp_path, entry, want):
    p = tmp_path / "c.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\n[[executor.agents]]\n%s' % (tmp_path, entry))
    with pytest.raises(ConfigError, match=want):
        load_config(p)


def test_two_agents_may_not_share_a_token(tmp_path):
    d = "a" * 64
    p = tmp_path / "c.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\n[[executor.agents]]\nname = "a"\ntoken_sha256 = "%s"\n'
                 '[[executor.agents]]\nname = "b"\ntoken_sha256 = "%s"\n' % (tmp_path, d, d))
    with pytest.raises(ConfigError, match="repeats another agent's token"):
        load_config(p)


# --- the API --------------------------------------------------------------------------------------------
def _bearer(token):
    return {"Authorization": "Bearer " + token}


def test_the_api_answers_only_a_configured_token(rig):
    h = rig.http()
    assert h.post("/executor/v1/claim", json={}).status_code == 401
    assert h.post("/executor/v1/claim", json={}, headers=_bearer(tokens.new_token())).status_code == 401
    assert h.post("/executor/v1/claim", json={}, headers={"Authorization": "Basic " + rig.token}).status_code == 401
    r = h.post("/executor/v1/claim", json={}, headers=_bearer(rig.token))
    assert r.status_code == 204 and "set-cookie" not in r.headers
    assert rig.token not in r.text


def test_the_api_is_post_only_and_has_four_paths(rig):
    h = rig.http()
    assert h.get("/executor/v1/claim", headers=_bearer(rig.token)).status_code == 405
    assert h.post("/executor/v1/tasks", headers=_bearer(rig.token)).status_code == 404
    assert h.post("/executor/v1/tasks/1/delete", json={}, headers=_bearer(rig.token)).status_code == 404
    assert h.post("/executor/v1/tasks/1/heartbeat", json={}, headers=_bearer(rig.token)).status_code == 422
    assert h.post("/executor/v1/tasks/99/heartbeat", json={"lease": "a" * 32},
                  headers=_bearer(rig.token)).status_code == 404


def test_the_token_opens_no_other_route_and_a_session_never_reaches_the_api(rig):
    h = rig.http()
    r = h.get("/api/v1/configurations", headers=_bearer(rig.token))
    assert r.status_code == 401                                          # the Guard's session check, untouched
    # A signed-in member with every role: the Guard serves them pages, but the executor API still wants its token.
    from starlette.testclient import TestClient
    from nanosoc_rm.auth import FakeMembership
    from nanosoc_rm.web.app import create_app
    with rig.config_path.open("a") as f:
        f.write('\n[roles]\ndavid = ["member", "admin"]\n')
    member = TestClient(create_app(rig.config_path, auth=FakeMembership("david")))
    assert member.get("/api/v1/configurations").status_code == 200
    assert member.post("/executor/v1/claim", json={}).status_code == 401


def test_a_revoked_token_is_refused_at_once(rig):
    h = rig.http()
    assert h.post("/executor/v1/claim", json={}, headers=_bearer(rig.token)).status_code == 204
    text = rig.config_path.read_text().replace('name = "eda-1"\n', 'name = "eda-1"\nrevoked = true\n')
    rig.config_path.write_text(text)
    assert h.post("/executor/v1/claim", json={}, headers=_bearer(rig.token)).status_code == 401


def test_no_executor_section_means_no_api(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % tmp_path)
    from starlette.testclient import TestClient
    from nanosoc_rm.web.app import create_app
    h = TestClient(create_app(p))
    assert h.post("/executor/v1/claim", json={}, headers=_bearer(tokens.new_token())).status_code == 404


def test_an_agent_claims_only_labels_it_was_granted(rig):
    t = rig.task()
    t["labels"] = ["vcs"]
    Q.enqueue(rig.config, t)
    h = rig.http()
    r = h.post("/executor/v1/claim", json={"labels": ["vcs"]}, headers=_bearer(rig.other_token))   # eda-2: no vcs
    assert r.status_code == 204
    r = h.post("/executor/v1/claim", json={"labels": ["vcs"]}, headers=_bearer(rig.token))
    assert r.status_code == 200 and r.json()["task"]["labels"] == ["vcs"]


def test_the_api_round_trip_and_the_body_limit(rig):
    rig.enqueue()
    h = rig.http()
    claim = h.post("/executor/v1/claim", json={"labels": ["verilator"]}, headers=_bearer(rig.token)).json()
    tid, lease = claim["task_id"], claim["lease"]
    r = h.post("/executor/v1/tasks/%d/log" % tid, content=b"hello", headers=dict(_bearer(rig.token),
                                                                               **{"X-Executor-Lease": lease}))
    assert r.status_code == 200
    big = b"x" * (Q.MAX_LOG_BYTES + 128 * 1024)
    r = h.post("/executor/v1/tasks/%d/log" % tid, content=big, headers=dict(_bearer(rig.token),
                                                                          **{"X-Executor-Lease": lease}))
    assert r.status_code == 413
    r = h.post("/executor/v1/tasks/%d/result" % tid, json={"lease": lease, "result": _result(claim)},
               headers=_bearer(rig.other_token))
    assert r.status_code == 409                                          # eda-2 does not hold it
    r = h.post("/executor/v1/tasks/%d/result" % tid, json={"lease": lease, "result": _result(claim)},
               headers=_bearer(rig.token))
    assert r.status_code == 200 and r.json() == {"state": "done", "verdict": "PASS"}
    assert Q.log_path(rig.config, tid).read_bytes() == b"hello"


# --- the client -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/v1/configurations", "/executor/v1/claim/../../auth/login",
                                  "/executor/v1/tasks/0/result", "/executor/v2/claim", "executor/v1/claim",
                                  "/executor/v1/tasks/1/result?x=1"])
def test_the_client_posts_only_to_the_executor_api(path):
    with pytest.raises(PermissionError):
        check_path(path)


def test_the_client_paths_it_uses_pass():
    for p in ("/executor/v1/claim", "/executor/v1/tasks/7/heartbeat", "/executor/v1/tasks/7/log",
              "/executor/v1/tasks/7/result"):
        assert check_path(p) == p


def test_token_new_writes_the_token_only_to_the_file(tmp_path):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    out = tmp_path / "tok"
    r = CliRunner().invoke(main, ["executor", "token", "new", "eda-9", "--out", str(out), "--labels", "vcs,xcelium"],
                           catch_exceptions=False)
    assert r.exit_code == 0
    value = out.read_text().strip()
    assert tokens.TOKEN_RE.match(value) and (out.stat().st_mode & 0o077) == 0
    assert value not in r.output and tokens.digest(value) in r.output and '["vcs", "xcelium"]' in r.output
    r = CliRunner().invoke(main, ["executor", "token", "new", "eda-9", "--out", str(out)])
    assert r.exit_code == 3 and out.read_text().strip() == value          # never overwritten


def test_cli_enqueue_list_show(rig, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(rig.config_path))
    f = tmp_path / "task.json"
    f.write_text(json.dumps(rig.task()))
    r = CliRunner().invoke(main, ["executor", "enqueue", str(f), "--json"], catch_exceptions=False)
    assert r.exit_code == 0 and json.loads(r.output)["task_id"] == 1
    f.write_text(json.dumps(rig.task(command="source set_env.sh; make")))
    r = CliRunner().invoke(main, ["executor", "enqueue", str(f)])
    assert r.exit_code == 3 and "set_env" in r.output
    r = CliRunner().invoke(main, ["executor", "list"], catch_exceptions=False)
    assert r.exit_code == 0 and "queued" in r.output and "fixture-root" in r.output
    r = CliRunner().invoke(main, ["executor", "show", "1"], catch_exceptions=False)
    assert r.exit_code == 0 and "lint" in r.output
    r = CliRunner().invoke(main, ["executor", "show", "9"])
    assert r.exit_code == 2


def test_the_executor_table_holds_both_units_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\nhost = "eda-1"\nlocal_fallback = false\nlease_seconds = 30\n'
                 % tmp_path)
    assert load_config(p)["executor"]["host"] == "eda-1"
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\nlocal_fallback = "yes"\n' % tmp_path)
    with pytest.raises(ConfigError, match="local_fallback must be bool"):
        load_config(p)
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\nlease_seconds = 5\n' % tmp_path)
    with pytest.raises(ConfigError, match="lease_seconds must be an integer >= 10"):
        load_config(p)
    p.write_text('[service]\ndata_dir = "%s"\n[executor]\n[[executor.agents]]\nname = "local"\ntoken_sha256 = "%s"\n'
                 % (tmp_path, "a" * 64))
    with pytest.raises(ConfigError, match="not \\\"local\\\""):
        load_config(p)
