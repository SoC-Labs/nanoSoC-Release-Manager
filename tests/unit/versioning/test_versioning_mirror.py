"""The versioning functions on hermetic component mirrors: version next (C1, C3, C6, C9, C11, C12), tag check
(C10), manifest init (C11), release draft and approvals (G1, G5), yank (G6), fixed-in from trailers (F6), the
CLI front ends and their exit codes."""
from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, db, versioning
from nanosoc_rm.cli import main
from nanosoc_rm.versioning.classify import RegenerationResult
import vfix
from vfix import commit, git, mirror_from, tag

pytestmark = pytest.mark.requirement("C1")


def fact(obs):
    assert contracts.validate_observation(obs) == [], contracts.validate_observation(obs)
    return obs["fact"]


@pytest.mark.requirement("C3")
@pytest.mark.requirement("C6")
def test_next_from_a_classed_changelog_with_the_signals(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."]},
                       gen_entries={"feature": ["New generator option."]})
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["current"]["tag"] == "v0.1.0" and f["current"]["kind"] == "release" and f["commits_since"] == 2
    sig = {x["signal"]: x for x in f["classification"]["signals"]}
    assert sig["declared"]["class"] == "fix" and sig["api"]["class"] == "none"
    assert sig["reexports"]["class"] == "feature", "C6: arch_tech passes nanosoc_gen's feature on"
    assert sig["consumers"]["status"] == "unmeasured" and "no Regenerator" in sig["consumers"]["detail"]
    assert f["classification"]["class"] == "feature" and f["classification"]["status"] == "partial"
    assert f["next"]["version"] == "0.1.1" and f["verdict"] == "WARN"
    assert f["manifest"] == {"present": True, "valid": True, "version": "0.1.0", "problems": []} and f["api_md"]
    assert s["head"] == f["commit"]


@pytest.mark.requirement("C3")
@pytest.mark.requirement("C12")
def test_an_api_line_removed_is_breaking_and_bumps_the_middle_number(tmp_path, monkeypatch):
    vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Tidy."]}, api_after="# API\n\n- flist layout v1\n")
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert {x["signal"]: x["class"] for x in f["classification"]["signals"]}["api"] == "breaking"
    assert f["classification"]["class"] == "breaking" and f["next"]["version"] == "0.2.0"


class FakeRegen:
    """The pluggable C3 step: one consumer whose port list changes between the two component commits."""
    name = "fake"

    def __init__(self, base, port):
        self.base, self.port = base, port

    def available(self):
        return True, ""

    def consumers(self, component):
        return ["eth-main"]

    def regenerate(self, consumer, component, commit):
        port = "hsel_o" if commit == self.base else self.port
        return RegenerationResult(consumer, {"soc.v": "module soc(\n  output wire %s\n);\n" % port},
                                  {"sim": ["a.v"] if commit == self.base else ["a.v", "b.v"]}, [])


