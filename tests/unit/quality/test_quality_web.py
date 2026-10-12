"""Q5 (stored per commit, trend of two commits) and Q6 (each command has a page with the same rows)."""
from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db, quality
from nanosoc_rm.cli import main
from nanosoc_rm.quality.source import GitSource
from nanosoc_rm.web import create_app
from qfix import commit, git, head, make_repo
from test_quality_findings import CASES
from test_quality_readme import FILES as README_FILES

pytestmark = [pytest.mark.requirement("Q5"), pytest.mark.requirement("Q6")]


@pytest.fixture
def site(tmp_path, monkeypatch):
    files = dict(README_FILES)
    for kind in ("commented_out_code", "banner"):
        for path, text in CASES[kind][0].items():
            files["src/%s_%s" % (kind, path)] = text
    files["docs/padded.md"] = "# Notes\n\nIt should be noted that this is basically just a very simple note.\n"
    repo = make_repo(tmp_path / "demo", files, executable=("scripts/run.sh",))
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[roles]\ndefault = ["member"]\n[quality]\npaths = { demo = "%s" }\n'
                   % (tmp_path / "data", repo))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    return {"repo": repo, "cfg": cfg, "client": client}


def _rows(html: str, attr: str) -> list[str]:
    return re.findall(r'<tr[^>]*\b%s="([^"]*)"' % attr, html)


def test_each_page_shows_the_cli_rows(site):
    c = site["client"]
    cli = lambda *a: [json.loads(x) for x in CliRunner().invoke(main, ["quality", *a, "demo", "--json"]).output.splitlines()]
    dens = c.get("/quality/demo/density")
    assert dens.status_code == 200 and "data-age-seconds" in dens.text and "data-source-for" in dens.text
    assert sorted(_rows(dens.text, "data-path")) == sorted(r["path"] for r in cli("density"))
    com = c.get("/quality/demo/comments")
    assert sorted(_rows(com.text, "data-kind")) == sorted(r["kind"] for r in cli("comments"))
    rd = c.get("/quality/demo/readme")
    mism = [r for r in cli("readme") if r["status"] == "missing"]
    assert len(_rows(rd.text, "data-kind")) == len(mism) == 3 and "## Repository layout" in rd.text
    docs = c.get("/quality/demo/docs")
    assert sorted(_rows(docs.text, "data-path")) == sorted(r["path"] for r in cli("docs"))
    summary = c.get("/quality/demo")
    assert summary.status_code == 200 and "comment ratio" in summary.text
    ov = c.get("/quality/")
    assert _rows(ov.text, "data-target") == ["demo"] and "<svg" in ov.text


def test_patch_downloads_apply(site, tmp_path):
    c, repo = site["client"], site["repo"]
    for page, fname in (("comments.diff", "c.diff"), ("readme.diff", "r.diff")):
        r = c.get("/quality/demo/" + page)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-diff") and r.text
        p = tmp_path / fname
        p.write_text(r.text)
        git("apply", "--check", str(p), cwd=repo)


def test_results_are_stored_per_commit_and_the_trend_shows_two(site):
    c, repo = site["client"], site["repo"]
    first = head(repo)
    c.get("/quality/demo/density")
    second = commit(repo, {"src/extra.py": "\n".join("# note %d" % i for i in range(25)) + "\nX = 1\n"},
                    "2026-10-02T10:00:00Z")
    c.get("/quality/demo/density")
    t = c.get("/quality/demo/trend")
    assert _rows(t.text, "data-commit") == [first, second]
    pts = quality.trend("density", "demo", config=site["cfg"])
    assert [p["commit"] for p in pts] == [first, second] and pts[1]["value"] > pts[0]["value"]
    stored = db.latest("quality_density", "quality_density:demo:", config=site["cfg"])
    assert len(stored) == 2
    out = CliRunner().invoke(main, ["quality", "trend", "density", "demo", "--json"]).output
    assert [json.loads(x)["commit"] for x in out.splitlines()] == [first, second]


def test_unknown_target_is_a_404_page(site):
    r = site["client"].get("/quality/nope/density")
    assert r.status_code in (404, 503) and "nope" in r.text


def test_mirror_namespace_ref_resolution(tmp_path):
    work = make_repo(tmp_path / "w", {"a.py": "# c\nX = 1\n"})
    mirror = tmp_path / "m.git"
    git("init", "-q", "--bare", str(mirror), cwd=tmp_path)
    ns = "refs/r/github/SoC-Labs%2Fdemo/"
    git("fetch", "-q", str(work), "main:%sheads/main" % ns, cwd=mirror)
    src = GitSource.resolve(mirror, "main", label="demo", prefixes=(ns,))
    assert src.commit == head(work) and [e.path for e in src.files()] == ["a.py"]
    with pytest.raises(quality.SourceError):
        GitSource.resolve(mirror, "nosuch", label="demo", prefixes=(ns,))


def test_cli_all_stores_every_kind(site):
    res = CliRunner().invoke(main, ["quality", "all", "demo", "--store"])
    assert res.exit_code == 0, res.output
    assert [ln.split()[0] for ln in res.output.splitlines()] == ["density", "comments", "readme", "docs"]
    for kind in quality.KINDS.values():
        assert len(db.latest(kind, "%s:demo:" % kind, config=site["cfg"])) == 1
