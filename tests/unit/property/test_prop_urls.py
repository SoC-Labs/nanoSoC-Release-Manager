"""Property tests: URL normalisation (registry/urls.py normalise_url, t0/gitwork.py repo_key, resolve_relative).

The two normalisers serve different callers (the walker and registry vs the T0 gates) but must name the same
repo for the same URL, every spelling of one repo must normalise to one key, a credential never takes part in
matching, and a host is matched as a host, never as a string prefix (github.example.evil.example is not
github.example).
"""
from __future__ import annotations

import urllib.parse

from prop_helpers import example, given, st  # loads the profile; skips without hypothesis

from nanosoc_rm.registry.urls import REPO_PATH, Repo, normalise_url
from nanosoc_rm.t0 import gitwork

HOSTS = {"github": {"web": "https://github.example", "ssh": "git@github.example:"},
         "gitlab": {"web": "https://gitlab.example", "ssh": "git@gitlab.example:", "git": "file:///srv/mirror"}}
HOSTNAME = {"github": "github.example", "gitlab": "gitlab.example"}

SEG = (st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,8}", fullmatch=True)
       .filter(lambda s: not s.endswith(".git") and not s.endswith(".")))
PATH = st.lists(SEG, min_size=2, max_size=4).map("/".join)
HOST = st.sampled_from(sorted(HOSTNAME))


def spellings(h: str, p: str) -> list[str]:
    return ["https://%s/%s" % (h, p), "https://%s/%s.git" % (h, p), "https://%s/%s/" % (h, p),
            "HTTPS://%s/%s" % (h.upper(), p), "https://user:secret@%s/%s.git" % (h, p),
            "https://oauth2@%s/%s.git" % (h, p), "git@%s:%s.git" % (h, p), "ssh://git@%s/%s.git" % (h, p),
            "ssh://git@%s:22/%s" % (h, p), "  https://%s/%s.git \t" % (h, p)]


@given(HOST, PATH)
def test_every_spelling_of_one_repo_has_one_key(host, path):
    h = HOSTNAME[host]
    for url in spellings(h, path):
        kind, repo = normalise_url(url, HOSTS)
        assert repo is not None and repo.key() == (host, path.lower()), url
        assert kind == ("ssh" if "ssh" in url or url.startswith("git@") else "https"), url
        assert gitwork.repo_key(url) == ("%s/%s" % (h, path)).lower(), url


@given(st.text(max_size=60))
def test_never_raises_and_a_repo_path_is_always_valid(url):
    kind, repo = normalise_url(url, HOSTS)
    assert kind in ("https", "ssh", "local", "relative", "other")
    if repo is not None:
        assert repo.host in HOSTS and REPO_PATH.match(repo.path)
    assert gitwork.url_kind(url) in ("https", "ssh", "local", "relative", "other")
    gitwork.repo_key(url)
    gitwork.to_https(url)


_PART = st.sampled_from(["https://", "ssh://", "git@", "github.example", "gitlab.example", "evil.example", ".",
                         "@", ":", "/", "a", "b", ".git", "%2e", "\\", "#", "?", "user:pw@", ":22", "//", " "])
ADVERSARIAL = st.lists(_PART, min_size=1, max_size=9).map("".join)


@given(ADVERSARIAL)
@example("https://github.example.evil.example/o/r")
@example("https://github.example@evil.example/o/r")
@example("ssh://git@github.example.evil.example/o/r.git")
@example("https://gitlab.examplex/o/r")
def test_a_url_is_matched_by_its_host_never_by_a_prefix(url):
    """When a URL is attributed to a configured host, that host is the URL's real host name, and the T0 key
    names the same repo."""
    kind, repo = normalise_url(url, HOSTS)
    if repo is None:
        return
    want, s = HOSTNAME[repo.host], url.strip()
    if kind == "https" or (kind == "ssh" and s.lower().startswith("ssh://")):
        assert urllib.parse.urlsplit(s).hostname == want, url
    elif kind == "ssh":
        assert s.split("@", 1)[1].split(":", 1)[0].lower() == want, url
    assert gitwork.repo_key(url) == ("%s/%s" % (want, repo.path)).lower(), url


@given(HOST, PATH, st.lists(st.sampled_from(["../", "./"]), max_size=3), SEG)
def test_relative_urls_resolve_the_same_in_both_normalisers(host, path, more, tail):
    """git's rule: each ../ drops one component of the parent's URL. (A leading ./ is left out: git resolves it
    under the parent's URL including `.git`, which the registry's Repo no longer carries.)"""
    rel = "../" + "".join(more) + tail + ".git"
    _kind, repo = normalise_url(rel, HOSTS, parent=Repo(host, path))
    resolved = gitwork.resolve_relative(rel, "https://%s/%s.git" % (HOSTNAME[host], path))
    via_t0 = normalise_url(resolved, HOSTS)[1]
    assert (repo.key() if repo else None) == (via_t0.key() if via_t0 else None), (rel, path, resolved)
    ups = 1 + more.count("../")
    parts = path.split("/")
    if ups < len(parts) and len(parts) - ups + 1 >= 2:
        assert repo == Repo(host, "/".join(parts[:len(parts) - ups] + [tail]))


@given(HOST, PATH, st.sampled_from(["user:pw@", "x-token:abc@", "a@b@"]))
def test_credentials_never_change_the_match(host, path, cred):
    h = HOSTNAME[host]
    plain = normalise_url("https://%s/%s" % (h, path), HOSTS)
    assert normalise_url("https://%s%s/%s" % (cred, h, path), HOSTS) == plain
