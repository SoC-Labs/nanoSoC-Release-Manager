"""Coverage angle (rob/coverage): the walker's submodule URL classification (walker/urls.py, B9).

The walker must name the repo git itself would fetch. These tests pin each url_kind branch and compare relative
URL resolution with git's own answer (`git submodule init` writes the resolved URL into .git/config)."""
from __future__ import annotations

import os
import subprocess

import pytest

from nanosoc_rm.walker.urls import classify, host_names, join_relative, repo_url

HOSTS = {"github": {"web": "https://github.com", "git": "https://github.com/", "ssh": "git@github.com:"},
         "gitlab": {"ssh": "ssh://git@git.example.invalid:2222/"}}   # ssh:// form only
PARENT = "https://github.com/SoC-Labs/top.git"


def test_host_names_from_web_git_and_both_ssh_forms():
    names = host_names(HOSTS)
    assert names == {"github.com": "github", "git.example.invalid": "gitlab"}
    assert host_names({"x": {}}) == {}


@pytest.mark.parametrize("url,kind,named", [
    ("https://github.com/SoC-Labs/ip.git", "https", ("github", "SoC-Labs/ip")),
    ("https://GitHub.com/SoC-Labs/ip/", "https", ("github", "SoC-Labs/ip")),
    ("  git@github.com:SoC-Labs/ip.git  ", "ssh", ("github", "SoC-Labs/ip")),
    ("ssh://git@git.example.invalid:2222/grp/ip.git", "ssh", ("gitlab", "grp/ip")),
    ("git://github.com/SoC-Labs/ip.git", "other", ("github", "SoC-Labs/ip")),
    ("http://github.com/SoC-Labs/ip", "other", ("github", "SoC-Labs/ip")),
    ("https://github.com/only-owner", "other", None),          # no repo part
    ("https://elsewhere.invalid/a/b", "other", None),          # host not in the table
    ("git@elsewhere.invalid:a/b", "other", None),
    ("ftp://github.com/a/b", "other", None),
    ("/abs/path/repo", "local", None),
    ("file:///abs/path/repo", "local", None),
    ("../ip.git", "relative", ("github", "SoC-Labs/ip")),
])
def test_classify(url, kind, named):
    k, n, _resolved = classify(url, HOSTS, PARENT)
    assert (k, n) == (kind, named)


def test_relative_without_a_parent_names_no_repo():
    assert classify("../ip.git", HOSTS, None) == ("relative", None, "../ip.git")


def test_join_relative_https_and_scp():
    assert join_relative(PARENT, "../ip.git") == "https://github.com/SoC-Labs/ip.git"
    assert join_relative(PARENT + "/", "./a/./b") == "https://github.com/SoC-Labs/top.git/a/b"
    assert join_relative("git@github.com:SoC-Labs/top.git", "../ip") == "git@github.com:SoC-Labs/ip"
    assert repo_url(HOSTS, "github", "SoC-Labs/ip") == "https://github.com/SoC-Labs/ip.git"


def _git_resolves(tmp_path, parent: str, rel: str) -> str:
    """The URL git itself resolves `rel` to, for a superproject whose origin is `parent`."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    repo = tmp_path / "sp"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    git("config", "remote.origin.url", parent)
    (repo / ".gitmodules").write_text('[submodule "s"]\n\tpath = s\n\turl = %s\n' % rel)
    git("update-index", "--add", "--cacheinfo", "160000,%s,s" % ("1" * 40))
    git("add", ".gitmodules")
    git("submodule", "init", "-q")
    return git("config", "submodule.s.url").stdout.strip()


@pytest.mark.parametrize("parent,rel", [
    (PARENT, "../ip.git"), (PARENT, "../../Other/ip"), ("git@github.com:SoC-Labs/top.git", "../ip"),
    ("git@github.com:SoC-Labs/top.git", "../../Other/ip"), (PARENT, "./sub/ip"),
])
def test_relative_resolution_names_the_repo_git_fetches(tmp_path, parent, rel):
    ours = classify(rel, HOSTS, parent)[1]
    theirs = classify(_git_resolves(tmp_path, parent, rel), HOSTS)[1]
    assert ours == theirs and ours is not None


@pytest.mark.parametrize("parent,rel", [(PARENT, "../../../evil/x"), ("git@github.com:SoC-Labs/top.git",
                                                                      "../../../evil/x")])
def test_a_relative_url_that_climbs_past_the_host_is_not_judged_on_the_parents_host(tmp_path, parent, rel):
    ours = classify(rel, HOSTS, parent)[1]
    theirs = classify(_git_resolves(tmp_path, parent, rel), HOSTS)[1]
    assert ours == theirs
