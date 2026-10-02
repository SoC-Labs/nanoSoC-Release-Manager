"""nanosoc_rm.registry loader: schema per file, README section 3 rules 1 to 13, never partial, commit."""
from __future__ import annotations

import re
import pytest
from registry_helpers import REPO, dump, edit, example_registry, git, git_registry, load, write_example_registry

from nanosoc_rm.registry import (CONFIGURATION_GATES, WAIVABLE_GATES, Problem, Registry, RegistryError, Repo,
                                 load_registry)
from nanosoc_rm.registry.rules import free_text_tokens

ERR = "errata/eth-rc4/ERR-001.yaml"
DEF = "defects/SL-DEF-0001.yaml"
RC4_ERR_RUN_D1 = {"result": "fired", "forms": ["A"], "input": {"path": "build_soc.tar", "md5": "0" * 32},
                  "detector_source": {"component": "nanosoc_gen", "commit": "adbb9abfaa81350e6f40d3b5c4e20f1f87b8002d"},
                  "command": "nanosoc-rm detect SL-DEF-0001 D1 --input build_soc.tar",
                  "observed_at": "2026-10-02T10:00:00Z"}


def problems(root) -> list[tuple[str, str, str]]:
    with pytest.raises(RegistryError) as ei:
        load_registry(root)
    assert ei.value.problems and all(isinstance(p, Problem) for p in ei.value.problems)
    return [(p.file, p.pointer, p.rule) for p in ei.value.problems]


# ---------------------------------------------------------------------------------------------------------
# The good registry
# ---------------------------------------------------------------------------------------------------------
def test_example_registry_loads(example_registry):
    reg = load_registry(example_registry)
    assert isinstance(reg, Registry)
    assert len(reg.components()) == 27 and len(reg.configurations()) == 3
    assert [d["id"] for d in reg.defects()] == ["SL-DEF-0001"]
    assert [e["id"] for e in reg.errata()] == ["ETH-R0P0-ERR-001"]
    assert reg.errata("eth-main") == [] and len(reg.errata("eth-rc4")) == 1
    assert reg.imports()["sources"][0]["doc"]["path"] == "docs/tapeout/11-known-issues.md"
    assert reg.configuration("eth-rc4")["commit"] == "ab73e40924317e45e7b3b941f52a7ed35f20832e"
    assert reg.component("nope") is None and reg.defect("SL-DEF-0001")["component"] == "nanosoc_gen"
    # timestamps stay strings (rule 8's reader)
    assert reg.configuration("eth-rc4")["t0_waivers"][0]["approved_on"] == "2026-10-01"
    assert reg.commit is None and reg.dirty is False                    # not a git checkout


def test_records_are_copies(example_registry):
    reg = load_registry(example_registry)
    reg.components()[0]["class"] = "legacy"
    reg.configurations()[0]["id"] = "x"
    reg.imports()["sources"].clear()
    assert reg.components()[0]["class"] == "ip_tech"
    assert reg.configurations()[0]["id"] == "eth-rc4"
    assert reg.imports()["sources"]


def test_imports_optional_but_catalogue_and_eligibility_required(example_registry):
    (example_registry / "imports.yaml").unlink()
    assert load_registry(example_registry).imports() is None
    (example_registry / "catalogue.yaml").unlink()
    (example_registry / "eligibility.yaml").unlink()
    assert sorted(problems(example_registry)) == [("catalogue.yaml", "", "layout"), ("eligibility.yaml", "", "layout")]


def test_not_a_directory(tmp_path):
    assert problems(tmp_path / "absent") == [("", "", "layout")]


# ---------------------------------------------------------------------------------------------------------
# Schema, version, YAML (rule 8), never partial
# ---------------------------------------------------------------------------------------------------------
def test_problems_from_every_file_are_reported_together(example_registry):
    edit(example_registry / "catalogue.yaml", lambda c: c["components"][0].pop("class"))
    edit(example_registry / DEF, lambda d: d["forms"][0]["fixed_in"][0].update(commit="036d381"))
    got = problems(example_registry)
    assert ("catalogue.yaml", "/components/0", "schema") in got
    assert (DEF, "/forms/0/fixed_in/0/commit", "schema") in got


