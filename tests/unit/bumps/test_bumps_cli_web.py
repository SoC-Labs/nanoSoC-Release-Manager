"""The front ends of the bump engine (interfaces.md section 18): `nanosoc-rm bumps`, the `bumps` job kind, and the
/bumps pages through the web plug-in seam (shared shell, verdict colours with text, CSRF, local-only POSTs)."""
from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bfix import world
from nanosoc_rm import auth

pytestmark = pytest.mark.requirement("E2")
PASSWORD = "correct horse battery"


def cli(*args):
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, list(args), standalone_mode=False, catch_exceptions=False)
    return r.return_value, r.output


def test_cli_scan_queue_show_patch_mark_ledger_schedule(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    code, out = cli("bumps", "eth-main", "--json")
    docs = [json.loads(x) for x in out.splitlines()]
    assert code == 0 and [d["kind"] for d in docs] == ["bump_scan", "bump_proposal", "bump_proposal"]
    code, out = cli("bumps", "eth-main")
    assert code == 0 and "SHADOW MODE" in out and "proposals, deepest first" in out and "DISABLED" in out
    code, out = cli("bumps", "queue")
    assert code == 0 and "no kept proposals" in out
    assert cli("bumps", "scan", "eth-main", "--store")[0] == 0
    code, out = cli("bumps", "queue", "--json")
    ids = [json.loads(x)["fact"]["id"] for x in out.splitlines()]
    assert code == 0 and len(ids) == 2
    code, out = cli("bumps", "show", ids[0])
    assert code == 0 and "blockers" in out and "nanosoc-rm bumps patch" in out
    patch = tmp_path / "p.diff"
    code, _ = cli("bumps", "patch", ids[0], "--out", str(patch))
    assert code == 0 and patch.read_text().startswith("nanosoc-rm bump proposal %s" % ids[0])
    assert cli("bumps", "patch", ids[0], "--out", str(patch))[0] == 3, "an existing --out is never overwritten"
    assert cli("bumps", "mark", ids[0], "done", "--by", "alice")[0] == 0
    assert cli("bumps", "mark", "bp-000000000000", "done", "--by", "alice")[0] == 3
    code, out = cli("bumps", "ledger", "--json")
    led = json.loads(out)["fact"]
    assert code == 0 and led["counts"]["pending"] == 2 and led["entries"][0]["marks"]
    code, out = cli("bumps", "schedule", "--json")
    assert code == 0 and json.loads(out)["enabled"] is False and json.loads(out)["gate"] == "Phase 4"


def test_cli_exit_codes(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    code, out = cli("bumps", "eth-rc4")
    assert code == 0 and "REFUSED" in out
    assert cli("bumps", "no-such-config")[0] == 3
    assert cli("bumps", "multicore-master")[0] == 2, "never walked: could not answer"
    assert cli("bumps", "show", "bp-000000000000")[0] == 3
    w["cfg"].write_text(w["cfg"].read_text() + '\n[credentials.bump_writer]\ntoken_file = "/nonexistent"\n')
    assert cli("bumps", "eth-main")[0] == 1, "a configured bump writer is a finding"


def test_the_bumps_job_kind_runs_the_scan_as_a_child_process(tmp_path, monkeypatch):
    from nanosoc_rm import bumps, jobs
    from nanosoc_rm.config import load_config
    world(tmp_path, monkeypatch)
    cfg = load_config()
    with pytest.raises(jobs.JobUsage):
        jobs.submit(cfg, "bumps", {"configuration": "Not An Id!"}, "alice")
    job = jobs.submit(cfg, "bumps", {"configuration": "eth-main"}, "alice")
    assert job["command"] == "nanosoc-rm bumps scan eth-main --store"
    w = jobs.Worker(str(cfg.path), poll_s=0.1)
    assert w.acquire(cfg)
    try:
        assert w.step() == job["id"]
    finally:
        w.release()
    done = jobs.get(cfg, job["id"])
    assert done["state"] == "done" and done["verdict"] == "PASS", done
    assert done["result"]["kinds"] == {"bump_scan": 1, "bump_proposal": 2}
    assert len(bumps.stored("eth-main", config=cfg)) == 2


# ----- web ---------------------------------------------------------------------------------------------------
def _local(tmp_path, monkeypatch, *, local=True):
    w = world(tmp_path, monkeypatch)
    h = auth.hash_password(PASSWORD, salt=b"0123456789abcdef")
    text = w["cfg"].read_text()
    if local:
        text += '\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\nhash = "%s"\n' % h
    else:
        text += '\n[roles]\ndefault = ["member"]\n[ci]\nproject = "soclabs/nanosoc-compat-ci"\nproject_id = 1\n'
    w["cfg"].write_text(text)
    from nanosoc_rm import bumps
    bumps.proposals("eth-main", store=True)
    bumps.proposals("eth-rc4", store=True)
    return w


def _signed_in(app):
    c = TestClient(app)
    r = c.get("/auth/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = c.post("/auth/login", data={"csrf_token": token, "login": "alice", "password": PASSWORD, "next": "/"},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def _csrf(c, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text).group(1)


def test_pages_in_the_shared_shell_with_text_labelled_colours(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    c = _signed_in(create_app(w["cfg"]))
    home = c.get("/bumps/")
    assert home.status_code == 200
    t = home.text
    assert '<a href="/bumps/"' in t and "nanoSoC <span>Release Manager</span>" in t, "the app's shell and nav"
    assert "data-age-seconds" in t and 'data-shadow="true"' in t and 'data-schedule="disabled"' in t
    assert "never pushes, writes a branch or opens a pull request" in t
    assert re.search(r'class="pill state-info">proposed<', t) and re.search(r'class="pill state-grey">pending<', t)
    assert "silicon configuration" in t and 'data-configuration="eth-rc4"' in t
    ids = [d["fact"]["id"] for d in bumps.stored("eth-main")]
    assert all('data-proposal="%s"' % i in t for i in ids)
    filtered = c.get("/bumps/?cls=feature").text
    assert filtered.count("data-proposal=") == 1
    gen = next(d["fact"] for d in bumps.stored("eth-main") if d["fact"]["dependency"]["component"] == "nanosoc_gen")
    p = c.get("/bumps/p/%s" % gen["id"])
    assert p.status_code == 200 and "Why it cannot auto-merge" in p.text and "git apply --index" in p.text
    assert 'data-source-for="bump_proposal:eth-main:%s"' % gen["id"] in p.text
    assert 'action="/compat/jobs"' in p.text, "Run queues the matching compat what-if"
    d = c.get("/bumps/p/%s/patch.diff" % gen["id"])
    assert d.status_code == 200 and d.headers["content-type"].startswith("text/x-diff")
    assert d.text.endswith("+Subproject commit %s\n" % gen["to"]["commit"])
    assert c.get("/bumps/p/bp-000000000000").status_code == 404
    assert c.get("/bumps/ledger").status_code == 200
    f = c.get("/bumps/facts/bump_proposal:eth-main:%s" % gen["id"])
    assert f.status_code == 200 and "for-each-ref" in f.text
    assert c.get("/bumps/facts/bump_scan:eth-main").status_code == 200
    assert c.get("/bumps/facts/bump_scan:nope").status_code == 404


def test_posts_need_csrf_queue_a_job_and_log_the_mark_with_the_login(tmp_path, monkeypatch):
    from nanosoc_rm import bumps, jobs
    from nanosoc_rm.config import load_config
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch)
    c = _signed_in(create_app(w["cfg"]))
    assert c.post("/bumps/scan", data={"configuration": "eth-main"}).status_code == 403
    token = _csrf(c, "/bumps/")
    r = c.post("/bumps/scan", data={"csrf_token": token, "configuration": "eth-main"}, follow_redirects=False)
    assert r.status_code == 303 and "/bumps/?job=" in r.headers["location"]
    q = jobs.jobs(load_config(), kind="bumps")
    assert len(q) == 1 and q[0]["by"] == "alice" and q[0]["state"] == "queued"
    r2 = c.post("/bumps/scan", data={"csrf_token": token, "configuration": "eth-main"}, follow_redirects=False)
    assert r2.headers["location"] == r.headers["location"], "the same scan is not queued twice"
    pid = bumps.stored("eth-main")[0]["fact"]["id"]
    r = c.post("/bumps/p/%s/mark" % pid, data={"csrf_token": token, "decision": "declined", "note": "not now"},
               follow_redirects=False)
    assert r.status_code == 303
    assert [(m["login"], m["decision"], m["note"]) for m in bumps.marks(pid)] == [("alice", "declined", "not now")]
    assert c.post("/bumps/p/%s/mark" % pid, data={"csrf_token": token, "decision": "merged"}).status_code == 400


def test_a_ci_deployment_shows_commands_and_refuses_the_local_posts(tmp_path, monkeypatch):
    from nanosoc_rm.auth import FakeMembership
    from nanosoc_rm.web import create_app
    w = _local(tmp_path, monkeypatch, local=False)
    app = create_app(w["cfg"], auth=FakeMembership(login="alice"))
    c = TestClient(app)
    t = c.get("/bumps/").text
    assert 'action="/bumps/scan"' not in t and "nanosoc-rm bumps --store" in t
    r = c.post("/bumps/scan", data={"configuration": "eth-main"})
    assert r.status_code == 403 and "nanosoc-rm bumps eth-main --store" in r.text
