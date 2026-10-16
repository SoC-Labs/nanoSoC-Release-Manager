"""Wave G: .nanosoc/capabilities.yaml read at a commit from the mirror, composition across a superproject, the bump
run set, `nanosoc-rm test-bump` (Now vs After, T0 as hygiene), the executor queue and the bump gate.

Hermetic: LocalHosts bare repos behind url.insteadOf (the compat scene's machinery); capability commands are small
shell strings run in the sandbox checkout the T0 gates' own fetches lay out.

    top    pins a -> alpha@a1, b -> beta@b1;  root file: `integ` affected_by [a], `by-hand` manual; composes b:unit
    alpha  a1 good (own `lint` passes) ; a2 = tag v2 + branch next: bad (lint FAILS) ; a3 = branch fixed: good
    beta   b1: `unit` (always passes), `slow` (not composed)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from analysis_fixtures import load_at
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, jobs
from nanosoc_rm.capabilities import gate as GATE
from nanosoc_rm.capabilities import manifest as MF
from nanosoc_rm.capabilities import plan as PL
from nanosoc_rm.capabilities import run as RUN

GH = "github"
CAPS = MF.PATH


def caps(component, capabilities=None, compose=None, tools=None, env=None, alias=False):
    doc = {"schema": 1, "component": component}
    if tools:
        doc["tools"] = tools
    if env:
        doc["env"] = env
    if capabilities is not None:
        doc["tiers" if alias else "capabilities"] = capabilities
    if compose is not None:
        doc["composes" if alias else "compose"] = compose
    return yaml.safe_dump(doc, sort_keys=False)


SH = {"sh": {"probe": "sh --version", "min": "4.0"}}


class Scene:
    def __init__(self, at, h):
        self.h = h
        alpha_caps = caps("alpha", [{"id": "lint", "kind": "lint", "tier": "t1", "run": "grep -q good rtl/a.v",
                                     "tools": ["sh"], "budget_s": 60,
                                     "verdict": {"forbid": ["SKIPPED"]}}], tools=SH)
        self.a1 = h.commit(GH, "SoC-Labs/alpha", files={"rtl/a.v": "good\n", CAPS: alpha_caps})
        h.push(GH, "SoC-Labs/alpha", self.a1, "main")
        self.a2 = h.commit(GH, "SoC-Labs/alpha", parents=[self.a1], files={"rtl/a.v": "bad\n", CAPS: alpha_caps})
        h.push(GH, "SoC-Labs/alpha", self.a2, "next")
        h.tag(GH, "SoC-Labs/alpha", "v2", self.a2)
        self.a3 = h.commit(GH, "SoC-Labs/alpha", parents=[self.a2], files={"rtl/a.v": "good again\n",
                                                                         CAPS: alpha_caps})
        h.push(GH, "SoC-Labs/alpha", self.a3, "fixed")
        # a4: lint passes but prints SKIPPED (the eth wrapper case): forbid makes it a FAIL
        skip_caps = caps("alpha", [{"id": "lint", "kind": "lint", "tier": "t1",
                                    "run": "grep -q good rtl/a.v && echo 'WRAPPER: SKIPPED'", "tools": ["sh"],
                                    "budget_s": 60, "verdict": {"forbid": ["SKIPPED"]}}], tools=SH)
        self.a4 = h.commit(GH, "SoC-Labs/alpha", parents=[self.a3], files={"rtl/a.v": "good\n", CAPS: skip_caps})
        h.push(GH, "SoC-Labs/alpha", self.a4, "skipping")
        self.b1 = h.commit(GH, "SoC-Labs/beta", files={"rtl/b.v": "b\n", CAPS: caps("beta", [
            {"id": "unit", "kind": "unit", "run": "test -f rtl/b.v", "budget_s": 30},
            {"id": "slow", "kind": "sim", "run": "true", "budget_s": 30, "gate": "report"}])})
        h.push(GH, "SoC-Labs/beta", self.b1, "main")
        top_caps = caps("top", [
            {"id": "integ", "kind": "elab", "tier": "t2", "run": "grep -q good a/rtl/a.v", "affected_by": ["a"],
             "budget_s": 60, "tools": ["sh"]},
            {"id": "by-hand", "kind": "hw", "run": "false", "manual": True, "affected_by": ["a"]}],
            compose=[{"path": "b", "run": ["unit"]}], tools=SH)
        self.top = h.commit(GH, "SoC-Labs/top", files={"README": "top\n", CAPS: top_caps},
                            gitlinks={"a": self.a1, "b": self.b1},
                            gitmodules=[{"path": "a", "url": h.url(GH, "SoC-Labs/alpha")},
                                        {"path": "b", "url": h.url(GH, "SoC-Labs/beta")}])
        h.push(GH, "SoC-Labs/top", self.top, "main")
        comps = ["top", "alpha", "beta"]
        catalogue = {"schema": 1, "components": [
            {"id": c, "class": "soc_ref" if c == "top" else "ip_tech",
             "canonical": {"host": GH, "path": "SoC-Labs/%s" % c}} for c in comps]}
        eligibility = {"schema": 1, "configurations": [{"id": "top-main", "component": "top", "ref": {"branch": "main"}}]}
        self.registry = at.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
        (self.registry / "tiers.yaml").write_text(yaml.safe_dump({"schema": 1, "hosts": {
            "eda": {"tools": {"sh": {"version": "5.1", "bin": "/usr/bin"}}},
            "bare": {"tools": {"sh": {"version": "3.2", "bin": "/usr/bin"}}}}}))
        self.env = h.tool_env(self.registry, "caps")
        with self.env.active():
            for c in comps:
                at.rm("mirror.sync_component")(c)
            at.rm("walker.walk")("top-main")
        self.url_map = {"https://github.com/SoC-Labs/%s.git" % c: str(h.bare(GH, "SoC-Labs/%s" % c)) for c in comps}

    def config(self):
        from nanosoc_rm.config import load_config
        with self.env.active():
            return load_config()

    def test_bump(self, path, to, **kw):
        kw.setdefault("host", "eda")
        with self.env.active():
            return RUN.test_bump("top-main", path, to, config=self.config(), url_map=self.url_map, **kw)


@pytest.fixture(autouse=True)
def _developer_env(monkeypatch):
    for k in ("CI_JOB_ID", "CI_SERVER_URL", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT", "RM_PROBE_URLS",
              "NANOSOC_RM_EXECUTOR_HOST"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    at = load_at("test_pins")
    h = at.LocalHosts(tmp_path_factory.mktemp("caps"))
    yield Scene(at, h)
    h.api.close()


def rows_of(doc):
    return {r["key"]: r for r in doc["fact"]["rows"]}


# ---------------------------------------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------------------------------------
def test_parse_normalises_both_spellings_and_fills_defaults():
    a = MF.parse(caps("x", [{"id": "l", "kind": "lint", "command": "make lint", "workdir": "rtl",
                             "verdict": {"require_count": {"pattern": "PASS", "min": 2}}}],
                      compose=[{"path": "deps/y/", "run": ["u"]}], alias=True))
    c = a["capabilities"][0]
    assert c["run"] == "make lint" and c["cwd"] == "rtl" and "command" not in c and "workdir" not in c
    assert c["gate"] == "block" and c["when"] == ["on_bump"] and c["budget_s"] == MF.DEFAULT_BUDGET_S
    assert c["runtime"] == "M" and c["verdict"] == {"exit": 0, "forbid": [], "require": [{"pattern": "PASS", "min": 2}]}
    assert a["compose"] == [{"path": "deps/y", "run": ["u"], "when": ["on_bump"]}] and "composes" not in a


@pytest.mark.parametrize("doc,needle", [
    (caps("x", [{"id": "l", "kind": "lint", "run": "a"}, {"id": "l", "kind": "sim", "run": "b"}]), "given twice"),
    (caps("x", [{"id": "l", "kind": "lint", "run": "a", "tools": ["vcs"]}]), "which `tools` does not declare"),
    (caps("x", [{"id": "l", "kind": "lint", "run": "a"}], env={"provides": {"PATH": "/x"}}), "may not set PATH"),
    (caps("x", [{"id": "l", "kind": "lint", "run": "source set_env.sh; make"}]), "run"),
    (caps("x", [{"id": "l", "kind": "lint", "run": "a", "command": "b"}]), "should not be valid"),
    ("schema: 1\ncomponent: x\ncapabilities: [\n", "not YAML"),
])
def test_parse_refuses_a_bad_file_whole(doc, needle):
    with pytest.raises(MF.ManifestError) as e:
        MF.parse(doc)
    assert any(needle in p for p in e.value.problems), e.value.problems


def test_schema_examples_parse_and_the_eth_draft_composes_three_paths():
    ex = Path(contracts.schemas_dir()) / "examples"
    eth = MF.parse((ex / "capabilities.eth-chiplet.example.yaml").read_text())
    assert [c["path"] for c in eth["compose"]] == ["tidelink", "tidechart", "nanosoc-multicore-system"]
    lint = next(c for c in eth["capabilities"] if c["id"] == "lint")
    assert lint["verdict"]["forbid"] == ["SKIPPED"] and "nanosoc-multicore-system" in lint["affected_by"]
    MF.parse((ex / "capabilities.tidelink.example.yaml").read_text())


def test_version_compare_and_host_check():
    assert PL.version_ge("4.028", "4.028") and PL.version_ge("T-2022.06-SP2", "T-2022.06")
    assert PL.version_ge("22.03-s005", "22.03") and not PL.version_ge("4.0", "4.028")
    assert not PL.version_ge("2021.1", "2023.2") and PL.version_ge("2025.2", "2023.2")
    cap = {"tools": ["vcs"], "needs": ["vcs-seat", "pdk", "regen:build_soc"]}
    tools = {"vcs": {"min": "T-2022.06", "licence": "synopsys", "seat": "vcs"}}
    ok = {"tools": {"vcs": {"version": "T-2022.06-SP2"}}, "licences": ["synopsys"], "needs": ["pdk"]}
    assert PL.host_check(cap, tools, ok) == []
    assert PL.host_check(cap, tools, {"tools": {"vcs": {"version": "S-2021.09"}}}) == [
        "vcs S-2021.09 is older than T-2022.06", "no synopsys licence", "cannot meet need pdk"]
    assert PL.hosts_for(cap, tools, {"a": ok, "b": {}}) == ["a"]


# ---------------------------------------------------------------------------------------------------------
# Read at the commit, from the mirror
# ---------------------------------------------------------------------------------------------------------
def test_reader_reads_the_file_at_each_commit_from_the_mirror(scene):
    r = MF.Reader(scene.config())
    with scene.env.active():           # a blob the partial mirror lacks is fetched from its promisor (the host)
        one, four = r.at("alpha", scene.a1), r.at("alpha", scene.a4)
    assert one.ok and four.ok
    assert one.capabilities()[0]["run"] == "grep -q good rtl/a.v"
    assert "SKIPPED" in four.capabilities()[0]["run"]                # the file as THAT commit holds it
    assert any("cat-file -p %s:%s" % (scene.a1, CAPS) in s["command"] for s in one.sources)
    g = r.at("alpha", "0123456789abcdef0123456789abcdef01234567")
    assert g.status == "unreadable" and "not in the alpha mirror" in g.problems[0]
    assert r.at("nosuch", scene.a1).status == "unreadable"


def test_reader_absent_invalid_and_wrong_component(scene, tmp_path):
    h = scene.h
    none = h.commit(GH, "SoC-Labs/beta", parents=[scene.b1], files={"rtl/b.v": "b\n"})
    bad = h.commit(GH, "SoC-Labs/beta", parents=[none], files={CAPS: "schema: 1\ncomponent: beta\ncapabilities: 3\n"})
    other = h.commit(GH, "SoC-Labs/beta", parents=[bad], files={CAPS: caps("alpha", [])})
    h.push(GH, "SoC-Labs/beta", other, "reader-cases")
    from nanosoc_rm.mirror.sync import sync_component
    with scene.env.active():
        sync_component("beta")
    r = MF.Reader(scene.config())
    with scene.env.active():
        assert r.at("beta", none).status == "absent"
        assert r.at("beta", bad).status == "invalid"
        w = r.at("beta", other)
    assert w.status == "invalid" and "names component 'alpha'" in w.problems[0]


# ---------------------------------------------------------------------------------------------------------
# Composition and the run set
# ---------------------------------------------------------------------------------------------------------
def _nodes(scene, overrides=None):
    from nanosoc_rm import walker
    with scene.env.active():
        cfg = scene.config()
        docs = walker.walk("top-main", config=cfg, store=False, alerts=False, overrides=overrides)
        return PL.nodes_of(docs, RUN._root_node(cfg, "top-main", docs)), cfg


def test_composition_takes_the_root_file_and_the_composed_submodule_ids(scene):
    nodes, cfg = _nodes(scene)
    with scene.env.active():
        comp = PL.composed(nodes, MF.Reader(cfg))
    assert [(i.key, i.origin) for i in comp.items] == [(".:integ", "own"), (".:by-hand", "own"),
                                                       ("b:unit", "composed by .")]
    assert comp.problems == []


def test_bump_plan_runs_the_moved_trees_own_gates_and_the_consumers_that_name_it(scene):
    now, cfg = _nodes(scene)
    after, _ = _nodes(scene, {"a": scene.a2})
    r = MF.Reader(cfg)
    with scene.env.active():
        rows = PL.plan_bump(PL.composed(now, r), PL.composed(after, r), ["a"], "alpha")
        rows_b = PL.plan_bump(PL.composed(now, r), PL.composed(now, r), ["b"], "beta")
    assert [(x.key, x.why) for x in rows] == [(".:integ", ".'s capability is affected_by a"),
                                             ("a:lint", "own gate of a")]
    lint = next(x for x in rows if x.key == "a:lint")
    assert lint.now.commit == scene.a1 and lint.after.commit == scene.a2     # each side reads its own commit
    # by-hand is manual: never planned; b is not moved: b:unit is not planned
    assert [x.key for x in rows_b] == ["b:unit"] and rows_b[0].why == "own gate of b, composed by ."


# ---------------------------------------------------------------------------------------------------------
# test-bump end to end
# ---------------------------------------------------------------------------------------------------------
def test_a_bump_that_breaks_lint_is_fail_green_to_red(scene):
    doc = scene.test_bump("a", "v2")
    assert contracts.validate(RUN.SCHEMA, doc) == []
    f, rows = doc["fact"], rows_of(doc)
    assert f["to"] == {"ref": "v2", "commit": scene.a2, "resolved_as": "tag v2"} and f["from"] == scene.a1
    assert rows["a:lint"]["now"]["verdict"] == "PASS" and rows["a:lint"]["after"]["verdict"] == "FAIL"
    assert rows["a:lint"]["change"] == "worse" and rows[".:integ"]["change"] == "worse"
    assert f["verdict"] == "FAIL" and "a:lint worse" in f["reason"]
    assert f["hygiene"]["now"]["verdict"] == "PASS" and f["hygiene"]["now"]["commit"] == scene.top
    assert rows["a:lint"]["now"]["commit"] == scene.a1 and rows["a:lint"]["after"]["commit"] == scene.a2


def test_a_bump_to_a_fixed_branch_passes(scene):
    doc = scene.test_bump("a", "fixed")
    f = doc["fact"]
    assert f["to"]["resolved_as"] == "branch fixed" and f["verdict"] == "PASS", f
    assert {r["change"] for r in f["rows"]} == {"same"}


def test_forbid_turns_a_skipped_pass_into_fail(scene):
    f = scene.test_bump("a", "skipping")["fact"]
    lint = rows_of({"fact": f})["a:lint"]
    assert lint["after"]["exit_code"] == 0 and lint["after"]["verdict"] == "FAIL"
    assert any(r["gate"] == "tier.a.lint.forbid" and r["status"] == "FAIL" for r in lint["after"]["rows"])
    assert lint["change"] == "worse" and f["verdict"] == "FAIL"


def test_a_host_without_the_tool_version_is_infra_and_the_move_unverified(scene):
    f = scene.test_bump("a", "fixed", host="bare")["fact"]
    lint = rows_of({"fact": f})["a:lint"]
    assert lint["after"]["verdict"] == "INFRA" and "sh 3.2 is older than 4.0" in lint["after"]["reason"]
    assert lint["change"] == "not-run" and f["verdict"] == "UNVERIFIED"


def test_plan_only_runs_nothing_and_resolve_refuses_unknown_refs(scene):
    f = scene.test_bump("a", scene.a3[:9], plan_only=True)["fact"]
    assert f["plan_only"] and f["rows"] == [] and f["hygiene"] is None and f["verdict"] == "UNVERIFIED"
    assert f["to"]["resolved_as"] == "commit prefix %s" % scene.a3[:9]
    assert [p["key"] for p in f["plan"]] == [".:integ", "a:lint"]
    with pytest.raises(RUN.TestBumpUsage, match="no tag, branch or commit"):
        scene.test_bump("a", "no-such-ref")
    with pytest.raises(RUN.TestBumpUsage, match="has no pin at"):
        scene.test_bump("nope", "v2")
    with pytest.raises(RUN.TestBumpUsage, match="not in the registry"):
        scene.test_bump("a", "v2", host="elsewhere")


def test_cli_exit_codes_follow_the_functional_verdict(scene, monkeypatch):
    for k, v in scene.env.env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(RUN, "test_bump", _with_url_map(RUN.test_bump, scene.url_map))
    r = CliRunner().invoke(cli.main, ["test-bump", "top-main", "a", "v2", "--host", "eda"])
    assert r.exit_code == 1 and "functional verdict: FAIL" in r.output, r.output
    r = CliRunner().invoke(cli.main, ["test-bump", "top-main", "a", "fixed", "--host", "eda", "--json"])
    assert r.exit_code == 0 and json.loads(r.output.strip().splitlines()[-1])["fact"]["verdict"] == "PASS"
    r = CliRunner().invoke(cli.main, ["test-bump", "top-main", "a", "nope", "--host", "eda"])
    assert r.exit_code == 3
    r = CliRunner().invoke(cli.main, ["capabilities", "show", "top-main"])
    assert r.exit_code == 0 and "integ" in r.output and "eda" in r.output, r.output


def _with_url_map(fn, url_map):
    def wrapped(*a, **kw):
        kw.setdefault("url_map", url_map)
        return fn(*a, **kw)
    return wrapped


def test_extra_rows_junit_regex_and_unparsed_formats(tmp_path):
    log = tmp_path / "log"
    log.write_text("ok\nError-SOMETHING bad\nPASS\n")
    (tmp_path / "r.xml").write_text('<testsuite><testcase name="a"/><testcase name="b"><failure/></testcase>'
                                    '</testsuite>')
    junit = RUN._extra_rows({"verdict": {"rows": {"format": "junit", "path": "r.xml"}}}, "x", tmp_path, "", log, 0, "n")
    assert [(r["status"], r["detail"]) for r in junit] == [("FAIL", "2 case(s): 1 failed, 0 skipped, first b")]
    rx = RUN._extra_rows({"verdict": {"rows": {"format": "regex", "fail": "^Error-"}}}, "x", tmp_path, "", log, 0, "n")
    assert rx[0]["status"] == "FAIL"
    un = RUN._extra_rows({"verdict": {"rows": {"format": "regress-table"}}}, "x", tmp_path, "", log, 0, "n")
    assert un[0]["status"] == "UNVERIFIED" and "no parser yet" in un[0]["detail"]
    req = RUN._extra_rows({"verdict": {"require": [{"pattern": "PASS", "min": 2}]}}, "x", tmp_path, "", log, 0, "n")
    assert req[0]["status"] == "FAIL"


@pytest.mark.parametrize("now,after,change", [
    ("PASS", "PASS", "same"), ("FAIL", "PASS", "better"), ("PASS", "FAIL", "worse"), ("FAIL", "FAIL", "same-red"),
    (None, "FAIL", "new-red"), (None, "PASS", "new"), ("PASS", None, "gone"), ("PASS", "INFRA", "not-run"),
    ("INFRA", "FAIL", "new-red")])
def test_compare(now, after, change):
    assert RUN.compare(now and {"verdict": now}, after and {"verdict": after}) == change


def test_functional_verdict_ladder_and_report_gates_never_decide():
    row = lambda ch, gate="block": {"key": "k", "gate": gate, "change": ch}
    assert RUN.functional_verdict([])[0] == "UNVERIFIED"
    assert RUN.functional_verdict([row("same"), row("worse", "report")]) == ("PASS", None)
    assert RUN.functional_verdict([row("same-red"), row("new-red")])[0] == "UNVERIFIED"
    assert RUN.functional_verdict([row("same-red"), row("worse"), row("new-red")])[0] == "FAIL"
    assert RUN.functional_verdict([row("same-red"), row("better")])[0] == "WARN"


# ---------------------------------------------------------------------------------------------------------
# The executor queue (jobs.py) and the bump gate
# ---------------------------------------------------------------------------------------------------------
def _fake_doc(scene, verdict, *, to=None, root=None, paths=("a",)):
    f = {"configuration": "top-main", "root_commit": root or scene.top, "path": "a", "component": "alpha",
         "from": scene.a1, "to": {"ref": "x", "commit": to or scene.a3, "resolved_as": "branch x"},
         "tree_paths": list(paths), "moved": list(paths), "host": "eda", "plan_only": False, "plan": [],
         "manifests": [], "problems": [], "hygiene": None,
         "rows": [{"key": "a:lint", "owner": "a", "id": "lint", "kind": "lint", "tier": "t1", "gate": "block",
                   "why": "w", "now": {"verdict": "PASS", "reason": None, "rows": [], "commit": scene.a1},
                   "after": {"verdict": "PASS" if verdict == "PASS" else "FAIL", "reason": None, "rows": [],
                             "commit": to or scene.a3},
                   "change": "same" if verdict == "PASS" else "worse"}],
         "verdict": verdict, "reason": None, "started_at": "2026-10-16T10:00:00Z",
         "finished_at": "2026-10-16T10:01:00Z"}
    doc = {"schema": 1, "kind": "test_bump", "subject": "test_bump:top-main:%s" % ("ab" * 8),
           "observed_at": "2026-10-16T10:00:00Z", "sources": [{"kind": "derived", "command": "x"}], "fact": f}
    assert contracts.validate(RUN.SCHEMA, doc) == []
    return doc


def test_tier_jobs_wait_for_an_executor_and_the_local_worker_never_runs_them(scene, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_LOCAL_TIERS", raising=False)
    cfg = scene.config()
    with scene.env.active():
        job = jobs.submit(cfg, "test-bump", {"configuration": "top-main", "path": "a", "to": "fixed"}, by="member-a")
        tier_compat = jobs.submit(cfg, "compat", {"configuration": "top-main", "tier": "t1"}, by="member-a")
        assert jobs.needs_executor("test-bump", job["params"]) and jobs.needs_executor("compat", tier_compat["params"])
        assert not jobs.needs_executor("compat", {"configuration": "top-main"})
        assert jobs.Worker(cfg.path, argv_prefix=["/bin/false"]).step() is None      # nothing it may run
        assert jobs.get(cfg, job["id"])["state"] == "queued"
        assert [j["id"] for j in jobs.executor_queue(cfg)] == [job["id"], tier_compat["id"]]
        with pytest.raises(jobs.JobUsage):
            jobs.claim_for_executor(cfg, "local")
        got = jobs.claim_for_executor(cfg, "eda-agent", accepts=lambda j: j["kind"] == "test-bump")
        assert got["id"] == job["id"] and got["state"] == "running" and got["executor"] == "eda-agent"
        assert jobs.cli_args(got["kind"], got["params"]) == ["test-bump", "top-main", "a", "fixed", "--json"]
        assert jobs.recover(cfg) == [] and jobs.get(cfg, job["id"])["state"] == "running"   # another host's job
        with pytest.raises(jobs.JobUsage):
            jobs.finish_for_executor(cfg, job["id"], "someone-else", 0, "")
        done = jobs.finish_for_executor(cfg, job["id"], "eda-agent", 0,
                                        json.dumps(_fake_doc(scene, "PASS")) + "\n", "log line\n")
        assert done["state"] == "done" and done["verdict"] == "PASS" and done["result"]["kind"] == "test_bump"
        jobs.cancel(cfg, tier_compat["id"], "member-a")


def test_the_local_fallback_runs_tier_jobs_in_process(scene, monkeypatch):
    cfg = scene.config()
    with scene.env.active():
        job = jobs.submit(cfg, "test-bump", {"configuration": "top-main", "path": "a", "to": "nope"}, by="member-a")
        monkeypatch.setenv("NANOSOC_RM_LOCAL_TIERS", "1")
        assert jobs.Worker(cfg.path).step() == job["id"]
        got = jobs.get(cfg, job["id"])
        assert got["executor"] == jobs.LOCAL_EXECUTOR and got["state"] == "failed"     # exit 3: no such ref


def test_bump_gate_needs_a_passing_test_bump_of_exactly_this_move(scene, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_LOCAL_TIERS", raising=False)
    cfg = scene.config()
    to = scene.a2
    with scene.env.active():
        ev = GATE.evidence(cfg, "top-main", ["a"], to, scene.top)
        assert ev["verdict"] is None and "no test-bump" in GATE.blocker(ev, "top-main", "a", to)
        for verdict, root in (("FAIL", scene.top), ("PASS", "f" * 40)):
            j = jobs.submit(cfg, "test-bump", {"configuration": "top-main", "path": "a", "to": to}, by="m")
            jobs.claim_for_executor(cfg, "eda-agent")
            jobs.finish_for_executor(cfg, j["id"], "eda-agent", 1 if verdict == "FAIL" else 0,
                                     json.dumps(_fake_doc(scene, verdict, to=to, root=root)) + "\n")
        ev = GATE.evidence(cfg, "top-main", ["a"], to, scene.top)
        assert ev["verdict"] == "FAIL" and "is FAIL" in GATE.blocker(ev, "top-main", "a", to)
        assert "another root commit" not in (ev["note"] or "")
        j = jobs.submit(cfg, "test-bump", {"configuration": "top-main", "path": "a", "to": to}, by="m")
        jobs.claim_for_executor(cfg, "eda-agent")
        jobs.finish_for_executor(cfg, j["id"], "eda-agent", 0, json.dumps(_fake_doc(scene, "PASS", to=to)) + "\n")
        ev = GATE.evidence(cfg, "top-main", ["a"], to, scene.top)
        assert ev["verdict"] == "PASS" and ev["job"] == j["id"] and GATE.blocker(ev, "top-main", "a", to) is None
        assert GATE.evidence(cfg, "top-main", ["a", "z"], to, scene.top)["verdict"] is None   # not every path
        assert GATE.evidence(cfg, "top-main", ["a"], scene.a4, scene.top)["verdict"] is None  # another move
        # review g1: a PASS document from a command that exited non-zero is not a pass
        j = jobs.submit(cfg, "test-bump", {"configuration": "top-main", "path": "a", "to": to}, by="m")
        jobs.claim_for_executor(cfg, "eda-agent")
        jobs.finish_for_executor(cfg, j["id"], "eda-agent", 1, json.dumps(_fake_doc(scene, "PASS", to=to)) + "\n")
        ev = GATE.evidence(cfg, "top-main", ["a"], to, scene.top)
        assert ev["job"] == j["id"] and ev["verdict"] == "UNVERIFIED" and "exited 1" in ev["note"]
        assert GATE.blocker(ev, "top-main", "a", to) is not None


def test_jobs_store_gains_the_executor_column_once(tmp_path):
    import sqlite3
    from nanosoc_rm.config import load_config
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(str(data / "jobs.sqlite"))
    con.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        params TEXT NOT NULL, state TEXT NOT NULL, by TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
        finished_at TEXT, pid INTEGER, exit_code INTEGER, verdict TEXT, reason TEXT, result TEXT, cancel_by TEXT,
        command TEXT NOT NULL); PRAGMA user_version = 1;""")
    con.execute("INSERT INTO jobs (kind, params, state, by, created_at, command) VALUES "
                "('walk', '{\"configuration\": \"x\"}', 'done', 'm', '2026-10-01T00:00:00Z', 'c')")
    con.commit()
    con.close()
    (tmp_path / "c.toml").write_text('[service]\ndata_dir = "%s"\n[registry]\npath = "%s"\n' % (data, tmp_path))
    cfg = load_config(tmp_path / "c.toml")
    assert jobs.get(cfg, 1)["executor"] is None
    con = sqlite3.connect(str(data / "jobs.sqlite"))
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2


