"""Mutation-killing tests for analysis.branches (robustness angle: mutation).

Scripted AST mutation over _verdict, _repo_rows, _reaching, _cite_holders, _tag_reaches, delete_list and
delete_commands (2026-10-14) left 16 of 83 mutants alive against test_analysis_branches.py: the default-branch
fallback (none recorded, or a recorded one that no longer exists), a repo with no branch, annotated tags, the
STALE boundary and delete_list's own guard were untested. Each test names the mutant it kills."""
from __future__ import annotations

import datetime as dt

import pytest

from nanosoc_rm import clock
from nanosoc_rm.analysis import branches as B
from nanosoc_rm.analysis.branches import branches, delete_list

from analysis_fixtures import Mirror, git, write_config
import test_analysis_branches
from test_analysis_branches import COMP, NEW, OLD, REPO, by_branch, dated

env = test_analysis_branches.env                 # the shared fixture, bound under its own name


@pytest.fixture
def bare(tmp_path, monkeypatch):
    _, data, _ = write_config(tmp_path, monkeypatch)
    return Mirror(data, COMP)


def _heads(m, heads):
    for name, sha in heads.items():
        m.set(REPO["host"], REPO["path"], "heads", name, sha)


@pytest.mark.parametrize("recorded", [None, "trunk"])
def test_an_unrecorded_or_vanished_default_falls_back_to_master(bare, recorded):
    """`default is None or default not in names`: kills `or -> and`, `is -> is not`, `not in -> in` and the
    negated test (each makes the rows use a default that is not there, or skips the fallback)."""
    c1, c2 = bare.chain(2)
    _heads(bare, {"master": c2, "feat/x": dated(bare, c2, NEW, "x")})
    if recorded:
        bare.head(REPO["host"], REPO["path"], recorded)
    docs = branches(COMP)
    assert docs.unanswered == []
    f = by_branch(docs)
    assert list(f) == ["feat/x"] and f["feat/x"]["default_branch"] == "master" and f["feat/x"]["ahead"] == 1
    assert any("master assumed" in s.get("command", "") for s in docs[0]["sources"])


def test_main_is_preferred_over_master_when_guessing(bare):
    """next(n for n in ("main", "master")): kills `in -> not in` in the guess."""
    c1, c2 = bare.chain(2)
    _heads(bare, {"master": c1, "main": c2})
    f = by_branch(branches(COMP))
    assert list(f) == ["master"] and f["master"]["default_branch"] == "main" and f["master"]["verdict"] == "MERGED"


def test_no_default_and_no_main_or_master_is_unanswered_not_a_crash(bare):
    """Kills `return [] -> return None` (an Answer cannot add None) and `is -> is not` on the guess."""
    c1, = bare.chain(1)
    _heads(bare, {"dev": c1, "feat/y": c1})
    docs = branches(COMP)
    assert list(docs) == []
    assert len(docs.unanswered) == 1 and "neither main nor master exists" in docs.unanswered[0]


def test_a_repo_with_no_branch_is_unanswered_not_a_crash(bare):
    """Kills `return [] -> return None` on the no-heads branch."""
    c1, = bare.chain(1)
    bare.set("gitlab", "soclabs/other", "heads", "main", c1)      # the mirror exists, this repo has no ref
    docs = branches(COMP)
    assert list(docs) == [] and "holds no branch of this repo" in docs.unanswered[0]


def test_an_annotated_tag_on_the_tip_protects_the_branch(bare):
    """tags_at is keyed by the PEELED commit. Kills the negated `peeled.ok` (the tag object's sha would key it)."""
    c1, c2 = bare.chain(2)
    tip = dated(bare, c2, OLD, "t")
    _heads(bare, {"main": c2, "feat/annotated": tip})
    bare.head(REPO["host"], REPO["path"], "main")
    tag = git("--git-dir", bare.path, "mktag",
              input="object %s\ntype commit\ntag rel-1\ntagger rm-unit <rm-unit@example.invalid> 0 +0000\n\nrelease\n"
              % tip)
    bare.set(REPO["host"], REPO["path"], "tags", "rel-1", tag)
    f = by_branch(branches(COMP))
    assert (f["feat/annotated"]["verdict"], f["feat/annotated"]["tags"]) == ("PROTECTED", ["rel-1"])
    assert f["feat/annotated"]["deletable"] is False


