"""nanosoc_rm.registry.imports: the F5 census (interfaces.md section 2a) and its git reader."""
from __future__ import annotations

import shutil

import pytest
from registry_helpers import edit, example_registry, git, git_registry

from nanosoc_rm.config import ConfigError, load_config
from nanosoc_rm.registry import load_registry
from nanosoc_rm.registry.imports import CensusRow, ReadError, census, census_source, git_reader

DOC = "docs/register.md"
TEXT = "# R\n## a) one\n## b) two\nnot ## c) an item\n## c) three\n## b) two again\n"


def source(items, rule=None, path=DOC):
    doc = {"component": "nanosoc-ethernet-chiplet", "commit": "1" * 40, "path": path}
    if rule is not None:
        doc["items_rule"] = rule
    return {"doc": doc, "items": items}


def item(key, disposition="dismissed", **kw):
    it = {"key": key, "title": "t", "disposition": disposition}
    if disposition == "dismissed":
        it["reason"] = "r"
    it.update(kw)
    return it


def read(component, commit, path):
    return TEXT


RULE = {"pattern": r"^## ([a-z]\)) "}


def test_done_when_ledger_matches_census():
    row = census_source(source([item("a)"), item("b)"), item("c)", "record", record="SL-DEF-0001")], RULE), read)
    assert isinstance(row, CensusRow)
    assert (row.census, row.missing, row.extra, row.pending, row.done, row.error) == (["a)", "b)", "c)"], [], [], 0,
                                                                                      True, None)
    assert (row.path, row.commit, row.component) == (DOC, "1" * 40, "nanosoc-ethernet-chiplet")


def test_missing_extra_and_pending():
    row = census_source(source([item("a)", "pending"), item("z)")], RULE), read)
    assert (row.missing, row.extra, row.pending, row.done) == (["b)", "c)"], ["z)"], 1, False)
    row = census_source(source([item("a)", "pending"), item("b)"), item("c)")], RULE), read)
    assert (row.missing, row.extra, row.pending, row.done) == ([], [], 1, False)


def test_exclude_and_include():
    rule = dict(RULE, exclude=[{"key": "b)", "reason": "not an issue"}],
                include=[{"key": "addendum-1", "where": "2026-08-12 addendum"}])
    row = census_source(source([item("a)"), item("c)"), item("addendum-1")], rule), read)
    assert row.census == ["a)", "c)", "addendum-1"] and row.done
    assert row.excluded == [{"key": "b)", "reason": "not an issue"}]


@pytest.mark.parametrize("rule, why", [(None, "no items_rule"), ({"pattern": "([a-"}, "does not compile"),
                                       ({"pattern": "^## "}, "no group")])
def test_a_source_without_a_usable_rule_is_not_done(rule, why):
    row = census_source(source([item("a)")], rule), read)
    assert not row.done and why in row.error and row.extra == ["a)"]


def test_unreadable_document_is_not_done():
    def fail(component, commit, path):
        raise ReadError("host said 404")
    row = census_source(source([item("a)")], RULE), fail)
    assert not row.done and row.error.startswith("cannot read") and "404" in row.error



def test_a_config_error_in_the_reader_is_not_an_unreadable_source():
    """ConfigError leaves census, so the CLI exits 3 (review R6); any other reader failure, a KeyError from a
    dict-backed reader included, is still `cannot read`."""
    def unconfigured(component, commit, path):
        raise ConfigError("[hosts.github] is required for this command")
    with pytest.raises(ConfigError):
        census_source(source([item("a)")], RULE), unconfigured)
    row = census_source(source([item("a)")], RULE), lambda c, s, p: {}[p])
    assert not row.done and row.error.startswith("cannot read")


def test_census_of_a_ledger_keeps_source_order(example_registry):
    edit(example_registry / "imports.yaml", lambda d: [s["doc"].update(items_rule=dict(RULE)) for s in d["sources"]])
    ledger = load_registry(example_registry).imports()
    texts = {"docs/tapeout/11-known-issues.md": "## a) x\n## b) y\n", "docs/tapeout/16-open-defects.md": "## 1 x\n"}
    rows = census(ledger, lambda c, s, p: texts[p])
    assert [r.path for r in rows] == list(texts)
    assert [r.pending for r in rows] == [2, 1] and not any(r.done for r in rows)
    assert rows[1].census == [] and rows[1].extra == ["1"]          # RULE finds no numbered items


# ---------------------------------------------------------------------------------------------------------
# git_reader: mirror first, else an anonymous fetch of exactly the cited commit
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def host(tmp_path):
    """A local 'github' holding SoC-Labs/NanoSoC-Ethernet-Chiplet with one commit carrying DOC."""
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "docs").mkdir()
    (work / DOC).write_text(TEXT)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "doc", cwd=work)
    sha = git("rev-parse", "HEAD", cwd=work)
    base = tmp_path / "github"
    bare = base / "SoC-Labs" / "NanoSoC-Ethernet-Chiplet.git"
    bare.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", work, bare)
    cfg = tmp_path / "rm.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[hosts.github]\nweb = "https://github.invalid"\ngit = "file://%s"\n'
                   % (tmp_path / "data", base))
    return {"sha": sha, "config": load_config(cfg), "bare": bare, "data": tmp_path / "data"}


def test_git_reader_fetches_the_cited_commit_anonymously(host, example_registry):
    reader = git_reader(load_registry(example_registry), host["config"])
    assert reader("nanosoc-ethernet-chiplet", host["sha"], DOC) == TEXT
    with pytest.raises(ReadError):
        reader("nanosoc-ethernet-chiplet", "2" * 40, DOC)           # not on the host
    with pytest.raises(ReadError):
        reader("nanosoc-ethernet-chiplet", host["sha"], "docs/absent.md")


def test_git_reader_prefers_the_mirror(host, example_registry):
    mirror = host["data"] / "mirrors" / "nanosoc-ethernet-chiplet.git"
    git("clone", "-q", "--mirror", host["bare"], mirror)
    shutil.rmtree(host["bare"])                                     # the host no longer has the repo at all
    reader = git_reader(load_registry(example_registry), host["config"])
    assert reader("nanosoc-ethernet-chiplet", host["sha"], DOC) == TEXT


FIXTURES = (example_registry, git_registry)      # imported for pytest; named here for pyflakes
