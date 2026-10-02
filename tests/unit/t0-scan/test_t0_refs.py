"""t0.registry.refs-resolve (F3): every structured citation proved on its component's canonical host, hermetic."""
from __future__ import annotations

import yaml

from nanosoc_rm.t0 import gates

GID = "t0.registry.refs-resolve"


def registry(tree, root, *, with_c2=True, with_private=False, unknown=False):
    root.mkdir(parents=True)
    comps = [{"id": n, "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/" + n}}
             for n in ("rm-sub-a", "rm-leaf", "rm-sub-c", "rm-private")]
    (root / "catalogue.yaml").write_text(yaml.safe_dump({"schema": 1, "components": comps}))
    (root / "eligibility.yaml").write_text(yaml.safe_dump(
        {"schema": 1, "configurations": [{"id": "c", "component": "rm-sub-a", "ref": {"branch": "main"},
                                          "commit": tree.a1}]}))     # a configuration commit is not a citation
    defect = {"schema": 1, "id": "SL-DEF-0001", "component": "rm-sub-a",
              "origin": [{"component": "rm-leaf", "commit": tree.l2}],
              "regression_test": ({"component": "rm-sub-c", "commit": tree.c2, "path": "t", "expect": {}}
                                  if with_c2 else None)}
    if with_private:
        defect["origin"].append({"component": "rm-private", "commit": tree.a1})
    if unknown:
        defect["origin"].append({"component": "not-catalogued", "commit": tree.a1})
    (root / "defects").mkdir()
    (root / "defects" / "SL-DEF-0001.yaml").write_text(yaml.safe_dump(defect))
    (root / "errata" / "c").mkdir(parents=True)
    (root / "errata" / "c" / "ERR-001.yaml").write_text(yaml.safe_dump(
        {"schema": 1, "cites": [{"component": "rm-sub-a", "commit": tree.a1}]}))
    (root / "imports.yaml").write_text(yaml.safe_dump(
        {"schema": 1, "sources": [{"doc": {"component": "rm-leaf", "commit": tree.l1, "path": "README"}}]}))
    return root


def run(tree, tmp_path, reg, name="w"):
    url_map = dict(tree.url_map)
    url_map["https://github.com/SoC-Labs/rm-private.git"] = str(tmp_path / "private-not-readable.git")
    ctx = gates.Context.for_tests(nonce="12" * 16, workdir=tmp_path / name, registry_dir=reg, url_map=url_map)
    row, ctl = gates.run_with_control(GID, ctx)
    return row, ctl, ctx.items(GID)


def test_all_cited_commits_on_their_canonical_host_pass(tree, tmp_path):
    row, ctl, items = run(tree, tmp_path, registry(tree, tmp_path / "r0", with_c2=False))
    assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (row, ctl)
    assert {(i["component"], i["commit"], i["result"]) for i in items} == {
        ("rm-leaf", tree.l2, "pass"), ("rm-sub-a", tree.a1, "pass"), ("rm-leaf", tree.l1, "pass")}
    tag_only = [i for i in items if i["commit"] == tree.l2][0]
    assert "refs/tags/v1" in tag_only["why"] and tag_only["host"] == "github"
    assert {(i["file"], i["pointer"]) for i in items} == {
        ("defects/SL-DEF-0001.yaml", "/origin/0/commit"), ("errata/c/ERR-001.yaml", "/cites/0/commit"),
        ("imports.yaml", "/sources/0/doc/commit")}


def test_a_commit_only_on_the_copy_fails_naming_file_pointer_and_commit(tree, tmp_path):
    row, ctl, items = run(tree, tmp_path, registry(tree, tmp_path / "r1"))
    assert (row["status"], ctl["status"]) == ("FAIL", "PASS")
    failed = [i for i in items if i["result"] == "fail"]
    assert [(i["file"], i["pointer"], i["commit"]) for i in failed] == [
        ("defects/SL-DEF-0001.yaml", "/regression_test/commit", tree.c2)]
    assert tree.c2[:7] in row["detail"]


def test_private_canonical_is_unverified_and_unknown_component_fails(tree, tmp_path):
    row, _, items = run(tree, tmp_path, registry(tree, tmp_path / "r2", with_c2=False, with_private=True))
    assert row["status"] == "UNVERIFIED"
    assert [i["component"] for i in items if i["result"] == "unverified"] == ["rm-private"]
    row, _, items = run(tree, tmp_path, registry(tree, tmp_path / "r3", with_c2=False, unknown=True), "w3")
    assert row["status"] == "FAIL" and [i["component"] for i in items if i["result"] == "fail"] == ["not-catalogued"]


def test_no_registry_is_unverified_and_the_control_does_not_fire(tmp_path):
    row, ctl = gates.run_with_control(GID, gates.Context.for_tests(nonce="0" * 32, workdir=tmp_path))
    assert (row["status"], ctl["status"]) == ("UNVERIFIED", "FAIL")


def test_example_registry_citations_are_r0s_ten_commits_plus_the_bench(at, tmp_path):
    """The extraction alone (no network): the example registry cites exactly AT-F3's 10 commits plus the
    unpushed bench, each at the pointer a person would name."""
    from nanosoc_rm.t0.gates import registry_citations
    reg = at.example_registry(tmp_path / "ex")
    cites, unreadable = registry_citations(reg)
    assert unreadable == []
    assert {(c["component"], c["commit"]) for c in cites} == at.R0_REFS | {
        ("nanosoc-ethernet-chiplet", at.BENCH_UNPUSHED)}
    assert ("defects/SL-DEF-0001.yaml", "/regression_test/commit") in {(c["file"], c["pointer"]) for c in cites}
