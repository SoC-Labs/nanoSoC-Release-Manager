"""Review items of the versioning build (VER-1 to VER-8): each test failed before its fix and passes after it.

VER-1 a FAIL plan never becomes a draft; VER-2 the changelog parser follows CommonMark list items, so an indented,
numbered or plain-text entry is never dropped; VER-3 an unclassed entry fails the plan; VER-4 a deleted or
re-created release tag is a finding (against the stored tag-check history); VER-5 the phase-gate automation rule
reads the real scheduler; VER-6 an approval needs a proven identity; VER-8 the POST routes refuse a component that
is not in the catalogue."""
from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nanosoc_rm import auth, contracts, versioning
from nanosoc_rm.cli import main
from nanosoc_rm.versioning import changelog as cl
import vfix
from vfix import git, mirror_from

run = lambda *a, **k: CliRunner().invoke(main, list(a), **k)


def fact(obs):
    assert contracts.validate_observation(obs) == [], contracts.validate_observation(obs)
    return obs["fact"]


# ---------------------------------------------------------------------------------------------------------
# VER-1: a draft is refused while the plan has findings
# ---------------------------------------------------------------------------------------------------------
def _c9_site(tmp_path, monkeypatch):
    cfg, data, reg = vfix.write_config(tmp_path, monkeypatch)
    man = vfix.MANIFEST.replace('version: "0.1.0"', 'version: "1.0.0"').replace("reexports: [nanosoc_gen]",
                                                                               "reexports: []")
    man = man.replace("requires:\n  - {id: nanosoc_gen, path: nanosoc_gen, range: \"^0.1\"}\n", "requires: []\n")
    arch = vfix.work_repo(tmp_path / "arch", {
        "nanosoc-component.yaml": man, "API.md": "# API\n\n- port HSEL_O\n",
        "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n### fix\n- x\n\n"
                        "[Unreleased]: https://github.com/SoC-Labs/nanoSoC-Arch-Tech/compare/v0.0.0...HEAD\n"})
    mirror_from(data, "nanosoc_arch_tech", {vfix.ARCH: arch})
    return cfg, data


@pytest.mark.requirement("C9")
@pytest.mark.requirement("G1")
def test_ver1_a_plan_that_fails_c9_is_never_drafted(tmp_path, monkeypatch):
    cfg, data = _c9_site(tmp_path, monkeypatch)
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["verdict"] == "FAIL" and f["next"]["version"] == "1.0.0"
    with pytest.raises(versioning.VersioningUsage, match="C9 refuses 1.0.0"):
        versioning.draft("nanosoc_arch_tech", date="2026-10-13")
    r = run("version", "draft", "nanosoc_arch_tech", "--date", "2026-10-13")
    assert r.exit_code == 1, r.output
    assert "C9 refuses 1.0.0" in r.output
    assert not (data / "drafts" / "nanosoc_arch_tech" / "1.0.0").exists(), "no draft folder, no tag commands"


