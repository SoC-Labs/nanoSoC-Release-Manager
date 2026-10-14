"""Robustness (soak angle): how the walker reads a host API answer that is not a clean yes or no.

Found in a read-only soak on 2026-10-14 (demo data, every CLI command across the four configurations). The
anonymous GitHub quota (60 requests an hour) ran out part-way through. From then on, the walk stored
UNREADABLE for public repos that had answered minutes before:
- eth-rc4: ethernet-mac-ahb and socdebug_tech on github went from ORPHAN to UNREADABLE.
- multicore-master: the exit changed from 2 ("never synced, the host answered 200") to 1, with
  coresight_soc400_tech UNREADABLE.
`contains` then said "unknown" for those hosts, and silicon-protection listed the rows as uncovered and UNREADABLE.

Hermetic: `nanosoc_rm.walker.states.api_get` is replaced with a stub, so no network and no mirror are used.
The tests that record a defect are strict xfails. The tests without an xfail mark are the controls: they pin
the behaviour that is correct today, so a fix for a defect cannot quietly break it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nanosoc_rm.mirror.layout import ApiAnswer
from nanosoc_rm.walker import states
from nanosoc_rm.walker.states import HostJudge, WalkError, _RepoView

COMMIT = "44ff8b72654bc6953b97f86855fe50ad1a10e22c"
GH_REPO = {"host": "github", "path": "SoC-Labs/fx-public"}
GL_REPO = {"host": "gitlab", "path": "soclabs/fx-public"}

# What api.github.com answers an anonymous caller once the hourly quota is spent (REST docs, "rate limits"):
# 403 (sometimes 429) with x-ratelimit-remaining: 0 and a JSON message naming the limit.
RATE_LIMITED = (403, {"message": "API rate limit exceeded for 0.0.0.0. (But here's the good news: ...)",
                      "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api"},
                {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1791966643",
                 "content-type": "application/json; charset=utf-8"})
# A private repo read without a token: 404 for the repo (GitHub), 403/401 on some hosts. No rate-limit header.
PRIVATE = (403, {"message": "Forbidden"}, {"content-type": "application/json"})


def _stub(monkeypatch, answers: dict[str, tuple]) -> list[str]:
    """Replace the walker's api_get: `answers` maps a path suffix to (status, body, headers). Returns the call log."""
    calls: list[str] = []

    def fake(config, host, path, params=None, *, url=None, client=None):
        calls.append(path)
        for suffix, (status, body, headers) in answers.items():
            if path.endswith(suffix):
                return ApiAnswer(status, body, dict(headers), {"kind": "%s_api" % host, "command": "GET %s" % path})
        raise AssertionError("unexpected GET %s" % path)

    monkeypatch.setattr(states, "api_get", fake)
    return calls


def _judge() -> HostJudge:
    j = HostJudge(config=None)          # _ask_host only reads the config through api_get, which is stubbed
    j._client = object()                # never used by the stub; keeps _http() from opening a real client
    return j


# ---------------------------------------------------------------------------------------------- controls
def test_private_repo_403_is_unreadable(monkeypatch):
    """Control: a commit the host refuses (no rate-limit header) is UNREADABLE (README section 5)."""
    _stub(monkeypatch, {"/commits/" + COMMIT: PRIVATE})
    assert _judge()._ask_host(GH_REPO, COMMIT, []) == "UNREADABLE"


def test_commit_absent_from_readable_repo_is_missing(monkeypatch):
    """Control: 422 for the commit and 200 for the repo is MISSING (GitHub's answer for an unknown sha)."""
    _stub(monkeypatch, {"/commits/" + COMMIT: (422, {"message": "No commit found for SHA"}, {}),
                        "repos/" + GH_REPO["path"]: (200, {"full_name": GH_REPO["path"]}, {})})
    assert _judge()._ask_host(GH_REPO, COMMIT, []) == "MISSING"


def test_commit_with_no_pull_request_is_orphan(monkeypatch):
    """Control: the commit exists (JSON object) and no PR lists it: ORPHAN."""
    _stub(monkeypatch, {"/commits/" + COMMIT: (200, {"sha": COMMIT}, {}),
                        "/commits/%s/pulls" % COMMIT: (200, [], {})})
    assert _judge()._ask_host(GH_REPO, COMMIT, []) == "ORPHAN"


def test_server_error_is_could_not_answer(monkeypatch):
    """Control: a 5xx decides nothing, so the walk cannot answer (exit 2), never a red state."""
    _stub(monkeypatch, {"/commits/" + COMMIT: (502, None, {})})
    with pytest.raises(WalkError):
        _judge()._ask_host(GH_REPO, COMMIT, [])


def test_never_synced_readable_repo_is_could_not_answer(monkeypatch):
    """Control: the exit-2 path the soak saw for multicore-master while the quota lasted."""
    _stub(monkeypatch, {"repos/" + GH_REPO["path"]: (200, {"full_name": GH_REPO["path"]}, {})})
    j = _judge()
    monkeypatch.setattr(j, "view", lambda mirror, repo: _RepoView("k", False, None, None))
    entry = type("E", (), {"id": "fx-public", "known_rewrites": []})()
    with pytest.raises(WalkError, match="never been synced"):
        j._judge(Path("/nonexistent"), entry, GH_REPO, COMMIT)


# ---------------------------------------------------------------------------------------------- defects
@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason="ROB-soak-1: GitHub rate-limit 403 on a commit is stored as UNREADABLE "
                                       "(states.py _ask_host: 403 -> UNREADABLE without reading x-ratelimit-remaining)")
def test_rate_limited_commit_lookup_is_could_not_answer(monkeypatch):
    _stub(monkeypatch, {"/commits/" + COMMIT: RATE_LIMITED})
    with pytest.raises(WalkError):
        _judge()._ask_host(GH_REPO, COMMIT, [])


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason="ROB-soak-2: a rate-limited repo probe for a never-synced mirror turns "
                                       "exit 2 'never synced' into a stored UNREADABLE (states.py _judge, 401/403/404)")
def test_rate_limited_never_synced_repo_is_could_not_answer(monkeypatch):
    _stub(monkeypatch, {"repos/" + GH_REPO["path"]: RATE_LIMITED})
    j = _judge()
    monkeypatch.setattr(j, "view", lambda mirror, repo: _RepoView("k", False, None, None))
    entry = type("E", (), {"id": "fx-public", "known_rewrites": []})()
    with pytest.raises(WalkError):
        j._judge(Path("/nonexistent"), entry, GH_REPO, COMMIT)


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason="ROB-soak-1: a rate-limited repo probe after a 404/422 commit is stored as "
                                       "UNREADABLE (states.py _ask_host, the whole-repo branch)")
def test_rate_limited_repo_probe_after_missing_commit_is_could_not_answer(monkeypatch):
    _stub(monkeypatch, {"/commits/" + COMMIT: (404, {"message": "Not Found"}, {}),
                        "repos/" + GH_REPO["path"]: RATE_LIMITED})
    with pytest.raises(WalkError):
        _judge()._ask_host(GH_REPO, COMMIT, [])


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason="ROB-soak-3: a 200 whose body is not JSON (a proxy or sign-in page) counts "
                                       "as 'the host has the commit' (states.py _ask_host: status only, body unread)")
def test_non_json_200_for_commit_is_not_orphan(monkeypatch):
    _stub(monkeypatch, {"/repository/commits/" + COMMIT: (200, None, {"content-type": "text/html"}),
                        "/repository/commits/%s/merge_requests" % COMMIT: (200, [], {})})
    with pytest.raises(WalkError):
        _judge()._ask_host(GL_REPO, COMMIT, [])
