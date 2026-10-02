"""Registry records as facts (interfaces.md section 2 "Facts"; A3, H1): read by the real registry loader, stored
by its one producer (registry.store_records), snapshotted per file."""
from __future__ import annotations

from nanosoc_rm import contracts, db
from nanosoc_rm.config import load_config
from nanosoc_rm.web import registry_facts
from webfix import PLANTED, git, make_registry, plant_problem, write_config


def _config(service, reg):
    cfg = write_config(service["cfg"], data=service["tmp"] / "data", github_url=service["github"].url, registry=reg)
    return load_config(cfg)


def test_refresh_stores_every_record_as_a_valid_fact(service, registry_spy):
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
        assert d["sources"] == [{"kind": "registry", "command": "git -C %s show %s:%s" % (reg.resolve(), head, d["fact"]["file"])}]
    confs = registry_facts.current(config, "configuration")
    assert [(d["fact"]["id"], d["fact"]["pointer"]) for d in sorted(confs, key=lambda d: d["fact"]["pointer"])] == [
        ("eth-rc4", "/configurations/0"), ("eth-main", "/configurations/1"), ("multicore-master", "/configurations/2")]
    (er,) = registry_facts.current(config, "erratum")
    assert er["subject"] == "registry_record:errata/eth-rc4/ERR-001.yaml#" and er["fact"]["id"] == "ETH-R0P0-ERR-001"
    # The record is the file's content at that commit, exactly.
    raw = contracts.load_yaml(git("show", "%s:eligibility.yaml" % head, cwd=reg))
    assert confs[0]["fact"]["record"] in raw["configurations"]
    assert registry_spy.calls, "the contracted loader validated the checkout first"


def test_refresh_is_a_noop_while_head_is_unchanged(service, registry_spy):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    before = len(db.history("registry_record:eligibility.yaml#/configurations/0", config=config))
    n = len(registry_spy.calls)
    assert registry_facts.refresh(config) == []
    assert len(db.history("registry_record:eligibility.yaml#/configurations/0", config=config)) == before
    assert len(registry_spy.calls) == n


def test_a_deleted_record_leaves_the_current_view(service, registry_spy):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    assert [d["fact"]["id"] for d in registry_facts.current(config, "defect")] == ["SL-DEF-0001"]
    make_registry(reg, drop_defect=True, extra_configs=[{"id": "estate-main", "component": "nanosoc-ethernet-chiplet",
                                                         "ref": {"branch": "main"}}])
    assert registry_facts.refresh(config) == []
    assert registry_facts.current(config, "defect") == []
    assert registry_facts.current(config, "erratum") == []
    assert "estate-main" in {d["fact"]["id"] for d in registry_facts.current(config, "configuration")}


def test_a_registry_with_problems_is_not_loaded_and_the_last_good_one_stays(service, registry_spy):
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    registry_facts.refresh(config)
    good = git("rev-parse", "HEAD", cwd=reg)
    plant_problem(reg)
    problems = registry_facts.refresh(config)
    assert any(PLANTED in p and "rule-7" in p for p in problems), problems
    assert any("was not loaded" in p for p in problems)
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good}


def test_no_checkout_configured_or_unreadable(service, registry_spy):
    config = load_config(service["cfg"])
    assert any("no registry checkout" in p for p in registry_facts.refresh(config))
    config = _config(service, service["tmp"] / "missing")
    assert any("not readable" in p for p in registry_facts.refresh(config))


def test_a_dirty_checkout_is_not_loaded_and_its_head_is_not_stored(service, registry_spy):
    """Review W1: HEAD holds a registry the loader refuses; the working tree holds a valid one, uncommitted.
    What the loader validated (the working tree) is not what `git show HEAD:<file>` reads, so nothing is
    stored, the last good records stay, and the page is told why."""
    reg = make_registry(service["tmp"] / "registry")
    config = _config(service, reg)
    assert registry_facts.refresh(config) == []
    good = git("rev-parse", "HEAD", cwd=reg)
    plant_problem(reg)                                         # the loader refuses this tree...
    bad = git("rev-parse", "HEAD", cwd=reg)
    (reg / PLANTED).unlink()                                   # ...and accepts the working tree, uncommitted
    problems = registry_facts.refresh(config)
    assert any("not committed" in p for p in problems), problems
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good} != {bad}
    git("checkout", "-q", "--", PLANTED, cwd=reg)              # clean again: HEAD is refused as itself
    assert any(PLANTED in p for p in registry_facts.refresh(config))
    assert {d["fact"]["commit"] for d in registry_facts.current(config)} == {good}
