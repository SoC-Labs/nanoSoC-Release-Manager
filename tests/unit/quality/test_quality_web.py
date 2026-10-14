"""Q5 (stored per commit, trend of two commits) and Q6 (each command has a page with the same rows)."""
from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db, jobs, quality
from nanosoc_rm.cli import main
from nanosoc_rm.config import load_config
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


def _settle(client, url, cfg):
    """CR-R2: a page with no stored result queues a quality job and answers 202; the worker runs it (the real CLI,
    as a child process), and the same page then shows the stored result."""
    r = client.get(url)
    if r.status_code == 202:
        assert "Analysing, refresh" in r.text and "data-pending-job" in r.text, r.text[:500]
        w = jobs.Worker(cfg)
        while w.step() is not None:
            pass
        r = client.get(url)
    return r


def test_a_cold_page_queues_a_job_and_never_analyses_in_the_request(site):
    """CR-R2 (Wave A follow-up b): no stored result -> 202 "analysing, refresh" and one queued quality job; nothing
    is stored by the request itself; a second view reuses the queued job; the worker's run stores every kind."""
    c, cfg = site["client"], load_config(site["cfg"])
    first = c.get("/quality/demo/density")
    assert first.status_code == 202 and "Analysing, refresh" in first.text
    assert db.latest("quality_density", "quality_density:demo:", config=cfg) == [], "the request analysed inline"
    queued = jobs.jobs(cfg, kind="quality", states=("queued",))
    assert len(queued) == 1 and queued[0]["params"] == {"target": "demo", "ref": None}
    assert queued[0]["command"] == "nanosoc-rm quality all demo --store"
    assert c.get("/quality/demo/comments").status_code == 202
    assert len(jobs.jobs(cfg, kind="quality")) == 1, "a second cold page queues no second job"
    assert c.get("/quality/demo/comments.diff").status_code == 202
    assert jobs.Worker(site["cfg"]).step() == queued[0]["id"]
    assert jobs.get(cfg, queued[0]["id"])["state"] == "done"
    for kind in quality.KINDS.values():
        assert len(db.latest(kind, "%s:demo:" % kind, config=cfg)) == 1, kind
    assert c.get("/quality/demo/density").status_code == 200
    again = c.get("/quality/demo/density?analyse=1")
    assert again.status_code == 202 and 'href="http://testserver/quality/demo/density"' in again.text
    assert len(jobs.jobs(cfg, kind="quality", states=("queued",))) == 1


def test_each_page_shows_the_cli_rows(site):
    c = site["client"]
    cli = lambda *a: [json.loads(x) for x in CliRunner().invoke(main, ["quality", *a, "demo", "--json"]).output.splitlines()]
    dens = _settle(c, "/quality/demo/density", site["cfg"])
    assert dens.status_code == 200 and "data-age-seconds" in dens.text and "data-source-for" in dens.text
    assert sorted(_rows(dens.text, "data-path")) == sorted(r["path"] for r in cli("density"))
    com = c.get("/quality/demo/comments")
    rows = cli("comments")
    assert sorted(_rows(com.text, "data-kind")) == sorted(r["kind"] for r in rows
                                                          if r["kind"] not in ("banner", "boilerplate"))
    ban = c.get("/quality/demo/comments?kind=banner")                  # CR-W5: banners one click away
    assert sorted(_rows(ban.text, "data-kind")) == sorted(r["kind"] for r in rows if r["kind"] == "banner")
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
    _settle(c, "/quality/demo", site["cfg"])
    for page, fname in (("comments.diff", "c.diff"), ("readme.diff", "r.diff")):
        r = c.get("/quality/demo/" + page)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-diff") and r.text
        p = tmp_path / fname
        p.write_text(r.text)
        git("apply", "--check", str(p), cwd=repo)


