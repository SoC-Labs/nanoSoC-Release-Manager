"""The phase-gate check (A7) and `nanosoc-rm trace` (PR7)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from nanosoc_rm import phasegates, trace
from webfix import REPO, git

LATER = phasegates.LATER_PHASES


def gates(status="not-met", checked=None, drop=None):
    return {"phases": [{"phase": ph, "capabilities": [], "status": status if ph == "Phase 2" else "not-met",
                        "entry_gate": [{"criterion": "c", "check": "k"}],
                        "checked": (checked or {}) if ph == "Phase 2" else {}}
                       for ph in LATER if ph != drop]}


def scratch_repo(tmp_path, commits):
    repo = tmp_path / "toolrepo"
    repo.mkdir()
    git("init", "-q", "-b", "main", ".", cwd=repo)
    for path, msg, date in commits:
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(msg + "\n")
        git("add", path, cwd=repo)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": date,
                            "GIT_COMMITTER_DATE": date, "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": os.environ["PATH"]})
    return repo


PHASE_OF = phasegates.requirement_phases((REPO / "docs" / "REQUIREMENTS.md").read_text())


def test_requirement_map_reads_the_headings():
    assert PHASE_OF["D3"] == "Phase 2" and PHASE_OF["A7"] == "All" and PHASE_OF["G3"] == "Phase 5"
    assert PHASE_OF["H1"] == "v1" and PHASE_OF["PR7"] == "Process"
    idx = phasegates.requirement_index((REPO / "docs" / "REQUIREMENTS.md").read_text())
    assert idx["B7"]["priority"] == "Should"


def test_a_later_phase_commit_after_cr15_is_building_not_a_breach(tmp_path):
    repo = scratch_repo(tmp_path, [("README.md", "docs: start", "2026-09-30T10:00:00Z"),
                                   ("src/matrix/x.py", "feat(matrix): D3 matrix\n\nRequirements: D3, D8",
                                    "2026-10-13T10:00:00Z")])
    res = phasegates.check(gates(), PHASE_OF, phasegates.git_commits(repo), ["/", "/matrix/"], ["matrix"])
    assert all(r["problems"] == [] for r in res), res


def test_an_implementation_commit_before_its_gate_is_caught(tmp_path):
    repo = scratch_repo(tmp_path, [("README.md", "docs: start", "2026-09-30T10:00:00Z"),
                                   ("schemas/x.json", "contracts: release-lock for G3, manifest for C11", "2026-10-01T10:00:00Z"),
                                   ("src/matrix.py", "feat(matrix): D3 compatibility matrix", "2026-10-05T10:00:00Z")])
    commits = phasegates.git_commits(repo)
    res = {r["phase"]: r for r in phasegates.check(gates(), PHASE_OF, commits, ["/"], ["walk"])}
    assert any("implementation commit" in p and "['D3']" in p for p in res["Phase 2"]["problems"])
    assert res["Phase 3"]["problems"] == [] and res["Phase 5"]["problems"] == [], "a contracts-only commit is gate-2 work"
    met = gates("gate-met", {"at": "2026-10-04T09:00:00Z", "by": "david", "evidence": "x"})
    assert all(r["problems"] == [] for r in phasegates.check(met, PHASE_OF, commits, ["/"], ["walk"]))
    late = gates("gate-met", {"at": "2026-10-06T09:00:00Z", "by": "david"})
    assert phasegates.check(late, PHASE_OF, commits, ["/"], ["walk"])[0]["problems"], "the gate was checked after the code"


def test_a_review_label_is_not_a_requirement_id():
    """Integration: `Review CI-E1 rejected` in build/ci's message was read as Phase 4's E1."""
    commits = [{"sha": "a" * 40, "at": "2026-10-02T10:00:00Z", "files": ["src/x.py"],
                "message": "ci: fixes\n\nReview CI-E1 rejected; MW-R1 fixed.\n\nRequirements: A2, D5\n"}]
    res = {r["phase"]: r["problems"] for r in phasegates.check(gates(), PHASE_OF, commits, ["/"], ["walk"])}
    assert res["Phase 4"] == [] and res["Phase 2"] == []
    named = [dict(commits[0], message="feat: bump writer (E1)\n\nRequirements: E1, A2\n")]
    res = {r["phase"]: r["problems"] for r in phasegates.check(gates(), PHASE_OF, named, ["/"], ["walk"])}
    assert any("['E1']" in p for p in res["Phase 4"])


def test_a_cr_recorded_exemption_takes_only_its_ids_out_of_only_its_commit():
    """CR-5: a commit that named D7 because the contract told it to is accepted only while CHANGES.md records it."""
    sha = "b" * 40
    commits = [{"sha": sha, "at": "2026-10-02T10:00:00Z", "files": ["src/ci.py"],
                "message": "ci: x\n\nRequirements: A2, D7, D10\n"}]
    reg = dict(gates(), exemptions=[{"commit": sha, "ids": ["D7"], "cr": "CR-5", "reason": "contract text"}])
    row = "| CR-5 | 2026-10-10 | D7 | commit %s names D7 | ... | David |\n" % sha[:7]
    p2 = lambda r: {x["phase"]: x["problems"] for x in r}["Phase 2"]
    assert p2(phasegates.check(reg, PHASE_OF, commits, ["/"], ["walk"], changes=row)) == []
    # no CHANGES.md row, a row for another CR, or a row naming another commit: the exemption does not count
    for changes in (None, "", row.replace("CR-5", "CR-4"), row.replace(sha[:7], "c" * 7)):
        assert any("['D7']" in p for p in p2(phasegates.check(reg, PHASE_OF, commits, ["/"], ["walk"], changes=changes)))
    # it takes out only its ids: the same commit naming D3 too is still caught, for D3 alone
    d3 = [dict(commits[0], message="ci: x\n\nRequirements: D3, D7\n")]
    assert any("['D3']" in p for p in p2(phasegates.check(reg, PHASE_OF, d3, ["/"], ["walk"], changes=row)))
    # and only its commit
    other = [dict(commits[0], sha="d" * 40)]
    assert p2(phasegates.check(reg, PHASE_OF, other, ["/"], ["walk"], changes=row))
    assert phasegates.exemptions(reg, row) == {sha: {"D7"}}


def test_automation_and_register_shape_are_checked():
    """CR-15: later-phase routes and commands may be built; an enabled later-phase automation is the breach."""
    res = {r["phase"]: r["problems"] for r in phasegates.check(gates(), PHASE_OF, [], ["/", "/releases", "/bumps/x",
                                                                "/matrix/x"], ["walk", "lock", "matrix"])}
    assert all(p == [] for p in res.values()), res
    res = {r["phase"]: r["problems"] for r in phasegates.check(gates(), PHASE_OF, [], ["/"], ["walk"],
                                                                automation=["poll", "nightly_matrix", "bump_bot"])}
    assert res["Phase 2"] == ["Phase 2: automation `nightly_matrix` is enabled before its entry gate (CR-15: build, "
                              "do not enable)"]
    assert any("`bump_bot`" in p for p in res["Phase 4"]) and res["Phase 5"] == []
    met = gates("gate-met", {"at": "2026-10-04T09:00:00Z", "by": "david"})
    assert phasegates.check(met, PHASE_OF, [], [], [], automation=["nightly_matrix"])[0]["problems"] == []
    own = gates()
    own["phases"][0]["automation"] = ["matrix_sweep"]               # the register names what its gate governs
    p2 = phasegates.check(own, PHASE_OF, [], [], [], automation=["matrix_sweep", "nightly_matrix"])[0]["problems"]
    assert p2 == ["Phase 2: automation `matrix_sweep` is enabled before its entry gate (CR-15: build, do not enable)"]
    res = {r["phase"]: r["problems"] for r in phasegates.check(gates(drop="Phase 2"), PHASE_OF, [], [], [])}
    assert res["Phase 2"] == ["Phase 2: no entry gate written down"]
    unchecked = gates("gate-met", {})
    assert "Phase 2: marked gate-met without checked.at and checked.by" in phasegates.check(unchecked, PHASE_OF, [], [], [])[0]["problems"]
    nocheck = gates()
    nocheck["phases"][1]["entry_gate"] = [{"criterion": "c"}]
    assert phasegates.check(nocheck, PHASE_OF, [], [], [])[1]["problems"] == ["Phase 3: criterion 'c' has no check"]


def test_this_repo_meets_a7_with_the_real_app_and_cli():
    res = phasegates.check_repo(REPO)
    assert [r["phase"] for r in res] == list(LATER)
    assert all(r["problems"] == [] for r in res), res
    assert "/" in phasegates.app_routes() and "phase-gates" in phasegates.cli_commands()


def _cli(*args, cwd=None):
    exe = shutil.which("nanosoc-rm")
    assert exe
    return subprocess.run([exe, *args], capture_output=True, text=True, cwd=cwd, timeout=300)


def test_phase_gates_command_exit_codes(tmp_path):
    p = _cli("phase-gates", "--json", "--repo", str(REPO))
    assert p.returncode == 0, p.stderr
    assert [json.loads(x)["phase"] for x in p.stdout.splitlines()] == list(LATER)
    assert _cli("phase-gates", "--repo", str(tmp_path)).returncode == 3, "a --repo that is not the tool repo"
    assert _cli("trace", "--repo", str(tmp_path)).returncode == 3


def test_phase_gates_command_finds_a_planted_breach(tmp_path):
    repo = tmp_path / "copy"
    shutil.copytree(REPO / "docs", repo / "docs")
    (repo / "src").mkdir()
    git("init", "-q", "-b", "main", ".", cwd=repo)
    git("add", "docs", cwd=repo)
    git("commit", "-q", "-m", "docs", cwd=repo)
    (repo / "src" / "lock.py").write_text("x\n")
    git("add", "src", cwd=repo)
    git("commit", "-q", "-m", "feat: lock writer for G3", cwd=repo)          # dated 2026-09-30: before CR-15
    p = _cli("phase-gates", "--json", "--repo", str(repo))
    assert p.returncode == 1, p.stdout + p.stderr
    probs = {json.loads(x)["phase"]: json.loads(x)["problems"] for x in p.stdout.splitlines()}
    assert any("names ['G3']" in x for x in probs["Phase 5"])


# ----- trace (PR7) ---------------------------------------------------------------------------------------------
def test_requirement_markers_are_read_statically(tmp_path):
    t = tmp_path / "tests" / "acceptance"
    t.mkdir(parents=True)
    (t / "test_x.py").write_text(
        "import pytest\n@pytest.mark.requirement('A5')\ndef test_AT_A5():\n    raise SystemExit('never run')\n"
        "@pytest.mark.requirement('A5')\n@pytest.mark.live\ndef test_AT_A5_negative_control():\n    pass\n"
        "def test_unmarked():\n    pass\n")
    assert trace.requirement_markers(tmp_path / "tests") == {
        "A5": ["tests/acceptance/test_x.py::test_AT_A5", "tests/acceptance/test_x.py::test_AT_A5_negative_control"]}


def test_commit_trailers():
    commits = [{"sha": "a" * 40, "at": "2026-10-02T10:00:00+00:00", "message": "web: pages\n\nbody\n\nRequirements: A3, H1 H2\n"},
               {"sha": "b" * 40, "at": "2026-10-02T11:00:00+00:00", "message": "docs only\n"}]
    assert trace.commit_trailers(commits) == [{"sha": "a" * 40, "at": "2026-10-02T10:00:00+00:00",
                                               "subject": "web: pages", "ids": ["A3", "H1", "H2"]}]


def test_trace_on_this_repo():
    rows, problems = trace.trace(REPO)
    by = {r["requirement"]: r for r in rows}
    assert problems == [], problems
    assert "tests/acceptance/test_scope_web.py::test_AT_A5" in by["A5"]["tests"]
    assert by["A7"]["commits"], "the foundation commit names A7 in its trailer"
    p = _cli("trace", "--json", "--requirement", "A5", "--repo", str(REPO))
    assert p.returncode == 0 and json.loads(p.stdout)["requirement"] == "A5"


def test_trace_flags_an_untested_must_and_an_unknown_id(tmp_path):
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### A1 · Must · v1\n\n### D3 · Must · Phase 2\n")
    (repo / "docs" / "phase-gates.yaml").write_text("phases: []\n")
    git("init", "-q", "-b", "main", ".", cwd=repo)
    git("add", "-A", ".", cwd=repo)
    git("commit", "-q", "-m", "x\n\nRequirements: A1, Z9", cwd=repo)
    rows, problems = trace.trace(repo)
    assert "A1 (Must, v1) has no acceptance test" in problems
    assert not any(p.startswith("D3") for p in problems), "a later phase gets its tests at its own gate 2"
    assert any("unknown requirement Z9" in p for p in problems)
    assert _cli("trace", "--repo", str(repo)).returncode == 1


def test_trace_one_requirement_unknown_id_is_usage_and_findings_are_its_own(tmp_path):
    """Review W7: --requirement with an ID REQUIREMENTS.md lacks is a usage error (3, section 0); with a known
    ID, only that requirement's findings set the exit code."""
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### A1 · Must · v1\n\n### A2 · Must · v1\n")
    (repo / "docs" / "phase-gates.yaml").write_text("phases: []\n")
    (repo / "tests" / "test_a2.py").write_text(
        "import pytest\n\n@pytest.mark.requirement(\"A2\")\ndef test_AT_A2():\n    pass\n")
    git("init", "-q", "-b", "main", ".", cwd=repo)
    git("add", "-A", ".", cwd=repo)
    git("commit", "-q", "-m", "x\n\nRequirements: A2, Z9", cwd=repo)
    p = _cli("trace", "--json", "--requirement", "NOPE", "--repo", str(repo))
    assert p.returncode == 3 and p.stdout == "" and "NOPE" in p.stderr
    with pytest.raises(trace.UnknownRequirement):
        trace.trace(repo, "NOPE")
    rows, problems = trace.trace(repo, "A2")
    assert [r["requirement"] for r in rows] == ["A2"] and problems == []
    p = _cli("trace", "--json", "--requirement", "A2", "--repo", str(repo))
    assert p.returncode == 0, p.stderr                       # A1 untested and Z9 unknown are not A2's findings
    assert _cli("trace", "--requirement", "A1", "--repo", str(repo)).returncode == 1
    assert _cli("trace", "--repo", str(repo)).returncode == 1
