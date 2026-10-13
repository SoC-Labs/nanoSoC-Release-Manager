"""Second review of the versioning build (review items R1 to R8): each test is the reviewer's reproduction, and
each failed on d96f0da before its fix.

R1 an indented or numbered entry is never dropped; R2 no next version or draft over an unclassed entry; R3 the
phase-gate automation rule sees jobs of the real scheduler, by declared phase or by name; R4 an ambiguous
grandfathered baseline gives no next version; R5 a local checkout is never stored as the component's plan; R6 one
login fills one role of a step; R7 `version yank` keeps a column-0 yanked list valid; R8 CL13 reads the common
phrasings of a warning turned into a failure."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, versioning
from nanosoc_rm.cli import main
from nanosoc_rm.versioning import changelog as cl
from nanosoc_rm.versioning.repo import baseline
import vfix
from vfix import commit, git, mirror_from, tag

run = lambda *a, **k: CliRunner().invoke(main, list(a), **k)


def fact(obs):
    assert contracts.validate_observation(obs) == [], contracts.validate_observation(obs)
    return obs["fact"]


# ---------------------------------------------------------------------------------------------------------
# R1: indented and numbered entries
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C2")
@pytest.mark.requirement("C3")
def test_r1_an_indented_breaking_entry_is_kept_and_classes_the_release():
    doc = vfix.CHANGELOG_010.replace("## [Unreleased]\n", "## [Unreleased]\n### breaking\n  - Removed port HSEL_O.\n"
                                                         "### fix\n- small\n")
    assert [f["rule"] for f in cl.lint(doc)] == [], cl.lint(doc)
    got = cl.unreleased_classes(cl.parse(doc))
    assert [e["text"] for e in got.get("breaking", [])] == ["Removed port HSEL_O."], got
    for body in ("  - Removed port HSEL_O.\n", "1. Removed port HSEL_O.\n"):
        doc = vfix.CHANGELOG_010.replace("## [Unreleased]\n", "## [Unreleased]\n" + body)
        assert [(f["rule"], f["severity"]) for f in cl.lint(doc)] == [("CL8", "error")], (body, cl.lint(doc))


# ---------------------------------------------------------------------------------------------------------
# R2: an unclassed entry stops next and draft
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C2")
@pytest.mark.requirement("G1")
def test_r2_no_next_version_or_draft_over_an_unclassed_entry(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."]})
    text = (s["arch"] / "CHANGELOG.md").read_text().replace("## [Unreleased]\n",
                                                             "## [Unreleased]\n- Removed port HSEL_O.\n", 1)
    commit(s["arch"], {"CHANGELOG.md": text}, "2026-09-13T10:00:00Z", "unclassed entry")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"], vfix.ARCH_COPY: s["arch"]})
    assert fact(versioning.changelog_lint("nanosoc_arch_tech"))["verdict"] == "FAIL"
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert sorted(f["unreleased"]) == ["fix", "unclassed"]
    assert f["verdict"] == "FAIL" and any("changelog lint FAIL" in x for x in f["findings"]), f
    assert run("version", "next", "nanosoc_arch_tech").exit_code == 1
    with pytest.raises(versioning.VersioningUsage):
        versioning.draft("nanosoc_arch_tech", tmp_path / "d", date="2026-10-13")
    assert not (tmp_path / "d").exists()


# ---------------------------------------------------------------------------------------------------------
# R3: rule 5 of the phase-gate check on the real scheduler
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("A7")
@pytest.mark.parametrize("name,phase", [("nightly_matrix", "Phase 2"), ("versions_refresh", "Phase 3"),
                                        ("changelog_lint", "Phase 3"), ("bump_prs", "Phase 4"),
                                        ("lock_refresh", "Phase 5")])
def test_r3_an_enabled_later_phase_job_is_a_problem(monkeypatch, name, phase):
    from nanosoc_rm import phasegates, schedule
    assert phasegates.enabled_automation() == [], "v1 ships no later-phase job"
    monkeypatch.setattr(schedule, "JOBS", schedule.JOBS + (schedule.Job(name, "nightly", lambda c: None),))
    assert phasegates.enabled_automation() == [(name, phase)]
    reg = {"phases": [{"phase": p, "status": "not-met"} for p in phasegates.LATER_PHASES]}
    res = {r["phase"]: r for r in phasegates.check(reg, {}, [], [], [], automation=phasegates.enabled_automation())}
    assert any("automation `%s` is enabled" % name in p for p in res[phase]["problems"]), res[phase]


# ---------------------------------------------------------------------------------------------------------
# R4: the tidelink pattern, v1.0 (May) and the newer v0.90 (August)
# ---------------------------------------------------------------------------------------------------------
def _tidelink_like(root):
    w = vfix.work_repo(root, {"README.md": "x\n"}, date="2026-05-22T10:00:00Z")
    tag(w, "v1.0", date="2026-05-22T10:00:00Z")
    commit(w, {"README.md": "y\n"}, "2026-08-22T10:00:00Z")
    tag(w, "v0.90", date="2026-08-22T10:00:00Z")
    commit(w, {"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n### fix\n- small fix\n"}, "2026-09-01T10:00:00Z")
    return w


@pytest.mark.requirement("C1")
@pytest.mark.requirement("C9")
def test_r4_an_ambiguous_grandfathered_baseline_gives_no_next_version(tmp_path):
    w = _tidelink_like(tmp_path / "w")
    f = fact(versioning.plan(None, path=str(w), ref="HEAD"))
    assert f["current"]["tag"] == "v1.0" and f["current"].get("ambiguous") is True, f["current"]
    assert f["next"]["version"] is None and "ambiguous" in f["next"]["reason"]
    assert f["verdict"] == "UNVERIFIED", f
    # a person's confirmation (versioning.yaml grandfathered[].baseline) settles it
    tags = {"v1.0": {"commit": "a" * 40, "created_at": "2026-05-22T10:00:00Z"},
            "v0.90": {"commit": "b" * 40, "created_at": "2026-08-22T10:00:00Z"}}
    assert baseline(tags).get("ambiguous") is True
    b = baseline(tags, confirmed={"v0.90"})
    assert b["tag"] == "v0.90" and b["version"] == "0.90.0" and not b.get("ambiguous")
    assert not baseline({"v1.0": tags["v1.0"]}).get("ambiguous"), "one tag is never ambiguous"


# ---------------------------------------------------------------------------------------------------------
# R5: a local checkout's plan is never the component's plan
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C1")
def test_r5_a_local_checkout_is_never_stored_as_the_component_plan(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    assert run("version", "refresh", "nanosoc_arch_tech").exit_code == 0
    clone = tmp_path / "clone"
    git("clone", "-q", str(s["arch"]), str(clone))
    (clone / "CHANGELOG.md").write_text(vfix.unreleased({"breaking": ["Remove port HSEL_O."]}))
    r = run("version", "next", "nanosoc_arch_tech", "--path", str(clone), "--store")
    assert r.exit_code == 3 and "never stored" in r.output, r.output
    with pytest.raises(versioning.VersioningUsage):
        versioning.tag_check("nanosoc_arch_tech", path=str(clone), alerts=True)
    # one stored before the fix is still not the component's answer
    from nanosoc_rm.versioning import core
    core._store(versioning.plan("nanosoc_arch_tech", path=str(clone)), _cfg())
    row = next(r for r in versioning.overview(_cfg()) if r["component"] == "nanosoc_arch_tech")
    assert not row["plan"]["fact"]["worktree"] and row["plan"]["fact"]["next"]["version"] == "0.1.1"


def _cfg():
    from nanosoc_rm.config import load_config
    return load_config()


# ---------------------------------------------------------------------------------------------------------
# R6: one login, one role per step
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("G5")
def test_r6_one_login_never_fills_two_roles_of_a_step(tmp_path, monkeypatch):
    vfix.arch_site(tmp_path, monkeypatch, entries={"breaking": ["Remove HSEL_O."]},
                       extra_config='david = ["member", "release-manager", "owner:nanosoc_arch_tech"]\n')
    out = tmp_path / "D"
    versioning.draft("nanosoc_arch_tech", out, date="2026-10-13")
    st = versioning.approve(out, "tool-final-breaking", "david", session_login="david")
    assert st["state"] == "pending" and len(st["approvals"]) == 1
    with pytest.raises(versioning.VersioningUsage, match="another person"):
        versioning.approve(out, "tool-final-breaking", "david", session_login="david")


# ---------------------------------------------------------------------------------------------------------
# R7: yank into a column-0 yanked list
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("G6")
def test_r7_yank_keeps_a_column0_list_valid(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    (s["registry"] / "versioning.yaml").write_text(
        "schema: 1\nyanked:\n- {component: nanosoc_gen, version: 0.1.0, tag: v0.1.0, commit: %s, reason: x, by: d, "
        "date: '2026-10-01'}\n" % ("a" * 40))
    obs, patch = versioning.yank("nanosoc_arch_tech", "0.1.0", "bad", "david", date="2026-10-13")
    assert fact(obs)["refused"] is None and patch
    (tmp_path / "y.diff").write_text(patch)
    git("init", "-q", cwd=s["registry"])
    git("apply", str(tmp_path / "y.diff"), cwd=s["registry"])
    doc = contracts.load_yaml((s["registry"] / "versioning.yaml").read_text())
    assert contracts.validate("tool-release", doc, "/$defs/versioning_file") == []
    assert [(y["component"], str(y["version"])) for y in doc["yanked"]] == [("nanosoc_gen", "0.1.0"),
                                                                            ("nanosoc_arch_tech", "0.1.0")]


# ---------------------------------------------------------------------------------------------------------
# R8: CL13 phrasings
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C5")
@pytest.mark.parametrize("text,hit", [
    ("HSEL lint is now fatal.", True), ("HSEL lint now errors on overlap.", True),
    ("Treat lint warnings as errors.", True), ("Enable the HSEL lint check by default; it exits 1 on overlap.", True),
    ("HSEL lint fails by default.", True),
    ("Fix the flist order.", False), ("Fix a crash when the file is missing.", False),
    ("Fix the default HSEL decoder width.", False), ("Add a lint for overlap, off by default.", False),
    ("Errors now print the file name.", False)])
def test_r8_cl13_reads_the_common_phrasings(text, hit):
    doc = vfix.CHANGELOG_010.replace("## [Unreleased]\n", "## [Unreleased]\n### fix\n- %s\n" % text)
    assert ("CL13" in [f["rule"] for f in cl.lint(doc)]) is hit, text
