"""Registry records as facts (interfaces.md section 2 "Facts"; A3, H1): read, validated, snapshotted per file."""
from __future__ import annotations

import sys

from nanosoc_rm import contracts, db
from nanosoc_rm.config import load_config
from nanosoc_rm.web import registry_facts
from webfix import git, make_registry, write_config


def _config(service, reg):
    cfg = write_config(service["cfg"], data=service["tmp"] / "data", github_url=service["github"].url, registry=reg)
    return load_config(cfg)


def test_refresh_stores_every_record_as_a_valid_fact(service, fake_registry_module):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    assert registry_facts.refresh(config) == []
    head = git("rev-parse", "HEAD", cwd=reg)
    docs = registry_facts.current(config)
    kinds = sorted({d["fact"]["record_kind"] for d in docs})
    assert kinds == ["component", "configuration", "defect", "erratum", "import_source"]
    for d in docs:
        assert db.check_observation(d) == [], d["subject"]
        assert d["fact"]["commit"] == head
        assert d["sources"] == [{"kind": "registry", "command": "git -C <registry> show %s:%s" % (head, d["fact"]["file"])}]
    confs = registry_facts.current(config, "configuration")
    assert [(d["fact"]["id"], d["fact"]["pointer"]) for d in sorted(confs, key=lambda d: d["fact"]["pointer"])] == [
        ("eth-rc4", "/configurations/0"), ("eth-main", "/configurations/1"), ("multicore-master", "/configurations/2")]
    (er,) = registry_facts.current(config, "erratum")
    assert er["subject"] == "registry_record:errata/eth-rc4/ERR-001.yaml#" and er["fact"]["id"] == "ETH-R0P0-ERR-001"
    # The record is the file's content at that commit, exactly.
    raw = contracts.load_yaml(git("show", "%s:eligibility.yaml" % head, cwd=reg))
    assert confs[0]["fact"]["record"] in raw["configurations"]
    assert fake_registry_module.calls, "the contracted loader validated the checkout first"


def test_refresh_is_a_noop_while_head_is_unchanged(service, fake_registry_module):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    before = len(db.history("registry_record:eligibility.yaml#/configurations/0", config=config))
    n = len(fake_registry_module.calls)
    assert registry_facts.refresh(config) == []
    assert len(db.history("registry_record:eligibility.yaml#/configurations/0", config=config)) == before
    assert len(fake_registry_module.calls) == n


def test_a_deleted_record_leaves_the_current_view(service, fake_registry_module):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    assert [d["fact"]["id"] for d in registry_facts.current(config, "defect")] == ["SL-DEF-0001"]
    make_registry(reg, drop_defect=True, extra_configs=[{"id": "estate-main", "component": "nanosoc-ethernet-chiplet",
                                                         "ref": {"branch": "main"}}])
    assert registry_facts.refresh(config) == []
    assert registry_facts.current(config, "defect") == []
    assert "estate-main" in {d["fact"]["id"] for d in registry_facts.current(config, "configuration")}


def test_a_registry_with_problems_is_not_loaded_and_the_last_good_one_stays(service, fake_registry_module):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    good = git("rev-parse", "HEAD", cwd=reg)
    (reg / "BROKEN").write_text("x\n")
    git("add", "BROKEN", cwd=reg)
    git("commit", "-q", "-m", "broken", cwd=reg)
    problems = registry_facts.refresh(config)
    assert any("planted problem" in p and "eligibility.yaml:/configurations/0" in p for p in problems), problems
    assert any("was not loaded" in p for p in problems)
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good}


def test_without_the_loader_nothing_is_read_and_stored_records_are_served(service, monkeypatch):
    monkeypatch.setitem(sys.modules, "nanosoc_rm.registry", None)       # import raises ImportError
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    problems = registry_facts.refresh(config)
    assert any("not installed" in p for p in problems)
    # The seeded example record is still the current one.
    assert [d["fact"]["commit"] for d in registry_facts.current(config)] == ["0" * 39 + "2"]


def test_no_checkout_configured_or_unreadable(service, fake_registry_module):
    config = load_config(service["cfg"])
    assert any("no registry checkout" in p for p in registry_facts.refresh(config))
    config = _config(service, service["tmp"] / "missing")
    assert any("not readable" in p for p in registry_facts.refresh(config))


def test_records_from_files_layout():
    files = {"eligibility.yaml": {"configurations": [{"id": "a"}, {"id": "b"}]},
             "defects/SL-DEF-0002.yaml": {"id": "SL-DEF-0002"}, "errata/a/ERR-001.yaml": {"id": "A-ERR-001"},
             "README.md": "not a record", "errata/stray.yaml": {"id": "x"}}
    out = registry_facts.records_from_files(files, "1" * 40, "2026-10-02T00:00:00Z")
    assert sorted(out) == ["defects/SL-DEF-0002.yaml", "eligibility.yaml", "errata/a/ERR-001.yaml"]
    assert [d["subject"] for d in out["eligibility.yaml"]] == ["registry_record:eligibility.yaml#/configurations/0",
                                                              "registry_record:eligibility.yaml#/configurations/1"]
    assert all(db.check_observation(d) == [] for rows in out.values() for d in rows)


def test_a_dirty_checkout_is_not_loaded_and_its_head_is_not_stored(service, fake_registry_module):
    """Review W1: HEAD holds a registry the loader refuses; the working tree holds a valid one, uncommitted.
    What the loader validated (the working tree) is not what `git show HEAD:<file>` reads, so nothing is
    stored, the last good records stay, and the page is told why."""
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    assert registry_facts.refresh(config) == []
    good = git("rev-parse", "HEAD", cwd=reg)
    (reg / "BROKEN").write_text("x\n")                         # the loader refuses this tree...
    git("add", "BROKEN", cwd=reg)
    git("commit", "-q", "-m", "broken at HEAD", cwd=reg)
    bad = git("rev-parse", "HEAD", cwd=reg)
    (reg / "BROKEN").unlink()                                  # ...and accepts the working tree, uncommitted
    problems = registry_facts.refresh(config)
    assert any("not committed" in p for p in problems), problems
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good} != {bad}
    git("checkout", "-q", "--", "BROKEN", cwd=reg)             # clean again: HEAD is refused as itself
    assert any("planted problem" in p for p in registry_facts.refresh(config))
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good}


def test_records_that_differ_from_what_was_validated_are_not_stored(service, fake_registry_module):
    """The records read at Registry.commit must be the records the loader validated: a loader that saw
    another tree (here, one configuration renamed) gets nothing stored."""
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    assert registry_facts.refresh(config) == []
    first = git("rev-parse", "HEAD", cwd=reg)
    make_registry(reg, extra_configs=[{"id": "estate-main", "component": "nanosoc-ethernet-chiplet",
                                       "ref": {"branch": "main"}}])

    def renamed(r):
        r._files["eligibility.yaml"]["configurations"][-1]["id"] = "something-else"
    fake_registry_module.tamper = renamed
    problems = registry_facts.refresh(config)
    assert any("differs" in p and "configuration records" in p for p in problems), problems
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {first}
    fake_registry_module.tamper = None
    assert registry_facts.refresh(config) == []
    assert "estate-main" in {d["fact"]["id"] for d in registry_facts.current(config, "configuration")}