def test_results_are_stored_per_commit_and_the_trend_shows_two(site):
    c, repo = site["client"], site["repo"]
    first = head(repo)
    _settle(c, "/quality/demo/density", site["cfg"])
    second = commit(repo, {"src/extra.py": "\n".join("# note %d" % i for i in range(25)) + "\nX = 1\n"},
                    "2026-10-02T10:00:00Z")
    _settle(c, "/quality/demo/density", site["cfg"])
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
    assert res.exit_code == 1, "findings exit 1, as each single command does (interfaces 14a): %s" % res.output
    assert [ln.split()[0] for ln in res.output.splitlines()] == ["density", "comments", "readme", "docs"]
    for kind in quality.KINDS.values():
        assert len(db.latest(kind, "%s:demo:" % kind, config=site["cfg"])) == 1


def test_worktree_pages_follow_the_disk(site):
    """Review Q-5: a stored work-tree result is reused only while the disk is unchanged."""
    c, repo = site["client"], site["repo"]
    assert "src/b.py" not in _rows(_settle(c, "/quality/demo/density", site["cfg"]).text, "data-path")
    (repo / "src" / "b.py").write_text("".join("# note %d about the helper\n" % i for i in range(50)) + "X = 1\n")
    git("add", "src/b.py", cwd=repo)
    assert c.get("/quality/demo/density").status_code == 202, "a changed disk is never served the old result"
    assert "src/b.py" in _rows(_settle(c, "/quality/demo/density", site["cfg"]).text, "data-path"), \
        "a new tracked file shows"
    before = c.get("/quality/demo/comments.diff?kind=banner").text
    assert c.get("/quality/demo/comments.diff?kind=banner").text == before, "an unchanged disk serves the same patch"
    stored = [d for d in quality.stored("quality_density", "demo", config=site["cfg"])]
    assert stored and all(d["fact"].get("worktree_state") for d in stored)


def test_nothing_to_patch_is_a_message_not_an_empty_file(tmp_path, monkeypatch):
    """Review Q-6: no edits -> 404 with a reason, and no download link on the page."""
    repo = make_repo(tmp_path / "clean", {"a.py": "# Callers rely on identity.\nX = 1\n"})
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[roles]\ndefault = ["member"]\n[quality]\npaths = { clean = "%s" }\n'
                   % (tmp_path / "data", repo))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    c = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    _settle(c, "/quality/clean", cfg)
    for url in ("/quality/clean/comments.diff", "/quality/clean/readme.diff"):
        r = c.get(url)
        assert r.status_code == 404 and r.text.startswith("Nothing to patch"), (url, r.status_code, r.text)
    page = c.get("/quality/clean/comments").text
    assert "data-patch" not in page and "no patch" in page
    out = tmp_path / "x.diff"
    res = CliRunner().invoke(main, ["quality", "comments", "--path", str(repo), "--patch", str(out)])
    assert not out.exists() and "nothing to patch" in res.output


def test_the_quality_pages_sit_in_the_gui_shell(site):
    """Integration: the plug-in seam mounts the real quality router; its pages carry the GUI's navigation (Quality
    marked current), the CSRF header and the sign-out control, and every other page links to it."""
    c = site["client"]
    home = c.get("/").text
    assert '<a href="/quality/"' in home and ">Quality</a>" in home
    for url in ("/quality/", "/quality/demo/density", "/quality/demo/trend"):
        r = _settle(c, url, site["cfg"])
        assert r.status_code == 200, url
        assert '<a href="/quality/" aria-current="page" class="current">Quality</a>' in r.text, url
        assert 'hx-headers=\'{"X-CSRF-Token": "' in r.text and "sign out" in r.text, url
        assert r.text.count("<html") == 1, url
    assert c.get("/quality", follow_redirects=False).status_code in (307, 308)   # the bare prefix redirects