def test_duplicate_key_is_rule_8(example_registry):
    p = example_registry / "eligibility.yaml"
    p.write_text(p.read_text().replace("    frozen: true\n", "    frozen: true\n    frozen: true\n", 1))
    assert problems(example_registry) == [("eligibility.yaml", "", "rule-8")]


def test_unparseable_yaml(example_registry):
    (example_registry / DEF).write_text("id: [unclosed\n")
    got = problems(example_registry)
    assert (DEF, "", "yaml") in got
    assert (ERR, "/defect", "rule-4") in got          # the erratum's defect is now unknown


def test_unknown_schema_version_is_refused_not_guessed(example_registry):
    edit(example_registry / "imports.yaml", lambda d: d.update(schema=2))
    assert problems(example_registry) == [("imports.yaml", "/schema", "version")]


def test_unquoted_date_stays_a_string(example_registry):
    p = example_registry / "eligibility.yaml"
    p.write_text(p.read_text().replace('approved_on: "2026-10-01"', "approved_on: 2026-10-01"))
    assert load_registry(example_registry).configuration("eth-rc4")["t0_waivers"][0]["approved_on"] == "2026-10-01"


# ---------------------------------------------------------------------------------------------------------
# Rules 1 to 13, one targeted breakage each
# ---------------------------------------------------------------------------------------------------------
def _comp(c, cid):
    return next(x for x in c["components"] if x["id"] == cid)


def _cfg(e, cid):
    return next(x for x in e["configurations"] if x["id"] == cid)


