"""Unit tests for walker.states.HostJudge, branch by branch (robustness angle: mutation).

The only tests of HostJudge were the AT-B3 state table (test_mw_states.py): a full hermetic mirror sync and
walk, minutes per run on a loaded host, and one fixture per state. A mutation run cannot use it as its oracle,
so these tests drive HostJudge directly: a bare mirror built with raw git in the section 3 layout, stored
mirror_status rows, and the host API stubbed at `states.api_get`. Every first-match rule of the module
docstring has a test, and each test names the mutants it kills (scripted AST mutation, 2026-10-14)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanosoc_rm.config import load_config
from nanosoc_rm.mirror.layout import ApiAnswer
from nanosoc_rm.walker import states


def _analysis_fixtures():
    """tests/unit/analysis/analysis_fixtures.py (the raw-git mirror builder), loaded by path: that directory is
    not on sys.path from here."""
    name = "analysis_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "analysis" / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_af = _analysis_fixtures()
Mirror, git, store_mirror_statuses, write_config = _af.Mirror, _af.git, _af.store_mirror_statuses, _af.write_config

COMP = "fx-comp"
GH = {"host": "github", "path": "SoC-Labs/fx-comp"}
GL = {"host": "gitlab", "path": "soclabs/fx-comp"}


class FakeApi:
    """states.api_get stand-in: answers[path] = (status, body); anything else is a 404."""

    def __init__(self, answers=None):
        self.answers, self.calls = dict(answers or {}), []

    def __call__(self, config, host, path, params=None, *, url=None, client=None):
        self.calls.append((host, path))
        status, body = self.answers.get(path, (404, None))
        return ApiAnswer(status, body, {}, {"kind": "%s_api" % host, "command": "GET %s" % path},
                         None if status else "unreachable")


@pytest.fixture
def judge(tmp_path, monkeypatch):
    _, data, _ = write_config(tmp_path, monkeypatch)
    m = Mirror(data, COMP)
    api = FakeApi()
    monkeypatch.setattr(states, "api_get", api)
    j = states.HostJudge(load_config())
    yield SimpleNamespace(m=m, api=api, j=j, entry=SimpleNamespace(id=COMP, known_rewrites=[]))
    j.close()


def _state(fx, commit, repo=GH, entry=None):
    return fx.j.judge(fx.m.path, entry or fx.entry, repo, "canonical", True, commit)


def _synced(fx, repo=GH, ok=True):
    store_mirror_statuses(COMP, [(repo, ok)])


def _main(fx, n=3, repo=GH):
    shas = fx.m.chain(n)
    fx.m.set(repo["host"], repo["path"], "heads", "main", shas[-1])
    fx.m.head(repo["host"], repo["path"], "main")
    return shas


# ---- refs in the mirror ---------------------------------------------------------------------------------------
def test_tagged_needs_a_non_keep_tag_pointing_exactly_at_the_commit(judge):
    """`any(not t.startswith("keep/") for t in points)`: kills drop-not, `any -> all` (with a keep tag beside),
    and the `if tags` guard on the --points-at read."""
    c1, c2, c3 = _main(judge)
    _synced(judge)
    judge.m.set("github", GH["path"], "tags", "v1.0.0", c2)
    judge.m.set("github", GH["path"], "tags", "keep/rc4", c2)
    s = _state(judge, c2)
    assert (s["state"], s["lag"], s["tags"], s["branches"]) == ("TAGGED", 1, ["keep/rc4", "v1.0.0"], ["main"])


def test_a_keep_tag_alone_is_not_tagged(judge):
    c1, c2, c3 = _main(judge)
    _synced(judge)
    judge.m.set("github", GH["path"], "tags", "keep/rc4", c2)
    assert _state(judge, c2)["state"] == "ON_DEFAULT"
    off = judge.m.commit(c3, msg="off main")
    judge.m.set("github", GH["path"], "tags", "keep/off", off)
    assert _state(judge, off)["state"] == "PROTECTED_BY_TAG"


def test_on_default_reports_the_lag(judge):
    """Kills `int(...)` arithmetic and `lag is not None` mutants: the pin lacks 2 commits of main."""
    c1, c2, c3 = _main(judge)
    _synced(judge)
    s = _state(judge, c1)
    assert (s["state"], s["lag"], s["default_branch"]) == ("ON_DEFAULT", 2, "main")
    assert _state(judge, c3)["lag"] == 0


def test_a_tag_that_reaches_but_does_not_point_is_protected_not_tagged(judge):
    c1, c2, c3 = _main(judge)
    _synced(judge)
    x = judge.m.commit(c3, msg="x")
    y = judge.m.commit(x, msg="y")
    judge.m.set("github", GH["path"], "tags", "v2", y)
    s = _state(judge, x)
    assert (s["state"], s["tags"], s["lag"]) == ("PROTECTED_BY_TAG", ["v2"], None)


def test_branch_only_lists_the_branches_sorted(judge):
    c1, c2, c3 = _main(judge)
    _synced(judge)
    x = judge.m.commit(c3, msg="x")
    for b in ("zeta", "alpha"):
        judge.m.set("github", GH["path"], "heads", b, x)
    s = _state(judge, x)
    assert (s["state"], s["branches"], s["tags"]) == ("BRANCH_ONLY", ["alpha", "zeta"], [])


def test_only_this_hosts_namespace_counts(judge):
    """A tag of the GitLab copy never makes the GitHub repo TAGGED (README section 5)."""
    c1, c2, c3 = _main(judge)
    _main(judge, repo=GL)
    _synced(judge)
    x = judge.m.commit(c3, msg="x")
    judge.m.set("gitlab", GL["path"], "tags", "v9", x)
    judge.api.answers["repos/%s/commits/%s" % (GH["path"], x)] = (200, {})
    judge.api.answers["repos/%s/commits/%s/pulls" % (GH["path"], x)] = (200, [])
    assert _state(judge, x)["state"] == "ORPHAN"


# ---- rewritten --------------------------------------------------------------------------------------------------
def test_rewritten_by_the_hosts_reflog(judge):
    """An ancestor of an OLD head of the default branch: kills `returncode == 0 -> != 0`, the `exists.ok and
    default` guard and `return REWRITTEN -> None`."""
    c1, c2, c3 = _main(judge)
    _synced(judge)
    ref = judge.m.ref("github", GH["path"], "heads", "main")
    lost = judge.m.commit(c3, msg="force-pushed away")
    git("--git-dir", judge.m.path, "update-ref", "--create-reflog", "-m", "push", ref, lost)
    other = judge.m.commit(c3, msg="replacement")
    git("--git-dir", judge.m.path, "update-ref", "--create-reflog", "-m", "force", ref, other)
    s = _state(judge, lost)
    assert s["state"] == "REWRITTEN" and judge.api.calls == []


def test_rewritten_by_known_rewrites_of_this_host_only(judge):
    """catalogue known_rewrites: the host must match. Kills `rw["host"] == host -> !=` and `commit in lost ->
    not in`."""
    _main(judge)
    _synced(judge)
    gone = "1" * 40
    rw = {"host": "github", "branch": "main", "date": "2026-01-01", "lost": [gone]}
    entry = SimpleNamespace(id=COMP, known_rewrites=[dict(rw, host="gitlab"), rw])
    s = _state(judge, gone, entry=entry)
    assert s["state"] == "REWRITTEN" and judge.api.calls == []
    assert any("known_rewrites" in src["command"] for src in s["sources"])
    entry = SimpleNamespace(id=COMP, known_rewrites=[dict(rw, host="gitlab")])
    judge.api.answers["repos/%s" % GH["path"]] = (200, {})
    assert _state(judge, "2" * 40, entry=entry)["state"] == "MISSING"
    other = SimpleNamespace(id=COMP, known_rewrites=[dict(rw, host="gitlab")])
    assert states.HostJudge(judge.j.config).judge(judge.m.path, other, GH, "c", True, gone)["state"] == "MISSING"


# ---- the host's API ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("commit_status, prs, repo_status, want", [
    (200, (200, [{"number": 1}]), None, "PR_ONLY"),
    (200, (200, []), None, "ORPHAN"),
    (401, None, None, "UNREADABLE"),
    (403, None, None, "UNREADABLE"),
    (404, None, 200, "MISSING"),
    (422, None, 200, "MISSING"),
    (404, None, 404, "UNREADABLE"),
    (404, None, 403, "UNREADABLE"),
    (404, None, 401, "UNREADABLE"),
])
def test_ask_host_states(judge, commit_status, prs, repo_status, want):
    """_ask_host's table. Kills each status-set mutant (`in -> not in`, a dropped member) and the return values."""
    _main(judge)
    _synced(judge)
    x = "3" * 40
    base = "repos/%s" % GH["path"]
    judge.api.answers["%s/commits/%s" % (base, x)] = (commit_status, {})
    if prs:
        judge.api.answers["%s/commits/%s/pulls" % (base, x)] = prs
    if repo_status:
        judge.api.answers[base] = (repo_status, {})
    assert _state(judge, x)["state"] == want


