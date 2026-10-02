"""Pure parts of the mirror and walker: URL normalisation, .gitmodules reading, namespace keys, credentials env."""
from __future__ import annotations

import pytest

from nanosoc_rm.config import load_config
from nanosoc_rm.mirror.catalogue import Catalogue
from nanosoc_rm.mirror.layout import git_env, ref_prefix, repo_key, split_key
from nanosoc_rm.walker.gitmodules import parse_gitmodules
from nanosoc_rm.walker.urls import classify, join_relative

HOSTS = {
    "github": {"web": "https://github.com", "api": "https://api.github.com", "ssh": "git@github.com:",
               "git": "https://github.com"},
    "gitlab": {"web": "https://gitlab.example.invalid", "api": "https://gitlab.example.invalid/api/v4",
               "ssh": "git@gitlab.example.invalid:", "git": "https://gitlab.example.invalid"},
}


@pytest.mark.parametrize("url, parent, want", [
    ("https://github.com/SoC-Labs/AHB-QSPI.git", None, ("https", ("github", "SoC-Labs/AHB-QSPI"))),
    ("https://github.com/SoC-Labs/AHB-QSPI/", None, ("https", ("github", "SoC-Labs/AHB-QSPI"))),
    ("https://GitLab.Example.Invalid/soclabs/nanosoc-m0/nanosoc_arch_tech.git", None,
     ("https", ("gitlab", "soclabs/nanosoc-m0/nanosoc_arch_tech"))),
    ("git@github.com:SoC-Labs/TideLink-Chiplet-Interconnect-AHB.git", None,
     ("ssh", ("github", "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"))),
    ("ssh://git@gitlab.example.invalid/soclabs/x.git", None, ("ssh", ("gitlab", "soclabs/x"))),
    ("../b.git", "https://gitlab.example.invalid/soclabs/a.git", ("relative", ("gitlab", "soclabs/b"))),
    ("../../other/b.git", "https://gitlab.example.invalid/soclabs/sub/a.git", ("relative", ("gitlab", "soclabs/other/b"))),
    ("../b.git", "git@github.com:SoC-Labs/a.git", ("relative", ("github", "SoC-Labs/b"))),
    ("./c.git", "https://github.com/SoC-Labs/a.git", ("relative", ("github", "SoC-Labs/a.git/c"))),  # as git
    ("/srv/fixture/x.git", None, ("local", None)),
    ("file:///srv/fixture/x.git", None, ("local", None)),
    ("https://example.org/someone/x.git", None, ("other", None)),
    ("git://github.com/SoC-Labs/x.git", None, ("other", ("github", "SoC-Labs/x"))),
])
def test_classify(url, parent, want):
    kind, repo, _resolved = classify(url, HOSTS, parent)
    assert (kind, repo) == want


def test_join_relative_like_git():
    assert join_relative("https://h.invalid/g/a.git", "../b.git") == "https://h.invalid/g/b.git"
    assert join_relative("git@h.invalid:g/a.git", "../b.git") == "git@h.invalid:g/b.git"


def test_parse_gitmodules():
    text = ('# comment\n[submodule "tidelink"]\n\tpath = tidelink\n\turl = git@github.com:SoC-Labs/T.git\n'
            '[submodule "with \\"quote\\""]\n\tPath = "deps/a b"  ; trailing comment\n\turl = https://x.invalid/a.git\n'
            '\tbranch = main\n[core]\n\tbare = false\n[submodule.legacy]\n\tpath = old\n\turl = ../old.git\n')
    mods = parse_gitmodules(text)
    assert [m["name"] for m in mods] == ["tidelink", 'with "quote"', "legacy"]
    assert mods[0] == {"name": "tidelink", "path": "tidelink", "url": "git@github.com:SoC-Labs/T.git"}
    assert mods[1]["path"] == "deps/a b" and mods[1]["branch"] == "main"
    assert mods[2]["url"] == "../old.git"


def test_namespace_keys():
    assert repo_key("gitlab", "soclabs/nanosoc-m0/nanosoc_arch_tech") == "gitlab/soclabs%2Fnanosoc-m0%2Fnanosoc_arch_tech"
    assert ref_prefix("github", "SoC-Labs/AHB-QSPI") == "refs/r/github/SoC-Labs%2FAHB-QSPI"
    assert split_key("gitlab/soclabs%2Fip") == ("gitlab", "soclabs/ip")


def test_catalogue_alias_and_roles():
    cat = Catalogue({"schema": 1, "components": [
        {"id": "a", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/A"},
         "copies": [{"host": "gitlab", "path": "soclabs/a"}],
         "aliases": [{"host": "gitlab", "path": "soclabs/old/a"}, {"host": "github", "path": "SoC-Labs/A-old"}]}],
        "external": [{"id": "x", "repo": {"host": "github", "path": "someone/x"}}],
        "ignored": [{"repo": {"host": "github", "path": "SoC-Labs/.github"}, "reason": "housekeeping"}]}, None)
    assert cat.resolve("github", "soc-labs/a")[:2] == ("a", "canonical")          # case-insensitive
    assert cat.normalise("gitlab", "soclabs/old/a") == ("a", "copy", {"host": "gitlab", "path": "soclabs/a"})
    assert cat.normalise("github", "SoC-Labs/A-old")[1:] == ("canonical", {"host": "github", "path": "SoC-Labs/A"})
    assert cat.resolve("github", "someone/x")[:2] == ("x", "external")
    assert cat.resolve("github", "SoC-Labs/.github")[:2] == (None, "ignored")
    assert cat.resolve("github", "SoC-Labs/nope")[:2] == (None, "unclassified")
    assert [e.id for e in cat.entries()] == ["a", "x"]


def test_git_env_keeps_the_token_out_of_argv(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("s3cret-token\n")
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[hosts.github]\nweb = "https://github.com"\napi = "x"\nssh = "git@github.com:"\n'
                   '[hosts.gitlab]\nweb = "https://gl.invalid"\napi = "y"\nssh = "git@gl.invalid:"\ngit = "file:///srv/gl"\n'
                   '[credentials.github_read]\ntoken_file = "%s"\nscope = ["contents:read"]\nexpires = "2027-01-01"\n'
                   % (tmp_path / "data", tok))
    config = load_config(cfg)
    env = git_env(config, "github")
    assert env["NANOSOC_RM_GIT_TOKEN"] == "s3cret-token"
    keys = {env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")}
    assert keys == {"credential.https://github.com.helper"}
    assert all("s3cret" not in v for k, v in env.items() if k != "NANOSOC_RM_GIT_TOKEN")
    assert git_env(config, "gitlab") == {}          # no gitlab_read credential, and a file:// base