@pytest.mark.requirement("G1")
@pytest.mark.requirement("G6")
def test_ver1_a_yanked_next_version_is_never_drafted(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix."]})
    (s["registry"] / "versioning.yaml").write_text(
        "schema: 1\nyanked:\n  - {component: nanosoc_arch_tech, version: \"0.1.1\", tag: v0.1.1, "
        "commit: \"%s\", reason: r, by: david, date: \"2026-10-13\"}\n" % ("a" * 40))
    with pytest.raises(versioning.VersioningUsage, match="yanked"):
        versioning.draft("nanosoc_arch_tech", tmp_path / "d1", date="2026-10-13")
    assert not (tmp_path / "d1").exists()


@pytest.mark.requirement("G1")
def test_ver1_the_gui_offers_no_draft_button_for_a_failing_plan(tmp_path, monkeypatch):
    cfg, data = _c9_site(tmp_path, monkeypatch)
    assert run("version", "refresh", "nanosoc_arch_tech").exit_code == 0
    c = TestClient(versioning_app(cfg))
    page = c.get("/versions/nanosoc_arch_tech").text
    assert ">FAIL</span>" in page and "Draft release" not in page
    assert "no draft while the plan has findings" in page


def versioning_app(cfg):
    from nanosoc_rm.web import create_app
    return create_app(cfg, auth=auth.FakeMembership(login="alice"))


# ---------------------------------------------------------------------------------------------------------
# VER-2: CommonMark list items, numbered items and plain text are entries the linter sees
# ---------------------------------------------------------------------------------------------------------
LINKS = ("\n[Unreleased]: https://github.com/SoC-Labs/x/compare/v0.1.0...HEAD\n"
         "[0.1.0]: https://github.com/SoC-Labs/x/releases/tag/v0.1.0\n")


def _doc(body: str) -> str:
    return "# Changelog\n\n## [Unreleased]\n%s\n## [0.1.0] - 2026-09-02\n### feature\n- First.\n%s" % (body, LINKS)


@pytest.mark.requirement("C2")
@pytest.mark.parametrize("body", [" - Removed port HSEL_O.", "   * Removed port HSEL_O.", "1. Removed port HSEL_O.",
                                  "2) Removed port HSEL_O.", "Removed port HSEL_O.", "- Removed port HSEL_O."])
def test_ver2_every_unclassed_form_is_an_error(body):
    found = [f for f in cl.lint(_doc(body)) if f["severity"] == "error"]
    assert [f["rule"] for f in found] == ["CL8"], (body, found)
    assert "Removed port HSEL_O." in found[0]["message"]
    assert cl.unreleased_classes(cl.parse(_doc(body))) == {"unclassed": [{"text": "Removed port HSEL_O.", "line": 4}]}


@pytest.mark.requirement("C2")
def test_ver2_a_sibling_item_is_never_glued_onto_the_entry_above():
    doc = _doc("### fix\n- Fix a.\n\n - Removed port HSEL_O.\n  continued.\n")
    got = {k: [e["text"] for e in v] for k, v in cl.unreleased_classes(cl.parse(doc)).items()}
    assert got == {"fix": ["Fix a.", "Removed port HSEL_O. continued."]}
    # a nested item or an indented paragraph still belongs to its entry
    doc = _doc("### fix\n- Fix a.\n  - detail one\n\n  more detail.\n")
    got = {k: [e["text"] for e in v] for k, v in cl.unreleased_classes(cl.parse(doc)).items()}
    assert got == {"fix": ["Fix a. - detail one more detail."]}
    # prose under the title, before any version, and an HTML comment, are not entries
    doc = "# Changelog\n\nAll notable changes.\n<!-- keep this -->\n" + _doc("### fix\n- Fix a.\n")[len("# Changelog\n"):]
    assert [f for f in cl.lint(doc) if f["severity"] == "error"] == []


# ---------------------------------------------------------------------------------------------------------
# VER-3: an unclassed entry fails the plan, so no class and no draft rests on it
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C2")
@pytest.mark.requirement("C3")
def test_ver3_an_unclassed_entry_fails_the_plan(tmp_path, monkeypatch):
    vfix.arch_site(tmp_path, monkeypatch, entries={"fix": ["Fix the flist order."], "Removed": ["Remove port HSEL_O."]})
    assert run("changelog", "lint", "nanosoc_arch_tech").exit_code == 1
    f = fact(versioning.plan("nanosoc_arch_tech"))
    assert f["verdict"] == "FAIL" and any("changelog lint FAIL" in x for x in f["findings"]), f["findings"]
    declared = next(x for x in f["classification"]["signals"] if x["signal"] == "declared")
    assert declared["status"] != "measured" and "1 unclassed" in declared["detail"]
    assert run("version", "next", "nanosoc_arch_tech").exit_code == 1
    with pytest.raises(versioning.VersioningUsage, match="changelog lint FAIL"):
        versioning.draft("nanosoc_arch_tech", tmp_path / "d", date="2026-10-13")


# ---------------------------------------------------------------------------------------------------------
# VER-4: a release tag deleted, or deleted and re-created elsewhere, is a finding
# ---------------------------------------------------------------------------------------------------------
def _sync_prune(data, arch):
    m = data / "mirrors" / "nanosoc_arch_tech.git"
    for host, path in (vfix.ARCH, vfix.ARCH_COPY):
        ns = "refs/r/%s" % vfix.key(host, path)
        git("fetch", "-q", "--no-tags", "--prune", "--force", str(arch), "+refs/heads/*:%s/heads/*" % ns,
            "+refs/tags/*:%s/tags/*" % ns, cwd=m)


@pytest.mark.requirement("C10")
def test_ver4_a_deleted_or_recreated_release_tag_is_a_finding(tmp_path, monkeypatch):
    s = vfix.arch_site(tmp_path, monkeypatch)
    (s["registry"] / "versioning.yaml").write_text('schema: 1\ntag_scheme_from: "2026-08-01T00:00:00Z"\n')
    git("tag", "-d", "integ-step1", cwd=s["arch"])
    _sync_prune(s["data"], s["arch"])
    f = fact(versioning.tag_check("nanosoc_arch_tech", store=True))
    assert f["verdict"] == "PASS", f
    git("tag", "-d", "v0.1.0", cwd=s["arch"])
    _sync_prune(s["data"], s["arch"])
    f = fact(versioning.tag_check("nanosoc_arch_tech", store=True))
    row = {r["tag"]: r for r in f["rows"]}.get("v0.1.0")
    assert row is not None and row["finding"] and row.get("deleted") is True, f["rows"]
    assert any("DELETED" in p for p in row["problems"]) and f["verdict"] == "FAIL"
    # the deletion stays a finding on the next check, although the stored check before it no longer lists the tag
    f = fact(versioning.tag_check("nanosoc_arch_tech", store=True))
    assert f["verdict"] == "FAIL"
    vfix.git("tag", "-a", "-m", "again", "v0.1.0", "HEAD", cwd=s["arch"])
    _sync_prune(s["data"], s["arch"])
    f = fact(versioning.tag_check("nanosoc_arch_tech", store=True))
    row = {r["tag"]: r for r in f["rows"]}["v0.1.0"]
    assert row["moved"] and row["finding"] and any("re-created" in p for p in row["problems"]), row
    assert run("version", "tags", "nanosoc_arch_tech").exit_code == 1


# ---------------------------------------------------------------------------------------------------------
# VER-5: rule 5 of the phase-gate check sees the real scheduler's jobs
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("A7")
def test_ver5_enabled_later_phase_jobs_on_the_real_scheduler_are_caught(monkeypatch):
    from nanosoc_rm import phasegates, schedule
    assert phasegates.enabled_automation() == []
    noop = lambda c: None
    monkeypatch.setattr(schedule, "JOBS", schedule.JOBS + (schedule.Job("bump_prs", "nightly", noop),))
    assert phasegates.enabled_automation() == [("bump_prs", "Phase 4")], "inferred from the job's name"
    monkeypatch.setattr(schedule, "JOBS", schedule.JOBS[:-1] + (
        schedule.Job("nightly_thing", "nightly", noop, phase="Phase 2"),))
    assert phasegates.enabled_automation() == [("nightly_thing", "Phase 2")], "declared phase"
    monkeypatch.setattr(schedule, "JOBS", schedule.JOBS[:-1] + (
        schedule.Job("release_tags", "nightly", noop, phase="Phase 3", enabled=False),))
    assert phasegates.enabled_automation() == [], "ships disabled"


def test_ver5_the_scheduler_never_runs_a_disabled_job(tmp_path, monkeypatch):
    import datetime as dt
    from nanosoc_rm import schedule
    cfg, data, reg = vfix.write_config(tmp_path, monkeypatch)
    from nanosoc_rm.config import load_config
    ran = []
    jobs_ = (schedule.Job("a", "poll", lambda c: ran.append("a")),
             schedule.Job("b", "poll", lambda c: ran.append("b"), phase="Phase 4", enabled=False))
    assert schedule.tick(dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc), config=load_config(cfg),
                         jobs=jobs_) == ["a"] and ran == ["a"]