def test_one_navigation_holds_every_wave_b_page_once(site):
    """Wave B integration: one top navigation, in the shared layout, with each plug-in once, on every page; each
    plug-in's landing page answers inside that shell (503 "could not answer" where this site has no registry)."""
    import re as _re
    c = site["client"]
    labels = ["Quality", "Matrix", "Bumps", "Versions", "Releases"]
    for url in ["/"] + ["/%s/" % p for p in ("quality", "matrix", "bumps", "versions", "releases")]:
        r = _settle(c, url, site["cfg"]) if url.startswith("/quality") else c.get(url)
        assert r.status_code in (200, 503), (url, r.status_code, r.text[:300])
        assert r.text.count("<html") == 1, url
        # the header holds each plug-in once (main entries or More; CR-WF-gui); pages with the rail also list
        # them once in its Tools block. The projects home is full width (no rail).
        head = _re.findall(r'<nav aria-label="Main" class="topnav">(.*?)</nav>', r.text, _re.S)
        assert len(head) == 1, url
        navs = head + _re.findall(r'<nav aria-label="main">(.*?)</nav>', r.text, _re.S)
        assert len(navs) == (1 if url == "/" else 2) or url == "/matrix/", url    # /matrix/: full width when it answers
        for nav in navs:
            for label in labels:
                assert nav.count(">%s</a>" % label) == 1, (url, label)


def test_comments_page_hides_banners_by_default_and_shows_the_real_line_text(site, tmp_path):
    """CR-W5 (build-first 5): banners are left out of the default table and patch (one rule, not N findings),
    `?kind=banner` shows them, `?banners=1` adds them to the patch, and a suggested edit shows the ORIGINAL line
    text (not "line N") when the blob is on this host; rows are in review order."""
    c, repo = site["client"], site["repo"]
    page = _settle(c, "/quality/demo/comments", site["cfg"]).text
    assert "banner" not in _rows(page, "data-kind")
    assert "data-banner-link" in page and "data-patch-banners" in page
    edits = re.findall(r'<pre class="edit">(.*?)</pre>', page, re.S)
    assert edits and not any("- line " in e for e in edits), edits[:2]
    import html as _html
    row = re.search(r'<tr data-kind="[^"]+"><td><code>([^<:]+):(\d+)</code></td>.*?<span class="del">- (.*?)</span>',
                    page, re.S)
    path, line, text = row.group(1), int(row.group(2)), _html.unescape(row.group(3))
    assert text == (repo / path).read_text().split("\n")[line - 1]          # the very line, at the analysed commit
    sev = re.findall(r'<tr data-kind="[^"]+"><td><code>[^<]+</code></td><td>(\w+)</td>', page)
    has_edit = [bool(re.search(r'<pre class="edit">', row)) for row in re.findall(r"<tr data-kind=.*?</tr>", page, re.S)]
    assert has_edit == sorted(has_edit, reverse=True), "CR-WD-Q3: suggested edits first"
    order = ["High", "Medium", "Check", "Low"]
    for flag in (True, False):
        part = [s for s, e in zip(sev, has_edit) if e == flag]
        assert part == sorted(part, key=order.index), sev
    plain = c.get("/quality/demo/comments.diff").text
    with_b = c.get("/quality/demo/comments.diff?banners=1").text
    only_b = c.get("/quality/demo/comments.diff?kind=banner").text
    banner_files = set(re.findall(r"^diff --git a/(\S+)", only_b, re.M))
    assert banner_files and not banner_files & set(re.findall(r"^diff --git a/(\S*banner\S*)", plain, re.M))
    assert banner_files <= set(re.findall(r"^diff --git a/(\S+)", with_b, re.M))
    for name, text in (("p.diff", plain), ("b.diff", with_b)):
        p = tmp_path / name
        p.write_text(text)
        git("apply", "--check", str(p), cwd=repo)