@pytest.mark.parametrize("answers", [
    {"commits/X": (200, {}), "commits/X/pulls": (500, None)},
    {"commits/X": (200, {}), "commits/X/pulls": (200, {"not": "a list"})},
    {"commits/X": (500, None)},
    {"commits/X": (None, None)},
    {"commits/X": (404, None), "": (500, None)},
])
def test_an_answer_that_decides_nothing_is_a_walk_error(judge, answers):
    """Kills `Raise -> pass` (each would return None or fall through to a state) and `!= 200 -> == 200`."""
    _main(judge)
    _synced(judge)
    x = "4" * 40
    base = "repos/%s" % GH["path"]
    for k, v in answers.items():
        judge.api.answers[(base + "/" + k.replace("X", x)).rstrip("/")] = v
    with pytest.raises(states.WalkError):
        _state(judge, x)


def test_gitlab_api_paths_are_project_quoted(judge):
    """_repo_api_path / _ask_host GitLab branch: kills `host == "github" -> !=`."""
    _main(judge, repo=GL)
    _synced(judge, repo=GL)
    x = "5" * 40
    base = "projects/soclabs%2Ffx-comp"
    judge.api.answers["%s/repository/commits/%s" % (base, x)] = (200, {})
    judge.api.answers["%s/repository/commits/%s/merge_requests" % (base, x)] = (200, [{"iid": 3}])
    assert _state(judge, x, repo=GL)["state"] == "PR_ONLY"