# ---------------------------------------------------------------------------------------------------------
# VER-6: an approval needs a proven identity: the local password (CLI) or the signed-in session (GUI)
# ---------------------------------------------------------------------------------------------------------
PW = {"bob": "bob's long password", "david": "david's long password"}


def _approval_site(tmp_path, monkeypatch, local=True):
    users = "".join('[[auth.users]]\nlogin = "%s"\nhash = "%s"\n' % (u, auth.hash_password(p, salt=b"0123456789abcdef"))
                    for u, p in PW.items())
    extra = 'david = ["release-manager"]\nbob = ["owner:nanosoc_arch_tech"]\n' + (
        '[auth]\nmode = "local"\n' + users if local else "")
    s = vfix.arch_site(tmp_path, monkeypatch, entries={"breaking": ["Remove HSEL_O."]}, extra_config=extra)
    out = tmp_path / "D"
    versioning.draft("nanosoc_arch_tech", out, date="2026-10-13")
    return s, out


@pytest.mark.requirement("G5")
def test_ver6_a_typed_login_alone_approves_nothing(tmp_path, monkeypatch):
    s, out = _approval_site(tmp_path, monkeypatch)
    with pytest.raises(versioning.VersioningUsage, match="identity"):
        versioning.approve(out, "tool-final-breaking", "bob")
    with pytest.raises(versioning.VersioningUsage, match="identity"):
        versioning.approve(out, "tool-final-breaking", "bob", password="wrong")
    with pytest.raises(versioning.VersioningUsage, match="identity"):
        versioning.approve(out, "tool-final-breaking", "david", session_login="bob")
    r = run("version", "approve", str(out), "--step", "tool-final-breaking", "--login", "bob", input="wrong\n")
    assert r.exit_code == 3 and "identity" in r.output
    r = run("version", "approve", str(out), "--step", "tool-final-breaking", "--login", "bob", "--password-stdin",
            input=PW["bob"] + "\n")
    assert r.exit_code == 0 and "pending" in r.output, r.output
    rel = json.loads((out / "release.json").read_text())
    st = next(a for a in rel["approvals"] if a["step"] == "tool-final-breaking")
    assert [(a["role"], a["by"]) for a in st["approvals"]] == [("owner:nanosoc_arch_tech", "bob")]