def test_comments_preview_shows_the_deleted_text_even_when_the_blob_is_not_here(site, monkeypatch):
    """CR-W4 / CR-WD-Q1: the analysis stores each finding's lines (`context`), so the preview shows the actual
    deleted text with no local blob at all; a finding without context still reads "line N" (rt.edit_lines_for)."""
    from nanosoc_rm.quality import review_text
    c, repo = site["client"], site["repo"]
    _settle(c, "/quality/demo/comments", site["cfg"])
    monkeypatch.setattr(review_text, "local_source", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    page = c.get("/quality/demo/comments").text
    assert "- line " not in page and "not on this host yet" not in page
    dels = [__import__("html").unescape(t) for t in re.findall(r'<span class="del">- (.*?)</span>', page)]
    every = {ln for f in repo.rglob("*") if f.is_file() and ".git" not in f.parts for ln in f.read_text().split("\n")}
    assert dels and "    # y = compute(x)" in dels and all(d in every for d in dels)
    bare = {"path": "x.py", "line": 2, "end_line": 2, "kind": "restates_code",
            "edit": {"start": 2, "end": 2, "replacement": []}}
    assert review_text.edit_lines_for(None, bare) == ([("-", "line 2")], False)


def test_overview_filters_to_the_repos_in_my_configurations(site, monkeypatch):
    """CR-WD-Q5: the overview defaults to the repos in my configurations when any listed target is one; ?scope=all shows
    every target. my_components reads the stored pin_state facts (a walked tree's children)."""
    from pathlib import Path

    from nanosoc_rm import contracts
    from nanosoc_rm.quality import web as qweb
    c, cfg = site["client"], load_config(site["cfg"])
    _settle(c, "/quality/demo", site["cfg"])
    assert qweb.my_components(cfg) == set()
    ov = c.get("/quality/")
    assert _rows(ov.text, "data-target") == ["demo"] and 'data-scope="all"' in ov.text   # nothing registered
    examples = Path(contracts.__file__).resolve().parents[3] / "schemas" / "examples" / "pin-state.example.jsonl"
    for _, doc in contracts.docs_in(examples):
        db.put(doc, config=cfg)
    mine = qweb.my_components(cfg)
    assert "nanosoc_arch_tech" in mine and "demo" not in mine
    ov = c.get("/quality/")      # no listed target is mine (a [quality] paths name): never an empty default
    assert 'data-scope="all"' in ov.text and _rows(ov.text, "data-target") == ["demo"]
    ov = c.get("/quality/?scope=mine")
    assert 'data-scope="mine"' in ov.text and _rows(ov.text, "data-target") == []
    assert "Show all 1 targets" in ov.text and "Repos in my configurations: 0" in ov.text
    monkeypatch.setattr(qweb, "my_components", lambda config: mine | {"demo"})
    ov = c.get("/quality/")
    assert 'data-scope="mine"' in ov.text and _rows(ov.text, "data-target") == ["demo"]
    assert _rows(c.get("/quality/?scope=all").text, "data-target") == ["demo"]


def test_decoration_is_one_rule_of_banners_and_boilerplate(tmp_path, monkeypatch):
    """CR-WD-Q1: banner and boilerplate findings are one rule: hidden from the default table and patch, listed
    together under ?kind=decoration, counted in one chip, and added to the patch together by ?banners=1."""
    files = dict(CASES["banner"][0])
    files.update(CASES["boilerplate"][0])
    files.update({"dead.py": CASES["commented_out_code"][0]["a.py"]})
    repo = make_repo(tmp_path / "demo", files)
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[roles]\ndefault = ["member"]\n[quality]\npaths = { demo = "%s" }\n'
                   % (tmp_path / "data", repo))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    c = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    page = _settle(c, "/quality/demo/comments", cfg).text
    assert set(_rows(page, "data-kind")) == {"commented_out_code"}
    assert "decoration: 3 (one rule: banner 2, boilerplate 1)" in page
    deco = c.get("/quality/demo/comments?kind=decoration").text
    assert sorted(_rows(deco, "data-kind")) == ["banner", "banner", "boilerplate"]
    plain = c.get("/quality/demo/comments.diff").text
    with_rule = c.get("/quality/demo/comments.diff?banners=1").text
    assert "tool.py" not in plain and "a/tool.py" in with_rule and "a/m.sv" in with_rule
    p = tmp_path / "w.diff"
    p.write_text(with_rule)
    git("apply", "--check", str(p), cwd=repo)
