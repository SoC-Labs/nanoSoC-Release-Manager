"""analysis.branches (CR8, MVP S1): the branch inventory of one repo from its local mirror, on a hermetic
mirror where every verdict occurs. The delete-list never names a pinned, cited or tagged branch.

Mutation (run in a scratch copy, not committed): make `_reaching` return [] for pins and test_delete_list_...
goes red because feat/pinned (old, unmerged) then reads STALE and enters the delete-list."""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.analysis import CannotAnswer, UsageProblem
from nanosoc_rm.analysis.branches import branches, delete_commands, delete_list

from analysis_fixtures import Mirror, host_state, pin_doc, store_tree, write_config

COMP = "nanosoc-ethernet-chiplet"
REPO = {"host": "github", "path": "SoC-Labs/NanoSoC-Ethernet-Chiplet"}
OLD = "2025-01-01T00:00:00+0000"
NEW = "2026-10-01T00:00:00+0000"
BENCH_SHA = "6aafc943f2750be92e80b31be99f0cd60f8927e4"      # the example defect's regression_test commit


def cli_run(*args):
    r = CliRunner().invoke(cli.main, list(args))
    return r.exit_code, r.stdout, r


def dated(mirror, parent, date, msg):
    env = dict(os.environ, GIT_COMMITTER_DATE=date, GIT_AUTHOR_DATE=date)
    import subprocess
    p = subprocess.run(["git", "--git-dir", str(mirror.path), "commit-tree", mirror.empty_tree, "-p", parent, "-m", msg],
                       capture_output=True, text=True, check=True, env=env, input="")
    return p.stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """main c1-c2-c3 (recent); every verdict on a branch of its own; one pin and one citation into the mirror."""
    _, data, registry = write_config(tmp_path, monkeypatch)
    m = Mirror(data, COMP)
    c1, c2 = m.chain(2)
    c3 = dated(m, c2, NEW, "c3")
    heads = {"main": c3,
             "feat/merged": c2,                                 # MERGED, deletable
             "rel/merged-tagged": c1,                           # MERGED, but a tag on the tip: not in the delete list
             "feat/tagged": dated(m, c3, OLD, "t1"),            # PROTECTED by keep/eth-rc4/x on its tip
             "feat/pinned": dated(m, c3, OLD, "p1"),            # PINNED: a stored walk pins p1 (old, would be STALE)
             "bench/cited": dated(m, c3, OLD, "b1"),            # PINNED by the registry: the defect cites b1
             "old/stale": dated(m, c3, OLD, "s1"),              # STALE, deletable
             "feat/active": dated(m, c3, NEW, "a1")}            # ACTIVE
    for name, sha in heads.items():
        m.set(REPO["host"], REPO["path"], "heads", name, sha)
    m.set(REPO["host"], REPO["path"], "tags", "v0.1", c1)
    m.set(REPO["host"], REPO["path"], "tags", "keep/eth-rc4/x", heads["feat/tagged"])
    m.head(REPO["host"], REPO["path"], "main")
    # a stored walk of eth-main pins this component at p1 (and another pin at c1, which main already holds)
    store_tree("eth-main", [
        pin_doc("eth-main", "chiplet", COMP, heads["feat/pinned"],
                {"github": host_state(REPO, "canonical", True, "BRANCH_ONLY", branches=["feat/pinned"])}),
        pin_doc("eth-main", "chiplet-old", COMP, c1,
                {"github": host_state(REPO, "canonical", True, "ON_DEFAULT", lag=2, branches=["main"])})])
    # the registry's defect cites b1 as its regression bench (structured citation: regression_test.commit)
    d = registry / "defects" / "SL-DEF-0001.yaml"
    d.write_text(d.read_text().replace(BENCH_SHA, heads["bench/cited"]))
    return {"mirror": m, "heads": heads, "c1": c1, "c2": c2, "c3": c3}


def by_branch(docs):
    return {d["fact"]["branch"]: d["fact"] for d in docs}