@pytest.mark.requirement("G5")
def test_ver6_without_local_users_the_cli_refuses_and_the_gui_uses_the_session(tmp_path, monkeypatch):
    s, out = _approval_site(tmp_path, monkeypatch, local=False)
    r = run("version", "approve", str(out), "--step", "tool-final-breaking", "--login", "bob", "--password-stdin",
            input="x\n")
    assert r.exit_code == 3 and "GUI" in r.output
    # the GUI records the signed-in login, never a typed one
    drafts = s["data"] / "drafts" / "nanosoc_arch_tech" / "0.2.0"
    drafts.parent.mkdir(parents=True)
    out.rename(drafts)
    c = TestClient(_app(s["cfg"], "bob"))
    page = c.get("/versions/nanosoc_arch_tech").text
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = c.post("/versions/nanosoc_arch_tech/drafts/0.2.0/approve",
               data={"csrf_token": tok, "step": "tool-final-breaking", "login": "david"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    st = next(a for a in json.loads((drafts / "release.json").read_text())["approvals"] if a["step"] == "tool-final-breaking")
    assert [(a["role"], a["by"]) for a in st["approvals"]] == [("owner:nanosoc_arch_tech", "bob")]
    r = c.post("/versions/nanosoc_arch_tech/drafts/0.2.0/approve", data={"csrf_token": tok, "step": "tool-final-breaking"},
               follow_redirects=False)
    assert r.status_code == 400 and "another person" in r.text
    assert c.post("/versions/nanosoc_arch_tech/drafts/9.9.9/approve", data={"csrf_token": tok, "step": "x"},
                  follow_redirects=False).status_code == 404


def _app(cfg, login):
    from nanosoc_rm.web import create_app
    return create_app(cfg, auth=auth.FakeMembership(login=login))


# ---------------------------------------------------------------------------------------------------------
# VER-8: the POST routes refuse a component outside the catalogue before queueing anything
# ---------------------------------------------------------------------------------------------------------
def test_ver8_post_routes_refuse_an_unknown_component(tmp_path, monkeypatch):
    from nanosoc_rm import jobs
    from nanosoc_rm.config import load_config
    s = vfix.arch_site(tmp_path, monkeypatch)
    c = TestClient(_app(s["cfg"], "alice"))
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/versions/").text).group(1)
    assert c.post("/versions/no_such_comp/draft", data={"csrf_token": tok}, follow_redirects=False).status_code == 404
    assert c.post("/versions/refresh", data={"csrf_token": tok, "component": "no_such_comp"},
                  follow_redirects=False).status_code == 404
    assert jobs.jobs(load_config(s["cfg"])) == []
