"""analysis.protect.silicon_protection (B5, interfaces.md section 7; GATE2 Q9), from stored trees."""
from __future__ import annotations

import copy
import json

import pytest
import yaml
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.analysis import CannotAnswer, UsageProblem
from nanosoc_rm.analysis.protect import silicon_protection

from analysis_fixtures import host_state, load_at, pin_doc, store_tree, write_config

PINS = load_at("test_pins")


def cli_run(*args):
    r = CliRunner().invoke(cli.main, list(args))
    return r.exit_code, [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")], r


def rc4_tree():
    """rc4's 44 pins as a correct walker stores them, rebuilt by the acceptance file from the rc4 lock and the
    second-host golden (test_pins.synth_rc4_pin_docs); default branches and branch lists filled in the way the
    walker reports them (a BRANCH_ONLY or PROTECTED_BY_TAG commit is on some branch other than main)."""
    docs = PINS.synth_rc4_pin_docs()
    for d in docs:
        for st in d["fact"]["hosts"].values():
            st["default_branch"] = "master" if st["repo"]["path"] in ("soclabs/nanosoc-multicore-system",
                                                                       "SoC-Labs/AHB-QSPI", "soclabs/ahb_qspi") else "main"
            if st["state"] in ("ON_DEFAULT",) or (st["state"] == "TAGGED" and st["lag"] is not None):
                st["branches"] = [st["default_branch"]]
            elif st["state"] == "PROTECTED_BY_TAG":
                st["branches"] = ["wip/uncommitted-2026-08-18"]
    return docs


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return write_config(tmp_path, monkeypatch)


def test_rc4_golden_13_rows(cfg):
    """The AT-B5 golden (11 pinned-host rows covered, 2 canonical-host rows uncovered), from the rc4 tree."""
    store_tree("eth-rc4", rc4_tree())
    rows = silicon_protection("eth-rc4")
    assert PINS.check_B5(rows, PINS.rc4_protection_golden()) == []          # the AT's own checker
    pinned = [r["fact"] for r in rows if r["fact"]["pinned"]]
    assert len(rows) == 13 and len(pinned) == 11 and all(f["covered"] for f in pinned)
    assert sorted((r["fact"]["component"], r["fact"]["repo"]["host"]) for r in rows if not r["fact"]["covered"]) == \
        [("ahb_qspi", "github"), ("nanosoc_arch_tech", "github")]
    for r in rows:
        assert contracts.validate_observation(r) == [] and db.check_observation(r) == []
    assert len(db.latest("silicon_protection", "silicon_protection:eth-rc4:")) == 13
    # neither frozen nor silicon: [] rather than an error (and nothing to walk)
    assert silicon_protection("eth-main") == [] and silicon_protection("multicore-master") == []
    assert silicon_protection("eth-main") == []                  # again within the second: nothing stored, no clash


def test_negative_control_lost_keep_tag_uncovers_exactly_that_row(cfg):
    """B5 control: keep/eth-rc4/tidechart gone, the walk re-run: tidechart 4b4b898@gitlab turns uncovered."""
    tree = rc4_tree()
    tc = next(d for d in tree if d["fact"]["tree_paths"] == ["tidechart"])
    tc["fact"]["hosts"]["gitlab"].update(state="BRANCH_ONLY", tags=[])
    store_tree("eth-rc4", tree)
    rows = silicon_protection("eth-rc4")
    assert any(p.startswith("tidechart 4b4b898@gitlab") for p in PINS.check_B5(rows, PINS.rc4_protection_golden()))
    rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--uncovered", "--json")
    assert rc == 1, r.output
    assert {(d["fact"]["component"], d["fact"]["repo"]["host"]) for d in docs} == \
        {("ahb_qspi", "github"), ("nanosoc_arch_tech", "github"), ("tidechart", "gitlab")}


def test_coverage_by_containment_not_name_and_rows_merge(cfg):
    gh = {"host": "github", "path": "SoC-Labs/TideLink-Chiplet-GPIO-PHY"}
    gen = {"host": "gitlab", "path": "soclabs/nanosoc_gen"}
    tl = ({"host": "github", "path": "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"}, {"host": "gitlab", "path": "soclabs/tidelink"})
    c1, c2, c3 = "1" * 40, "2" * 40, "3" * 40
    tree = [
        # one commit at two tree paths (two parents): one row, both paths; tag named after the submodule path
        pin_doc("eth-rc4", "tidelink/deps/tidelink-phy", "tidelink-gpio-phy", c1,
                {"github": host_state(gh, "canonical", True, "PROTECTED_BY_TAG", branches=["fix/x"],
                                      tags=["keep/eth-rc4/tidelink-phy"])}),
        pin_doc("eth-rc4", "other/tidelink-phy", "tidelink-gpio-phy", c1,
                {"github": host_state(gh, "canonical", True, "PROTECTED_BY_TAG", branches=["fix/x"],
                                      tags=["keep/eth-rc4/tidelink-phy"])}),
        # TAGGED on the default branch (lag set): reached, no row; ON_DEFAULT: no row
        pin_doc("eth-rc4", "tidelink", "tidelink", c2,
                {"github": host_state(tl[0], "canonical", True, "TAGGED", lag=5, branches=["main"], tags=["v0.90"]),
                 "gitlab": host_state(tl[1], "copy", False, "MISSING")}),
        pin_doc("eth-rc4", "gen", "nanosoc_gen", c3, {"gitlab": host_state(gen, "canonical", True, "ON_DEFAULT", lag=0,
                                                                           branches=["main"])}),
    ]
    store_tree("eth-rc4", tree)
    rows = silicon_protection("eth-rc4")
    assert len(rows) == 1                     # the GitLab copy of tidelink is neither pinned nor canonical: no row
    f = rows[0]["fact"]
    assert f["tree_paths"] == ["tidelink/deps/tidelink-phy", "other/tidelink-phy"]
    assert (f["covered"], f["covering_tags"], f["pinned"], f["canonical"]) == (True, ["keep/eth-rc4/tidelink-phy"], True, True)
    assert rows[0]["observed_at"] == "2026-10-01T10:00:00Z"                  # the walk's time, never fresher


def test_usage_and_missing_tree(cfg):
    with pytest.raises(UsageProblem):
        silicon_protection("no-such-config")
    with pytest.raises(CannotAnswer, match="eth-rc4 has no stored tree"):
        silicon_protection("eth-rc4")
    rc, _docs, r = cli_run("silicon-protection", "eth-rc4", "--json")
    assert rc == 2, r.output
    rc, _docs, r = cli_run("silicon-protection", "no-such-config")
    assert rc == 3, r.output


def test_all_configurations_and_exit_codes(cfg):
    tree = rc4_tree()
    store_tree("eth-rc4", copy.deepcopy(tree))
    rows = silicon_protection()                                              # every registered configuration
    assert {r["fact"]["configuration"] for r in rows} == {"eth-rc4"} and len(rows) == 13
    rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--json")
    assert rc == 1 and len(docs) == 13                                       # 2 uncovered today
    # cover the two canonical-host rows (David's GATE2 Q2 / Q9 writes): exit 0
    for d in tree:
        for st in d["fact"]["hosts"].values():
            if st["role"] == "canonical" and st["state"] in ("BRANCH_ONLY", "MISSING") and not st["pinned"]:
                st.update(state="PROTECTED_BY_TAG", tags=["keep/eth-rc4/%s" % d["fact"]["child"]["component"]])
    store_tree("eth-rc4", tree, snap_id="pin_state:eth-rc4@later")
    rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--uncovered", "--json")
    assert rc == 0 and docs == [], r.output
    rc, _docs, r = cli_run("silicon-protection", "eth-rc4")
    assert rc == 0 and "COVERED" in r.stdout


def test_a_pin_that_cannot_be_judged_is_never_a_clean_answer(cfg):
    """AN-5: a silicon pin with no host state (LOCAL_URL here) gives no row, is named, and the CLI exits 2 rather
    than 0. An uncovered row elsewhere still makes it exit 1 (a finding wins)."""
    tree = rc4_tree()
    tc = copy.deepcopy(next(d for d in tree if d["fact"]["tree_paths"] == ["tidechart"]))
    tc["subject"] = "pin_state:eth-rc4:local/tidechart"
    tc["fact"].update(url="/srv/tidechart.git", url_kind="local", state="LOCAL_URL", hosts={},
                      tree_paths=["local/tidechart"], path="tidechart")
    tc["fact"]["child"].update(component=None, catalogued_as="unknown", repo=None)
    assert contracts.validate_observation(tc) == []
    store_tree("eth-rc4", [tc])
    rows = silicon_protection("eth-rc4")
    assert rows == [] and len(rows.unanswered) == 1
    assert "local/tidechart" in rows.unanswered[0] and "LOCAL_URL" in rows.unanswered[0]
    rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--uncovered", "--json")
    assert rc == 2 and docs == [] and "local/tidechart" in r.stderr, r.output
    store_tree("eth-rc4", rc4_tree() + [tc], snap_id="pin_state:eth-rc4@later")
    rows = silicon_protection("eth-rc4")
    assert len(rows) == 13 and len(rows.unanswered) == 1
    rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--uncovered", "--json")
    assert rc == 1 and len(docs) == 2 and "local/tidechart" in r.stderr, r.output


def test_every_configuration_answers_the_walked_ones(cfg, tmp_path):
    """AN-3: with no CONFIG, a frozen configuration never walked is named, not allowed to hide the others."""
    reg = cfg[2] / "eligibility.yaml"
    doc = yaml.safe_load(reg.read_text())
    doc["configurations"].append({"id": "eth-rc3", "component": "nanosoc-ethernet-chiplet", "ref": {"tag": "v0.9.0"},
                                  "commit": "a" * 40, "frozen": True})
    reg.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(CannotAnswer, match="has no stored tree"):
        silicon_protection()                                                 # no frozen configuration walked
    store_tree("eth-rc4", rc4_tree())
    rows = silicon_protection()
    assert len(rows) == 13 and len(rows.unanswered) == 1 and "eth-rc3 has no stored tree" in rows.unanswered[0]
    rc, docs, r = cli_run("silicon-protection", "--json")
    assert rc == 1 and len(docs) == 13 and "walk eth-rc3" in r.stderr, r.output


def test_an_empty_answer_asked_twice_in_one_second(cfg):
    """A frozen configuration whose pins are all on their default branch has no row. The store names an empty
    snapshot `<scope>@<second>`; asking twice in that second must not crash (it is the same, stored answer), while
    a non-empty answer after it is still stored and an empty one after that supersedes it."""
    gen = {"host": "gitlab", "path": "soclabs/nanosoc_gen"}
    on_default = pin_doc("eth-rc4", "gen", "nanosoc_gen", "3" * 40,
                         {"gitlab": host_state(gen, "canonical", True, "ON_DEFAULT", lag=0, branches=["main"])})
    store_tree("eth-rc4", [on_default])
    for _ in range(3):
        assert silicon_protection("eth-rc4") == []
        rc, docs, r = cli_run("silicon-protection", "eth-rc4", "--json")
        assert rc == 0 and docs == [], r.output
    store_tree("eth-rc4", rc4_tree(), snap_id="pin_state:eth-rc4@later")
    assert len(silicon_protection("eth-rc4")) == 13
    assert len(db.latest("silicon_protection", "silicon_protection:eth-rc4:")) == 13
