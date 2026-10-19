"""Bump proposals in shadow mode (E1, E2, E3, E5, E7; interfaces.md section 19): the engine over a hermetic world
(bfix.py), the versions and ranges, the changelog class, the patch, the ledger, and the shadow-only guarantees."""
from __future__ import annotations

import json
import subprocess

import pytest

from bfix import CHANGELOG, GL_ARCH, GL_GEN, GL_MC, git, world
from bfix import bump_gate_passes

from nanosoc_rm import contracts

AUTOUSE = (bump_gate_passes,)          # the autouse fixture: a passing test-bump exists (Wave G bump gate)

pytestmark = pytest.mark.requirement("E1")


def _scan(cid="eth-main", **kw):
    from nanosoc_rm import bumps
    res = bumps.proposals(cid, **kw)
    return res[0], res[1:]


def _by_dep(props):
    return {p["fact"]["dependency"]["component"]: p["fact"] for p in props}


# ----- versions and ranges -----------------------------------------------------------------------------------
@pytest.mark.parametrize("tag,want", [("v0.2.1", "0.2.1"), ("0.23", "0.23.0"), ("v1.0", "1.0.0"), ("V1.2.3", "1.2.3"),
                                      ("v1.0-rc1", "1.0.0-rc1"), ("keep/eth/x", None), ("rtl-freeze-2026", None),
                                      ("v2026.07.16-chiplet-verified", None), ("v2026.7.16-label", "2026.7.16-label"), ("v01.2.3", None)])
def test_tags_parse_as_versions(tag, want):
    from nanosoc_rm.bumps.versions import parse_tag
    v = parse_tag(tag)
    assert (str(v) if v else None) == want
    if v is not None and "-" in tag:
        assert not v.release, "a pre-release is never a release"


@pytest.mark.parametrize("rng,ver,ok", [
    ("^0.1", "0.1.9", True), ("^0.1", "0.2.0", False), ("^0.1.2", "0.1.1", False), ("^1.2", "1.9.0", True),
    ("^1.2", "2.0.0", False), ("^0.0.3", "0.0.4", False), ("~1.2", "1.2.9", True), ("~1.2", "1.3.0", False),
    (">=0.1.0, <0.3.0", "0.2.5", True), (">=0.1.0, <0.3.0", "0.3.0", False), ("=1.2.3", "1.2.4", False),
    ("1.2.3", "1.9.0", True), ("*", "9.9.9", True), ("0.1.*", "0.1.7", True), ("0.1.*", "0.2.0", False),
    ("^0.2", "0.2.0-rc.1", False), (">=0.2.0-rc.1", "0.2.0-rc.1", True), ("<=1.2", "1.2.9", True), (">1.2", "1.2.9", False)])
def test_cargo_ranges(rng, ver, ok):
    from nanosoc_rm.bumps.versions import allows, parse_version
    assert allows(rng, parse_version(ver)) is ok


@pytest.mark.parametrize("bad", ["", "^", "lots", ">=0.1.0; <1", "1.*.3", "^0.1 || ^0.2"])
def test_a_range_that_does_not_parse_is_an_error_never_a_guess(bad):
    from nanosoc_rm.bumps.versions import RangeError, allows, parse_version
    with pytest.raises(RangeError):
        allows(bad, parse_version("0.1.0"))


def test_changelog_class_is_the_most_severe_declared_and_unknown_when_any_section_is_silent():
    from nanosoc_rm.bumps import changelog as clog
    from nanosoc_rm.bumps.versions import parse_version
    secs = clog.excerpt(CHANGELOG, parse_version("0.1.0"), parse_version("0.2.0"))
    assert [str(v) for v, _ in secs] == ["0.2.0", "0.1.1"]
    assert [clog.declared_class(s) for _, s in secs] == ["feature", "fix"]
    assert clog.declared_class("## 0.3.0\n- something") is None
    assert clog.excerpt(CHANGELOG, None, parse_version("0.2.0"))[0][0] == parse_version("0.2.0")


