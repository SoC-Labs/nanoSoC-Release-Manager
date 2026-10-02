"""nanosoc_rm.registry.normalise_url (B9): host names from the config's host table only."""
from __future__ import annotations

import pytest

from nanosoc_rm.registry import Repo, normalise_url

HOSTS = {
    "github": {"web": "https://github.com", "git": "https://github.com", "ssh": "git@github.com:", "org": "SoC-Labs"},
    "gitlab": {"web": "https://gitlab.example.invalid", "git": "file:///srv/fixture/gitlab",
               "ssh": "git@gitlab.example.invalid:", "group": "soclabs"},
}


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/SoC-Labs/TideChart.git", ("https", Repo("github", "SoC-Labs/TideChart"))),
    ("https://github.com/SoC-Labs/TideChart/", ("https", Repo("github", "SoC-Labs/TideChart"))),
    ("https://GitHub.com/SoC-Labs/TideChart", ("https", Repo("github", "SoC-Labs/TideChart"))),
    ("https://user:secret@github.com/SoC-Labs/TideChart.git", ("https", Repo("github", "SoC-Labs/TideChart"))),
    ("git@github.com:SoC-Labs/TideLink-Chiplet-Interconnect-AHB.git",
     ("ssh", Repo("github", "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"))),
    ("ssh://git@github.com/SoC-Labs/TideChart.git", ("ssh", Repo("github", "SoC-Labs/TideChart"))),
    ("ssh://git@gitlab.example.invalid:2222/soclabs/nanosoc-m0/nanosoc_arch_tech.git",
     ("ssh", Repo("gitlab", "soclabs/nanosoc-m0/nanosoc_arch_tech"))),
    ("https://gitlab.example.invalid/soclabs/chiplets/axi-chiplet-controller.git",
     ("https", Repo("gitlab", "soclabs/chiplets/axi-chiplet-controller"))),
    ("file:///srv/fixture/gitlab/soclabs/ip.git", ("other", Repo("gitlab", "soclabs/ip"))),   # the git base
    ("https://bitbucket.org/x/y.git", ("other", None)),                                      # unknown host
    ("git@bitbucket.org:x/y.git", ("other", None)),                     # unknown host: other (defs url_kind)
    ("/home/someone/ip.git", ("local", None)),
    ("file:///home/someone/ip.git", ("local", None)),
    ("git://github.com/SoC-Labs/TideChart.git", ("other", None)),
    ("https://github.com/SoC-Labs", ("https", None)),                                        # not a repo path
])
def test_normalise(url, expected):
    assert normalise_url(url, HOSTS) == expected


def test_accepts_a_whole_config_and_no_hosts():
    assert normalise_url("https://github.com/a/b", {"hosts": HOSTS}) == ("https", Repo("github", "a/b"))
    assert normalise_url("https://github.com/a/b", {}) == ("other", None)
    assert normalise_url("https://github.com/a/b", None) == ("other", None)


@pytest.mark.parametrize("url, parent, expected", [
    ("../TideChart.git", Repo("github", "SoC-Labs/TideLink"), Repo("github", "SoC-Labs/TideChart")),
    ("../../other/x.git", Repo("gitlab", "soclabs/chiplets/axi"), Repo("gitlab", "soclabs/other/x")),
    ("./sub/x", Repo("gitlab", "soclabs/parent"), Repo("gitlab", "soclabs/parent/sub/x")),
    ("../../../x.git", Repo("github", "a/b"), None),                                         # above the host root
])
def test_relative(url, parent, expected):
    assert normalise_url(url, HOSTS, parent) == ("relative", expected)
    assert normalise_url(url, HOSTS, parent.as_doc())[1] == expected


def test_relative_without_parent():
    assert normalise_url("../x.git", HOSTS) == ("relative", None)


def test_no_host_name_is_hard_coded():
    """A table that names other hosts: github.com is then an unknown host."""
    other = {"github": {"web": "https://ghe.example.invalid", "ssh": "git@ghe.example.invalid:"}}
    assert normalise_url("https://github.com/a/b", other) == ("other", None)
    assert normalise_url("git@ghe.example.invalid:a/b.git", other) == ("ssh", Repo("github", "a/b"))