def test_stale_is_strictly_older_than_stale_days():
    """_verdict: `>` not `>=`. Kills `Gt -> GtE`."""
    now = clock.parse("2026-10-14T00:00:00Z")
    exactly = clock.iso(now - dt.timedelta(days=90))
    older = clock.iso(now - dt.timedelta(days=90, seconds=1))
    assert B._verdict(False, [], [], [], exactly, 90, now) == "ACTIVE"
    assert B._verdict(False, [], [], [], older, 90, now) == "STALE"


def _doc(**fact):
    base = {"deletable": True, "pins": [], "cited": [], "tags": [], "verdict": "STALE", "branch": "b",
            "repo": dict(REPO), "component": COMP, "default_branch": "main", "last_commit_at": "2025-01-01T00:00:00Z"}
    base.update(fact)
    return {"fact": base}


def test_delete_list_requires_the_deletable_flag_too():
    """delete_list re-checks every condition AND the flag. Kills `and -> or`."""
    assert delete_list([_doc(deletable=False)]) == []
    assert delete_list([_doc(verdict="ACTIVE")]) == []
    assert delete_list([_doc(pins=[{"commit": "x"}])]) == []
    assert [d["fact"]["branch"] for d in delete_list([_doc()])] == ["b"]


def test_derived_sources_name_the_anchors_and_cr10_only_where_they_apply(env):
    """Kills the negated `if pins or cites` / `if cites` / `if reach_c` source notes."""
    f = {d["fact"]["branch"]: d["sources"] for d in branches(COMP)}
    cmds = {b: [s.get("command", "") for s in srcs] for b, srcs in f.items()}
    assert all(any("registered pin(s)" in c for c in cs) for cs in cmds.values())
    assert any(c.startswith("CR-10:") for c in cmds["bench/cited"])
    assert not any(c.startswith("CR-10:") for c in cmds["feat/active"])


def test_a_recorded_default_that_exists_wins_over_main(bare):
    """Kills `is None -> is not None` (which would always guess main)."""
    c1, c2 = bare.chain(2)
    _heads(bare, {"main": c1, "develop": c2})
    bare.head(REPO["host"], REPO["path"], "develop")
    f = by_branch(branches(COMP))
    assert list(f) == ["main"] and f["main"]["default_branch"] == "develop" and f["main"]["verdict"] == "MERGED"


@pytest.mark.parametrize("recorded, words", [(None, "(never recorded)"), ("trunk", "(trunk)")])
def test_the_unanswered_reason_names_what_was_recorded(bare, recorded, words):
    """Kills `default or "never recorded"` -> `and`."""
    c1, = bare.chain(1)
    _heads(bare, {"dev": c1})
    if recorded:
        bare.head(REPO["host"], REPO["path"], recorded)
    assert words in branches(COMP).unanswered[0]


def test_a_tag_on_a_tip_settles_the_citation_by_rule_1_not_rule_3(tmp_path, monkeypatch):
    """A tag on a branch tip reaches every cited commit below it, so rule 1 (a tag reaches c: no branch holds it)
    always fires before rule 3's "already kept by a tag" pool. The tag half of `kept` (rows' r[7]) is therefore
    unreachable, and the `r[7] -> r[8]` mutant is equivalent. Pinned here so a change to rule 1 shows up."""
    from test_analysis_branches import BENCH_SHA
    _, data, registry = write_config(tmp_path, monkeypatch)
    m = Mirror(data, COMP)
    c1, c2 = m.chain(2)
    b1 = dated(m, c2, OLD, "b1")
    near = dated(m, b1, OLD, "n1")
    far = dated(m, dated(m, b1, OLD, "f1"), OLD, "f2")
    _heads(m, {"main": c2, "bench/near": near, "bench/far": far})
    m.set(REPO["host"], REPO["path"], "tags", "keep/far", far)
    m.head(REPO["host"], REPO["path"], "main")
    d = registry / "defects" / "SL-DEF-0001.yaml"
    d.write_text(d.read_text().replace(BENCH_SHA, b1))
    f = by_branch(branches(COMP))
    assert (f["bench/far"]["verdict"], f["bench/far"]["cited"]) == ("PROTECTED", [])
    assert (f["bench/near"]["verdict"], f["bench/near"]["cited"], f["bench/near"]["deletable"]) == ("STALE", [], True)


def test_delete_commands_print_the_date_only():
    """Kills `[:10] -> [:11]`."""
    (line,) = B.delete_commands([_doc(branch="old/x")])[1:]
    assert line == "git push origin --delete old/x   # STALE, last commit 2025-01-01"