# ----- the engine ------------------------------------------------------------------------------------------
def test_eth_main_proposals_deepest_first_with_changelog_defects_and_waits_for(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    scan, props = _scan()
    for d in [scan] + props:
        assert contracts.validate_observation(d) == [], d["subject"]
    st = {r["tree_path"]: r["status"] for r in scan["fact"]["rows"]}
    assert st == {"nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen": "proposal",
                  "nanosoc-multicore-system/nanosoc_arch_tech": "proposal",
                  "nanosoc-multicore-system": "no-release-tag", "tidelink": "no-release-tag"}
    by = _by_dep(props)
    gen, arch = by["nanosoc_gen"], by["nanosoc_arch_tech"]
    assert [p["fact"]["dependency"]["component"] for p in props] == ["nanosoc_gen", "nanosoc_arch_tech"]
    assert (gen["order"], arch["order"]) == (1, 2), "E1: deepest first"
    assert arch["waits_for"] == [gen["id"]] and gen["waits_for"] == []
    # the move: from the gitlink at the consumer's branch tip, to the newest RELEASE (never the rc)
    assert gen["consumer"] == {"component": "nanosoc_arch_tech", "repo": GL_ARCH, "branch": "main", "base": w["a"][0],
                               "configured": w["a"][0], "at_configured": True}
    assert (gen["from"]["commit"], gen["from"]["tag"], gen["from"]["exact"]) == (w["g"][0], "v0.1.0", True)
    assert (gen["to"]["commit"], gen["to"]["tag"], gen["to"]["repo"]) == (w["g"][3], "v0.2.0", GL_GEN)
    assert gen["change"]["class"] == "feature" and gen["change"]["class_source"] == "changelog"
    assert "## [0.2.0]" in gen["change"]["changelog"]["excerpt"] and "## [0.1.1]" in gen["change"]["changelog"]["excerpt"]
    assert "## [0.1.0]" not in gen["change"]["changelog"]["excerpt"]
    assert gen["change"]["commits"]["count"] == 3 and not gen["change"]["diverged"]
    # review item 7: SL-DEF-0042's origin (g2) and its fix (g3) both lie between g1 and v0.2.0: neither
    assert gen["defects"]["fixed"] == [] and gen["defects"]["introduced"] == []
    assert gen["defects"]["within_range"] == [{"id": "SL-DEF-0042", "title": gen["defects"]["within_range"][0]["title"],
                                              "commits": sorted([w["g"][1], w["g"][2]])}]
    assert gen["matrix"]["verdict"] is None and gen["matrix"]["pins"] == [
        "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=%s" % w["g"][3]]
    assert arch["from"]["tag"] is None and arch["to"]["tag"] == "v0.5.0" and arch["consumer"]["repo"] == GL_MC
    assert arch["consumer"]["branch"] == "master"
    for p in (gen, arch):
        d = p["decision"]
        assert (d["mode"], d["auto_merge"], d["goes_to"], d["would"]) == ("shadow", False, "person", "open-pull-request")
        assert d["blockers"][0].startswith("shadow mode")
        assert p["patch"]["regenerated"] is False and "generator" in p["patch"]["note"]
    assert not any("introduces SL-DEF-0042" in b for b in gen["decision"]["blockers"])


def test_a_defect_already_in_the_pin_is_not_introduced_and_a_fix_already_in_it_is_not_claimed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][2]}, msg="pin g3: has the origin and the fix")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    gen = _by_dep(_scan()[1])["nanosoc_gen"]
    assert gen["from"]["commit"] == w["g"][2] and gen["from"]["tag"] == "v0.1.1" and not gen["from"]["exact"]
    assert gen["to"]["tag"] == "v0.2.0"
    assert gen["defects"] == {"fixed": [], "introduced": [], "unjudged": [], "within_range": []}


