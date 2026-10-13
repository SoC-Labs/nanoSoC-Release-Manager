"""Release drafts, re-verification, the errata document and its revision numbering, the generated-RTL manifest,
forks (CR-R1; G2, G3, G4, G7, F4, I1, I2), and the job kinds behind the pages (CR-R2).

Hermetic: the acceptance file's LocalHosts (bare repos behind url.insteadOf, a fake host API) are the hosts; the
tool syncs its own mirrors from them, as in the compat tests. Each test that changes a host builds its own scene.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from analysis_fixtures import load_at  # noqa: E402

from nanosoc_rm import cli, contracts, db, jobs  # noqa: E402
from nanosoc_rm.config import load_config  # noqa: E402
from nanosoc_rm.releases import archive as A  # noqa: E402
from nanosoc_rm.releases import builder as D  # noqa: E402
from nanosoc_rm.releases import errata as E  # noqa: E402
from nanosoc_rm.releases import forkscan as F  # noqa: E402
from nanosoc_rm.releases import notes as N  # noqa: E402
from nanosoc_rm.releases.core import ReleaseUsage, ladder  # noqa: E402

pytestmark = [pytest.mark.requirement("G2"), pytest.mark.requirement("G3")]
GH = "github"


def _defect(i0):
    return {"schema": 1, "id": "SL-DEF-0009", "title": "Demo defect in ip", "component": "ip",
            "origin": [{"component": "ip", "commit": i0}],
            "forms": [{"id": "A", "name": "demo form", "variant": "all", "fixed_in": []}],
            "trigger": "a demo trigger", "detectors": [], "effects": [{"id": "E1", "forms": ["A"]}],
            "regression_test": None}


def _erratum(conf, prefix, status="permanent", rev=1, conditions="always", category=None):
    return {"schema": 1, "id": "%s-ERR-001" % prefix, "defect": "SL-DEF-0009", "configuration": conf,
            "forms_present": ["A"], "effects_present": ["E1"],
            "exposure": {"level": "probably-exposed", "method": "ancestry", "evidence": "by ancestry"},
            "category": category, "conditions": conditions, "workaround": None, "status": status, "doc_revision": rev}


class Scene:
    """github SoC-Labs/soc: s0 (tag v0.1.0) -> s1 (main; tags v1.0.0 and silicon/demo-r0p0), s1 pins
    `ip` -> SoC-Labs/ip@i1 (on main, untagged) and `gen` -> SoC-Labs/nanosoc_gen@g1 (branch dev only, keep tag
    keep/soc-v1/nanosoc_gen). Configurations: soc-main (branch main), soc-v1 (frozen silicon at s1)."""

    def __init__(self, at, root: Path):
        self.at = at
        self.h = h = at.LocalHosts(root)
        self.i0 = h.commit(GH, "SoC-Labs/ip", files={"rtl/ip.v": "module ip; endmodule\n"}, message="feat: ip")
        self.i1 = h.commit(GH, "SoC-Labs/ip", parents=[self.i0], files={"rtl/ip.v": "module ip2; endmodule\n"},
                           message="fix: ip reset")
        h.push(GH, "SoC-Labs/ip", self.i1, "main")
        self.g1 = h.commit(GH, "SoC-Labs/nanosoc_gen", files={"gen.py": "print(1)\n"}, message="feat: gen")
        h.push(GH, "SoC-Labs/nanosoc_gen", self.g1, "dev")
        g_main = h.commit(GH, "SoC-Labs/nanosoc_gen", files={"README": "gen\n"}, message="docs: readme")
        h.push(GH, "SoC-Labs/nanosoc_gen", g_main, "main")
        h.tag(GH, "SoC-Labs/nanosoc_gen", "keep/soc-v1/nanosoc_gen", self.g1, annotated=False)
        h.git("--git-dir", h.bare(GH, "SoC-Labs/nanosoc_gen"), "symbolic-ref", "HEAD", "refs/heads/main")
        self.s0 = h.commit(GH, "SoC-Labs/soc", files={"README": "soc\n"}, message="feat: first")
        gm = [{"path": "ip", "url": h.url(GH, "SoC-Labs/ip")}, {"path": "gen", "url": h.url(GH, "SoC-Labs/nanosoc_gen")}]
        self.s1 = h.commit(GH, "SoC-Labs/soc", parents=[self.s0], files={"README": "soc 1\n"},
                           gitlinks={"ip": self.i1, "gen": self.g1}, gitmodules=gm,
                           message="feat: wire ip and gen\n\nChange-Class: feature\n")
        h.push(GH, "SoC-Labs/soc", self.s1, "main")
        h.tag(GH, "SoC-Labs/soc", "v0.1.0", self.s0)
        h.tag(GH, "SoC-Labs/soc", "v1.0.0", self.s1)
        h.tag(GH, "SoC-Labs/soc", "silicon/demo-r0p0", self.s1)
        self.write_registry()

    def write_registry(self, errata_rev=1, conditions="always", category=None):
        h, at = self.h, self.at
        comps = ["soc", "ip", "nanosoc_gen"]
        catalogue = {"schema": 1, "components": [
            {"id": c, "class": "soc_ref" if c == "soc" else ("generator" if c == "nanosoc_gen" else "ip_tech"),
             "canonical": {"host": GH, "path": "SoC-Labs/%s" % c}} for c in comps]}
        eligibility = {"schema": 1, "configurations": [
            {"id": "soc-main", "component": "soc", "ref": {"branch": "main"}, "errata_prefix": "SOC-MAIN"},
            {"id": "soc-v1", "component": "soc", "ref": {"tag": "v1.0.0"}, "commit": self.s1, "frozen": True,
             "silicon": {"chip": "demo", "revision": "r0p0"}, "errata_prefix": "DEMO-R0P0"}]}
        reg = at.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
        (reg / "defects").mkdir(exist_ok=True)
        (reg / "defects" / "SL-DEF-0009.yaml").write_text(yaml.safe_dump(_defect(self.i0), sort_keys=False))
        (reg / "errata" / "soc-v1").mkdir(parents=True, exist_ok=True)
        (reg / "errata" / "soc-v1" / "ERR-001.yaml").write_text(yaml.safe_dump(
            _erratum("soc-v1", "DEMO-R0P0", rev=errata_rev, conditions=conditions,
                                                category=category), sort_keys=False))
        self.registry = reg
        self.env = h.tool_env(reg, "rel")
        with self.env.active():
            for c in comps:
                at.rm("mirror.sync_component")(c)

    def sync(self):
        with self.env.active():
            for c in ("soc", "ip", "nanosoc_gen"):
                self.at.rm("mirror.sync_component")(c)

    def config(self):
        return load_config(self.env.config)

    def refs(self):
        """Every ref of every host repo: a draft must leave them exactly as they were (never push, never tag)."""
        out = {}
        for p in sorted((self.h.root / "hosts").rglob("*.git")):
            out[str(p)] = self.h.git("--git-dir", p, "for-each-ref", "--format=%(refname) %(objectname)")
        return out


@pytest.fixture(scope="module")
def at_pins():
    return load_at("test_pins")


@pytest.fixture
def scene(at_pins, tmp_path, monkeypatch):
    for k in ("RM_SCAN_RULES", "RM_SCAN_TOOLKIT"):
        monkeypatch.delenv(k, raising=False)
    s = Scene(at_pins, tmp_path / "w")
    with s.env.active():
        yield s
    s.h.api.close()


def _archive(tmp_path, files, name="build_soc.tar.gz", extra=None):
    p = tmp_path / name
    with tarfile.open(p, "w:gz") as tar:
        for n, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return p


RTL = {"build_soc/rtl/top.v": "module top; endmodule\n", "build_soc/flist/soc.flist": "rtl/top.v\n"}


def _checks(doc):
    return {c["id"]: c for c in doc["fact"]["checks"]}


# ---------------------------------------------------------------------------------------------------------
def test_silicon_draft_writes_a_complete_lock_and_changes_no_host(scene, tmp_path):
    before = scene.refs()
    arc = _archive(tmp_path, RTL)
    gds, net = tmp_path / "chip.gds", tmp_path / "chip.v"
    gds.write_bytes(b"GDS" * 100)
    net.write_text("module chip; endmodule\n")
    doc = D.draft("soc-v1", "1.0.0", archive=arc, gds=gds, netlist=net, out=tmp_path / "d")
    f, c = doc["fact"], _checks(doc)
    assert contracts.validate_observation(doc) == []
    assert f["lock"] == {"file": "release.lock.yaml", "complete": True, "pins": 2, "unprotected": [], "pending": []}
    lock = yaml.safe_load((tmp_path / "d" / "release.lock.yaml").read_text())
    assert contracts.validate("release-lock", lock) == []
    pins = {p["tree_paths"][0]: p for p in lock["pins"]}
    assert pins["ip"]["state"] == "ON_DEFAULT" and pins["ip"]["protection"]["by"] == "default-branch"
    assert pins["gen"]["state"] == "PROTECTED_BY_TAG" and pins["gen"]["protection"]["by"] == "keep-tag"
    assert lock["release"]["silicon"] == {"chip": "demo", "revision": "r0p0", "tag": "silicon/demo-r0p0"}
    assert lock["outputs"]["gds"]["md5"] == hashlib.md5(b"GDS" * 100).hexdigest()
    assert lock["generated_rtl"]["generator"] == {"component": "nanosoc_gen", "commit": scene.g1, "tree_path": "gen"}
    assert c["release.tag"]["verdict"] == "PASS" and c["release.silicon-tag"]["verdict"] == "PASS"
    assert c["lock.pins-tagged"]["verdict"] == "FAIL" and "ip" in c["lock.pins-tagged"]["detail"]
    assert c["lock.pins-protected"]["verdict"] == "PASS"
    assert c["rtl.archive"]["verdict"] == "PASS" and c["vendor.scan"]["verdict"] == "PASS"
    assert f["kind"] == "tapeout" and {"tier.t6", "tier.t4"} <= set(c) and c["tier.t6"]["person"]
    # the keep tag for the untagged pin is a person's command, on the component's canonical host
    step = next(s for s in f["person_steps"] if "keep/soc-v1/ip" in s["step"])
    assert step["command"] == "git tag keep/soc-v1/ip %s\ngit push git@github.com:SoC-Labs/ip.git refs/tags/keep/soc-v1/ip" % scene.i1
    assert any("David and a second reviewer" in s["who"] for s in f["person_steps"])
    assert f["train"] == [{"component": "ip", "commit": scene.i1, "depth": 1, "tree_paths": ["ip"]},
                          {"component": "nanosoc_gen", "commit": scene.g1, "depth": 1, "tree_paths": ["gen"]}]
    assert f["published"] is False and not f["ready"]
    names = {x["name"] for x in f["files"]}
    assert names == {"release.lock.yaml", "ERRATA.md", "ERRATA.html", "RELEASE_NOTES.md", "CHECKLIST.md",
                     "GATES.md", "README.txt", "build_soc.md5sums.txt", "build_soc.tar.gz"}
    assert json.loads((tmp_path / "d" / "draft.json").read_text()) == doc
    assert db.latest("release_draft", "release_draft:soc-v1:")[0]["subject"] == "release_draft:soc-v1:1.0.0"
    assert scene.refs() == before, "a draft wrote to a host"


def test_a_waiver_covers_untagged_pins_and_evidence_decides_readiness(scene, tmp_path, monkeypatch):
    arc = _archive(tmp_path, RTL)
    gds = tmp_path / "chip.gds"
    gds.write_bytes(b"x")
    kw = dict(archive=arc, gds=gds, netlist=gds, out=tmp_path / "d", replace=True,
              waivers=[D.parse_waiver("*=taped out before the scheme")])
    doc = D.draft("soc-v1", "1.0.0", **kw)
    c = _checks(doc)
    assert c["lock.pins-tagged"]["verdict"] == "WAIVED" and "1 covered by a waiver" in c["lock.pins-tagged"]["detail"]
    assert not any("keep/soc-v1/ip" in s["step"] for s in doc["fact"]["person_steps"])
    # G2 evidence the tool can read but nobody produced yet blocks readiness, with the command that makes it
    blocking = {x["id"] for x in doc["fact"]["checks"] if not x["person"] and x["verdict"] == "UNVERIFIED"}
    assert blocking == {"tier.t0", "tier.t1", "tier.t2", "clone.clean"} and not doc["fact"]["ready"]
    assert "nanosoc-rm compat soc-v1 --store --tier t1" in c["tier.t1"]["detail"]
    # a stored local compat run of this commit is the T0 evidence and the clean-clone evidence
    from nanosoc_rm.analysis import compat as C
    url_map = {"https://github.com/SoC-Labs/%s.git" % n: str(scene.h.bare(GH, "SoC-Labs/%s" % n))
               for n in ("soc", "ip", "nanosoc_gen")}
    run = C.compat("soc-v1", url_map=url_map, store=True)
    c = _checks(D.draft("soc-v1", "1.0.0", **kw))
    assert c["tier.t0"]["verdict"] == run["fact"]["verdict"] and run["subject"] in c["tier.t0"]["detail"]
    fetchable = next(g for g in run["fact"]["gates"] if g["id"] == "t0.pins.fetchable")
    assert c["clone.clean"]["verdict"] == fetchable["verdict"]
    # with every machine check passing, the draft is ready and only the person checks keep it UNVERIFIED
    import nanosoc_rm.releases.checklist as CL
    monkeypatch.setattr(CL, "tier_checks", lambda *a: ([D.check("tier.%s" % t, "PASS", "stored") for t in
                                                        ("t0", "t1", "t2")] + [D.check("tier.t6", "UNVERIFIED",
                                                                                       "person", person=True)], []))
    monkeypatch.setattr(CL, "clone_checks", lambda *a: [D.check("clone.clean", "PASS", "stored")])
    ready = D.draft("soc-v1", "1.0.0", **kw)
    assert ready["fact"]["ready"] and ready["fact"]["verdict"] == "UNVERIFIED", _checks(ready)


def test_without_an_archive_the_lock_is_a_draft_lock(scene, tmp_path):
    res = CliRunner().invoke(cli.main, ["release", "draft", "soc-main", "1.1.0", "--out", str(tmp_path / "q"),
                                        "--json"])
    assert res.exit_code == 1, res.output                  # lock.pins-tagged FAILs: `ip` has no tag
    doc = json.loads(res.output.splitlines()[-1])
    assert doc["sources"][0]["command"] == "nanosoc-rm release draft soc-main 1.1.0"
    assert doc["fact"]["lock"]["file"] == "release.lock.draft.yaml" and not doc["fact"]["lock"]["complete"]
    lock = yaml.safe_load((tmp_path / "q" / "release.lock.draft.yaml").read_text())
    assert contracts.validate("release-lock", lock, "/$defs/draft_lock") == []
    assert [p["field"] for p in lock["pending"]] == ["generated_rtl"]
    assert contracts.validate("release-lock", {k: v for k, v in lock.items() if k != "pending"}), \
        "a draft lock is not a release lock"
    c = _checks(doc)
    assert c["rtl.archive"]["verdict"] == "UNVERIFIED" and c["lock.complete"]["verdict"] == "UNVERIFIED"
    assert c["release.tag"]["verdict"] == "UNVERIFIED" and c["release.tag"]["person"]
    tag_step = next(s for s in doc["fact"]["person_steps"] if s["step"].startswith("Create tag v1.1.0"))
    assert tag_step["command"].startswith('git tag -a v1.1.0 %s -m "soc 1.1.0 (soc-main)"' % scene.s1)
    notes = (tmp_path / "q" / "RELEASE_NOTES.md").read_text()
    assert "Changes since: v1.0.0" in notes and "None listed." in notes
    assert "outputs" not in lock and doc["fact"]["kind"] == "feature"


def test_a_tag_that_exists_elsewhere_and_an_older_version_fail(scene, tmp_path):
    scene.h.tag(GH, "SoC-Labs/soc", "v2.0.0", scene.s0)
    scene.sync()
    c = _checks(D.draft("soc-main", "2.0.0", out=tmp_path / "a"))
    assert c["release.tag"]["verdict"] == "FAIL" and "never moved" in c["release.tag"]["detail"]
    c = _checks(D.draft("soc-main", "1.5.0", out=tmp_path / "b"))
    assert c["release.tag"]["verdict"] == "UNVERIFIED"
    assert c["release.version-order"]["verdict"] == "FAIL" and "v2.0.0" in c["release.version-order"]["detail"]


def test_an_unprotected_pin_fails_a_silicon_draft(scene, tmp_path):
    """G3/B5: a pin whose commit no branch or tag reaches on its host is `none`; on silicon that is FAIL."""
    scene.h.delete_remote_ref(GH, "SoC-Labs/nanosoc_gen", "refs/tags/keep/soc-v1/nanosoc_gen")
    scene.h.delete_remote_ref(GH, "SoC-Labs/nanosoc_gen", "refs/heads/dev")
    scene.sync()
    doc = D.draft("soc-v1", "1.0.0", out=tmp_path / "d")
    c = _checks(doc)
    assert c["lock.pins-protected"]["verdict"] == "FAIL" and "gen" in c["lock.pins-protected"]["detail"]
    assert doc["fact"]["lock"]["unprotected"] == ["gen"]


def test_errata_revision_numbering(scene, tmp_path):
    """F4: the document revision is the highest doc_revision; changed entries without a raised revision FAIL."""
    first = D.draft("soc-v1", "1.0.0", out=D.draft_path(scene.config(), "soc-v1", "1.0.0"))
    assert first["fact"]["errata"]["revision"] == 1 and _checks(first)["errata.revision"]["verdict"] == "PASS"
    md = (D.draft_path(scene.config(), "soc-v1", "1.0.0") / "ERRATA.md").read_text()
    assert "Errata: demo silicon r0p0" in md and "DEMO-R0P0-ERR-001" in md and "Document revision 1." in md
    scene.write_registry(errata_rev=1, conditions="only when the moon is full")
    c = _checks(D.draft("soc-v1", "1.0.1", out=D.draft_path(scene.config(), "soc-v1", "1.0.1")))
    assert c["errata.revision"]["verdict"] == "FAIL" and "still 1" in c["errata.revision"]["detail"]
    scene.write_registry(errata_rev=2, conditions="only when the moon is full")
    doc = D.draft("soc-v1", "1.0.2", out=D.draft_path(scene.config(), "soc-v1", "1.0.2"))
    assert doc["fact"]["errata"]["revision"] == 2 and _checks(doc)["errata.revision"]["verdict"] == "PASS"
    assert any("errata revision" in s["step"] for s in doc["fact"]["person_steps"]), "G5: a revision needs two"


def test_errata_document_renders_from_records_only(scene):
    from nanosoc_rm.registry import load_registry
    reg = load_registry(scene.registry)
    a = E.document(reg, "soc-v1", release={"version": "1.0.0", "tag": "v1.0.0", "commit": scene.s1})
    b = E.document(reg, "soc-v1", release={"version": "9.9.9", "tag": "v9.9.9", "commit": scene.s0})
    assert a.digest == b.digest and a.markdown != b.markdown, "the digest covers the entries, not the header"
    assert 'data-erratum="DEMO-R0P0-ERR-001"' in a.html and "<script" not in a.html
    none = E.document(reg, "soc-main")
    assert none.revision == 1 and none.records == [] and "No erratum is recorded" in none.markdown


def test_verify_finds_drift(scene, tmp_path):
    d = tmp_path / "d"
    arc = _archive(tmp_path, RTL)
    D.draft("soc-v1", "1.0.0", archive=arc, out=d)
    ok = D.verify(d)
    assert ok["fact"]["verdict"] == "PASS" and ok["fact"]["drift"] == [], ok["fact"]
    assert contracts.validate_observation(ok) == []
    # G7: the archive reproduces a build_soc/ checkout file for file; one changed file is named
    tree = tmp_path / "tree"
    for n, text in RTL.items():
        (tree / n).parent.mkdir(parents=True, exist_ok=True)
        (tree / n).write_text(text)
    assert {c["id"]: c for c in D.verify(d, against=tree)["fact"]["checks"]}["rtl.reproduces"]["verdict"] == "PASS"
    (tree / "build_soc/rtl/top.v").write_text("module top2; endmodule\n")
    bad = D.verify(d, against=tree)
    assert bad["fact"]["verdict"] == "FAIL" and "build_soc differs: build_soc/rtl/top.v" in bad["fact"]["drift"]
    # a host change: the keep tag goes, so the pin's protection moves
    scene.h.delete_remote_ref(GH, "SoC-Labs/nanosoc_gen", "refs/tags/keep/soc-v1/nanosoc_gen")
    scene.sync()
    drift = D.verify(d)["fact"]
    assert drift["verdict"] == "FAIL" and "gen: state PROTECTED_BY_TAG -> BRANCH_ONLY" in drift["drift"], drift
    # an edited file in the folder
    (d / "GATES.md").write_text("signed by nobody\n")
    assert "GATES.md changed since the draft (md5)" in D.verify(d)["fact"]["drift"]


def test_verify_fails_when_a_drafted_tag_is_deleted(scene, tmp_path):
    """Review item R1: a release or silicon tag the draft saw at the commit and that is gone now is drift (C10)."""
    d = tmp_path / "d"
    doc = D.draft("soc-v1", "1.0.0", archive=_archive(tmp_path, RTL), out=d)
    assert _checks(doc)["release.tag"]["verdict"] == "PASS" and _checks(doc)["release.silicon-tag"]["verdict"] == "PASS"
    assert D.verify(d)["fact"]["verdict"] == "PASS"
    scene.h.delete_remote_ref(GH, "SoC-Labs/soc", "refs/tags/v1.0.0")
    scene.h.delete_remote_ref(GH, "SoC-Labs/soc", "refs/tags/silicon/demo-r0p0")
    scene.sync()
    v = D.verify(d)["fact"]
    assert v["verdict"] == "FAIL", v
    assert {c["id"]: c["verdict"] for c in v["checks"]}["tag.v1.0.0"] == "FAIL"
    assert {c["id"]: c["verdict"] for c in v["checks"]}["tag.silicon-demo-r0p0"] == "FAIL"
    assert any(x.startswith("v1.0.0 pointed at") for x in v["drift"]) and \
        any(x.startswith("silicon/demo-r0p0 pointed at") for x in v["drift"])
    # a tag that did not exist at draft time and still does not is a person step, not drift
    d2 = tmp_path / "d2"
    D.draft("soc-main", "2.0.0", archive=_archive(tmp_path, RTL), out=d2)
    w = D.verify(d2)["fact"]
    assert {c["id"]: c["verdict"] for c in w["checks"]}["tag.v2.0.0"] == "NOT-RUN" and w["drift"] == [], w


def test_a_signed_ledger_verifies_and_any_other_edit_is_drift(scene, tmp_path):
    """Review item R2: G2 needs a signed GATES.md; signing it must not make verify FAIL."""
    d = tmp_path / "d"
    D.draft("soc-v1", "1.0.0", archive=_archive(tmp_path, RTL), out=d)
    g = d / "GATES.md"
    drafted = g.read_text()
    unsigned = {c["id"]: c for c in D.verify(d)["fact"]["checks"]}["ledger.signed"]
    assert unsigned["verdict"] == "NOT-RUN" and unsigned["person"]
    g.write_text(drafted.replace("| Release approver | | | |", "| Release approver | David | 2026-10-13 | DF |")
                 .replace("| Second reviewer (tapeout, silicon or errata revision) | | | |",
                          "| Second reviewer (tapeout, silicon or errata revision) | Ann | 2026-10-13 | AB |"))
    v = D.verify(d)["fact"]
    assert v["verdict"] == "PASS" and v["drift"] == [], v
    sig = {c["id"]: c for c in v["checks"]}["ledger.signed"]
    assert sig["verdict"] == "PASS" and "Release approver" in sig["detail"] and "Second reviewer" in sig["detail"]
    # a changed gate row in a signed ledger is still drift
    g.write_text(g.read_text().replace("| FAIL |", "| PASS |", 1) if "| FAIL |" in drafted else
                 g.read_text().replace("| UNVERIFIED |", "| PASS |", 1))
    assert "GATES.md changed since the draft (md5)" in D.verify(d)["fact"]["drift"]
    # so is a signature row added or a role renamed
    g.write_text(drafted.replace("| Release approver | | | |", "| Release approver | | | |\n| Extra | x | y | z |"))
    assert "GATES.md changed since the draft (md5)" in D.verify(d)["fact"]["drift"]
    g.write_text(drafted.replace("| Release approver | | | |", "| Releaser | David | x | y |"))
    assert "GATES.md changed since the draft (md5)" in D.verify(d)["fact"]["drift"]


def test_an_archive_with_a_duplicate_member_is_refused(tmp_path):
    """Review item R6: two copies of one path: the manifest could not be one line per extracted file."""
    p = tmp_path / "dup.tar"
    with tarfile.open(p, "w") as tar:
        for data in (b"old\n", b"new\n"):
            info = tarfile.TarInfo("build_soc/a.v")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(A.ArchiveError, match="more than once"):
        A.manifest(p)
    # a repeated directory entry is harmless
    q = tmp_path / "dirs.tar"
    with tarfile.open(q, "w") as tar:
        for _ in range(2):
            info = tarfile.TarInfo("build_soc")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        info = tarfile.TarInfo("build_soc/a.v")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"a\n"))
    assert A.manifest(q).files == 1


@pytest.mark.requirement("I2")
def test_a_rename_into_user_paths_still_changes_the_old_path(tmp_path):
    """Review item R5: with rename detection on, `git mv core.v user/core.v` listed only the new path."""
    import os
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    r = tmp_path / "r"
    run = lambda *a: subprocess.run(["git", *a], cwd=r, env=env, check=True, capture_output=True, text=True).stdout
    r.mkdir()
    run("init", "-q", "-b", "main", ".")
    (r / "core.v").write_text("".join("line %d\n" % i for i in range(50)))
    (r / "user").mkdir()
    (r / "user" / "a.txt").write_text("a\n")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    base = run("rev-parse", "HEAD").strip()
    run("mv", "core.v", "user/core.v")
    with open(r / "user" / "core.v", "a") as fh:
        fh.write("one more\n")
    run("commit", "-qam", "move")
    changed = F.changed_paths(r / ".git", base, "HEAD")
    assert changed == ["core.v", "user/core.v"], changed
    assert [p for p in changed if not F._under(p, ["user/"])] == ["core.v"]
    assert F.changed_paths(r / ".git", base, "no-such-ref") is None


def test_the_gui_withholds_an_archive_with_vendor_content(scene, tmp_path):
    """Review item R4 (D11, D12): a flagged archive is never handed out by the GUI, alone or in the zip."""
    import zipfile
    from fastapi.testclient import TestClient
    from nanosoc_rm import auth
    from nanosoc_rm.scan.rules import specimen
    from nanosoc_rm.web import create_app
    with open(scene.env.config, "a") as fh:
        fh.write('[roles]\ndefault = ["member"]\n')
    cfg = scene.config()
    bad = _archive(tmp_path, dict(RTL, **{"build_soc/rtl/vendor.v": specimen("arm.banner")}), name="v.tar.gz")
    doc = D.draft("soc-main", "1.1.0", archive=bad, out=D.draft_path(cfg, "soc-main", "1.1.0"))
    assert _checks(doc)["vendor.scan-private"]["verdict"] == "WARN"
    clean = _archive(tmp_path, RTL, name="c.tar.gz")
    D.draft("soc-main", "1.2.0", archive=clean, out=D.draft_path(cfg, "soc-main", "1.2.0"))
    c = TestClient(create_app(scene.env.config, auth=auth.FakeMembership(login="alice")))
    r = c.get("/releases/soc-main/1.1.0/files/v.tar.gz")
    assert r.status_code == 403 and "private release store" in r.text
    z = c.get("/releases/soc-main/1.1.0/download.zip")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert z.status_code == 200 and "soc-main-1.1.0/v.tar.gz" not in names and "soc-main-1.1.0/WITHHELD.txt" in names
    assert "soc-main-1.1.0/build_soc.md5sums.txt" in names
    page = c.get("/releases/soc-main/1.1.0").text
    assert 'data-file="v.tar.gz" data-withheld' in page and "WITHHELD" in page
    assert 'href="/releases/soc-main/1.1.0/files/v.tar.gz"' not in page
    # a clean archive is served
    assert c.get("/releases/soc-main/1.2.0/files/c.tar.gz").status_code == 200
    names = zipfile.ZipFile(io.BytesIO(c.get("/releases/soc-main/1.2.0/download.zip").content)).namelist()
    assert "soc-main-1.2.0/c.tar.gz" in names and "soc-main-1.2.0/WITHHELD.txt" not in names


def test_archive_manifest_matches_md5sum_and_refuses_escaping_members(tmp_path):
    files = {"build_soc/b.v": "b\n", "build_soc/a/x.sv": "x\n", "build_soc/A.v": "A\n"}
    link = tarfile.TarInfo("build_soc/link.v")
    link.type, link.linkname = tarfile.SYMTYPE, "b.v"
    m = A.manifest(_archive(tmp_path, files, extra=[link]))
    want = "".join("%s  %s\n" % (hashlib.md5(files[n].encode()).hexdigest(), n) for n in sorted(files))
    assert m.text == want and m.files == 3 and m.symlinks == 1 and m.manifest_md5 == hashlib.md5(want.encode()).hexdigest()
    with pytest.raises(A.ArchiveError):
        A.manifest(_archive(tmp_path, {"../evil.v": "x\n"}, name="bad.tar.gz"))


def test_vendor_content_in_a_release_file_fails_and_in_the_archive_warns(scene, tmp_path):
    from nanosoc_rm.scan.rules import specimen
    arc = _archive(tmp_path, dict(RTL, **{"build_soc/rtl/vendor.v": specimen("arm.banner")}))
    flist = tmp_path / "soc.flist"
    flist.write_text(specimen("arm.banner"))
    doc = D.draft("soc-main", "1.1.0", archive=arc, artefacts=[("flists", str(flist))], out=tmp_path / "d")
    c = _checks(doc)
    assert c["vendor.scan"]["verdict"] == "PASS"
    priv = c["vendor.scan-private"]
    assert priv["verdict"] == "WARN" and "archive:build_soc/rtl/vendor.v" in priv["detail"] \
        and "artefact:soc.flist" in priv["detail"]
    assert D._scan_check("vendor.scan", [("RELEASE_NOTES.md", specimen("arm.banner").encode())],
                         False)["verdict"] == "FAIL"


def test_usage_errors_and_an_existing_draft(scene, tmp_path):
    for args in (["soc-main", "1.0"], ["nope", "1.0.0"], ["soc-main", "1.0.0", "--archive", "/no/such"],
                 ["soc-main", "1.0.0", "--waiver", "no-reason"], ["soc-main", "1.0.0", "--artefact", "zip=/x"]):
        res = CliRunner().invoke(cli.main, ["release", "draft", *args, "--out", str(tmp_path / "u")])
        assert res.exit_code == 3, (args, res.output)
    D.draft("soc-main", "1.1.0", out=tmp_path / "e")
    with pytest.raises(ReleaseUsage):
        D.draft("soc-main", "1.1.0", out=tmp_path / "e")
    assert D.draft("soc-main", "1.1.0", out=tmp_path / "e", replace=True)["fact"]["version"] == "1.1.0"


def test_classes_and_ladder():
    assert N.classify("feat(x): add\n") == ("feature", "inferred")
    assert N.classify("fix: y\n\nChange-Class: breaking\n") == ("breaking", "declared")
    assert N.classify("refactor!: drop the old port\n") == ("breaking", "inferred")
    assert N.classify("docs: z\n") == ("none", "inferred")
    assert N.classify("Tidy up\n") == ("unclassed", "none")
    assert ladder(["PASS", "WARN", "NOT-RUN"]) == "WARN" and ladder(["NOT-RUN"]) == "UNVERIFIED"
    assert ladder(["WAIVED", "UNVERIFIED", "PASS"]) == "UNVERIFIED" and ladder(["INFRA", "FAIL"]) == "FAIL"


# ---------------------------------------------------------------------------------------------------------
# forks (I1, I2)
# ---------------------------------------------------------------------------------------------------------
class _Answer:
    def __init__(self, status, body, path):
        self.status, self.body, self.error = status, body, None
        self.source = {"kind": "github_api", "command": "GET %s" % path}


def _fake_get(forks, seen):
    def get(config, host, path, params=None, **kw):
        seen.append(dict(config.get("credentials") or {}))
        if path == "repos/SoC-Labs/ip/forks" and (params or {}).get("page", 1) == 1:
            return _Answer(200, forks, path)
        return _Answer(200, [], path)
    return get


@pytest.mark.requirement("I1")
@pytest.mark.requirement("I2")
def test_forks_report_divergence_and_user_paths(at_pins, tmp_path, monkeypatch):
    s = Scene(at_pins, tmp_path / "w")
    try:
        h = s.h
        # the upstream example declares a fork contract (user paths: user/) and its upgrade steps
        manifest = {"fork_contract": {"upstream": None, "user_paths": ["user/"]}}
        up2 = h.commit(GH, "SoC-Labs/ip", parents=[s.i1], files={
            "rtl/ip.v": "module ip2; endmodule\n", "nanosoc-component.yaml": yaml.safe_dump(manifest),
            "README.md": "Upgrade: git merge v1.0.0, then git submodule update --init --recursive\n"},
            message="docs: fork contract")
        h.push(GH, "SoC-Labs/ip", up2, "main")
        h.tag(GH, "SoC-Labs/ip", "v1.0.0", s.i1)
        mine = h.commit(GH, "SoC-Labs/ip", parents=[s.i1], files={"rtl/ip.v": "module mine; endmodule\n",
                                                                    "user/app.c": "int main;\n"}, message="mine")
        h.push(GH, "SoC-Labs/ip", mine, "main", to_path="someone/ip")
        h.git("--git-dir", h.bare(GH, "someone/ip"), "symbolic-ref", "HEAD", "refs/heads/main")
        s.sync()
        cfg = s.config()
        cfg["credentials"] = {"github_read": {"token_file": "/nonexistent"}}
        # the fork fetch ignores every git config (anonymity), so it reaches the local host through the
        # contracted `hosts.<host>.git` fetch base (interfaces.md section 17.1), not through url.insteadOf
        cfg["hosts"]["github"]["git"] = (h.hosts_dir / "github").as_uri()
        seen = []
        with s.env.active():
            docs = F.forks("ip", config=cfg, get=_fake_get([{"full_name": "someone/ip", "default_branch": "main"}], seen))
        assert seen and all(c == {} for c in seen), "a fork listing must be anonymous (I1)"
        assert len(docs) == 1 and contracts.validate_observation(docs[0]) == []
        f = docs[0]["fact"]
        assert (f["ahead"], f["behind"], f["merge_base"], f["based_on"]) == (1, 1, s.i1, "v1.0.0"), f["reason"]
        assert f["releases_behind"] == 0 and f["contract"] == {"declared": True, "upgrade_steps": True}
        assert f["user_paths"]["verdict"] == "FAIL" and f["user_paths"]["outside"] == ["rtl/ip.v"]
        assert f["verdict"] == "WARN"
        # a listing the host refuses is one UNVERIFIED row, never silence
        def refused(config, host, path, params=None, **kw):
            a = _Answer(403, {"message": "rate limit"}, path)
            return a
        with s.env.active():
            bad = F.forks("ip", config=cfg, get=refused)
        assert [d["fact"]["verdict"] for d in bad] == ["UNVERIFIED"] and "403" in bad[0]["fact"]["reason"]
    finally:
        s.h.api.close()


# ---------------------------------------------------------------------------------------------------------
# job kinds and pages (CR-R2)
# ---------------------------------------------------------------------------------------------------------
def test_job_kinds_check_their_params(scene, tmp_path):
    cfg = scene.config()
    with pytest.raises(jobs.JobUsage):
        jobs.submit(cfg, "release-draft", {"configuration": "soc-main", "version": "1.0"}, "alice")
    with pytest.raises(jobs.JobUsage):
        jobs.submit(cfg, "release-draft", {"configuration": "soc-main", "version": "1.0.0", "archive": "rel/x"}, "alice")
    j = jobs.submit(cfg, "release-draft", {"configuration": "soc-main", "version": "1.2.0",
                                           "waivers": ["*=old"]}, "alice")
    assert j["command"] == "nanosoc-rm release draft soc-main 1.2.0 --waiver '*=old'"
    assert jobs.submit(cfg, "forks", {}, "alice")["command"] == "nanosoc-rm forks --store"
    assert jobs.submit(cfg, "release-verify", {"configuration": "soc-main", "version": "1.2.0"}, "alice")["command"] \
        == "nanosoc-rm release verify soc-main/1.2.0"
    assert jobs.Worker(scene.env.config).step() == j["id"]
    done = jobs.get(cfg, j["id"])
    assert done["state"] == "done" and done["verdict"] == "UNVERIFIED", done
    assert (D.draft_path(cfg, "soc-main", "1.2.0") / "draft.json").is_file()


def test_release_pages(scene, tmp_path):
    from fastapi.testclient import TestClient
    from nanosoc_rm import auth
    from nanosoc_rm.web import create_app
    with open(scene.env.config, "a") as fh:
        fh.write('[roles]\ndefault = ["member"]\n')
    cfg = scene.config()
    D.draft("soc-v1", "1.0.0", out=D.draft_path(cfg, "soc-v1", "1.0.0"))
    c = TestClient(create_app(scene.env.config, auth=auth.FakeMembership(login="alice")))
    idx = c.get("/releases/")
    assert idx.status_code == 200 and 'data-draft="soc-v1/1.0.0"' in idx.text and "data-publish-note" in idx.text
    assert '<a href="/releases/" aria-current="page" class="current">Releases</a>' in idx.text
    page = c.get("/releases/soc-v1/1.0.0")
    assert page.status_code == 200 and 'data-check="lock.pins-tagged" data-verdict="FAIL"' in page.text
    assert 'data-pin="gen" data-protection="keep-tag"' in page.text and 'data-age-seconds="' in page.text
    assert 'data-source-for="release_draft:soc-v1:1.0.0"' in page.text
    er = c.get("/releases/soc-v1/1.0.0/errata")
    assert er.status_code == 200 and 'data-erratum="DEMO-R0P0-ERR-001"' in er.text and er.text.count("<html") == 1
    z = c.get("/releases/soc-v1/1.0.0/download.zip")
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert z.status_code == 200 and "soc-v1-1.0.0/release.lock.draft.yaml" in names and "soc-v1-1.0.0/draft.json" in names
    assert c.get("/releases/soc-v1/1.0.0/files/ERRATA.md").text.startswith("# Errata: demo silicon r0p0")
    for bad in ("/releases/soc-v1/1.0.0/files/..%2Fdraft.json", "/releases/soc-v1/9.9.9", "/releases/x/1.0.0/errata"):
        assert c.get(bad).status_code == 404, bad
    # POST queues a job (local deployment: no [ci] project); the page shows its card
    csrf = c.get("/releases/").text.split('"X-CSRF-Token": "', 1)[1].split('"', 1)[0]
    r = c.post("/releases/jobs", data={"configuration": "soc-main", "version": "1.3.0", "archive": "../../etc/passwd",
                                       "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "job=" in r.headers["location"]
    job = jobs.jobs(cfg, kind="release-draft")[0]
    assert job["params"]["archive"] is None, "a page can only pick a file from release-inputs/"
    assert c.post("/releases/soc-v1/1.0.0/verify", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    fk = c.get("/releases/forks")
    assert fk.status_code == 200 and "No fork listing yet" in fk.text


def test_nothing_in_the_package_can_push_or_tag():
    """Hard rule 1: the release modules run git only through gitrun (which refuses push) and declare no write."""
    root = Path(__file__).resolve().parents[3] / "src" / "nanosoc_rm" / "releases"
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    assert "subprocess" not in text and "httpx" not in text
    assert not [w for w in contracts.WRITE_CALLS if "releases/" in w["file"]]
    from nanosoc_rm.gitrun import GitRefused, run_git
    with pytest.raises(GitRefused):
        run_git("push", "origin", "refs/tags/v1.0.0")
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


def test_an_unreadable_pin_is_not_judged_never_unprotected(scene, tmp_path, monkeypatch):
    """A private repo read without a token is UNREADABLE: its protection is `unreadable` and the check is
    UNVERIFIED (could not answer), not FAIL (the real eth-rc4 draft's ASIC toolkit pin)."""
    from nanosoc_rm.releases import lock as L
    from nanosoc_rm.walker import walk
    assert L.host_protection({"state": "UNREADABLE", "tags": [], "branches": [], "default_branch": None,
                              "lag": None}) == "unreadable"

    def unreadable_gen(cid, config):
        docs = walk(cid, config=config, store=False, alerts=False)
        for d in docs:
            if d["fact"]["path"] == "gen":
                for st in d["fact"]["hosts"].values():
                    st.update(state="UNREADABLE", tags=[], branches=[])
        return docs
    doc = D.draft("soc-v1", "1.0.0", out=tmp_path / "d", walk=unreadable_gen)
    c = _checks(doc)["lock.pins-protected"]
    assert c["verdict"] == "UNVERIFIED" and "gen" in c["detail"] and doc["fact"]["lock"]["unprotected"] == []
    lock = yaml.safe_load((tmp_path / "d" / "release.lock.draft.yaml").read_text())
    assert {p["path"]: p["protection"]["by"] for p in lock["pins"]}["gen"] == "unreadable"


# ---------------------------------------------------------------------------------------------------------
# review items (R-1 to R-7)
# ---------------------------------------------------------------------------------------------------------
def test_replace_never_deletes_a_directory_that_is_not_a_draft(scene, tmp_path):
    """R-1: --out naming a checkout with --replace is refused, and the directory is left whole."""
    precious = tmp_path / "precious"
    (precious / ".git").mkdir(parents=True)
    (precious / "keep.txt").write_text("mine\n")
    with pytest.raises(ReleaseUsage, match="holds no draft.json"):
        D.draft("soc-v1", "1.0.0", out=precious, replace=True)
    assert sorted(p.name for p in precious.iterdir()) == [".git", "keep.txt"]
    res = CliRunner().invoke(cli.main, ["release", "draft", "soc-v1", "1.0.0", "--out", str(precious), "--replace"])
    assert res.exit_code == 3 and (precious / "keep.txt").read_text() == "mine\n", res.output
    f = tmp_path / "a-file"
    f.write_text("x")
    with pytest.raises(ReleaseUsage):
        D.draft("soc-v1", "1.0.0", out=f, replace=True)
    assert f.read_text() == "x"


def test_verify_rejudges_the_drafted_tree_after_the_branch_moves(scene, tmp_path):
    """R-2: a moved branch is WARN, but the drafted commit's pins are still re-judged: a pin that lost its
    protection is drift (FAIL), never a blind WARN."""
    d = tmp_path / "d"
    D.draft("soc-main", "2.0.0", out=d)
    h = scene.h
    s2 = h.commit(GH, "SoC-Labs/soc", parents=[scene.s1], files={"README": "soc 2\n"}, message="docs: two")
    h.push(GH, "SoC-Labs/soc", s2, "main")
    scene.sync()
    calm = D.verify(d)["fact"]
    cc = {c["id"]: c for c in calm["checks"]}
    assert cc["release.commit"]["verdict"] == "WARN" and cc["lock.pins"]["verdict"] == "PASS", calm
    assert calm["verdict"] == "WARN" and calm["drift"] == []
    h.delete_remote_ref(GH, "SoC-Labs/nanosoc_gen", "refs/tags/keep/soc-v1/nanosoc_gen")
    h.delete_remote_ref(GH, "SoC-Labs/nanosoc_gen", "refs/heads/dev")
    scene.sync()
    f = D.verify(d)["fact"]
    c = {x["id"]: x for x in f["checks"]}
    assert c["release.commit"]["verdict"] == "WARN" and c["lock.pins"]["verdict"] == "FAIL", f
    assert f["verdict"] == "FAIL" and any(x.startswith("gen: state PROTECTED_BY_TAG") for x in f["drift"]), f


def test_verify_sees_an_added_file_a_changed_archive_and_changed_errata(scene, tmp_path):
    """R-3 (mutations R3, R7, R10): each verify check can fail on its own."""
    d = tmp_path / "d"
    D.draft("soc-v1", "1.0.0", archive=_archive(tmp_path, RTL), out=d)
    (d / "extra.txt").write_text("added later\n")
    f = D.verify(d)["fact"]
    assert "extra.txt was added after the draft" in f["drift"] and f["verdict"] == "FAIL"
    (d / "extra.txt").unlink()
    other = _archive(tmp_path, dict(RTL, **{"build_soc/rtl/top.v": "module other; endmodule\n"}), name="o.tar.gz")
    (d / "build_soc.tar.gz").write_bytes(other.read_bytes())
    c = {x["id"]: x for x in D.verify(d)["fact"]["checks"]}
    assert c["rtl.archive"]["verdict"] == "FAIL" and c["errata.digest"]["verdict"] == "PASS"
    d2 = tmp_path / "d2"
    D.draft("soc-v1", "1.0.0", out=d2)
    scene.write_registry(errata_rev=1, conditions="only on Tuesdays")
    f = D.verify(d2)["fact"]
    c = {x["id"]: x for x in f["checks"]}
    assert c["errata.digest"]["verdict"] == "FAIL" and f["verdict"] == "FAIL", f


def test_canonical_gaps_category_a_and_t0_evidence(scene, tmp_path, monkeypatch):
    """R-3 (mutations R1, R4, R5, R6): a canonical host without the commit WARNs; an open Category A erratum
    FAILs; a forced T1 is not evidence; a T0 result of another commit is not evidence."""
    from nanosoc_rm.walker import walk

    def gap_on_a_second_host(cid, config):
        docs = walk(cid, config=config, store=False, alerts=False)
        for d in docs:
            if d["fact"]["path"] == "ip":
                st = next(iter(d["fact"]["hosts"].values()))
                d["fact"]["hosts"]["gitlab"] = dict(st, pinned=False, role="canonical", state="MISSING", tags=[],
                                                    branches=[], repo={"host": "gitlab", "path": "soclabs/ip"})
        return docs
    c = _checks(D.draft("soc-v1", "1.0.0", out=tmp_path / "g", walk=gap_on_a_second_host))
    assert c["lock.pins-canonical"]["verdict"] == "WARN" and "ip (gitlab: not-held)" in c["lock.pins-canonical"]["detail"]
    assert _checks(D.draft("soc-v1", "1.0.0", out=tmp_path / "n"))["lock.pins-canonical"]["verdict"] == "PASS"

    scene.write_registry(category="A")
    doc = D.draft("soc-v1", "1.0.0", out=tmp_path / "a")
    assert _checks(doc)["errata.category-a"]["verdict"] == "FAIL" and "DEMO-R0P0-ERR-001" in \
        _checks(doc)["errata.category-a"]["detail"]

    import nanosoc_rm.releases.checklist as CL
    stored = []
    monkeypatch.setattr(CL.db, "latest", lambda kind, prefix, config=None: [x for x in stored if x["kind"] == kind])

    def result(commit, tier=None, forced=False, verdict="PASS"):
        fact = {"configuration": "soc-v1", "commit": commit, "verdict": verdict}
        if tier:
            fact["tier"] = {"tier": tier, "verdict": verdict, "forced": forced}
        return {"kind": "compat_result", "subject": "compat_result:soc-v1:%s:%s" % (commit[:7], tier or "t0"),
                "observed_at": "2026-10-13T00:00:00Z", "fact": fact}
    stored[:] = [result(scene.s0)]
    c = {x["id"]: x for x in CL.tier_checks(scene.config(), "soc-v1", scene.s1, "fix")[0]}
    assert c["tier.t0"]["verdict"] == "UNVERIFIED", "a T0 result of another commit is not evidence"
    stored[:] = [result(scene.s1), result(scene.s1, tier="t1", forced=True)]
    c = {x["id"]: x for x in CL.tier_checks(scene.config(), "soc-v1", scene.s1, "fix")[0]}
    assert c["tier.t0"]["verdict"] == "PASS"
    assert c["tier.t1"]["verdict"] == "UNVERIFIED" and "forced past" in c["tier.t1"]["detail"]


def test_errata_revision_remembers_a_replaced_or_deleted_draft(scene, tmp_path):
    """R-4: rebuilding the same version with --replace, or deleting the earlier draft, does not reset F4's memory."""
    import shutil
    where = D.draft_path(scene.config(), "soc-v1", "1.0.0")
    D.draft("soc-v1", "1.0.0", out=where)
    scene.write_registry(errata_rev=1, conditions="only on Tuesdays")
    c = _checks(D.draft("soc-v1", "1.0.0", out=where, replace=True, store=False))
    assert c["errata.revision"]["verdict"] == "FAIL" and "still 1" in c["errata.revision"]["detail"]
    shutil.rmtree(where)
    c = _checks(D.draft("soc-v1", "1.0.1", out=tmp_path / "x", store=False))
    assert c["errata.revision"]["verdict"] == "FAIL", "the store's history is the memory once a folder is gone"


def test_every_shared_file_is_scanned_and_says_it_is_a_draft(scene, tmp_path):
    """R-5: the vendor scan covers GATES.md, CHECKLIST.md and README.txt too. R-6: every generated file says it is
    a draft that nothing published. R-7: G3's file lists, bitstream and logs are named when absent."""
    from nanosoc_rm.releases.core import PUBLISH_NOTE
    d = tmp_path / "d"
    clean = D.draft("soc-v1", "1.0.0", out=d)
    shared = [x["name"] for x in clean["fact"]["files"]]
    assert len(shared) == 7 and {"GATES.md", "CHECKLIST.md", "README.txt"} <= set(shared)
    assert _checks(clean)["vendor.scan"]["detail"].startswith("7 release file(s) scanned"), _checks(clean)["vendor.scan"]
    for name in ("RELEASE_NOTES.md", "ERRATA.md", "ERRATA.html", "CHECKLIST.md", "GATES.md", "README.txt",
                 "release.lock.draft.yaml"):
        assert PUBLISH_NOTE in (d / name).read_text(), name
    assert "Draft." in (d / "ERRATA.md").read_text() and "data-publish-note" in (d / "ERRATA.html").read_text()
    assert _checks(clean)["lock.artefacts"]["verdict"] == "WARN"
    arts = []
    for k in ("flists", "bitstream", "logs"):
        p = tmp_path / ("%s.txt" % k)
        p.write_text(k)
        arts.append((k, str(p)))
    assert _checks(D.draft("soc-v1", "1.0.0", out=d, replace=True, artefacts=arts))["lock.artefacts"]["verdict"] == "PASS"