def test_every_verdict_occurs_and_rows_are_valid_observations(env):
    docs = branches(COMP)
    assert docs.unanswered == []
    f = by_branch(docs)
    assert "main" not in f                                     # the default branch is not a row
    assert {k: v["verdict"] for k, v in f.items()} == {
        "feat/merged": "MERGED", "rel/merged-tagged": "MERGED", "feat/tagged": "PROTECTED", "feat/pinned": "PINNED",
        "bench/cited": "PINNED", "old/stale": "STALE", "feat/active": "ACTIVE"}
    assert (f["feat/merged"]["ahead"], f["feat/merged"]["behind"], f["feat/merged"]["merged"]) == (0, 1, True)
    assert (f["old/stale"]["ahead"], f["old/stale"]["behind"], f["old/stale"]["merged"]) == (1, 0, False)
    assert f["feat/tagged"]["tags"] == ["keep/eth-rc4/x"] == f["feat/tagged"]["keep_tags"]
    assert f["rel/merged-tagged"]["tags"] == ["v0.1"] and f["rel/merged-tagged"]["keep_tags"] == []
    assert f["feat/pinned"]["pins"] == [{"configuration": "eth-main", "tree_path": "chiplet", "commit": env["heads"]["feat/pinned"]}]
    assert f["feat/active"]["pins"] == [] and f["feat/pinned"]["cited"] == []
    assert f["bench/cited"]["cited"] == [{"file": "defects/SL-DEF-0001.yaml", "pointer": "/regression_test/commit",
                                         "commit": env["heads"]["bench/cited"]}]
    assert f["old/stale"]["last_commit_at"] == "2025-01-01T00:00:00Z" and f["old/stale"]["default_branch"] == "main"
    assert {k for k, v in f.items() if v["deletable"]} == {"feat/merged", "old/stale"}
    for d in docs:
        assert contracts.validate_observation(d) == [] and db.check_observation(d) == []
        assert d["fact"]["tip"] == env["heads"][d["fact"]["branch"]]
    assert len(db.latest("branch_state", "branch_state:github:SoC-Labs/NanoSoC-Ethernet-Chiplet:")) == 7


def test_delete_list_never_names_a_pinned_cited_or_tagged_branch(env):
    docs = branches(COMP)
    names = sorted(d["fact"]["branch"] for d in delete_list(docs))
    assert names == ["feat/merged", "old/stale"]
    for d in delete_list(docs):
        assert not d["fact"]["pins"] and not d["fact"]["cited"] and not d["fact"]["tags"]
    lines = delete_commands(docs)
    assert lines[0].startswith("# github:SoC-Labs/NanoSoC-Ethernet-Chiplet (nanosoc-ethernet-chiplet)")
    assert [l.split()[4] for l in lines[1:]] == ["feat/merged", "old/stale"]
    assert all(l.startswith("git push origin --delete ") for l in lines[1:])
    # the same old, unmerged shape as old/stale, held only by the pin or the citation: never deletable
    f = by_branch(docs)
    assert not f["feat/pinned"]["deletable"] and not f["bench/cited"]["deletable"] and not f["feat/tagged"]["deletable"]


def test_stale_days_moves_the_stale_active_line(env):
    f = by_branch(branches(COMP, stale_days=0))
    assert f["feat/active"]["verdict"] == "STALE" and f["feat/pinned"]["verdict"] == "PINNED"
    f = by_branch(branches(COMP, stale_days=100000))
    assert f["old/stale"]["verdict"] == "ACTIVE"


def test_cli_table_delete_list_and_exit_codes(env):
    code, out, _ = cli_run("branches", COMP)
    assert code == 1 and "feat/pinned" in out and "PINNED" in out and "STALE" in out
    code, out, _ = cli_run("branches", COMP, "--delete-list")
    assert code == 1
    assert [l.split()[4] for l in out.splitlines() if l.startswith("git push")] == ["feat/merged", "old/stale"]
    assert "feat/pinned" not in out and "feat/tagged" not in out and "bench/cited" not in out
    code, out, _ = cli_run("branches", "github:SoC-Labs/NanoSoC-Ethernet-Chiplet", "--json")
    docs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    assert code == 1 and len(docs) == 7 and all(d["kind"] == "branch_state" for d in docs)
    # nothing deletable: exit 0
    m = env["mirror"]
    for b in ("feat/merged", "old/stale"):
        m.delete(REPO["host"], REPO["path"], "heads", b)
    code, out, _ = cli_run("branches", COMP, "--delete-list")
    assert code == 0 and "nothing to delete" in out


