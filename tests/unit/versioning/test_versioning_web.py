"""The /versions pages (C1-C12 shown, G1 drafts) through the web plug-in seam, the job kinds they queue, and the
GUI rule that a page shows the same rows as its CLI command."""
from __future__ import annotations

import re

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nanosoc_rm import auth, jobs, versioning
from nanosoc_rm.cli import main
from nanosoc_rm.config import load_config
from nanosoc_rm.web import create_app
import vfix

pytestmark = [pytest.mark.requirement("C1"), pytest.mark.requirement("C2")]


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."], "feature": ["Add a variant."]})
    s["client"] = TestClient(create_app(s["cfg"], auth=auth.FakeMembership(login="alice")))
    return s


def _token(c, path="/versions/"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text).group(1)


def test_overview_and_component_pages_show_the_stored_cli_rows(site):
    c = site["client"]
    r = c.get("/versions/")
    assert r.status_code == 200 and "Nothing stored yet" in r.text
    res = CliRunner().invoke(main, ["version", "refresh", "nanosoc_arch_tech"])
    assert res.exit_code == 0, res.output
    plan = versioning.stored("version_plan", "nanosoc_arch_tech", config=site["cfg"])[-1]
    page = c.get("/versions/").text
    row = re.search(r'<tr data-component="nanosoc_arch_tech" data-verdict="([A-Z]+)">(.*?)</tr>', page, re.S)
    assert row and row.group(1) == plan["fact"]["verdict"] == "WARN"
    assert ">0.1.1<" in row.group(2) and ">WARN</span>" in row.group(2) and "state-amber" in row.group(2), \
        "the verdict is a colour AND a text label (README section 6)"
    assert "1 feature" in row.group(2) and "1 fix" in row.group(2) and "<code>v0.1.0</code>" in row.group(2)
    assert 'data-source-for="%s"' % plan["subject"] in page and "data-age-seconds" in page
    detail = c.get("/versions/nanosoc_arch_tech").text
    assert set(re.findall(r'<tr data-signal="([a-z_]+)"', detail)) == {s["signal"] for s in plan["fact"]["classification"]["signals"]}
    assert "Nothing here is pushed" in detail and "proposed manifest patch" in detail
    assert c.get("/versions/no_such").status_code == 404 and c.get("/versions/bad%20id").status_code == 404


def test_the_pages_sit_in_the_gui_shell(site):
    c = site["client"]
    home = c.get("/").text
    assert '<a href="/versions/"' in home and ">Versions</a>" in home
    r = c.get("/versions/")
    assert '<a href="/versions/" aria-current="page" class="current">Versions</a>' in r.text and r.text.count("<html") == 1


@pytest.mark.requirement("C11")
def test_manifest_patch_download(site):
    r = site["client"].get("/versions/nanosoc_arch_tech/manifest.diff")
    assert r.status_code == 200 and "diff --git a/nanosoc-component.yaml" in r.text and "nothing was applied" in r.text


@pytest.mark.requirement("G1")
def test_refresh_and_draft_are_jobs_and_draft_files_are_served(site):
    c = site["client"]
    tok = _token(c)
    r = c.post("/versions/refresh", data={"csrf_token": tok, "component": "nanosoc_arch_tech"}, follow_redirects=False)
    assert r.status_code == 303 and "/versions/nanosoc_arch_tech?job=" in r.headers["location"]
    job = jobs.jobs(load_config(site["cfg"]))[0]
    assert job["kind"] == "versions-refresh" and job["command"] == "nanosoc-rm version refresh nanosoc_arch_tech"
    r = c.post("/versions/nanosoc_arch_tech/draft", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert c.post("/versions/bad id/draft", data={"csrf_token": tok}).status_code in (400, 404)
    # the draft job's command, run as the worker would, writes under <data_dir>/drafts
    res = CliRunner().invoke(main, ["version", "draft", "nanosoc_arch_tech", "--date", "2026-10-13"])
    assert res.exit_code == 0, res.output
    txt = c.get("/versions/nanosoc_arch_tech/drafts/0.1.1/README.md").text
    assert "Nothing was tagged, pushed" in txt
    assert c.get("/versions/nanosoc_arch_tech/drafts/0.1.1/secret.txt").status_code == 404
    assert c.get("/versions/nanosoc_arch_tech/drafts/..%2F..%2Fx/README.md").status_code == 404
    assert "0.1.1" in c.get("/versions/nanosoc_arch_tech").text


def test_job_kinds_go_through_the_seam_and_cannot_replace_a_built_in():
    from nanosoc_rm.versioning.commands import ensure_job_kinds
    ensure_job_kinds()
    assert jobs.cli_args("versions-refresh", {"components": ["a"]}) == ["version", "refresh", "a", "--json"]
    with pytest.raises(jobs.JobUsage):
        jobs._params("version-draft", {"component": "../x"})
    with pytest.raises(ValueError):
        jobs.register_kind("compat", lambda p: p, lambda p: [])
    with pytest.raises(jobs.JobUsage, match="unknown job kind"):
        jobs._params("nope", {})


def test_post_routes_only_in_a_local_deployment(site, tmp_path, monkeypatch):
    assert TestClient(site["client"].app).post("/versions/refresh", data={}, follow_redirects=False).status_code == 303
    s = vfix.arch_site(tmp_path / "ci", monkeypatch, extra_config='[ci]\nproject = "soclabs/nanosoc-compat-ci"\nproject_id = 1\n'
                       'ref = "main"\njob = "t0"\nallowed_refs = ["main"]\nallowed_runner_ids = [1]\nfetch_timeout_minutes = 120')
    app = create_app(s["cfg"], auth=auth.FakeMembership(login="alice"))
    c = TestClient(app)
    assert c.get("/versions/").status_code == 200 and "nanosoc-rm version refresh" in c.get("/versions/").text
    assert c.post("/versions/refresh", data={}, follow_redirects=False).status_code in (404, 405)
