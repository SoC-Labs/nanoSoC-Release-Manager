"""Coverage angle (rob/coverage): job ids taken from a URL (path or `?job=`).

The routes accept an id when `str.isdigit()` says so, then call `int()`. Unicode digits that are not decimal
(superscripts) pass isdigit() and fail int(); so does a decimal string longer than Python's int-parsing limit.
An id that names no job must be a 404 (path) or no job shown (query), never a 500."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, jobs
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[roles]\ndefault = ["member"]\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    app = create_app(cfg, auth=auth.FakeMembership("alice"))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, load_config(cfg)


@pytest.mark.parametrize("route", ["/jobs/{}/log", "/jobs/{}/fragment"])
@pytest.mark.parametrize("ident", ["12345", "99999999999999999999999", "abc"])
def test_a_job_id_that_names_no_job_is_404(client, route, ident):
    c, _ = client
    assert c.get(route.format(ident)).status_code == 404


def test_the_mirrors_page_ignores_a_job_id_that_names_no_job(client):
    c, _ = client
    assert c.get("/mirrors?job=12345").status_code == 200 and c.get("/mirrors?job=x").status_code == 200


def test_a_job_id_that_names_a_job_is_served(client):
    c, cfg = client
    j = jobs.submit(cfg, "walk", {"configuration": "eth-main"}, "alice")
    r = c.get("/jobs/%d/log" % j["id"])
    assert r.status_code == 200 and r.text == "(no output yet)"


@pytest.mark.xfail(strict=True, reason="ROB-coverage-2: a superscript digit in a job id passes str.isdigit() and "
                                       "int() raises: HTTP 500 instead of 404 (web/app.py job routes)")
@pytest.mark.parametrize("route", ["/jobs/%C2%B2/log", "/jobs/%C2%B2/fragment", "/mirrors?job=%C2%B2"])
def test_a_superscript_job_id_is_404_not_500(client, route):
    c, _ = client
    assert c.get(route).status_code in (200, 404)


@pytest.mark.xfail(strict=True, reason="ROB-coverage-3: a 5000-digit job id passes str.isdigit() and int() "
                                       "raises (int max str digits): HTTP 500 instead of 404")
def test_a_job_id_beyond_the_int_parsing_limit_is_404_not_500(client):
    c, _ = client
    assert c.get("/jobs/%s/log" % ("9" * 5000)).status_code == 404
