"""Mutation-killing tests for analysis.contains (robustness angle: mutation).

Scripted AST mutation over nanosoc_rm/analysis/contains.py (2026-10-14) left 10 of 92 mutants alive against
test_analysis_contains.py. Two are equivalent (the prover's "no mirror" branch cannot be reached: contains()
only runs after the prefix resolved inside that same mirror). The rest are below; each test names its mutant."""
from __future__ import annotations

import pytest

from nanosoc_rm.analysis import UsageProblem
from nanosoc_rm.analysis.contains import contains, resolve_commit

from analysis_fixtures import git, store_mirror_statuses, store_tree
import test_analysis_contains
from test_analysis_contains import GEN, PATH_B

fx = test_analysis_contains.fx                         # the shared fixture, bound under its own name


def test_a_full_sha_of_a_tree_is_not_a_commit(fx):
    """_commits_in's 40-hex path: `r.ok and type == "commit"`. Kills `and -> or` (any existing object would
    resolve, so a tree id would be answered as a commit)."""
    tree = git("--git-dir", fx["g"].path, "rev-parse", "%s^{tree}" % fx["main"])
    assert len(tree) == 40
    with pytest.raises(UsageProblem, match="resolves to no commit"):
        resolve_commit(tree, "nanosoc_gen")


def test_resolution_cites_the_command_that_found_the_commit(fx):
    """resolve_commit appends the listing command only for a mirror that held the commit. Kills the negated
    `if shas`, and (via the 40-hex command) `len == 40 -> 41`."""
    _cid, _sha, sources = resolve_commit(fx["fix"][:7], "nanosoc_gen")
    assert len(sources) == 1 and "rev-parse --disambiguate=%s" % fx["fix"][:7] in sources[0]["command"]
    _cid, _sha, sources = resolve_commit(fx["fix"])
    assert [s["command"] for s in sources if "cat-file -t %s" % fx["fix"] in s["command"]], sources


def test_sources_carry_no_duplicate_proof(fx):
    """The query commit IS a pin here (`a`), so the pin's existence proof equals the query's. Kills
    `p_src and p_src not in sources -> or`."""
    (doc,) = contains(fx["a"], component="nanosoc_gen", configurations=["eth-rc4"])
    cmds = [s["command"] for s in doc["sources"]]
    assert len(cmds) == len(set(cmds)), cmds
    assert {p["tree_path"]: p["contains"] for p in doc["fact"]["pins"]}[PATH_B] is True


def test_an_empty_walk_says_so_in_its_derived_source(fx):
    """Kills `", ".join(sorted(snap)) or "an empty tree" -> and`."""
    store_tree("eth-main", [])
    (doc,) = contains(fx["fix"], component="nanosoc_gen", configurations=["eth-main"])
    assert doc["fact"]["answer"] == "absent"
    assert any(s["command"].endswith("(an empty tree)") for s in doc["sources"]), doc["sources"]


def test_a_failed_mirror_sync_ages_the_row_by_its_own_observation(fx):
    """A failed sync has last_success_at None: the row falls back to the status's observed_at (A3: never fresher
    than its data). Kills `last_success_at or observed_at -> and`."""
    store_mirror_statuses("nanosoc_gen", [(GEN, False)], at="2026-01-02T03:04:05Z")
    (doc,) = contains(fx["fix"], component="nanosoc_gen", configurations=["eth-rc4"])
    assert doc["observed_at"] == "2026-01-02T03:04:05Z"


def test_unproven_reasons_name_twelve_hex(fx):
    """The reason names sha[:12] exactly. Kills `12 -> 13`."""
    orphan = fx["g"].commit(fx["a"], msg="object in the mirror, on no ref")
    (doc,) = contains(orphan, component="nanosoc_gen", configurations=["eth-rc4"])
    reasons = {p["reason"] for p in doc["fact"]["pins"]}
    assert reasons == {"%s is not reachable from any branch or tag of gitlab (not proven)" % orphan[:12]}

