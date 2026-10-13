"""The review queue and the patch basket (Wave C spec 4.5, unit K): derived severity and caution rules, decisions,
the per-login and per-commit basket, the combined patch (git apply --check), and the blob-presence rule (a page
never fetches a blob into a blob-less mirror)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanosoc_rm import auth, db, quality
from nanosoc_rm.config import load_config
from nanosoc_rm.quality import review_text as rt
from nanosoc_rm.web import create_app, review, uistate

pytestmark = [pytest.mark.requirement("Q2"), pytest.mark.requirement("Q6")]

HASH = auth.hash_password("correct horse battery", salt=b"0123456789abcdef")
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_DATE": "2026-10-01T10:00:00Z", "GIT_COMMITTER_DATE": "2026-10-01T10:00:00Z"}

FILES = {
    "a.py": "def f(x):\n    # y = compute(x)\n    # return y * 2\n    return x\n",
    "b.c": "int g(int result) {\n    // return the result\n    return result;\n}\n",
    "c.py": "# ------------------------------------------------\n# Helpers\n"
            "# ------------------------------------------------\nX = 1\n",
    "tool.py": "# File: tool.py\n# Author: someone\n# Date: 2019-01-01\n# Copyright 2024 SoC Labs\nX = 1\n",
    "calc.py": "def compute_total(values):\n    # compute_totl() adds the values; `old_helper()` used to round them\n"
               "    return sum(values)\n",
    "README.md": "# Demo\n\nSee `docs/gone.md`.\n",
}


def git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=check,
                          capture_output=True, text=True)


def make_repo(root: Path, files: dict) -> Path:
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", ".", cwd=root)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "fixture", cwd=root)
    return root


def cfg_text(data, repo, extra=""):
    return "\n".join(["[service]", 'data_dir = "%s"' % data, "[auth]", 'mode = "local"',
                      "[[auth.users]]", 'login = "alice"', 'hash = "%s"' % HASH,
                      "[[auth.users]]", 'login = "carol"', 'hash = "%s"' % HASH,
                      "[quality]", 'paths = { demo = "%s" }' % repo]) + "\n" + extra


@pytest.fixture
def rv(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "demo", FILES)
    cfg = tmp_path / "rm.toml"
    cfg.write_text(cfg_text(tmp_path / "data", repo))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    for fn in (quality.comments, quality.readme, quality.density, quality.docs):
        fn("demo", config=cfg, store=True)                     # analysed by the test (a job's work), not a page
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    return {"cfg": cfg, "repo": repo, "app": app, "client": TestClient(app), "tmp": tmp_path}


def _obs(rv):
    return review.stored_fact(load_config(rv["cfg"]), "demo", "comments")


# ----- rules ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("finding, sev", [
    ({"kind": "stale_todo", "age_days": 190}, "High"),
    ({"kind": "stale_todo", "age_days": 120}, "Low"),
    ({"kind": "unknown_identifier", "did_you_mean": "u_rx_ptp_det"}, "High"),
    ({"kind": "unknown_identifier"}, "Check"),
    ({"readme": "README.md", "status": "missing", "kind": "path"}, "Medium"),
    ({"kind": "restates_code", "edit": {"start": 1, "end": 1, "replacement": []}}, "Low"),
    ({"kind": "boilerplate", "edit": {"start": 1, "end": 1, "replacement": []}}, "Low"),
    ({"kind": "commented_out_code", "edit": {"start": 1, "end": 2, "replacement": []}}, "Low"),
    ({"kind": "commented_out_code", "edit": None}, "Check"),
    ({"kind": "restates_code", "edit": None}, "Check"),
])
def test_severity_rules(finding, sev):
    assert review.severity(finding) == sev


def test_rank_orders_high_medium_check_low_then_path_and_line():
    fs = [{"kind": "restates_code", "edit": {"start": 1}, "path": "a", "line": 1},
          {"kind": "unknown_identifier", "path": "z", "line": 9},
          {"kind": "stale_todo", "age_days": 400, "path": "b", "line": 5},
          {"kind": "stale_todo", "age_days": 400, "path": "b", "line": 2}]
    assert [(f["path"], f["line"]) for f in review.rank(fs)] == [("b", 2), ("b", 5), ("z", 9), ("a", 1)]


def test_caution_rules():
    f = {"kind": "unknown_identifier", "identifier": "tb_tops", "did_you_mean": "tb_top", "line": 2}
    assert "plural" in review.caution(f, ["x", "# runs tb_tops", "y"])
    f2 = {"kind": "unknown_identifier", "identifier": "eth_rx_cksum", "did_you_mean": "u_eth_rx_cksum", "line": 1}
    assert "list it twice" in review.caution(f2, ["# eth_rx_cksum feeds u_eth_rx_cksum"])
    f3 = {"kind": "stale_todo", "line": 1}
    assert review.caution(f3, ["// TODO: a", "// TODO: b"]) == "Line 2 has the same pattern; use Edit to fix both."
    assert review.caution({"kind": "restates_code", "line": 1}, ["# x", "y = 1"]) is None


def test_queue_hides_banners_and_keys_are_stable(rv):
    obs = _obs(rv)
    items = review.queue(obs)
    kinds = {it["kind"] for it in items}
    assert "banner" not in kinds and {"commented_out_code", "restates_code", "boilerplate"} <= kinds
    assert [it["key"] for it in items] == [it["key"] for it in review.queue(_obs(rv))]
    assert len({it["key"] for it in items}) == len(items), "two findings on one line must not share a key"
    assert any(it["key"].endswith("#2") for it in items)        # calc.py:2 names two unknown identifiers
    c = obs["fact"]["commit"]
    assert all(it["key"].startswith(c) for it in items)


# ----- pages and decisions ------------------------------------------------------------------------------
def test_review_page_shows_real_code_and_the_suggested_edit_as_text(rv):
    i = next(it["n"] for it in review.queue(_obs(rv)) if it["finding"].get("edit"))
    t = rv["client"].get("/components/demo/review?i=%d" % i).text
    assert 'id="review"' in t and "Finding %d of" % (i + 1) in t
    assert 'data-excerpt' in t and '<span class="hl">' in t
    edit = re.search(r"<pre class=\"code\" data-edit>(.*?)</pre>", t, re.S)
    assert edit and "- line " not in edit.group(1), "the removed lines must be text, not line numbers"
    assert 'aria-current="step"' in t and "0 of" in t


def test_accept_moves_to_the_next_item_and_adds_it_to_the_basket(rv):
    c, obs = rv["client"], _obs(rv)
    items = review.queue(obs)
    first = next(it for it in items if it["finding"].get("edit"))
    r = c.post("/components/demo/review/decide", data={"key": first["key"], "decision": "accept",
                                                       "commit": review.commit_of(obs)}, follow_redirects=False)
    assert r.status_code == 303
    nxt = re.search(r"i=(\d+)", r.headers["location"]).group(1)
    assert int(nxt) == next(it["n"] for it in items if it["n"] > first["n"])
    t = c.get(r.headers["location"]).text
    assert 'data-basket="%s"' % first["key"] in t and "1 in patch" in t
    assert "data-download" in t


def test_htmx_decide_answers_the_review_fragment(rv):
    c, obs = rv["client"], _obs(rv)
    it = review.queue(obs)[0]
    r = c.post("/components/demo/review/decide", data={"key": it["key"], "decision": "skip",
                                                       "commit": review.commit_of(obs)}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text, r.text[:300]
    assert "1 skipped" in r.text and 'id="basket"' in r.text, r.text[:600]


def test_skip_and_dismiss_are_counted(rv):
    c, obs = rv["client"], _obs(rv)
    items = review.queue(obs)
    for it, d in zip(items, ("skip", "dismiss", "dismiss")):
        c.post("/components/demo/review/decide", data={"key": it["key"], "decision": d,
                                                       "commit": review.commit_of(obs)})
    t = c.get("/components/demo/review").text
    prog = re.search(r'data-progress>(.*?)</p>', t, re.S).group(1)
    assert "3 of %d reviewed" % len(items) in prog and "1 skipped" in prog and "2 not a finding" in prog, prog


def test_accept_without_an_edit_is_refused(rv):
    obs = _obs(rv)
    it = next((it for it in review.queue(obs) if not it["finding"].get("edit")), None)
    if it is None:
        pytest.skip("every fixture finding has an edit")
    r = rv["client"].post("/components/demo/review/decide", data={"key": it["key"], "decision": "accept",
                                                                  "commit": review.commit_of(obs)})
    assert r.status_code == 400


def _apply_check(rv, text):
    p = rv["tmp"] / "x.patch"
    p.write_text(text)
    r = git("apply", "--check", str(p), cwd=rv["repo"], check=False)
    assert r.returncode == 0, r.stderr + text


def test_patch_diff_applies_and_edit_with_empty_text_deletes_only(rv):
    c, obs = rv["client"], _obs(rv)
    items = review.queue(obs)
    acc = [it for it in items if it["finding"].get("edit")]
    cm = review.commit_of(obs)
    c.post("/components/demo/review/decide", data={"key": acc[0]["key"], "decision": "accept", "commit": cm})
    target = next(it for it in items if it["kind"] == "unknown_identifier")
    c.post("/components/demo/review/decide", data={"key": target["key"], "decision": "edit", "text": "", "commit": cm})
    r = c.get("/components/demo/review/patch.diff")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-diff")
    assert 'filename="demo-comments-%s.patch"' % obs["fact"]["commit"][:7] in r.headers["content-disposition"]
    _apply_check(rv, r.text)
    calc = r.text[r.text.index("a/calc.py"):]
    calc = calc[:calc.index("diff --git")] if "diff --git" in calc else calc
    assert "-    # compute_totl()" in calc and not re.search(r"^\+(?!\+\+)", calc, re.M), "edit with no text must only delete"


def test_banner_rule_is_off_by_default_and_the_flag_adds_it(rv):
    c, obs = rv["client"], _obs(rv)
    cm = review.commit_of(obs)
    it = next(it for it in review.queue(obs) if it["finding"].get("edit"))
    c.post("/components/demo/review/decide", data={"key": it["key"], "decision": "accept", "commit": cm})
    before = c.get("/components/demo/review/patch.diff").text
    assert "a/c.py" not in before
    assert 'aria-pressed="false"' in c.get("/components/demo/review").text
    c.post("/components/demo/review/basket", data={"op": "banner-on", "commit": cm})
    after = c.get("/components/demo/review/patch.diff").text
    assert "a/c.py" in after
    _apply_check(rv, after)


def test_empty_basket_is_a_404_with_the_reason(rv):
    r = rv["client"].get("/components/demo/review/patch.diff")
    assert r.status_code == 404 and "basket is empty" in r.text


def test_basket_is_per_login_and_per_commit(rv):
    obs = _obs(rv)
    cm = review.commit_of(obs)
    it = next(it for it in review.queue(obs) if it["finding"].get("edit"))
    rv["client"].post("/components/demo/review/decide", data={"key": it["key"], "decision": "accept", "commit": cm})
    carol = TestClient(create_app(rv["cfg"], auth=auth.FakeMembership(login="carol")))
    assert "data-basket=" not in carol.get("/components/demo/review").text
    assert carol.get("/components/demo/review/patch.diff").status_code == 404
    # a new analysis commit starts a new basket; the old one stays readable through ?commit=
    (rv["repo"] / "d.py").write_text("# z = 1\n# return z\nZ = 2\n")
    git("add", "-A", cwd=rv["repo"])
    git("commit", "-q", "-m", "more", cwd=rv["repo"])
    quality.comments("demo", config=rv["cfg"], store=True)
    new = _obs(rv)
    assert new["fact"]["commit"] != obs["fact"]["commit"]
    t = rv["client"].get("/components/demo/review").text
    assert "data-basket=" not in t
    old = rv["client"].get("/components/demo/review?commit=%s" % obs["fact"]["commit"][:12]).text
    assert 'data-basket="%s"' % it["key"] in old


def test_remove_from_the_basket(rv):
    c, obs = rv["client"], _obs(rv)
    cm = review.commit_of(obs)
    it = next(it for it in review.queue(obs) if it["finding"].get("edit"))
    c.post("/components/demo/review/decide", data={"key": it["key"], "decision": "accept", "commit": cm})
    c.post("/components/demo/review/basket", data={"op": "remove", "key": it["key"], "commit": cm})
    assert "data-basket=" not in c.get("/components/demo/review").text
    assert uistate.decisions(load_config(rv["cfg"]), "alice", "demo", cm)[it["key"]]["decision"] == "skip"


def test_quality_tab_counts_banners_as_one_rule(rv):
    t = rv["client"].get("/components/demo/quality").text
    obs = _obs(rv)
    n_banner = sum(1 for f in obs["fact"]["findings"] if f["kind"] == "banner")
    assert n_banner and "%d banner lines are one rule, not %d findings" % (n_banner, n_banner) in t
    assert 'data-tile="worth"' in t and "Review: one finding at a time" in t
    assert "README mismatches" in t


# ----- blob presence -----------------------------------------------------------------------------------
def test_a_blob_less_mirror_is_never_fetched_by_a_page(tmp_path, monkeypatch):
    """The mirror holds trees but not blobs (fetch --filter=blob:none). The review page must say "not on this
    host yet" and leave the blob missing: a promisor fetch in a request is a bug."""
    src = make_repo(tmp_path / "src", FILES)
    git("config", "uploadpack.allowFilter", "true", cwd=src)
    data = tmp_path / "data"
    cfg = tmp_path / "rm.toml"
    cfg.write_text(cfg_text(data, tmp_path / "unused"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    from nanosoc_rm.analysis import _inputs as inp
    mirror = inp.mirror_path(load_config(cfg), "tidelink")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--bare", "--filter=blob:none", "file://%s" % src, str(mirror), cwd=tmp_path)
    head = git("rev-parse", "HEAD", cwd=src).stdout.strip()
    obs = quality.comments("tidelink", path=src)               # the findings of the same commit, labelled tidelink
    obs["fact"].update(target="tidelink", commit=head, worktree=False)
    obs["subject"] = "quality_finding:tidelink:%s" % head
    obs["fact"].pop("worktree_state", None)
    db.put(obs, config=cfg)
    missing = lambda: {ln[1:] for ln in git("rev-list", "--objects", "--missing=print", "--no-walk", head,
                                            cwd=mirror).stdout.split() if ln.startswith("?")}
    before = missing()
    assert before, "the fixture mirror should be blob-less"
    src_mod = rt.LocalSource(mirror, head, label="tidelink")
    assert src_mod.text("a.py") is None and missing() == before
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    i = next(it["n"] for it in review.queue(obs) if it["finding"].get("edit"))
    t = client.get("/components/tidelink/review?i=%d" % i).text
    assert rt.NOT_LOCAL in t and "- line " in t                 # edit shown as line numbers, with the note
    assert missing() == before, "the page fetched a blob"