def test_one_gitlink_reached_through_two_tree_paths_is_one_proposal(tmp_path, monkeypatch):
    from bfix import pin, store_walk
    from nanosoc_rm import db
    w = world(tmp_path, monkeypatch)
    docs = db.latest("pin_state", "pin_state:eth-main:")
    for d in docs:
        d.pop("snapshot")
    archp = {"component": "nanosoc_arch_tech", "repo": GL_ARCH, "commit": w["a"][0]}
    docs.append(pin("eth-main", "tidelink/deps/nanosoc_arch_tech/nanosoc_gen", "nanosoc_gen", 3, archp, "nanosoc_gen",
                    GL_GEN, w["g"][0]))
    store_walk("eth-main", docs)
    scan, props = _scan()
    gens = [p["fact"] for p in props if p["fact"]["dependency"]["component"] == "nanosoc_gen"]
    assert len(gens) == 1 and gens[0]["tree_paths"] == ["nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen",
                                                        "tidelink/deps/nanosoc_arch_tech/nanosoc_gen"]
    assert len(gens[0]["matrix"]["pins"]) == 2, "the what-if moves the pin at every tree path"
    dup = [r for r in scan["fact"]["rows"] if r["status"] == "duplicate"]
    assert len(dup) == 1 and dup[0]["proposal"] == gens[0]["id"]


def test_a_declared_range_holds_the_proposal_to_what_it_allows(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch, manifest_range="^0.1")
    _, props = _scan()
    gen = _by_dep(props)["nanosoc_gen"]
    assert gen["to"]["tag"] == "v0.1.1" and gen["newest"]["tag"] == "v0.2.0"
    assert gen["range"]["declared"] == "^0.1" and gen["range"]["allows"] is True
    assert gen["range"]["source"].startswith("nanosoc_arch_tech:nanosoc-component.yaml@")
    # v0.1.1's own commit carries no changelog: the class is unknown, never guessed from the version number
    assert gen["change"]["class"] == "unknown" and "no changelog" in gen["change"]["class_detail"]


def test_a_range_that_allows_nothing_newer_holds_the_pin(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch, manifest_range="=0.1.0")
    scan, props = _scan()
    assert "nanosoc_gen" not in _by_dep(props)
    row = next(r for r in scan["fact"]["rows"] if r["component"] == "nanosoc_gen")
    assert row["status"] == "held-by-range" and row["tag"] == "v0.2.0"


def test_a_pin_that_already_holds_the_newest_release_is_up_to_date(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][3]}, msg="a person moved to v0.2.0")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    scan, props = _scan()
    assert "nanosoc_gen" not in _by_dep(props)
    row = next(r for r in scan["fact"]["rows"] if r["component"] == "nanosoc_gen")
    assert (row["status"], row["tag"]) == ("up-to-date", "v0.2.0")
    # the consumer's branch moved past the configured commit: the arch_tech proposal still names both
    arch = _by_dep(props)["nanosoc_arch_tech"]
    assert arch["consumer"]["at_configured"] is True


def test_a_tag_not_on_the_pinned_repo_is_blocked_and_held(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, arch_tag_on="github")
    scan, props = _scan()
    arch = _by_dep(props)["nanosoc_arch_tech"]
    assert arch["state"] == "blocked" and arch["decision"]["would"] == "hold"
    assert "gitlab:soclabs/nanosoc-m0/nanosoc_arch_tech" in arch["reason"]
    assert any(r["status"] == "blocked" for r in scan["fact"]["rows"])
    assert w  # the github namespace carries v0.5.0; the copy the .gitmodules names does not


