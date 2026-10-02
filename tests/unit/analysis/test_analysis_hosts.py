"""analysis.hosts.compare_hosts (B6, interfaces.md section 5), on hermetic mirrors built with raw git."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.analysis import UsageProblem
from nanosoc_rm.analysis.hosts import compare_hosts

from analysis_fixtures import (T, Mirror, git, host_state, key, load_at, pin_doc, route_hosts, store_mirror_statuses,
                               store_tree, write_config)

PINS = load_at("test_pins")
TL = ({"host": "github", "path": "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"}, {"host": "gitlab", "path": "soclabs/tidelink"})
QS = ({"host": "github", "path": "SoC-Labs/AHB-QSPI"}, {"host": "gitlab", "path": "soclabs/ahb_qspi"})
FL = ({"host": "github", "path": "SoC-Labs/ASIC-Flow"}, {"host": "gitlab", "path": "soclabs/asic_flow"})


def cli_run(*args):
    r = CliRunner().invoke(cli.main, list(args))
    return r.exit_code, [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")], r


def raw(mirror, a, b):
    """Oracle: rev-list --count a..b with raw git (no tool code)."""
    return int(git("--git-dir", mirror.path, "rev-list", "--count", "%s..%s" % (a, b)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    data = tmp_path / "data"
    m = {}
    # tidelink: the GitLab copy stopped 2 commits behind GitHub (AHEAD 2/0)
    tl = m["tidelink"] = Mirror(data, "tidelink")
    base = tl.chain(3)
    ahead = tl.chain(2, base[-1])
    tl.set(*TL[0].values(), "heads", "main", ahead[-1])
    tl.set(*TL[1].values(), "heads", "main", base[-1])
    tl.set(*TL[1].values(), "heads", "old-work", base[0])
    for r in TL:
        tl.head(r["host"], r["path"], "main")
    # ahb_qspi: GitHub +1, GitLab +3 (DIVERGED 1/3)
    qs = m["ahb_qspi"] = Mirror(data, "ahb_qspi")
    b = qs.chain(2)
    qs.set(*QS[0].values(), "heads", "master", qs.chain(1, b[-1])[-1])
    qs.set(*QS[1].values(), "heads", "master", qs.chain(3, b[-1])[-1])
    for r in QS:
        qs.head(r["host"], r["path"], "master")
    # asic-flow: different default-branch names (GitHub lpddr4-pll, GitLab main); GitHub +2, GitLab +1
    fl = m["asic-flow"] = Mirror(data, "asic-flow")
    b = fl.chain(1)
    fl.set(*FL[0].values(), "heads", "lpddr4-pll", fl.chain(2, b[-1])[-1])
    fl.set(*FL[0].values(), "heads", "main", b[-1])
    fl.set(*FL[1].values(), "heads", "main", fl.chain(1, b[-1])[-1])
    fl.head(FL[0]["host"], FL[0]["path"], "lpddr4-pll")
    fl.head(FL[1]["host"], FL[1]["path"], "main")
    store_mirror_statuses("tidelink", [(TL[0], True), (TL[1], True)])
    return m


def test_counts_are_rev_list_in_one_mirror_and_match_raw_git(env):
    docs = compare_hosts("tidelink") + compare_hosts("ahb_qspi") + compare_hosts("asic-flow")
    tl = env["tidelink"]
    gh = lambda r, b: tl.ref(r["host"], r["path"], "heads", b)                      # noqa: E731
    expected = {
        "tidelink": {"relation": "AHEAD", "canonical_only": raw(tl, gh(TL[1], "main"), gh(TL[0], "main")),
                     "copy_only": raw(tl, gh(TL[0], "main"), gh(TL[1], "main"))},
        "ahb_qspi": {"relation": "DIVERGED", "github_only": 1, "gitlab_only": 3},
        "asic-flow": {"relation": "DIVERGED", "github_only": 2, "gitlab_only": 1},
    }
    assert expected["tidelink"]["canonical_only"] == 2 and expected["tidelink"]["copy_only"] == 0
    assert PINS.check_B6(docs, expected) == []                     # the acceptance checker, on hermetic data
    for d in docs:
        assert contracts.validate_observation(d) == [] and db.check_observation(d) == []
    by = {d["fact"]["component"]: d["fact"] for d in docs}
    assert by["asic-flow"]["default_branch_mismatch"] is True
    assert (by["asic-flow"]["canonical"]["default_branch"], by["asic-flow"]["copy"]["default_branch"]) == ("lpddr4-pll", "main")
    assert by["tidelink"]["default_branch_mismatch"] is False
    assert by["tidelink"]["canonical"]["head"] == git("--git-dir", tl.path, "rev-parse", gh(TL[0], "main"))
    # stored as one snapshot per component; the data age is the mirror's last fetch, never fresher
    assert [d["subject"] for d in db.latest("host_compare", "host_compare:tidelink:")] == \
        ["host_compare:tidelink:gitlab:soclabs/tidelink"]
    assert db.latest("host_compare", "host_compare:tidelink:")[0]["observed_at"] == T


def test_negative_control_synced_copy_reads_same(env):
    """B6 control: the copy moved to the canonical head reads SAME, so the DIVERGED expectation fails."""
    qs = env["ahb_qspi"]
    canon = git("--git-dir", qs.path, "rev-parse", qs.ref(*QS[0].values(), "heads", "master"))
    qs.set(*QS[1].values(), "heads", "master", canon)
    docs = compare_hosts("ahb_qspi")
    assert docs[0]["fact"]["relation"] == "SAME" and docs[0]["fact"]["canonical_only"] == 0
    problems = PINS.check_B6(docs, {"ahb_qspi": {"relation": "DIVERGED", "github_only": 1, "gitlab_only": 3}})
    assert any(p.startswith("ahb_qspi: relation SAME") for p in problems), problems


def test_behind_and_unrelated(env, tmp_path):
    tl = env["tidelink"]
    gl_main = git("--git-dir", tl.path, "rev-parse", tl.ref(*TL[1].values(), "heads", "main"))
    tl.set(*TL[1].values(), "heads", "main", tl.chain(4, git("--git-dir", tl.path, "rev-parse",
                                                             tl.ref(*TL[0].values(), "heads", "main")))[-1])
    f = compare_hosts("tidelink")[0]["fact"]
    assert (f["relation"], f["canonical_only"], f["copy_only"]) == ("BEHIND", 0, 4)
    tl.set(*TL[1].values(), "heads", "main", tl.chain(2)[-1])                    # a new root: no merge base
    f = compare_hosts("tidelink")[0]["fact"]
    assert (f["relation"], f["canonical_only"], f["copy_only"]) == ("UNRELATED", None, None)
    assert f["copy"]["head"] != gl_main


def test_unreadable_side_is_unknown_never_same(env):
    """B6 control: every copy ref gone (or the mirror says the fetch failed): UNKNOWN, counts null."""
    fl = env["asic-flow"]
    for ref in fl.refs(*FL[1].values()):
        git("--git-dir", fl.path, "update-ref", "-d", ref)
    f = compare_hosts("asic-flow")[0]["fact"]
    assert (f["relation"], f["canonical_only"], f["copy_only"], f["copy"]["head"]) == ("UNKNOWN", None, None, None)
    assert f["canonical"]["head"] is not None
    problems = PINS.check_B6(compare_hosts("asic-flow"), {"asic-flow": {"relation": "DIVERGED"}})
    assert any(p.startswith("asic-flow: relation UNKNOWN") for p in problems)
    # a failed fetch: refs still present, but the side is unreadable
    store_mirror_statuses("ahb_qspi", [(QS[0], True), (QS[1], False)])
    f = compare_hosts("ahb_qspi")[0]["fact"]
    assert f["relation"] == "UNKNOWN" and f["copy"]["head"] is None
    # no mirror at all: UNKNOWN, and the default branch is unknown too when nothing records it
    f = compare_hosts("nanosoc_arch_tech")[0]["fact"]
    assert f["relation"] == "UNKNOWN" and f["copy"]["default_branch"] is None


def test_default_branch_is_read_where_the_mirror_records_it(env, tmp_path, monkeypatch):
    """AN-1: the mirror module keeps each repo's default branch in the mirror's git config. The compare reads
    it there and never asks a host (no anonymous ls-remote that a private GitLab repo would refuse)."""
    hosts = route_hosts(tmp_path, monkeypatch)                    # both hosts reachable, but empty: no answer there
    (hosts / "github").mkdir(parents=True)
    (hosts / "gitlab").mkdir(parents=True)
    d = compare_hosts("tidelink", store=False)[0]
    f = d["fact"]
    assert (f["relation"], f["canonical"]["default_branch"], f["copy"]["default_branch"]) == ("AHEAD", "main", "main")
    commands = [s["command"] for s in d["sources"]]
    assert not any("ls-remote" in c for c in commands), commands
    assert any("config --get nanosoc-rm.%s.defaultbranch" % key(TL[1]["host"], TL[1]["path"]) in c
               for c in commands), commands


def test_default_branch_falls_back_to_the_stored_walk_never_to_a_host(env, tmp_path, monkeypatch):
    """Nothing recorded in the mirror: the newest stored walk's host state names it; with no walk either, the
    side is unreadable (UNKNOWN), and no host was asked."""
    tl = env["tidelink"]
    for r in TL:
        tl.forget_head(r["host"], r["path"])
    route_hosts(tmp_path, monkeypatch)
    d = compare_hosts("tidelink")[0]
    assert (d["fact"]["relation"], d["fact"]["copy"]["default_branch"]) == ("UNKNOWN", None)
    assert not any("ls-remote" in s["command"] for s in d["sources"]), d["sources"]
    store_tree("eth-rc4", [pin_doc("eth-rc4", "tidelink", "tidelink", "1" * 40, {
        "github": host_state(TL[0], "canonical", True, "MISSING"),
        "gitlab": host_state(TL[1], "copy", False, "MISSING")})])
    d = compare_hosts("tidelink")[0]
    assert d["fact"]["relation"] == "AHEAD"
    assert any(s["kind"] == "derived" and "pin_state:eth-rc4:tidelink" in s["command"] for s in d["sources"])
    assert not any("ls-remote" in s["command"] for s in d["sources"])


def test_cli_exit_codes(env):
    rc, docs, r = cli_run("hosts", "tidelink", "--json")
    assert rc == 0 and [d["fact"]["relation"] for d in docs] == ["AHEAD"], r.output
    rc, docs, r = cli_run("hosts", "--json")                     # every component with a copy: one DIVERGED
    assert rc == 1, r.output
    assert {d["fact"]["component"] for d in docs} == {"tidelink", "ahb_qspi", "asic-flow", "nanosoc_arch_tech"}
    rc, docs, r = cli_run("hosts", "nanosoc_arch_tech", "--json")  # no mirror: UNKNOWN, could not answer
    assert rc == 2 and docs[0]["fact"]["relation"] == "UNKNOWN"
    rc, _docs, r = cli_run("hosts", "no-such-component")
    assert rc == 3 and "unknown component" in r.output
    rc, _docs, r = cli_run("hosts", "tidelink")
    assert rc == 0 and "AHEAD" in r.stdout and "RELATION" in r.stdout


def test_unknown_component_and_component_without_copies(env):
    with pytest.raises(UsageProblem):
        compare_hosts("no-such-component")
    assert compare_hosts("nanosoc_gen") == []                     # no copy: nothing to compare, nothing stored
    assert compare_hosts("nanosoc_gen") == []                     # twice in one second: no snapshot id to collide
    assert db.latest("host_compare", "host_compare:nanosoc_gen:") == []