# ---------------------------------------------------------------------------------------------------------
# The pages: Capabilities, Test this bump (POST, CSRF), Home's detail panel
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def site(scene, tmp_path_factory):
    import re
    from fastapi.testclient import TestClient
    from nanosoc_rm import auth
    from nanosoc_rm.web.app import create_app
    with scene.env.active():
        from nanosoc_rm.freshness.scan import scan as fscan
        fscan(["top-main"], config=scene.config(), store=True)
    pw = "correct horse battery"
    cfg = tmp_path_factory.mktemp("capsweb") / "rm.toml"
    cfg.write_text(scene.env.config.read_text() + '\n[auth]\nmode = "local"\n[[auth.users]]\nlogin = "alice"\n'
                   'hash = "%s"\n' % auth.hash_password(pw, salt=b"0123456789abcdef"))
    env = dict(scene.env.env, NANOSOC_RM_CONFIG=str(cfg))
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        c = TestClient(create_app(cfg))
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/auth/login").text).group(1)
        c.post("/auth/login", data={"csrf_token": tok, "login": "alice", "password": pw, "next": "/"},
               follow_redirects=False)
        yield {"client": c, "cfg": cfg}
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _csrf(client, path):
    import re
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def test_capabilities_page_shows_each_tree_its_hosts_and_last_result(site):
    c = site["client"]
    r = c.get("/capabilities/top-main")
    assert r.status_code == 200, r.text[:500]
    t = r.text
    assert 'data-tree="." data-status="ok"' in t and 'data-tree="a" data-status="ok"' in t
    assert 'data-capability=".:integ"' in t and 'data-capability="b:unit"' in t and "composed by ." in t
    # integ needs sh >= 4.0: host eda (5.1) runs it, host bare (3.2) cannot, and the page says why
    row = t.split('data-capability=".:integ"', 1)[1].split("</tr>", 1)[0]
    assert "<td>eda<br>" in row and "not bare: sh 3.2 is older than 4.0" in row
    assert c.get("/capabilities/").status_code == 200
    assert c.get("/capabilities/no-such").status_code == 404


