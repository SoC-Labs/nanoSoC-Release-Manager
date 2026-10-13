"""A partial-clone mirror (blob:none, as `mirror sync` makes them) is read with one batch fetch of the missing
blobs, not one lazy round trip per blob (found deploying: tidelink timed out after 300 s)."""
from __future__ import annotations

import pytest

from nanosoc_rm.quality.source import GitSource
from qfix import git, head, make_repo

pytestmark = [pytest.mark.requirement("Q1")]


def _missing(repo, sha) -> int:
    out = git("rev-list", "--objects", "--no-walk", "--missing=print", sha, cwd=repo).stdout
    return sum(1 for line in out.splitlines() if line.startswith("?"))


def test_a_partial_clone_mirror_is_read_with_one_batch_fetch(tmp_path):
    files = {"src/a%d.py" % i: "# note %d\nX = %d\n" % (i, i) for i in range(12)}
    files["README.md"] = "# Demo\n"
    src = make_repo(tmp_path / "src", files)
    git("config", "uploadpack.allowFilter", "true", cwd=src)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
    mirror = tmp_path / "mirror.git"
    git("clone", "-q", "--bare", "--filter=blob:none", "--no-checkout", "file://%s" % src, str(mirror), cwd=tmp_path)
    sha = head(src)
    assert _missing(mirror, sha) == 13                       # the fixture really is blob-less
    s = GitSource(mirror, sha, label="demo")
    fetches = [x["command"] for x in s.sources if " fetch " in x["command"]]
    assert len(fetches) == 1 and "--stdin" in fetches[0] and "--filter=blob:none" in fetches[0], s.sources
    assert _missing(mirror, sha) == 0
    assert s.text("src/a7.py") == "# note 7\nX = 7\n" and s.entry("README.md").size == len("# Demo\n")


def test_a_full_repository_fetches_nothing(tmp_path):
    repo = make_repo(tmp_path / "full", {"a.py": "X = 1\n"})
    s = GitSource(repo, head(repo), label="full")
    assert not [x for x in s.sources if " fetch " in x["command"]]
    assert s.text("a.py") == "X = 1\n"