@pytest.mark.requirement("C3")
@pytest.mark.requirement("C7")
def test_the_measured_mode_regenerates_consumers_and_completes_the_class(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."]})
    base = git("rev-parse", "v0.1.0^{commit}", cwd=s["arch"])
    f = fact(versioning.plan("nanosoc_arch_tech", regenerator=FakeRegen(base, "hsel")))
    sig = {x["signal"]: x for x in f["classification"]["signals"]}
    assert sig["consumers"]["class"] == "breaking" and sig["consumers"]["status"] == "measured"
    assert sig["build"]["build_affecting"] is True, "C7: computed from the file-list diff"
    assert f["classification"]["status"] == "complete" and f["next"]["version"] == "0.2.0" and f["verdict"] == "PASS"
    same = fact(versioning.plan("nanosoc_arch_tech", regenerator=FakeRegen(base, "hsel_o")))
    assert {x["signal"]: x["class"] for x in same["classification"]["signals"]}["consumers"] == "none", "identical output is class none"


def test_no_changelog_is_unverified_and_never_guessed_from_messages(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    commit(s["arch"], {"CHANGELOG.md": None, "API.md": None}, "2026-09-20T10:00:00Z", "feat!: drop everything")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["hint"]["class"] == "breaking", "the hint sees the message"
    assert f["classification"]["class"] == "fix", "only C6's re-export signal is measured: nanosoc_gen's fix"
    assert f["verdict"] == "WARN"
    # without the re-export the class is unmeasured: UNVERIFIED, no next version, exit 2
    commit(s["arch"], {"nanosoc-component.yaml": vfix.MANIFEST.replace("reexports: [nanosoc_gen]", "reexports: []")},
           "2026-09-21T10:00:00Z", "no reexport")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["classification"]["class"] is None and f["next"]["version"] is None and f["verdict"] == "UNVERIFIED"
    assert CliRunner().invoke(main, ["version", "next", "nanosoc_arch_tech"]).exit_code == 2


@pytest.mark.requirement("C9")
def test_c9_refuses_a_first_1_0_0_without_its_prerequisites(tmp_path, monkeypatch):
    cfg, data, reg = vfix.write_config(tmp_path, monkeypatch)
    w = vfix.work_repo(tmp_path / "gen", {"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n### feature\n- First.\n\n"
                                                         "[Unreleased]: https://gitlab.example.invalid/soclabs/nanosoc_gen/-/compare/v0.0.0...HEAD\n",
                                          "pyproject.toml": '[project]\nname = "nanosoc-gen"\nversion = "1.0.0"\n'})
    mirror_from(data, "nanosoc_gen", {vfix.GEN: w})
    f = fact(versioning.plan("nanosoc_gen"))
    assert f["next"]["version"] == "1.0.0" and f["verdict"] == "FAIL"
    assert any("C9 refuses 1.0.0" in x for x in f["findings"])
    items = {i["item"].split(" (")[0]: i["ok"] for i in f["readiness_1_0"]["items"]}
    assert items["API.md declares the public interface"] is False and items["deterministic output"] is None


@pytest.mark.requirement("C10")
def test_c10_tags_from_the_mirror_including_a_moved_release_tag(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    f = fact(versioning.tag_check("nanosoc_arch_tech"))
    rows = {r["tag"]: r for r in f["rows"]}
    assert rows["v0.1.0"]["kind"] == "release" and rows["integ-step1"]["kind"] is None
    assert f["verdict"] == "WARN" and f["findings"] == 0, "no scheme date: advisory"
    tag(s["arch"], "v0.1.0", rev="HEAD", force=True, date="2026-09-30T10:00:00Z")      # someone moves a release tag
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    f = fact(versioning.tag_check("nanosoc_arch_tech", store=True, alerts=True))
    moved = {r["tag"]: r for r in f["rows"]}["v0.1.0"]
    assert moved["moved"] and moved["finding"] and f["verdict"] == "FAIL"
    assert any(a["subject"] == "alert:tag_moved:tag_check:nanosoc_arch_tech:v0.1.0" for a in db.alerts(config=s["cfg"]))
    assert CliRunner().invoke(main, ["version", "tags", "nanosoc_arch_tech"]).exit_code == 1


@pytest.mark.requirement("C10")
def test_c10_the_walker_raises_tag_moved_for_a_frozen_configuration():
    """C10's existing alert (interfaces.md section 4 step 1) is covered by the walker's own test; this names it so a
    rename cannot silently drop it."""
    import pathlib
    text = (pathlib.Path(vfix.REPO) / "tests" / "unit" / "mirror-walker" / "test_mw_walk.py").read_text()
    assert "tag_moved" in text


@pytest.mark.requirement("C11")
def test_manifest_init_proposes_a_valid_manifest_as_a_patch_that_applies(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    commit(s["arch"], {"nanosoc-component.yaml": None}, "2026-09-20T10:00:00Z", "no manifest")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    obs, patch = versioning.manifest_init("nanosoc_arch_tech")
    f = fact(obs)
    assert not f["present"] and contracts.validate("component-manifest", f["proposed"]) == []
    assert f["proposed"]["canonical"] == "https://github.com/SoC-Labs/nanoSoC-Arch-Tech.git"
    assert f["proposed"]["requires"] == [{"id": "nanosoc_gen", "path": "nanosoc_gen", "range": ">=0.0.0"}]
    assert "/requires/0/range" in f["placeholders"] and "/tests/t0/run" in f["placeholders"]
    clone = tmp_path / "clone"
    git("clone", "-q", str(s["arch"]), str(clone))
    before = git("status", "--porcelain", cwd=s["arch"])
    (tmp_path / "m.diff").write_text(patch)
    git("apply", str(tmp_path / "m.diff"), cwd=clone)
    applied = contracts.load_yaml((clone / "nanosoc-component.yaml").read_text())
    assert applied == f["proposed"] and git("status", "--porcelain", cwd=s["arch"]) == before, "nothing written to the repo"
    # a pin at a release tag gets its Cargo range
    tag(s["gen"], "v0.2.0", rev=s["gen_b"], date="2026-09-25T10:00:00Z")
    mirror_from(s["data"], "nanosoc_gen", {vfix.GEN: s["gen"]})
    f = fact(versioning.manifest_init("nanosoc_arch_tech")[0])
    assert f["proposed"]["requires"][0]["range"] == "^0.2" and "/requires/0/range" not in f["placeholders"]


@pytest.mark.requirement("G1")
@pytest.mark.requirement("G5")
def test_g1_draft_folder_and_g5_approvals(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"breaking": ["Renamed port HSEL_O to HSEL."]},
                       extra_config='"alice" = ["owner:nanosoc_arch_tech"]\n"david" = ["release-manager"]')
    out = tmp_path / "draft"
    f = fact(versioning.draft("nanosoc_arch_tech", out, date="2026-10-13"))
    assert f["version"] == "0.2.0" and f["class"] == "breaking" and f["pushed"] is False
    assert sorted(p.name for p in out.iterdir()) == sorted(versioning.release.DRAFT_FILES)
    rel = json.loads((out / "release.json").read_text())
    assert contracts.validate("tool-release", rel) == []
    final = next(a for a in rel["approvals"] if a["step"] == "tool-final-breaking")
    assert final["roles"] == ["owner:nanosoc_arch_tech", "release-manager"]
    assert [x["path"] for x in rel["submodules"]] == ["nanosoc_gen"] and rel["gates"][3]["tier"] == "t3"
    for name in ("README.md", "commands.txt"):
        assert "Nothing was tagged, pushed" in (out / name).read_text()
    # the version patch applies in a clone and the result lints clean with the new tag
    clone = tmp_path / "c"
    git("clone", "-q", str(s["arch"]), str(clone))
    git("apply", "--check", str(out / "version.patch"), cwd=clone)
    git("apply", str(out / "version.patch"), cwd=clone)
    from nanosoc_rm.versioning import changelog as cl
    assert cl.lint((clone / "CHANGELOG.md").read_text(), tags={"v0.1.0": {}, "v0.2.0": {}}) == []
    assert 'version: "0.2.0"' in (clone / "nanosoc-component.yaml").read_text()
    # G5: roles from the service config, enforced; the bot's step cannot be approved by a person
    with pytest.raises(versioning.VersioningUsage, match="holds none"):
        versioning.approve(out, "tool-final-breaking", "mallory")
    with pytest.raises(versioning.VersioningUsage, match="bot"):
        versioning.approve(out, "tool-candidate", "david")
    assert versioning.approve(out, "tool-final-breaking", "alice")["state"] == "pending"
    with pytest.raises(versioning.VersioningUsage):
        versioning.approve(out, "tool-final-breaking", "alice")                       # the owner role is done
    assert versioning.approve(out, "tool-final-breaking", "david")["state"] == "approved"
    assert contracts.validate("tool-release", json.loads((out / "release.json").read_text())) == []
    with pytest.raises(versioning.VersioningUsage, match="not empty"):
        versioning.draft("nanosoc_arch_tech", out)


@pytest.mark.requirement("G6")
def test_g6_yank_is_a_registry_patch_and_a_silicon_release_is_never_yanked(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    obs, patch = versioning.yank("nanosoc_arch_tech", "0.1.0", "flist order wrong", "david", date="2026-10-13")
    f = fact(obs)
    assert f["refused"] is None and f["tag_kept"] and patch.startswith("diff --git a/versioning.yaml")
    assert not (s["registry"] / "versioning.yaml").exists(), "the registry is only read"
    (tmp_path / "y.diff").write_text(patch)
    git("init", "-q", cwd=s["registry"])
    git("apply", str(tmp_path / "y.diff"), cwd=s["registry"])
    assert versioning.is_yanked("nanosoc_arch_tech", "0.1.0") and not versioning.is_yanked("nanosoc_arch_tech", "0.1.1")
    assert versioning.allowed("nanosoc_arch_tech", ["0.1.0", "0.1.1"]) == ["0.1.1"]
    p = fact(versioning.plan("nanosoc_arch_tech"))
    assert p["yanked"] == ["0.1.0"]
    again = fact(versioning.yank("nanosoc_arch_tech", "0.1.0", "x", "david")[0])
    assert "already yanked" in again["refused"]
    tag(s["arch"], "silicon/demo-r0p0", rev="v0.1.0^{commit}", date="2026-09-30T10:00:00Z")
    tag(s["arch"], "v0.0.9", rev="v0.1.0^{commit}", date="2026-09-30T10:00:00Z")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    obs, patch = versioning.yank("nanosoc_arch_tech", "0.0.9", "x", "david")
    assert "silicon release is never yanked" in fact(obs)["refused"] and patch == ""
    r = CliRunner().invoke(main, ["version", "yank", "nanosoc_arch_tech", "0.0.9", "--reason", "x", "--by", "d"])
    assert r.exit_code == 1 and "REFUSED" in r.output


@pytest.mark.requirement("F6")
def test_f6_trailers_become_a_fixed_in_patch(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    fix = commit(s["gen"], {"a.txt": "1\n"}, "2026-09-20T10:00:00Z", "qualify HTRANS\n\nDefect: SL-DEF-0001 form B\n")
    commit(s["gen"], {"a.txt": "2\n"}, "2026-09-21T10:00:00Z", "fix\n\nFixes: b0a6069\n")                 # 2 forms: ambiguous
    commit(s["gen"], {"a.txt": "3\n"}, "2026-09-22T10:00:00Z", "x\n\nDefect: SL-DEF-0999\n")
    mirror_from(s["data"], "nanosoc_gen", {vfix.GEN: s["gen"]})
    obs, patch = versioning.defect_trailers("nanosoc_gen")
    f = fact(obs)
    states = sorted((r["trailer"], r["state"]) for r in f["rows"])
    assert states == [("Defect: SL-DEF-0001", "proposed"), ("Defect: SL-DEF-0999", "unresolved"),
                      ("Fixes: b0a6069", "unresolved")]
    (tmp_path / "f.diff").write_text(patch)
    git("init", "-q", cwd=s["registry"])
    git("apply", str(tmp_path / "f.diff"), cwd=s["registry"])
    doc = contracts.load_yaml((s["registry"] / "defects" / "SL-DEF-0001.yaml").read_text())
    assert contracts.validate("defect", doc) == []
    b = next(x for x in doc["forms"] if x["id"] == "B")
    assert {"component": "nanosoc_gen", "commit": fix} in b["fixed_in"] and len(b["fixed_in"]) == 2
    f2 = fact(versioning.defect_trailers("nanosoc_gen")[0])
    assert [r["state"] for r in f2["rows"] if r["trailer"] == "Defect: SL-DEF-0001"] == ["already"]


def test_cli_front_ends_print_valid_json_and_exit_codes(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix."]})
    run = lambda *a: CliRunner().invoke(main, list(a))
    for args, code in ((["changelog", "lint", "nanosoc_arch_tech", "--json"], 0),
                       (["version", "next", "nanosoc_arch_tech", "--json"], 0),
                       (["version", "tags", "nanosoc_arch_tech", "--json"], 0),
                       (["manifest", "init", "nanosoc_arch_tech", "--json"], 0)):
        r = run(*args)
        assert r.exit_code == code, (args, r.output)
        assert contracts.validate_observation(json.loads(r.output.splitlines()[0])) == []
    assert run("version", "next", "no_such_component").exit_code == 3
    assert run("changelog", "lint").exit_code == 3
    bad = vfix.unreleased({"fix": ["Fix."]}).replace("### fix\n- Fix.", "- unclassed")
    commit(s["arch"], {"CHANGELOG.md": bad}, "2026-09-20T10:00:00Z")
    mirror_from(s["data"], "nanosoc_arch_tech", {vfix.ARCH: s["arch"]})
    assert run("changelog", "lint", "nanosoc_arch_tech").exit_code == 1
    r = run("version", "refresh", "nanosoc_arch_tech", "--json")
    assert r.exit_code == 0, r.output
    assert {json.loads(x)["kind"] for x in r.output.splitlines()} == {"changelog_lint", "version_plan", "tag_check"}
    assert versioning.stored("version_plan", "nanosoc_arch_tech", config=s["cfg"])
    out = tmp_path / "p.diff"
    assert run("manifest", "init", "nanosoc_arch_tech", "--patch", str(out)).exit_code == 0
    assert run("manifest", "init", "nanosoc_arch_tech", "--patch", str(out)).exit_code == 3, "never overwritten"


def test_a_local_checkout_is_read_with_path_and_no_config(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    w = vfix.work_repo(tmp_path / "w", {"CHANGELOG.md": vfix.CHANGELOG_010})
    tag(w, "v0.1.0")
    commit(w, {"CHANGELOG.md": vfix.unreleased({"feature": ["New."]})}, "2026-09-20T10:00:00Z")
    (w / "CHANGELOG.md").write_text(vfix.unreleased({"breaking": ["Gone."]}))              # an uncommitted edit
    f = fact(versioning.plan("demo", path=str(w)))
    assert f["worktree"] and f["next"]["version"] == "0.2.0", "the work tree as it is on disk"
    f = fact(versioning.plan("demo", path=str(w), ref="main"))
    assert not f["worktree"] and f["next"]["version"] == "0.1.1"
    shutil.rmtree(w / ".git")
    f = fact(versioning.changelog_lint("demo", path=str(w)))
    assert f["commit"] is None and f["present"]


@pytest.mark.requirement("G6")
def test_g6_a_yanked_version_is_never_the_next_version(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix."]})
    (s["registry"] / "versioning.yaml").write_text(
        "schema: 1\nyanked:\n  - {component: nanosoc_arch_tech, version: \"0.1.1\", tag: v0.1.1, "
        "commit: \"%s\", reason: r, by: david, date: \"2026-10-13\"}\n" % ("a" * 40))
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["next"]["version"] == "0.1.1" and f["verdict"] == "FAIL"
    assert any("yanked" in x for x in f["findings"])
