"""Load: the GUI pages (docs/GUI.md; interfaces.md sections 13, 13a) signed in as a local user.

Every page carries the shell's left rail, and the rail ranks the whole inbox (web/shell.py rail ->
web/inbox.py cards), so whatever the inbox reads, every page reads. Measured on <host> (2026-10-14), p50 of
GET per page with the phase-1 demo data: 250 to 450 ms for a shell page (about 31 store connections each,
each running the DDL), 4.3 to 5.6 s for /versions/ and /versions/{component}, 2.4 s for /matrix/{configuration}.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db

import loadfix as L

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from webfix import make_registry, seed  # noqa: E402

PASSWORD = "load-test password"
# Per GET. Generous on purpose: <host> is a shared login server (load average near 300 while this was written, when
# the example-data sweep's slowest page took 18 s); the connection budget below is the deterministic guard.
PAGE_BUDGET_S = 60.0
CONNECTIONS_BUDGET = 80               # store connections per GET (28 to 57 on 2026-10-14), the overviews included
N_PLUS_ONE = ("/versions/", "/versions/tidelink", "/components/tidelink/versions", "/quality/")   # were ROB-load-7


def _signed_in(tmp_path, monkeypatch, extra_configs=()):
    reg = make_registry(tmp_path / "registry", extra_configs=extra_configs)
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[registry]\nrepo = "soclabs/nanosoc-registry"\nref = "main"\n'
                   'path = "%s"\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\nhash = "%s"\n'
                   % (data, reg, auth.hash_password(PASSWORD, salt=b"0123456789abcdef")))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    seed(cfg)
    seed(cfg, "observation.compat-result.example.jsonl", "observation.quality.example.jsonl")
    from nanosoc_rm.web import create_app
    client = TestClient(create_app(cfg))
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/auth/login").text).group(1)
    r = client.post("/auth/login", data={"csrf_token": token, "login": "alice", "password": PASSWORD, "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    return {"cfg": cfg, "client": client, "data": data}


@pytest.fixture
def local(tmp_path, monkeypatch):
    return _signed_in(tmp_path, monkeypatch)


_FILL = {"/configurations/": "eth-rc4", "/components/": "tidelink", "/defects/": "SL-DEF-0001",
         "/errata/": "eth-rc4", "/runs/": "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f", "/jobs/": "1",
         "/contains/": "036d3811e630c89b2344b56aa617b40a99cd652b", "/quality/": "demo", "/matrix/": "eth-rc4",
         "/versions/": "tidelink", "/releases/": "eth-rc4", "/bumps/": "x"}
_QUERY = {"/contains": "?sha=036d381", "/branches": "?component=tidelink", "/hosts": "?component=tidelink"}


def get_routes(app) -> list[str]:
    """Every GET page, the included routers' too (web/routers.py _walk). app.routes alone lists only 34 of the 72
    GET routes (auth included), so a sweep over app.routes misses every workspace and plug-in page."""
    from nanosoc_rm.web import routers
    out = []
    for path, methods, _ in routers._walk(app.routes):
        if "GET" not in methods or path.startswith(("/auth", "/static")):
            continue
        if path.startswith("/facts/"):
            out.append("/facts/" + urllib.parse.quote("registry_record:eligibility.yaml#/configurations/0", safe=""))
            continue
        val = next((v for k, v in _FILL.items() if path.startswith(k)), "x")
        out.append(re.sub(r"\{[^}]+\}", val, path) + _QUERY.get(path, ""))
    return out


def test_every_get_page_answers_within_budget(local, monkeypatch):
    """Every GET page (69 today) answers below 500 within PAGE_BUDGET_S and opens at most CONNECTIONS_BUDGET
    store connections, on the contract's example data. RM_LOAD_REPORT=1 prints each page's time and connections."""
    c = local["client"]
    urls = get_routes(c.app)
    assert len(urls) >= 65
    slow, errors, chatty = [], [], []
    for url in urls:
        n = L.OpCounter()
        with n.installed(monkeypatch):
            t = time.perf_counter()
            r = c.get(url)
            took = time.perf_counter() - t
        if os.environ.get("RM_LOAD_REPORT"):
            print("%7.0f ms %4d conn %4d %s" % (took * 1000, n.connections, r.status_code, url))
        if r.status_code >= 500 and r.status_code != 503:       # 503: "run mirror sync first" (no mirror here)
            errors.append((url, r.status_code, r.text[:200]))
        if took > PAGE_BUDGET_S:
            slow.append((url, round(took, 1)))
        if n.connections > CONNECTIONS_BUDGET:
            chatty.append((url, n.connections))
    assert not errors, errors
    assert not slow, slow
    assert not chatty, chatty


@pytest.mark.parametrize("url", N_PLUS_ONE)
def test_an_overview_page_opens_a_bounded_number_of_store_connections(local, monkeypatch, url):
    """ROB-load-7 (fixed). An overview reads each kind once, not once per catalogue component (before the fix 180
    to 189 connections for /versions/ with the example catalogue; 553 with the demo registry's, about 60 s under
    load on <host>)."""
    c = L.OpCounter()
    with c.installed(monkeypatch):
        assert local["client"].get(url).status_code == 200
    assert c.connections <= CONNECTIONS_BUDGET, c.connections


def _quality_history(cfg, commits: int) -> None:
    """One target analysed at `commits` successive commits (`quality all --store` after each push)."""
    with db.connect(cfg) as con, con:
        for i in range(commits):
            d = L.quality_doc("tidelink", "%040x" % (i + 1), 40, "2026-10-%02dT12:00:00Z" % (1 + i % 28))
            if i == 0:
                assert db.check_observation(d) == []
            db._insert(con, d)


@pytest.mark.xfail(strict=True, reason="ROB-load-2: the inbox reads every quality_finding document ever stored (one "
                                       "per analysed commit) on every page (web/inbox.py _quality)")
def test_a_page_reads_quality_findings_independent_of_their_history(local, monkeypatch):
    """/alerts shows no quality; its rail needs at most the newest finding document per target. With one target
    analysed at 30 commits, a GET must not decode all 30 (each is about 1 KB per finding: tidelink's 4092
    findings are a 4.3 MB document). Measured: a 50k-finding document makes every shell page take about 10 s."""
    _quality_history(local["cfg"], 30)
    seen = L.DocCounter().install(monkeypatch)
    assert local["client"].get("/alerts").status_code == 200
    assert seen.by_kind.get("quality_finding", 0) <= 2 * 2, seen.by_kind


def test_a_page_opens_a_bounded_number_of_store_connections_whatever_the_configuration_count(tmp_path, monkeypatch):
    """ROB-load-6 (fixed). The rail lists each configuration with its deciding gate. Reading that must not cost a store connection per
    configuration: each connection runs the DDL and commits (ROB-load-3), about 85 ms on <host>, so 500
    configurations made GET /alerts take 95 s (1031 connections) and / 186 s."""
    def connections(n_extra, where):
        where.mkdir()
        extra = [{"id": "load-%03d" % i, "component": "nanosoc-ethernet-chiplet", "ref": {"branch": "main"}}
                 for i in range(n_extra)]
        env = _signed_in(where, monkeypatch, extra)
        L.add_configurations(env["cfg"], 1)
        env["client"].get("/")                                    # the inbox refreshes the registry facts
        out = {}
        for url in ("/alerts", "/"):                              # the rail alone; the rail plus the inbox board
            c = L.OpCounter()
            with c.installed(monkeypatch):
                assert env["client"].get(url).status_code == 200
            out[url] = c.connections
        return out
    few, many = connections(0, tmp_path / "few"), connections(40, tmp_path / "many")
    assert all(many[u] <= few[u] + 10 for u in few), (few, many)


def test_the_batched_overview_reads_answer_what_the_per_target_reads_did(local):
    """ROB-load-7's batched reads return, per target, exactly the documents and order of the per-target read they
    replace (quality.stored(kind, target); versioning.stored(kind, component)). Seeded: the quality example."""
    from nanosoc_rm import quality as q
    from nanosoc_rm import versioning
    from nanosoc_rm.versioning.core import stored_by_target as v_by_target
    _quality_history(local["cfg"], 3)
    seen = 0
    for kind in q.KINDS:
        by = q.stored_by_target(kind, config=local["cfg"])
        targets = {d["fact"]["target"] for d in q.stored(kind, config=local["cfg"])}
        assert set(by) == targets
        for t in targets:
            assert by[t] == q.stored(kind, t, config=local["cfg"]), (kind, t)
            seen += len(by[t])
    assert seen >= 4                                              # the example's four kinds plus the history
    for kind in ("version_plan", "changelog_lint", "tag_check"):
        by = v_by_target(kind, config=local["cfg"])
        for t in by:
            assert by[t] == versioning.stored(kind, t, config=local["cfg"]), (kind, t)