# ---- never synced, failed sync ----------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 403, 404])
def test_never_synced_and_refused_by_the_host_is_unreadable(judge, status):
    judge.api.answers["repos/%s" % GH["path"]] = (status, None)
    s = _state(judge, "6" * 40)
    assert (s["state"], s["default_branch"]) == ("UNREADABLE", None)
    assert judge.api.calls == [("github", "repos/%s" % GH["path"])]


def test_never_synced_but_readable_is_a_walk_error(judge):
    """A readable repo with no mirror is missing data, never a state. Kills the `in (401, 403, 404)` mutants and
    `and view.status is None -> or`."""
    judge.api.answers["repos/%s" % GH["path"]] = (200, {})
    with pytest.raises(states.WalkError, match="never been synced"):
        _state(judge, "7" * 40)


def test_a_failed_sync_with_no_refs_is_unreadable_without_asking(judge):
    """status ok=False and no refs: UNREADABLE from the mirror alone (drop-not on `status.get("ok")`)."""
    _synced(judge, ok=False)
    s = _state(judge, "8" * 40)
    assert s["state"] == "UNREADABLE" and judge.api.calls == []
    assert "ok=False" in s["sources"][0]["command"]


def test_a_successful_sync_with_no_refs_asks_the_host(judge):
    """status ok=True and no refs (an empty repo): not UNREADABLE; the host's answer decides."""
    _synced(judge, ok=True)
    x = "9" * 40
    judge.api.answers["repos/%s" % GH["path"]] = (200, {})
    assert _state(judge, x)["state"] == "MISSING"