CASES = {
    "rule 1: component id twice": (
        "catalogue.yaml", lambda c: c["components"].append(
            {"id": "tidechart", "class": "toolkit", "canonical": {"host": "github", "path": "SoC-Labs/Other"}}),
        ("catalogue.yaml", "/components/27/id", "rule-1")),
    "rule 1: external id equals a component id": (
        "catalogue.yaml", lambda c: c["external"][0].update(id="tidechart"),
        ("catalogue.yaml", "/external/0/id", "rule-1")),
    "rule 1: a copy that is another component's canonical (case differs)": (
        "catalogue.yaml", lambda c: _comp(c, "tidelink").update(copies=[{"host": "gitlab", "path": "SOCLABS/tidechart"}]),
        ("catalogue.yaml", "/components/24/copies/0", "rule-1")),
    "rule 1: an ignored repo that is also an external": (
        "catalogue.yaml", lambda c: c["ignored"].append({"repo": dict(c["external"][1]["repo"]), "reason": "x"}),
        ("catalogue.yaml", "/ignored/3/repo", "rule-1")),
    "rule 2: unknown component": (
        "eligibility.yaml", lambda e: _cfg(e, "eth-main").update(component="no-such"),
        ("eligibility.yaml", "/configurations/1/component", "rule-2")),
    "rule 2: an ip_tech component": (
        "eligibility.yaml", lambda e: _cfg(e, "eth-main").update(component="tidechart"),
        ("eligibility.yaml", "/configurations/1/component", "rule-2")),
    "unique-id: configuration id twice": (
        "eligibility.yaml", lambda e: e["configurations"].append({"id": "eth-main", "component": "nanosoc-ethernet-chiplet",
                                                                  "ref": {"branch": "dev"}}),
        ("eligibility.yaml", "/configurations/3/id", "unique-id")),
    "rule 3: cites an unknown component": (
        ERR, lambda e: e["cites"][0].update(component="no-such"),
        (ERR, "/cites/0/component", "rule-3")),
    "rule 3: regression_test in an unknown component": (
        DEF, lambda d: d["regression_test"].update(component="no-such"),
        (DEF, "/regression_test/component", "rule-3")),
    "rule 3: the defect's own component unknown": (
        DEF, lambda d: d.update(component="no-such"),
        (DEF, "/component", "rule-3")),
    "rule 3: import record that does not exist": (
        "imports.yaml", lambda d: d["sources"][0]["items"][0].update(disposition="record", record="SL-DEF-0099"),
        ("imports.yaml", "/sources/0/items/0/record", "rule-3")),
    "rule 3: import source in an unknown component": (
        "imports.yaml", lambda d: d["sources"][0]["doc"].update(component="no-such"),
        ("imports.yaml", "/sources/0/doc/component", "rule-3")),
    "rule 4: unknown configuration": (
        ERR, lambda e: e.update(configuration="eth-r9"),
        (ERR, "/configuration", "rule-4")),
    "rule 4: unknown defect": (
        ERR, lambda e: e.update(defect="SL-DEF-0002"),
        (ERR, "/defect", "rule-4")),
    "rule 4: effect the defect lacks": (
        ERR, lambda e: e["effects_present"].append("E9"),
        (ERR, "/effects_present/5", "rule-4")),
    "rule 5: effect names an undefined form": (
        DEF, lambda d: d["effects"][0].update(forms=["A", "Z"]),
        (DEF, "/effects/0/forms/1", "rule-5")),
    "rule 5: detector names an undefined form": (
        DEF, lambda d: d["detectors"][1].update(forms=["Q"]),
        (DEF, "/detectors/1/forms/0", "rule-5")),
    "rule 9: uncited commit in erratum evidence": (
        ERR, lambda e: e["exposure"].update(evidence=e["exposure"]["evidence"] + " Also seen at 7a8b9c0."),
        (ERR, "/exposure/evidence", "rule-9")),
    "rule 9: uncited commit in a defect effect text": (
        DEF, lambda d: d["effects"][0].update(text="introduced by deadbe1"),
        (DEF, "/effects/0/text", "rule-9")),
    "rule 9: uncited commit in the defect trigger": (
        DEF, lambda d: d.update(trigger="since f1c435b"),
        (DEF, "/trigger", "rule-9")),
    "rule 10: fixed on silicon": (
        ERR, lambda e: e.update(status="fixed"),
        (ERR, "/status", "rule-10")),
    "rule 11: id outside the prefix": (
        ERR, lambda e: e.update(id="ETH-R0P1-ERR-001"),
        (ERR, "/id", "rule-11")),
    "rule 12: run detector_source differs from the detector's source": (
        ERR, lambda e: e.update(exposure={"level": "exposed", "method": "detector", "detector": "D1",
                                          "evidence": "D1 fired on build_soc md5 " + "0" * 32,
                                          "run": dict(RC4_ERR_RUN_D1, detector_source={
                                              "component": "nanosoc_gen",
                                              "commit": "036d3811e630c89b2344b56aa617b40a99cd652b"})}),
        (ERR, "/exposure/run/detector_source", "rule-12")),
    "rule 13: duplicate waiver": (
        "eligibility.yaml", lambda e: _cfg(e, "eth-rc4")["t0_waivers"].append(dict(_cfg(e, "eth-rc4")["t0_waivers"][0])),
        ("eligibility.yaml", "/configurations/0/t0_waivers/8", "rule-13")),
    "rule 13: undeclared gate": (
        "eligibility.yaml", lambda e: _cfg(e, "eth-rc4")["t0_waivers"][0].update(gate="t0.pins.bogus"),
        ("eligibility.yaml", "/configurations/0/t0_waivers/0/gate", "rule-13")),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_rule(example_registry, case):
    file, fn, expected = CASES[case]
    assert load_registry(example_registry)                       # the good half
    edit(example_registry / file, fn)
    got = problems(example_registry)
    assert expected in got, got


def test_rule_6_and_11_on_a_copied_silicon_erratum(example_registry):
    e = load(example_registry / ERR)
    e.update(configuration="eth-main", id="ETH-MAIN-ERR-001")
    dump(example_registry / "errata/eth-main/ERR-001.yaml", e)
    got = problems(example_registry)
    assert ("errata/eth-main/ERR-001.yaml", "/status", "rule-6") in got
    assert ("eligibility.yaml", "/configurations/1", "rule-11") in got


def test_rule_7_file_names(example_registry):
    (example_registry / DEF).rename(example_registry / "defects/SL-DEF-0002.yaml")
    assert ("defects/SL-DEF-0002.yaml", "/id", "rule-7") in problems(example_registry)
    (example_registry / "defects/SL-DEF-0002.yaml").rename(example_registry / "defects/SL-DEF-0001.yml")
    assert ("defects/SL-DEF-0001.yml", "", "rule-7") in problems(example_registry)
    (example_registry / "defects/SL-DEF-0001.yml").rename(example_registry / DEF)
    (example_registry / ERR).rename(example_registry / "errata/eth-rc4/ERR-002.yaml")
    assert ("errata/eth-rc4/ERR-002.yaml", "/id", "rule-7") in problems(example_registry)
    (example_registry / "errata/eth-main").mkdir()
    (example_registry / "errata/eth-rc4/ERR-002.yaml").rename(example_registry / "errata/eth-main/ERR-001.yaml")
    assert ("errata/eth-main/ERR-001.yaml", "/configuration", "rule-7") in problems(example_registry)


def test_rule_9_tokens():
    assert free_text_tokens("build_soc md5 80eb7ce7e81fee653b52c6821cea3942 and 036d381") == ["036d381"]
    assert free_text_tokens("md5: 80eb7ce7e81fee653b52c6821cea3942") == []
    assert free_text_tokens("decafbad defaced 1234567 ABC1234 abc123") == ["1234567"]     # digits, lower, >= 7
    assert free_text_tokens("x" + "a1" * 21) == []                                        # 42 hex: not a commit
    assert free_text_tokens("(b0a6069)") == ["b0a6069"]


def test_rule_9_accepts_a_prefix_of_a_structured_citation(example_registry):
    edit(example_registry / ERR, lambda e: e.update(conditions=e["conditions"] + " See 57d2c762aab6."))
    load_registry(example_registry)


def test_rule_12_matching_run_loads(example_registry):
    edit(example_registry / ERR, lambda e: e.update(exposure={
        "level": "exposed", "method": "detector", "detector": "D1",
        "evidence": "D1 fired on build_soc md5 " + "0" * 32, "run": RC4_ERR_RUN_D1}))
    load_registry(example_registry)
    edit(example_registry / ERR, lambda e: e.update(exposure=dict(e["exposure"], detector="D2", run=dict(
        RC4_ERR_RUN_D1, command="pattern"))))
    assert (ERR, "/exposure/run/detector_source", "rule-12") in problems(example_registry)  # D2 is a pattern: null
    edit(example_registry / ERR, lambda e: e["exposure"]["run"].update(detector_source=None))
    load_registry(example_registry)


def test_waivable_gates_are_the_contract_table():
    """CONFIGURATION_GATES is interfaces.md section 9's configuration gate table, and (once t0 is built) the
    gate table in nanosoc_rm.t0.gates."""
    text = (REPO / "docs/contracts/interfaces.md").read_text()
    section = text.split("### Gates", 1)[1].split("**Registry job**", 1)[0]
    table = re.findall(r"^\| `(t0\.[a-z0-9.-]+)` \|", section, re.M)
    assert tuple(table) == CONFIGURATION_GATES
    assert set(WAIVABLE_GATES) == set(CONFIGURATION_GATES) - {"t0.infra.probe", "t0.runner.hardened"}
    try:
        from nanosoc_rm.t0 import gates
    except ImportError:
        return
    declared = {g.id for g in gates.GATES.values() if g.scope == "configuration"}
    assert declared == set(CONFIGURATION_GATES)


# ---------------------------------------------------------------------------------------------------------
# Repos
# ---------------------------------------------------------------------------------------------------------
def test_resolve_repo(example_registry):
    reg = load_registry(example_registry)
    assert reg.resolve_repo(Repo("github", "SoC-Labs/NanoSoC-Ethernet-Chiplet")) == ("nanosoc-ethernet-chiplet", "canonical")
    assert reg.resolve_repo({"host": "github", "path": "soc-labs/nanosoc-ethernet-chiplet"})[1] == "canonical"
    assert reg.resolve_repo(("gitlab", "soclabs/tidelink")) == ("tidelink", "copy")
    assert reg.resolve_repo(("gitlab", "soclabs/nanosoc-m0/slcorem0p_tech")) == ("slcorem0p_tech", "alias")
    assert reg.resolve_repo(("github", "tass-belgium/picotcp")) == ("picotcp", "external")
    assert reg.resolve_repo(("github", "SoC-Labs/.github")) == (None, "ignored")
    assert reg.resolve_repo(("gitlab", "soclabs/tidechart-x")) == (None, "unknown")
    assert reg.resolve_repo(("github", "soclabs/tidechart")) == (None, "unknown")             # other host
    assert reg.catalogued_repo(("gitlab", "soclabs/nanosoc-m0/slcorem0p_tech")) == Repo("gitlab", "soclabs/slcorem0p_tech")
    assert reg.catalogued_repo(("github", "soc-labs/ahb-qspi")) == Repo("github", "SoC-Labs/AHB-QSPI")
    assert reg.repos("ahb_qspi") == [(Repo("github", "SoC-Labs/AHB-QSPI"), "canonical"),
                                     (Repo("gitlab", "soclabs/ahb_qspi"), "copy")]
    assert reg.repos("picotcp") == [(Repo("github", "tass-belgium/picotcp"), "external")]
    assert reg.canonical("nanosoc_gen") == Repo("gitlab", "soclabs/nanosoc_gen")
    with pytest.raises(KeyError):
        reg.repos("no-such")


# ---------------------------------------------------------------------------------------------------------
# The commit it was read at
# ---------------------------------------------------------------------------------------------------------
def test_commit_of_a_clean_checkout(git_registry):
    reg = load_registry(git_registry)
    assert reg.commit == git("rev-parse", "HEAD", cwd=git_registry) and not reg.dirty and reg.prefix == ""


@pytest.mark.parametrize("change", ["modify", "untracked", "staged"])
def test_dirty_checkout_has_no_commit(git_registry, change):
    if change == "modify":
        edit(git_registry / "imports.yaml", lambda d: d["sources"][0]["items"][0].update(title="changed"))
    elif change == "untracked":
        dump(git_registry / "defects/notes.yaml", {"schema": 1})      # an untracked file the loader also reads
    else:
        edit(git_registry / "imports.yaml", lambda d: d["sources"][0]["items"][0].update(title="staged"))
        git("add", "imports.yaml", cwd=git_registry)
    if change == "untracked":
        problems(git_registry)                                        # and it is refused (rule 7, schema)
        return
    reg = load_registry(git_registry)
    assert reg.commit is None and reg.dirty


def test_registry_in_a_subdirectory_keeps_its_prefix(tmp_path):
    top = tmp_path / "repo"
    write_example_registry(top / "reg")
    git("init", "-q", "-b", "main", cwd=top)
    git("add", "-A", cwd=top)
    git("commit", "-q", "-m", "r", cwd=top)
    reg = load_registry(top / "reg")
    assert reg.commit and reg.prefix == "reg/"
    out = git("show", "%s:%s%s" % (reg.commit, reg.prefix, "catalogue.yaml"), cwd=top)
    assert out == (top / "reg/catalogue.yaml").read_text().rstrip("\n")


def test_problem_prints_like_the_cli(example_registry):
    edit(example_registry / ERR, lambda e: e.update(status="fixed"))
    with pytest.raises(RegistryError) as ei:
        load_registry(example_registry)
    p = next(p for p in ei.value.problems if p.rule == "rule-10")
    assert str(p).startswith(ERR + ":/status: status fixed on silicon") and str(p).endswith("[rule-10]")
    assert "rule-10" in str(ei.value)


FIXTURES = (example_registry, git_registry)      # imported for pytest; named here for pyflakes
