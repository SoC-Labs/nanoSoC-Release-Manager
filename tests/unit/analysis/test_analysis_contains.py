"""analysis.contains.contains (B4, interfaces.md section 6), on hermetic mirrors and stored trees.

The fixture mirrors the real nanosoc_gen ancestry the AT uses (pins.md "Real facts"):
origin -> a (=1d7772d's role) -> d9 -> c2 -> fix (036d381's role) -> main; d04 branches off d9 (keep tag + wip).
eth-rc4 pins d04 and a; eth-main pins d9 and a; multicore-master pins c2 and a.
TideLink: rc4 pins `tl_pin` on GitHub; the GitLab copy's main is an ancestor of it, 2 behind, and lacks it."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.analysis import CannotAnswer, UsageProblem
from nanosoc_rm.analysis.contains import contains, resolve_commit

from analysis_fixtures import Mirror, git, host_state, load_at, pin_doc, store_tree, write_config

PINS = load_at("test_pins")
GEN = {"host": "gitlab", "path": "soclabs/nanosoc_gen"}
TL = ({"host": "github", "path": "SoC-Labs/TideLink-Chiplet-Interconnect-AHB"}, {"host": "gitlab", "path": "soclabs/tidelink"})
PATH_A = "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen"
PATH_B = "nanosoc-multicore-system/ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen"


def cli_run(*args):
    r = CliRunner().invoke(cli.main, list(args))
    return r.exit_code, [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")], r


def gen_pin(cfg, path, sha):
    return pin_doc(cfg, path, "nanosoc_gen", sha, {"gitlab": host_state(GEN, "canonical", True, "ON_DEFAULT", lag=1)})


@pytest.fixture
def fx(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    data = tmp_path / "data"
    g = Mirror(data, "nanosoc_gen")
    origin, a, d9, c2, fix, main = g.chain(6)
    d04 = g.commit(d9, msg="rc4 generator, off d9")
    g.set(GEN["host"], GEN["path"], "heads", "main", main)
    g.set(GEN["host"], GEN["path"], "heads", "wip/uncommitted", d04)
    g.set(GEN["host"], GEN["path"], "tags", "keep/eth-rc4/nanosoc_gen", d04)
    t = Mirror(data, "tidelink")
    tl = t.chain(5)
    t.set(TL[0]["host"], TL[0]["path"], "heads", "main", tl[4])
    t.set(TL[1]["host"], TL[1]["path"], "heads", "main", tl[1])          # the copy lags and lacks tl[3]
    tl_pin = tl[3]
    tl_hosts = {"github": host_state(TL[0], "canonical", True, "ON_DEFAULT", lag=1),
                "gitlab": host_state(TL[1], "copy", False, "MISSING")}
    store_tree("eth-rc4", [gen_pin("eth-rc4", PATH_A, d04), gen_pin("eth-rc4", PATH_B, a),
                           pin_doc("eth-rc4", "tidelink", "tidelink", tl_pin, tl_hosts)])
    store_tree("eth-main", [gen_pin("eth-main", "nanosoc_arch_tech/nanosoc_gen", d9),
                            gen_pin("eth-main", "ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen", a)])
    store_tree("multicore-master", [gen_pin("multicore-master", "nanosoc_arch_tech/nanosoc_gen", c2),
                                    gen_pin("multicore-master", "ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen", a)])
    return dict(g=g, t=t, origin=origin, a=a, d9=d9, c2=c2, fix=fix, main=main, d04=d04, tl=tl, tl_pin=tl_pin)


def test_fix_answers_no_with_both_pins_proven(fx):
    docs = contains(fx["fix"][:7], component="nanosoc_gen", configurations=["eth-rc4", "eth-main", "multicore-master"])
    assert {d["fact"]["query"]["commit"] for d in docs} == {fx["fix"]}            # the prefix resolved in full
    expected = {("eth-rc4", "gitlab"): ("no", {PATH_A: (fx["d04"], False), PATH_B: (fx["a"], False)}),
                ("eth-main", "gitlab"): ("no", {"nanosoc_arch_tech/nanosoc_gen": (fx["d9"], False),
                                                "ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen": (fx["a"], False)}),
                ("multicore-master", "gitlab"): ("no", {"nanosoc_arch_tech/nanosoc_gen": (fx["c2"], False),
                                                        "ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen": (fx["a"], False)})}
    assert PINS.check_B4(docs, expected) == []                                  # the AT's own checker
    rc4 = next(d for d in docs if d["fact"]["configuration"] == "eth-rc4")
    commands = " ".join(s["command"] for s in rc4["sources"])
    for sha in (fx["fix"], fx["d04"], fx["a"]):                                 # existence proofs (A3)
        assert "for-each-ref --count=1 --contains %s" % sha in commands
    assert "merge-base --is-ancestor %s %s" % (fx["fix"], fx["d04"]) in commands
    for d in docs:
        assert contracts.validate_observation(d) == [] and db.check_observation(d) == []
    assert len(db.latest("containment", "containment:nanosoc_gen:%s:" % fx["fix"])) == 3


def test_yes_partial_unknown_absent_all_occur(fx):
    yes = contains(fx["origin"], component="nanosoc_gen", configurations=["eth-rc4"])
    assert PINS.check_B4(yes, {("eth-rc4", "gitlab"): ("yes", {PATH_A: (fx["d04"], True), PATH_B: (fx["a"], True)})}) == []
    partial = contains(fx["d9"][:9], component="nanosoc_gen", configurations=["eth-rc4"])
    assert PINS.check_B4(partial, {("eth-rc4", "gitlab"): (
        "partial", {PATH_A: (fx["d04"], True), PATH_B: (fx["a"], False)})}) == []
    q = fx["tl"][1]                                                             # the GitLab copy's head
    tl = contains(q[:7], component="tidelink", configurations=["eth-rc4", "multicore-master"])
    assert PINS.check_B4(tl, {
        ("eth-rc4", "github"): ("yes", {"tidelink": (fx["tl_pin"], True)}),
        ("eth-rc4", "gitlab"): ("unknown", {"tidelink": (fx["tl_pin"], None)}),
        ("multicore-master", "github"): ("absent", {}),
        ("multicore-master", "gitlab"): ("absent", {})}) == []
    reason = next(d for d in tl if d["fact"]["host"] == "gitlab" and d["fact"]["configuration"] == "eth-rc4")["fact"]["pins"][0]["reason"]
    assert fx["tl_pin"][:12] in reason and "gitlab" in reason


def test_negative_control_unreachable_pin_is_unknown_never_no(fx):
    """B4 control: d04 stays an object in the mirror, but no branch or tag reaches it."""
    g = fx["g"]
    g.delete(GEN["host"], GEN["path"], "heads", "wip/uncommitted")
    g.delete(GEN["host"], GEN["path"], "tags", "keep/eth-rc4/nanosoc_gen")
    assert git("--git-dir", g.path, "cat-file", "-t", fx["d04"]) == "commit"
    docs = contains(fx["fix"], component="nanosoc_gen", configurations=["eth-rc4"])
    assert [d["fact"]["answer"] for d in docs] == ["unknown"]
    pin = next(p for p in docs[0]["fact"]["pins"] if p["tree_path"] == PATH_A)
    assert pin["contains"] is None and fx["d04"][:12] in pin["reason"]
    problems = PINS.check_B4(docs, {("eth-rc4", "gitlab"): ("no", {PATH_A: (fx["d04"], False), PATH_B: (fx["a"], False)})})
    assert problems


def test_query_commit_unreachable_on_the_host_makes_every_pin_unknown(fx):
    g = fx["g"]
    orphan = g.commit(fx["a"], msg="object in the mirror, on no ref")
    docs = contains(orphan, component="nanosoc_gen", configurations=["eth-rc4"])
    assert docs[0]["fact"]["answer"] == "unknown"
    assert all(p["contains"] is None and "not reachable" in p["reason"] for p in docs[0]["fact"]["pins"])


def test_resolution_and_usage_errors(fx):
    assert resolve_commit(fx["fix"][:7], "nanosoc_gen")[:2] == ("nanosoc_gen", fx["fix"])
    assert resolve_commit(fx["fix"][:10].upper())[:2] == ("nanosoc_gen", fx["fix"])    # every mirror searched
    with pytest.raises(UsageProblem, match="resolves to no commit"):
        contains("0000000", component="nanosoc_gen")
    with pytest.raises(UsageProblem, match="not a commit id"):
        contains("xyz")
    with pytest.raises(UsageProblem, match="unknown configuration"):
        contains(fx["fix"], component="nanosoc_gen", configurations=["no-such-config"])
    # one commit in two component mirrors: name one
    other = Mirror(fx["g"].path.parent.parent, "hostio4")
    git("--git-dir", other.path, "fetch", "--quiet", fx["g"].path, "%s:refs/r/gitlab/x/heads/main" % fx["g"].ref(GEN["host"], GEN["path"], "heads", "main"))
    with pytest.raises(UsageProblem, match="name one with --component"):
        resolve_commit(fx["fix"])


def test_ambiguous_prefix_lists_the_candidates(fx):
    g = fx["g"]
    seen = {}
    for i in range(5000):                                     # two commits sharing a 4-hex prefix
        sha = g.commit(msg="probe %d" % i)
        if sha[:4] in seen:
            break
        seen[sha[:4]] = sha
    else:
        pytest.fail("no 4-hex collision in 5000 commits")
    with pytest.raises(UsageProblem, match="ambiguous") as e:
        resolve_commit(sha[:4], "nanosoc_gen")
    assert sha in str(e.value) and seen[sha[:4]] in str(e.value)


def test_never_walked_configuration_cannot_be_answered(tmp_path, monkeypatch):
    """No stored tree is "could not answer" (exit 2), never `absent`."""
    write_config(tmp_path, monkeypatch)
    g = Mirror(tmp_path / "data", "nanosoc_gen")
    a, b = g.chain(2)
    g.set(GEN["host"], GEN["path"], "heads", "main", b)
    store_tree("eth-rc4", [gen_pin("eth-rc4", PATH_B, a)])
    assert contains(b, component="nanosoc_gen", configurations=["eth-rc4"])[0]["fact"]["answer"] == "no"
    with pytest.raises(CannotAnswer, match="eth-main has no stored tree"):
        contains(b, component="nanosoc_gen")                       # None = every registered configuration
    rc, _docs, r = cli_run("contains", b, "--component", "nanosoc_gen", "--json")
    assert rc == 2 and "walk eth-main" in r.output
    store_tree("eth-main", [])                                       # an empty walk is a complete answer: absent
    store_tree("multicore-master", [])
    docs = contains(b, component="nanosoc_gen")
    assert [d["fact"]["answer"] for d in docs] == ["no", "absent", "absent"]


def test_cli_exit_codes(fx):
    rc, docs, r = cli_run("contains", fx["fix"][:7], "--component", "nanosoc_gen", "--config", "eth-rc4", "--json")
    assert rc == 0 and [d["fact"]["answer"] for d in docs] == ["no"], r.output        # answered, even "no"
    rc, docs, r = cli_run("contains", fx["tl"][1][:7], "--component", "tidelink", "--config", "eth-rc4", "--json")
    assert rc == 2 and "unknown" in [d["fact"]["answer"] for d in docs], r.output
    rc, _docs, r = cli_run("contains", "0000000", "--component", "nanosoc_gen", "--json")
    assert rc == 3, r.output
    rc, _docs, r = cli_run("contains", fx["fix"][:7], "--component", "nanosoc_gen")
    assert rc == 0 and "ANSWER" in r.stdout