def test_a_failed_sync_that_left_refs_still_judges_from_them(judge):
    """has_refs wins over a failed latest sync: `not has_refs and not ok` -> `or` would make it UNREADABLE."""
    c1, c2, c3 = _main(judge)
    _synced(judge, ok=False)
    assert _state(judge, c2)["state"] == "ON_DEFAULT"


def test_judgements_are_cached_per_repo_and_commit(judge):
    _main(judge)
    _synced(judge)
    x = "a" * 40
    judge.api.answers["repos/%s/commits/%s" % (GH["path"], x)] = (200, {})
    judge.api.answers["repos/%s/commits/%s/pulls" % (GH["path"], x)] = (200, [])
    first = _state(judge, x)
    n = len(judge.api.calls)
    second = judge.j.judge(judge.m.path, judge.entry, GH, "copy", False, x)
    assert len(judge.api.calls) == n and second["state"] == first["state"] == "ORPHAN"
    assert (second["role"], second["pinned"]) == ("copy", False)


def test_refs_without_any_stored_sync_are_judged_from_the_refs(judge):
    """`not has_refs and status is None` (never synced) needs BOTH: kills `and -> or`."""
    c1, c2, c3 = _main(judge)
    assert _state(judge, c2)["state"] == "ON_DEFAULT" and judge.api.calls == []


def test_no_mirror_at_all_is_never_synced(judge, tmp_path):
    """A mirror path with no HEAD holds no refs. Kills `has_refs = False -> True`."""
    judge.api.answers["repos/%s" % GH["path"]] = (404, None)
    s = judge.j.judge(tmp_path / "no-such-mirror.git", judge.entry, GH, "canonical", True, "b" * 40)
    assert s["state"] == "UNREADABLE" and judge.api.calls == [("github", "repos/%s" % GH["path"])]


def test_an_unreached_commit_that_no_old_head_contains_is_asked_of_the_host(judge):
    """The reflog test needs `merge-base --is-ancestor` to answer 0. Kills `== 0 -> != 0` and `0 -> 1`."""
    c1, c2, c3 = _main(judge)
    _synced(judge)
    ref = judge.m.ref("github", GH["path"], "heads", "main")
    git("--git-dir", judge.m.path, "update-ref", "--create-reflog", "-m", "move", ref, judge.m.commit(c3, msg="n"))
    stray = judge.m.commit(c1, msg="never on main")
    judge.api.answers["repos/%s/commits/%s" % (GH["path"], stray)] = (200, {})
    judge.api.answers["repos/%s/commits/%s/pulls" % (GH["path"], stray)] = (200, [])
    assert _state(judge, stray)["state"] == "ORPHAN"


def test_never_synced_walk_error_names_the_answer_and_the_commit(judge):
    judge.api.answers["repos/%s" % GH["path"]] = (200, {})
    with pytest.raises(states.WalkError) as e:
        _state(judge, "c" * 40)
    assert "cannot judge %s there (the host answered 200;" % ("c" * 12) in str(e.value)


def test_a_ref_state_is_as_old_as_the_last_successful_sync(judge):
    """observed_at = mirror_status.last_success_at (A3). Kills `last_success_at or now -> and`."""
    c1, c2, c3 = _main(judge)
    store_mirror_statuses(COMP, [(GH, True)], at="2026-09-01T00:00:00Z")
    assert _state(judge, c2)["observed_at"] == "2026-09-01T00:00:00Z"
