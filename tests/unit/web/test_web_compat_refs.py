"""The Compat page's override choices: every head and tag of a component's repos in its mirror, an annotated tag
peeled to its commit (an override must name a commit, never a tag object)."""
from __future__ import annotations

from nanosoc_rm.config import load_config
from nanosoc_rm.web import views
from webfix import git


def test_component_refs_peel_annotated_tags(tmp_path):
    cfg_path = tmp_path / "rm.toml"
    cfg_path.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", ".", cwd=work)
    git("commit", "-q", "--allow-empty", "-m", "one", cwd=work)
    commit = git("rev-parse", "HEAD", cwd=work)
    git("tag", "-a", "-m", "annotated", "v1", cwd=work)
    git("tag", "light", cwd=work)
    tag_object = git("rev-parse", "v1", cwd=work)
    assert tag_object != commit
    mirror = tmp_path / "data" / "mirrors" / "tidelink.git"
    git("init", "-q", "--bare", str(mirror), cwd=tmp_path)
    prefix = "refs/r/github/SoC-Labs%2FTideLink"
    git("fetch", "-q", str(work), "refs/heads/*:%s/heads/*" % prefix, "refs/tags/*:%s/tags/*" % prefix, cwd=mirror)
    refs = views.component_refs(load_config(cfg_path), "tidelink")
    got = {(r["kind"], r["name"]): (r["host"], r["path"], r["sha"]) for r in refs}
    want = ("github", "SoC-Labs/TideLink", commit)
    assert got == {("branch", "main"): want, ("tag", "v1"): want, ("tag", "light"): want}
    assert views.component_refs(load_config(cfg_path), "no-mirror") == []
    assert views.component_refs(load_config(cfg_path), "../x") == []