def test_frozen_and_silicon_configurations_are_never_proposed(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    scan, props = _scan("eth-rc4")
    f = scan["fact"]
    assert props == [] and f["eligible"] is False and f["rows"] == [] and f["proposals"] == []
    assert "silicon" in f["refused"] and "E3" in f["refused"]
    # and the schema refuses a frozen scan that carries a proposal
    bad = json.loads(json.dumps(scan))
    bad["fact"]["proposals"] = ["bp-0123456789ab"]
    assert contracts.validate_observation(bad)


def test_release_branches_are_never_targets(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w["arch"].head(GL_ARCH, "release/0.5")
    w["arch"].set(GL_ARCH, "heads", "release/0.5", w["a"][0])
    scan, props = _scan()
    assert "nanosoc_gen" not in _by_dep(props)
    assert next(r for r in scan["fact"]["rows"] if r["component"] == "nanosoc_gen")["status"] == "release-branch"


def test_an_unknown_configuration_is_usage_and_an_unwalked_one_cannot_answer(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    world(tmp_path, monkeypatch)
    with pytest.raises(bumps.BumpUsage):
        bumps.proposals("no-such-config")
    with pytest.raises(bumps.CannotAnswer):
        bumps.proposals("multicore-master")


def test_ids_are_stable_and_shared_across_runs(tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    _, a = _scan()
    _, b = _scan()
    assert [p["fact"]["id"] for p in a] == [p["fact"]["id"] for p in b]


def test_a_stored_compat_what_if_is_the_matrix_cell(tmp_path, monkeypatch):
    from nanosoc_rm import jobs
    from nanosoc_rm.config import load_config
    w = world(tmp_path, monkeypatch)
    cfg = load_config()
    pin = "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=%s" % w["g"][3]
    ran = {"schema": 1, "kind": "compat_result", "fact": {"configuration": "eth-main", "commit": w["r1"]}}
    job = jobs.submit(cfg, "compat", {"configuration": "eth-main", "pins": [pin], "tier": "t1"}, "alice")
    jobs._update(cfg, job["id"], state="done", verdict="FAIL", finished_at="2026-10-02T10:00:00Z", result=ran)
    other = jobs.submit(cfg, "compat", {"configuration": "eth-main", "pins": [pin.replace(w["g"][3], w["g"][1])]}, "alice")
    jobs._update(cfg, other["id"], state="done", verdict="PASS", finished_at="2026-10-02T10:00:00Z", result=ran)
    gen = _by_dep(_scan()[1])["nanosoc_gen"]
    assert (gen["matrix"]["verdict"], gen["matrix"]["source"]) == ("FAIL", "compat-job")
    assert gen["matrix"]["href"] == "/compat?job=%d" % job["id"]
    assert any("T0-T2 on the exact commits: FAIL" in b for b in gen["decision"]["blockers"])


def test_a_bump_writer_credential_or_auto_merge_is_a_problem_never_used(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    cfg = w["cfg"]
    cfg.write_text(cfg.read_text() + '\n[credentials.bump_writer]\ntoken_file = "/nonexistent"\n')
    elig = w["registry"] / "eligibility.yaml"
    elig.write_text(elig.read_text().replace("auto_merge: {}", "auto_merge: {nanosoc_arch_tech: [fix]}"))
    scan, props = _scan()
    probs = " | ".join(scan["fact"]["problems"])
    assert "bump_writer" in probs and "auto_merge is not empty" in probs
    gen = _by_dep(props)["nanosoc_gen"]
    assert gen["decision"]["auto_merge"] is False
    assert any("shadow mode ignores it" in b for b in gen["decision"]["blockers"])


def test_the_schedule_ships_disabled_and_names_the_phase_4_gate():
    from nanosoc_rm import bumps, schedule
    s = bumps.schedule_status()
    assert s["enabled"] is False and s["gate"] == "Phase 4" and s["gate_status"] == "not-met"
    assert contracts.validate("bump-proposal", s, "/$defs/schedule") == []
    assert contracts.validate("bump-proposal", dict(s, enabled=True), "/$defs/schedule")
    assert not [j for j in schedule.JOBS if "bump" in j.name], "the scheduler must not run bumps before Phase 4"


# ----- the patch ------------------------------------------------------------------------------------------
def test_the_patch_applies_to_the_consumer_base_with_git_apply_index(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    gen = _by_dep(_scan()[1])["nanosoc_gen"]
    text = bumps.patch_text(gen)
    assert "SHADOW MODE" in text and "git apply --index" in text and "never runs" in text
    assert text.endswith("+Subproject commit %s\n" % w["g"][3])
    work = tmp_path / "consumer"
    git("init", "-q", work)
    (work / ".git" / "objects" / "info" / "alternates").write_text(str(w["arch"].path / "objects") + "\n")
    git("read-tree", w["a"][0], cwd=work)
    (work / "p.diff").write_text(text)
    git("apply", "--cached", "--check", "p.diff", cwd=work)
    git("apply", "--cached", "p.diff", cwd=work)
    assert git("ls-files", "-s", "nanosoc_gen", cwd=work).split()[1] == w["g"][3]


def test_the_patch_does_not_apply_to_a_branch_that_moved_on(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    gen = _by_dep(_scan()[1])["nanosoc_gen"]
    work = tmp_path / "consumer"
    git("init", "-q", work)
    (work / ".git" / "objects" / "info" / "alternates").write_text(str(w["arch"].path / "objects") + "\n")
    git("update-index", "--add", "--cacheinfo", "160000,%s,nanosoc_gen" % w["g"][1], cwd=work)
    (work / "p.diff").write_text(bumps.patch_text(gen))
    p = subprocess.run(["git", "apply", "--cached", "--check", "p.diff"], cwd=work, capture_output=True, text=True)
    assert p.returncode != 0


# ----- storage, marks and the ledger -----------------------------------------------------------------------
def test_the_ledger_compares_what_the_bot_would_do_with_what_people_did(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    _, props = _scan(store=True)
    gen, arch = (_by_dep(props)[k] for k in ("nanosoc_gen", "nanosoc_arch_tech"))
    led = bumps.ledger()["fact"]
    assert contracts.validate_observation(bumps.ledger()) == []
    assert led["counts"]["pending"] == 2 and led["counts"]["decided"] == 0 and led["agreement_rate"] is None
    assert led["threshold"]["met"] is False and led["auto_merge"]["enabled"] is False
    # a person applies the gen proposal exactly; another moves arch_tech in multicore to an untagged commit
    a3 = w["arch"].commit(w["a"][0], links={"nanosoc_gen": w["g"][3]}, msg="person: bump nanosoc_gen to v0.2.0")
    w["arch"].set(GL_ARCH, "heads", "main", a3)
    a4 = w["arch"].commit(w["a"][1], links={"nanosoc_gen": w["g"][0]}, msg="untagged work")
    m2 = w["mc"].commit(w["m1"], links={"nanosoc_arch_tech": a4}, msg="person: pin arch_tech work")
    w["mc"].set(GL_MC, "heads", "master", m2)
    led = bumps.ledger()["fact"]
    out = {e["id"]: e for e in led["entries"]}
    assert out[gen["id"]]["outcome"] == "agreed" and out[gen["id"]]["person"]["commit"] == a3
    assert out[arch["id"]]["outcome"] == "disagreed" and "no release tag" in out[arch["id"]]["note"]
    assert led["counts"]["agreed"] == 1 and led["counts"]["disagreed"] == 1 and led["agreement_rate"] == 0.5
    assert led["by_class"] == {"feature": {"agreed": 1, "disagreed": 0}, "unknown": {"agreed": 0, "disagreed": 1}}


def test_marks_are_logged_and_a_decline_is_a_disagreement(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    world(tmp_path, monkeypatch)
    _, props = _scan(store=True)
    gen, arch = (_by_dep(props)[k]["id"] for k in ("nanosoc_gen", "nanosoc_arch_tech"))
    bumps.mark(gen, "alice", "done", "PR 12")
    bumps.mark(arch, "carol", "declined", "waits for the generator")
    with pytest.raises(bumps.BumpUsage):
        bumps.mark(gen, "alice", "merged")
    with pytest.raises(bumps.BumpUsage):
        bumps.mark("bp-000000000000", "alice", "done")
    with pytest.raises(bumps.BumpUsage):
        bumps.mark(gen, "", "done")
    out = {e["id"]: e for e in bumps.ledger()["fact"]["entries"]}
    assert out[gen]["outcome"] == "pending" and "marked done by alice" in out[gen]["note"]
    assert out[arch]["outcome"] == "disagreed" and "declined by carol" in out[arch]["note"]
    assert [m["login"] for m in bumps.marks(gen)] == ["alice"]


def test_a_newer_proposal_supersedes_an_older_one_for_the_same_gitlink(tmp_path, monkeypatch):
    from nanosoc_rm import bumps
    w = world(tmp_path, monkeypatch)
    _, first = _scan(store=True)
    g6 = w["gen"].commit(w["g"][4], msg="g6 release 0.3.0")
    w["gen"].set(GL_GEN, "tags", "v0.3.0", g6)
    _, second = _scan(store=True)
    old, new = _by_dep(first)["nanosoc_gen"]["id"], _by_dep(second)["nanosoc_gen"]["id"]
    assert old != new
    out = {e["id"]: e for e in bumps.ledger()["fact"]["entries"]}
    assert out[old]["outcome"] == "superseded" and out[new]["outcome"] == "pending"
    assert {p["fact"]["id"] for p in bumps.stored("eth-main")} >= {old, new}


def test_shadow_mode_never_writes_outside_its_own_disk(tmp_path, monkeypatch):
    """Every git call the engine makes is a read: run the whole cycle with the runner recording argvs."""
    from nanosoc_rm import bumps, gitrun
    world(tmp_path, monkeypatch)
    seen = []
    real = gitrun.run_git

    def spy(*args, **kw):
        seen.append([str(a) for a in args])
        return real(*args, **kw)
    monkeypatch.setattr(gitrun, "run_git", spy)
    import nanosoc_rm.analysis._inputs as inp
    monkeypatch.setattr(inp, "run_git", spy)
    _, props = _scan(store=True)
    bumps.ledger()
    bumps.patch_text(props[0]["fact"])
    subs = {a[2] for a in seen if len(a) > 2 and a[0] == "-C"}
    assert seen and subs <= {"for-each-ref", "cat-file", "merge-base", "rev-parse", "ls-tree", "rev-list", "log",
                             "config"}, subs
    assert all("--get" in a for a in seen if len(a) > 2 and a[2] == "config"), "config is only ever read"
    assert not any(x in ("push", "update-ref", "tag", "branch", "commit") for a in seen for x in a)


def test_bump_gate_holds_a_proposal_without_a_passing_test_bump(tmp_path, monkeypatch):
    """Wave G decision 3: with no finished test-bump of exactly the move, the proposal holds, its second blocker
    names the command, and `test_bump` records that no evidence was found."""
    from nanosoc_rm.capabilities import gate
    monkeypatch.undo()                                    # drop bump_gate_passes: the real gate reads job records
    world(tmp_path, monkeypatch)
    assert gate.evidence.__module__ == "nanosoc_rm.capabilities.gate"
    scan, props = _scan()
    for d in props:
        assert contracts.validate_observation(d) == [], d["subject"]
        f = d["fact"]
        assert f["test_bump"]["verdict"] is None and f["decision"]["would"] == "hold"
        assert f["decision"]["blockers"][1].startswith("bump gate: no test-bump of exactly this move")
        assert "nanosoc-rm test-bump eth-main %s %s" % (f["tree_paths"][0], f["to"]["commit"][:12]) in \
            f["decision"]["blockers"][1]


@pytest.mark.requirement("E3")
@pytest.mark.requirement("PR6")
def test_bump_gate_lets_an_already_red_move_through_and_the_proposal_says_so(tmp_path, monkeypatch):
    """D12 (David 2026-10-19): a test-bump whose only reds were red before the move (WARN) does not hold the
    proposal; its second blocker line is the "already red before the bump" note instead of a hold."""
    from nanosoc_rm.capabilities import gate
    world(tmp_path, monkeypatch)
    note = "already red before the bump: a:lint is still red after it, so it does not hold the bump"
    monkeypatch.setattr(gate, "evidence", lambda config, configuration, tree_paths, to, root_commit: {
        "verdict": "WARN", "job": 7, "href": "/capabilities/eth-main/bump?job=7", "finished_at": None,
        "note": note})
    scan, props = _scan()
    proposed = [d["fact"] for d in props if d["fact"]["state"] == "proposed"]
    assert proposed, "the world has at least one proposal the gate alone decides"
    for d in props:
        assert contracts.validate_observation(d) == [], d["subject"]
    for f in proposed:
        assert f["decision"]["would"] == "open-pull-request"
        assert f["decision"]["blockers"][1] == "bump gate: WARN (job 7): " + note
        assert not any(b.startswith("bump gate: the newest") for b in f["decision"]["blockers"])