def test_no_mirror_is_exit_2_and_unknown_target_exit_3(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    with pytest.raises(CannotAnswer):
        branches("tidechart")
    assert cli_run("branches", "tidechart")[0] == 2
    with pytest.raises(UsageProblem):
        branches("no-such-component")
    assert cli_run("branches", "gitlab:soclabs/nothing")[0] == 3


def test_all_repos_adds_the_copies(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    data = tmp_path / "data"
    m = Mirror(data, "tidelink")
    gh = {"host": "github", "path": "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"}
    gl = {"host": "gitlab", "path": "soclabs/tidelink"}
    c1, c2 = m.chain(2)
    m.set(gh["host"], gh["path"], "heads", "main", c2)
    m.set(gh["host"], gh["path"], "heads", "gh-only", c1)
    m.set(gl["host"], gl["path"], "heads", "main", c2)
    m.set(gl["host"], gl["path"], "heads", "gl-only", c1)
    for r in (gh, gl):
        m.head(r["host"], r["path"], "main")
    assert [d["fact"]["branch"] for d in branches("tidelink")] == ["gh-only"]
    docs = branches("tidelink", all_repos=True)
    assert sorted((d["fact"]["repo"]["host"], d["fact"]["branch"]) for d in docs) == [("github", "gh-only"), ("gitlab", "gl-only")]
    assert len(delete_commands(docs)) == 4                   # two headers, two commands


# ----- CR-10: a registry citation holds at most one branch ---------------------------------------------------------
@pytest.fixture
def shared(tmp_path, monkeypatch):
    """main c1-c2-c3; the defect cites b1 (off main); bench/near = b1-n1 and bench/far = b1-f1-f2 both reach it.
    Both are old and unmerged, so with nothing holding them both would be STALE and deletable.

    Mutation (scratch copy, not committed): make `_cite_holders` give every reaching branch the citation (the
    pre-CR-10 rule) and test_a_shared_citation_holds_only_the_nearest_branch goes red (bench/far PINNED); make it
    give a shared citation to no branch and test_deleting_every_deletable_branch_never_loses_a_cited_commit goes
    red (both benches deletable, b1 lost)."""
    _, data, registry = write_config(tmp_path, monkeypatch)
    m = Mirror(data, COMP)
    c1, c2 = m.chain(2)
    c3 = dated(m, c2, NEW, "c3")
    b1 = dated(m, c3, OLD, "b1")
    n1 = dated(m, b1, OLD, "n1")
    f2 = dated(m, dated(m, b1, OLD, "f1"), OLD, "f2")
    for name, sha in {"main": c3, "bench/near": n1, "bench/far": f2}.items():
        m.set(REPO["host"], REPO["path"], "heads", name, sha)
    m.head(REPO["host"], REPO["path"], "main")
    d = registry / "defects" / "SL-DEF-0001.yaml"
    d.write_text(d.read_text().replace(BENCH_SHA, b1))
    return {"mirror": m, "b1": b1, "n1": n1, "f2": f2}


def _cite(sha):
    return [{"file": "defects/SL-DEF-0001.yaml", "pointer": "/regression_test/commit", "commit": sha}]


def test_a_shared_citation_holds_only_the_nearest_branch(shared):
    f = by_branch(branches(COMP))
    assert (f["bench/near"]["verdict"], f["bench/near"]["cited"]) == ("PINNED", _cite(shared["b1"]))
    assert (f["bench/far"]["verdict"], f["bench/far"]["cited"], f["bench/far"]["deletable"]) == ("STALE", [], True)


def test_a_branch_already_kept_takes_the_citation(shared):
    store_tree("eth-main", [pin_doc("eth-main", "chiplet", COMP, shared["f2"],
                                    {"github": host_state(REPO, "canonical", True, "BRANCH_ONLY", branches=["bench/far"])})])
    f = by_branch(branches(COMP))
    assert f["bench/far"]["verdict"] == "PINNED" and f["bench/far"]["cited"] == _cite(shared["b1"])
    assert (f["bench/near"]["verdict"], f["bench/near"]["cited"], f["bench/near"]["deletable"]) == ("STALE", [], True)


def test_a_tag_reaching_the_cited_commit_holds_no_branch(shared):
    shared["mirror"].set(REPO["host"], REPO["path"], "tags", "keep/bench", shared["b1"])
    f = by_branch(branches(COMP))
    assert all(f[b]["cited"] == [] and f[b]["verdict"] == "STALE" for b in ("bench/near", "bench/far"))


def test_deleting_every_deletable_branch_never_loses_a_cited_commit(shared):
    import subprocess
    docs = branches(COMP)
    gone = {d["fact"]["branch"] for d in delete_list(docs)}
    left = [d["fact"]["tip"] for d in docs if d["fact"]["branch"] not in gone]
    assert left, "every branch reaching b1 was deletable"
    assert any(subprocess.run(["git", "--git-dir", str(shared["mirror"].path), "merge-base", "--is-ancestor",
                               shared["b1"], t]).returncode == 0 for t in left)