def test_test_this_bump_queues_a_job_for_an_executor_behind_csrf(site, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_LOCAL_TIERS", raising=False)
    c = site["client"]
    page = c.get("/capabilities/top-main/bump?path=a")
    assert page.status_code == 200 and "data-test-bump" in page.text and 'value="main"' in page.text
    r = c.post("/capabilities/top-main/test-bump", data={"path": "a", "to": "fixed"}, follow_redirects=False)
    assert r.status_code == 403                                   # no CSRF token: refused before the handler
    tok = _csrf(c, "/capabilities/top-main/bump?path=a")
    r = c.post("/capabilities/top-main/test-bump", data={"csrf_token": tok, "path": "a", "to": "fixed"},
               follow_redirects=False)
    assert r.status_code == 303 and "/capabilities/top-main/bump?job=" in r.headers["location"]
    jid = int(r.headers["location"].split("job=")[1].split("&")[0])
    from nanosoc_rm.config import load_config
    cfg = load_config(site["cfg"])
    job = jobs.get(cfg, jid)
    assert job["kind"] == "test-bump" and job["state"] == "queued" and job["by"] == "alice"
    assert jobs.Worker(cfg.path, argv_prefix=["/bin/false"]).step() is None    # the service host never runs it
    shown = c.get(r.headers["location"]).text
    assert "waits for an executor" in shown and "waiting for an executor" in shown
    tok = _csrf(c, "/capabilities/top-main/bump?path=a")
    bad = c.post("/capabilities/top-main/test-bump", data={"csrf_token": tok, "path": "../x", "to": "fixed"},
                 follow_redirects=False)
    assert bad.status_code == 400
    jobs.cancel(cfg, jid, "alice")


def test_home_detail_panel_has_a_capabilities_section_and_the_button(site):
    c = site["client"]
    t = c.get("/?v=1&sel=n:top-main:a").text
    assert "Pin in top-main" in t, t[t.find('id="detail"'):][:600]
    assert "data-capabilities" in t and "<code>lint</code> lint" in t
    assert 'action="/capabilities/top-main/test-bump"' in t and 'name="path" value="a"' in t
    aside = c.get("/configurations/top-main/tree?node=a").text            # the submodule tree's pin aside
    assert 'data-test-bump-link' in aside and "/capabilities/top-main/bump?path=a" in aside
    # a configuration row (its root tree): the section, no button (nothing to move); a frozen pin: no button either
    from nanosoc_rm.capabilities.view import tree_view
    from nanosoc_rm.config import load_config
    from nanosoc_rm.web import home
    view = tree_view(load_config(site["cfg"]), "top-main")

    class Data:
        def capabilities(self, cid):
            return view
    sec, forms = home._capabilities_section(Data(), "top-main", None, None, True)
    assert [r["id"] for r in sec["rows"]] == ["integ", "by-hand"] and forms == []
    assert sec["rows"][1]["hosts"] == "bare, eda"                  # by-hand needs no tool: both hosts
    sec, forms = home._capabilities_section(Data(), "top-main", "b", None, True)
    assert [r["id"] for r in sec["rows"]] == ["unit", "slow"] and forms == []

    class Broken:
        def capabilities(self, cid):
            raise RuntimeError("mirror gone")
    sec, _ = home._capabilities_section(Broken(), "top-main", "a", "main", True)
    assert sec["rows"] == [] and sec["line"].startswith("Capabilities not read: mirror gone")


def test_a_gate_lost_to_a_broken_after_manifest_is_not_run_not_gone(scene):
    """Review g1: a bump whose new commit breaks (or deletes) its own manifest must not pass on the gates it no
    longer declares; `gone` (PASS) is only for a valid After manifest that drops the capability."""
    at = load_at("test_pins")
    h = scene.h
    a5 = h.commit(GH, "SoC-Labs/alpha", parents=[scene.a3], files={"rtl/a.v": "good\n", CAPS: "schema: 1\nbad: [\n"})
    h.push(GH, "SoC-Labs/alpha", a5, "broken-manifest")
    with scene.env.active():
        at.rm("mirror.sync_component")("alpha")
    f = scene.test_bump("a", "broken-manifest")["fact"]
    rows = rows_of({"fact": f})
    assert rows[".:integ"]["change"] == "same" and rows["a:lint"]["change"] == "not-run"
    assert f["verdict"] == "UNVERIFIED" and "a:lint not-run" in f["reason"]
