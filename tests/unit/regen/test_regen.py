"""The regenerator (interfaces.md section 20; C3 3/3, C7): sandbox renders, the class measurement, the refusals,
and the wiring into the versioning classifier and the bump engine's class.

Hermetic: a fake generator (a POSIX sh script, `gen/gen.sh`) on a local github host, pinned twice by a SoC (B7:
`gen` and `vendor/gen`); the walker's mirrors are synced from the hosts and the sandbox walk reaches the same bare
repos through the test-only url_map seam, so a render runs exactly the git commands of a real one.

fakegen commits (each on its own branch; main ends at `brk`, tagged v0.2.0; `g0` is v0.1.0):
    g0     base: top.v (ports clk, led), flist/asic.f, memmap.h, env.txt; prints a vendor "skipping" warning
    same   only gen.sh's own comment changes: identical output             -> none
    fix    a non-shape line of top.v changes (assign value)                -> fix
    feat   adds rtl/extra.v and its flist entry                            -> feature, build-affecting
    brk    port `led` renamed `leds`                                       -> breaking
    fail   the generator exits 1                                           -> FAIL, no class
    slow   sleeps past a 2 s budget                                        -> killed: INFRA, never C5 breaking
    nomod  exits 1 on a missing Python module (this host's environment)   -> INFRA, never C5 breaking
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from analysis_fixtures import load_at  # noqa: E402

from nanosoc_rm import contracts, db  # noqa: E402
from nanosoc_rm.regen import measure as M  # noqa: E402
from nanosoc_rm.regen import sandbox as SB  # noqa: E402
from nanosoc_rm.regen import spec as S  # noqa: E402

GH = "github"

GEN = r'''#!/bin/sh
# fake nanosoc_gen (%(note)s)
out="$1"
mkdir -p "$out/rtl" "$out/flist"
echo "  WARNING: ARM_IP_LIBRARY_PATH not set, skipping BuildBusMatrix.pl"
cat > "$out/rtl/top.v" <<EOF
// Generated: $(date '+%%Y-%%m-%%d %%H:%%M:%%S')
// from $TREE/gen
module top (
  input  wire clk,
  output wire [7:0] %(port)s
);
  assign %(port)s = 8'h%(value)s;
endmodule
EOF
printf 'rtl/top.v\n%(extra_flist)s' > "$out/flist/asic.f"
printf '#define UART_BASE 0x40000000\n' > "$out/memmap.h"
printf 'PYTHONDONTWRITEBYTECODE=%%s ARM=%%s\n' "${PYTHONDONTWRITEBYTECODE:-unset}" "${ARM_IP_LIBRARY_PATH:-unset}" > "$out/env.txt"
%(extra)s
'''


def gen(note="base", port="led", value="00", extra="", extra_flist=""):
    return GEN % {"note": note, "port": port, "value": value, "extra": extra, "extra_flist": extra_flist}


REGEN = {"command": "sh gen/gen.sh build_soc", "cwd": ".", "budget_s": 60, "output": "build_soc"}


class Scene:
    def __init__(self, at, h):
        self.at, self.h = at, h
        g0 = h.commit(GH, "SoC-Labs/fakegen", files={"gen.sh": gen()})
        h.push(GH, "SoC-Labs/fakegen", g0, "main")
        h.tag(GH, "SoC-Labs/fakegen", "v0.1.0", g0)
        c = {"g0": g0}
        variants = {
            "same": gen(note="comment only"),
            "fix": gen(value="01"),
            "feat": gen(extra="printf 'module extra; endmodule\\n' > \"$out/rtl/extra.v\"", extra_flist="rtl/extra.v\\n"),
            "fail": gen() + "exit 1\n",
            "brk": gen(port="leds"),
            "slow": gen() + "sleep 6\n",
            "nomod": gen() + "echo \"ModuleNotFoundError: No module named 'yaml'\" >&2\nexit 1\n",
        }
        for name, text in variants.items():
            c[name] = h.commit(GH, "SoC-Labs/fakegen", parents=[g0], files={"gen.sh": text}, message=name)
            h.push(GH, "SoC-Labs/fakegen", c[name], "b-" + name)
        h.push(GH, "SoC-Labs/fakegen", c["brk"], "main")
        h.tag(GH, "SoC-Labs/fakegen", "v0.2.0", c["brk"])
        self.c = c
        self.soc = h.commit(GH, "SoC-Labs/soc", files={"README": "soc\n", "build_soc/stale.v": "module stale;\n"},
                            gitlinks={"gen": g0, "vendor/gen": g0},
                            gitmodules=[{"path": "gen", "url": h.url(GH, "SoC-Labs/fakegen")},
                                        {"path": "vendor/gen", "url": h.url(GH, "SoC-Labs/fakegen")}])
        h.push(GH, "SoC-Labs/soc", self.soc, "main")
        comps = ["soc", "fakegen"]
        catalogue = {"schema": 1, "components": [
            {"id": "soc", "class": "soc_ref", "canonical": {"host": GH, "path": "SoC-Labs/soc"}},
            {"id": "fakegen", "class": "generator", "canonical": {"host": GH, "path": "SoC-Labs/fakegen"}}]}
        eligibility = {"schema": 1, "configurations": [
            {"id": "soc-main", "component": "soc", "ref": {"branch": "main"}}]}
        self.registry = at.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
        self.env = h.tool_env(self.registry, "regen")
        self.set_regen(REGEN)
        self.settings({"work_dir": str(h.root / "regen-work")})
        with self.env.active():
            for comp in comps:
                at.rm("mirror.sync_component")(comp)
            at.rm("walker.walk")("soc-main")
        self.url_map = {"https://github.com/SoC-Labs/%s.git" % x: str(h.bare(GH, "SoC-Labs/%s" % x)) for x in comps}

    def set_regen(self, spec):
        (self.registry / "tiers.yaml").write_text(yaml.safe_dump(
            {"schema": 1, "configurations": {"soc-main": {"regen": spec}}}, sort_keys=False))

    def settings(self, table):
        self.env.cfg["regen"] = dict(table)
        self.env.config.write_text(self.at.toml_dumps(self.env.cfg))

    def config(self):
        from nanosoc_rm.config import load_config
        return load_config(self.env.config)

    def render(self, commit, **kw):
        with self.env.active():
            return SB.render("soc-main", "fakegen", self.c.get(commit, commit), config=self.config(),
                             url_map=self.url_map, **kw)

    def measure(self, frm, to, **kw):
        with self.env.active():
            return M.measure("soc-main", "fakegen", self.c.get(frm, frm), self.c.get(to, to), config=self.config(),
                             url_map=self.url_map, **kw)


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    at = load_at("test_pins")
    h = at.LocalHosts(tmp_path_factory.mktemp("regen"))
    s = Scene(at, h)
    yield s
    h.api.close()


@pytest.fixture(autouse=True)
def _reset(scene, monkeypatch):
    for k in ("CI_JOB_ID", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT"):
        monkeypatch.delenv(k, raising=False)
    scene.set_regen(REGEN)
    scene.settings({"work_dir": str(scene.h.root / "regen-work")})
    yield


def fact(obs):
    assert contracts.validate_observation(obs) == [], contracts.validate_observation(obs)
    return obs["fact"]


# ---------------------------------------------------------------------------------------------------------
# One render
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C3")
def test_render_is_a_clean_sandbox_and_two_renders_of_one_commit_are_identical(scene, monkeypatch):
    monkeypatch.setenv("ARM_IP_LIBRARY_PATH", "/should/not/leak")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    a = scene.render("g0", fresh=True)
    b = scene.render("g0", fresh=True)
    assert a.status == "ok" and a.verdict == "PASS", a.reason
    assert a.tree_paths == ["gen", "vendor/gen"]                       # B7: both pins of the component
    assert a.outputs["env.txt"].strip() == "PYTHONDONTWRITEBYTECODE=1 ARM=unset"
    top = a.outputs["rtl/top.v"]
    assert "Generated:" not in top and "// from $TREE/gen" in top      # stamp dropped, sandbox path normalised
    assert a.digest == b.digest and a.outputs == b.outputs              # the sandboxes differ; the output does not
    assert "stale.v" not in a.outputs                                   # the committed build_soc was deleted first
    assert a.flists == {"asic.f": ["rtl/top.v"]}
    assert any("skipping BuildBusMatrix.pl" in c for c in a.coverage), a.coverage
    assert a.root_commit == scene.soc


def test_render_moves_every_split_pin_and_keeps_the_sandbox_when_asked(scene, tmp_path):
    import subprocess
    assert scene.render("brk").status == "ok"                          # cached now: keep_work must not serve it
    keep = tmp_path / "kept"
    r = scene.render("brk", keep_work=keep)
    assert r.status == "ok" and not r.cached
    for tp in ("gen", "vendor/gen"):
        head = subprocess.run(["git", "-C", str(keep / "tree" / tp), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        assert head == scene.c["brk"], tp
    assert (keep / "tree" / "build_soc" / "rtl" / "top.v").is_file()


def test_an_override_the_walk_does_not_reach_is_unverified_and_not_run(scene):
    r = scene.render("g0", paths=["nope"], fresh=True)
    assert r.status == "not-run" and r.verdict == "UNVERIFIED" and "did not reach nope" in r.reason
    assert r.outputs == {} and r.seconds is None


def test_a_tool_missing_on_this_host_is_infra(scene):
    scene.set_regen(dict(REGEN, env={"GEN_TOOL": "/no/such/tool/bin"}))
    r = scene.render("g0", fresh=True)
    assert r.status == "not-run" and r.verdict == "INFRA" and "tool not on this host" in r.reason


def test_the_cache_serves_a_second_render_and_refuses_a_tampered_one(scene):
    a = scene.render("fix", fresh=True)
    b = scene.render("fix")
    assert not a.cached and b.cached and b.outputs == a.outputs and b.digest == a.digest
    d = next((scene.h.root / "regen-work" / "cache" / "soc-main" / "fakegen" / scene.c["fix"]).iterdir())
    (d / "files" / "rtl" / "top.v").write_text("tampered\n")
    c = scene.render("fix")
    assert not c.cached and c.outputs == a.outputs                      # a digest mismatch is a cache miss


# ---------------------------------------------------------------------------------------------------------
# The class measurement (C3 decision table, C7)
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C3")
@pytest.mark.requirement("C7")
@pytest.mark.parametrize("to,cls,build", [("same", "none", False), ("fix", "fix", False),
                                          ("feat", "feature", True), ("brk", "breaking", False)])
def test_the_class_of_a_move_is_measured_from_the_regenerated_output(scene, to, cls, build):
    f = fact(scene.measure("g0", to))
    assert f["verdict"] == "PASS" and f["reason"] is None, f
    assert f["class"] == cls, f["reasons"]
    assert f["build_affecting"] is build and (f["flists_changed"] == (["asic.f"] if build else []))
    assert f["from"]["digest"] and f["to"]["digest"]
    assert (f["from"]["digest"] == f["to"]["digest"]) is (cls == "none")
    if cls == "breaking":
        assert any("port" in r and "removed or changed" in r for r in f["reasons"]), f["reasons"]
        assert f["files"]["counts"]["changed"] == 1 and f["files"]["changed"] == ["rtl/top.v"]
    if cls == "feature":
        assert f["files"]["added"] == ["rtl/extra.v"]
    assert f["tree_paths"] == ["gen", "vendor/gen"] and f["root_commit"] == scene.soc


def test_a_generator_that_starts_failing_is_breaking_and_one_that_failed_before_is_fail(scene):
    f = fact(scene.measure("g0", "fail"))
    assert f["verdict"] == "PASS" and f["class"] == "breaking", f
    assert f["files"] is None and f["build_affecting"] is None                      # C7 not measured at a failure
    assert f["from"]["status"] == "ok" and f["to"]["status"] == "failed" and f["to"]["exit_code"] == 1
    assert ("fails for soc-main at %s" % scene.c["fail"][:12]) in f["reasons"][0]
    f = fact(scene.measure("fail", "g0"))
    assert f["verdict"] == "FAIL" and f["class"] is None and f["files"] is None and f["build_affecting"] is None
    assert ("failed at %s" % scene.c["fail"][:12]) in f["reason"]
    assert M.EXIT[f["verdict"]] == 1
    assert all("$TREE" in x or "/" not in x for x in f["from"]["log_tail"]), f["from"]["log_tail"]


# ---------------------------------------------------------------------------------------------------------
# Refusals (exit 3), before any work
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change,needle", [
    ({"env": {"ARM_IP_LIBRARY_PATH": "/x"}}, "may not set ARM_IP_LIBRARY_PATH"),
    ({"env": {"SOCLABS_DMA250_DIR": "/x"}}, "may not set SOCLABS_DMA250_DIR"),
    ({"command": "source ./set_env.sh && make"}, "sources a set_env.sh"),
    ({"output": "/abs/build_soc"}, "output must be a tree-relative directory"),
    ({"output": "../up"}, "output must be a tree-relative directory"),
])
def test_a_spec_the_tool_will_not_run_is_refused(scene, change, needle):
    spec = S.check(dict(REGEN), "soc-main", S.Settings(work_dir=Path("/tmp/x")))
    assert spec["env"]["PYTHONDONTWRITEBYTECODE"] == "1" and spec["command"][:3] == ["nice", "-n", "15"]
    with pytest.raises(S.RegenUsage, match=needle.replace(".", r"\.")):
        S.check(dict(REGEN, **change), "soc-main", S.Settings(work_dir=Path("/tmp/x")))


def test_paths_under_a_read_only_root_are_refused(scene, tmp_path):
    ro = tmp_path / "ip_library"
    (ro / "lib").mkdir(parents=True)
    st = S.Settings(work_dir=tmp_path / "work", read_only_roots=[ro])
    with pytest.raises(S.RegenUsage, match="read-only root"):
        S.check(dict(REGEN, env={"GEN_LIB": "%s/lib" % ro}), "soc-main", st)
    with pytest.raises(S.RegenUsage, match="read-only root"):
        S.check(dict(REGEN, command="sh gen/gen.sh %s/out" % ro), "soc-main", st)
    with pytest.raises(S.RegenUsage, match="work_dir"):
        S.check(dict(REGEN), "soc-main", S.Settings(work_dir=ro / "lib" / "w", read_only_roots=[ro]))
    link = tmp_path / "innocent"
    link.symlink_to(ro)                                                 # a symlink into the root is still the root
    with pytest.raises(S.RegenUsage, match="read-only root"):
        S.check(dict(REGEN, env={"GEN_LIB": str(link)}), "soc-main", st)
    S.check(dict(REGEN, env={"GEN_LIB": str(tmp_path / "elsewhere")}), "soc-main", st)


def test_no_regen_spec_or_no_stored_pin_is_a_usage_error(scene):
    (scene.registry / "tiers.yaml").unlink()
    with pytest.raises(S.RegenUsage, match="declares no regen command"):
        scene.render("g0")
    scene.set_regen(REGEN)
    with scene.env.active():
        with pytest.raises(S.RegenUsage, match="pins no nosuch"):
            SB.render("soc-main", "nosuch", scene.c["g0"], config=scene.config(), url_map=scene.url_map)


def test_the_cli_refuses_and_answers_with_the_section_0_exit_codes(scene):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, ["regen", "class", "soc-main", "--component", "fakegen", "--from", "abc", "--to",
                                  scene.c["brk"]], env=scene.env.env)
    assert r.exit_code == 3 and "not a full 40-hex commit id" in r.output
    scene.set_regen(dict(REGEN, env={"ARM_IP_LIBRARY_PATH": "/x"}))   # the registry schema refuses it first
    r = CliRunner().invoke(main, ["regen", "run", "soc-main", "--component", "fakegen", "--commit", scene.c["g0"]],
                           env=scene.env.env)
    assert r.exit_code == 2 and "ARM_IP_LIBRARY_PATH" in r.output and "registry does not load" in r.output
    ro = scene.h.root / "read-only-lib"
    ro.mkdir(exist_ok=True)
    scene.set_regen(dict(REGEN, env={"GEN_LIB": str(ro)}))
    scene.settings({"work_dir": str(scene.h.root / "regen-work"), "read_only_roots": [str(ro)]})
    r = CliRunner().invoke(main, ["regen", "run", "soc-main", "--component", "fakegen", "--commit", scene.c["g0"]],
                           env=scene.env.env)
    assert r.exit_code == 3 and "under the read-only root" in r.output, r.output


# ---------------------------------------------------------------------------------------------------------
# Wiring: the classifier, version next, the bump engine's class, pending, the job kind
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.requirement("C3")
@pytest.mark.requirement("E1")
def test_a_stored_measurement_classes_the_bump_and_the_version_plan(scene):
    from nanosoc_rm import versioning
    from nanosoc_rm.bumps import engine
    g0, brk = scene.c["g0"], scene.c["brk"]
    with scene.env.active():
        cfg = scene.config()
        before = versioning.classify_bump("fakegen", g0, brk, config=cfg)
        assert before is None                                   # no changelog, no API.md, nothing measured: no class
        plan = versioning.plan("fakegen", config=cfg)["fact"]
        sig = {s["signal"]: s for s in plan["classification"]["signals"]}
        assert sig["consumers"]["status"] == "unmeasured" and "regen class CONFIG --component fakegen" in \
            sig["consumers"]["detail"]
        obs = scene.measure("g0", "brk", store=True)
        assert db.latest("regen_class", "regen_class:fakegen:%s:%s:" % (g0, brk)) == [obs]
        got = versioning.classify_bump("fakegen", g0, brk, config=cfg)
        assert got["class"] == "breaking" and "consumers breaking (measured)" in got["detail"], got
        assert {"kind": "derived", "command": "stored %s" % obs["subject"]} in got["sources"], got["sources"]
        _, props = engine.scan("soc-main", config=cfg)
        moves = [p["fact"] for p in props if p["fact"]["dependency"]["component"] == "fakegen"]
        assert moves, [p["fact"]["dependency"] for p in props]
        for f in moves:
            assert f["to"]["commit"] == brk
            assert f["change"]["class"] == "breaking" and f["change"]["class_source"] == "classifier", f["change"]
            assert "consumers breaking (measured)" in f["change"]["class_detail"]
    # version next: the baseline v0.2.0 (brk) to a new main head that only adds a feature
    post = scene.h.commit(GH, "SoC-Labs/fakegen", parents=[brk], message="post", files={"gen.sh": gen(
        port="leds", extra="printf 'module extra; endmodule\\n' > \"$out/rtl/extra.v\"", extra_flist="rtl/extra.v\\n")})
    scene.h.push(GH, "SoC-Labs/fakegen", post, "main")
    with scene.env.active():
        scene.at.rm("mirror.sync_component")("fakegen")
        plan = versioning.plan("fakegen", config=cfg)["fact"]
        sig = {s["signal"]: s for s in plan["classification"]["signals"]}
        assert sig["consumers"]["status"] == "unmeasured"
        assert ("--from %s --to %s" % (brk, post)) in sig["consumers"]["detail"]
        assert fact(scene.measure(brk, post, store=True))["class"] == "feature"
        plan = versioning.plan("fakegen", config=cfg)["fact"]
        sig = {s["signal"]: s for s in plan["classification"]["signals"]}
    assert sig["consumers"]["status"] == "measured" and sig["consumers"]["class"] == "feature", sig["consumers"]
    assert sig["build"]["status"] == "measured" and sig["build"]["build_affecting"] is True
    assert plan["classification"]["class"] == "feature" and plan["next"]["version"] == "0.2.1", plan["next"]


def test_a_failed_measurement_leaves_the_signal_unmeasured_with_its_reason(scene):
    from nanosoc_rm.versioning.consumers import regen_signals
    obs = scene.measure("fail", "g0", store=True)
    with scene.env.active():
        cons, build = regen_signals("fakegen", scene.c["fail"], scene.c["g0"], scene.config())
    assert cons["status"] == "unmeasured" and cons["class"] is None and "soc-main FAIL" in cons["detail"]
    assert build["status"] == "unmeasured" and build["build_affecting"] is None
    assert obs["fact"]["verdict"] == "FAIL"


def test_a_live_regenerator_plugs_into_the_classifier_and_a_failure_is_unmeasured_not_a_crash(scene):
    from nanosoc_rm.versioning import classify as cf
    with scene.env.active():
        regen = SB.SandboxRegenerator(scene.config(), url_map=scene.url_map)
        assert regen.available() == (True, "") and regen.consumers("fakegen") == ["soc-main"]
        ok = cf.consumer_signal("fakegen", scene.c["g0"], scene.c["feat"], regen)
        bad = cf.consumer_signal("fakegen", scene.c["fail"], scene.c["g0"], regen)
        newly = cf.consumer_signal("fakegen", scene.c["g0"], scene.c["fail"], regen)
        build = cf.build_signal("fakegen", scene.c["g0"], scene.c["feat"], regen)
    assert ok["status"] == "measured" and ok["class"] == "feature" and ok["generator"] == "sandbox"
    assert bad["status"] == "unmeasured" and bad["class"] is None and "the generator failed" in bad["detail"]
    assert newly["status"] == "measured" and newly["class"] == "breaking"          # C5, as the stored measurement
    assert build["status"] == "measured" and build["build_affecting"] is True


def test_pending_lists_the_moves_plans_and_bumps_need_and_run_measures_them(scene):
    from nanosoc_rm import versioning
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    with scene.env.active():
        db.put(versioning.plan("fakegen", config=scene.config()), config=scene.config())
        rows = M.pending(scene.config())
    want = {"configuration": "soc-main", "component": "fakegen", "from": scene.c["g0"], "to": scene.c["brk"]}
    if not any({k: r[k] for k in want} == want for r in rows):           # measured by an earlier test: none pending
        with scene.env.active():
            assert M.stored("fakegen", scene.c["g0"], scene.c["brk"], config=scene.config())
    r = CliRunner().invoke(main, ["regen", "pending", "--run", "--json"], env=scene.env.env)
    assert r.exit_code in (0, 1), r.output
    with scene.env.active():
        assert all({k: x[k] for k in want} != want for x in M.pending(scene.config()))


def test_the_regen_class_job_kind(scene):
    from nanosoc_rm import jobs
    from nanosoc_rm.regen import jobkind
    k = jobs.registered("regen-class")
    assert k is not None
    p = {"configuration": "soc-main", "component": "fakegen", "from": scene.c["g0"], "to": scene.c["brk"]}
    assert k.params(p) == p
    assert jobkind.cli_args(p)[:3] == ["regen", "class", "soc-main"] and jobkind.cli_args(p)[-2:] == ["--store", "--json"]
    for bad in ({**p, "from": "abc"}, {**p, "configuration": "../x"}):
        with pytest.raises(ValueError):
            k.params(bad)
    obs = scene.measure("g0", "fix")
    assert jobkind.outcome([obs], "")["verdict"] == "PASS"
    assert jobkind.outcome([obs], "")["result"]["class"] == "fix"
    assert jobkind.outcome([], "boom")["verdict"] == "UNVERIFIED"


def test_the_observation_contract_can_fail(scene):
    obs = scene.measure("g0", "fix")
    bad = json.loads(json.dumps(obs))
    bad["fact"]["class"] = None                                           # a PASS must carry a class
    assert contracts.validate_observation(bad)
    bad = json.loads(json.dumps(obs))
    bad["fact"]["to"]["status"] = "failed"                                # a failed render cannot be PASS
    assert contracts.validate_observation(bad)


# ---------------------------------------------------------------------------------------------------------
# Review fixes: cache key, killed/host-environment runs, unmeasured consumers, read-only keep_work, ids
# ---------------------------------------------------------------------------------------------------------
def test_a_partial_render_is_never_served_as_the_full_split_move(scene):
    import shutil
    shutil.rmtree(scene.h.root / "regen-work" / "cache", ignore_errors=True)
    part = scene.render("fix", paths=["vendor/gen"], fresh=True)
    assert part.status == "ok" and part.tree_paths == ["vendor/gen"]
    f = fact(scene.measure("g0", "fix"))
    assert f["tree_paths"] == ["gen", "vendor/gen"]
    assert f["class"] == "fix" and not f["to"]["cached"], (f["class"], f["to"])


def test_a_render_cached_by_other_code_is_a_miss(scene, monkeypatch):
    a = scene.render("same", fresh=True)
    assert scene.render("same").cached
    monkeypatch.setattr(SB, "CODE_VERSION", "0" * 16)
    b = scene.render("same")
    assert not b.cached and b.digest == a.digest


@pytest.mark.parametrize("to,needle", [("slow", "killed"), ("nomod", "environment")])
def test_a_killed_or_host_environment_failure_at_to_is_infra_not_breaking(scene, to, needle):
    if to == "slow":
        scene.set_regen(dict(REGEN, budget_s=2))
    f = fact(scene.measure("g0", to, fresh=True))
    assert f["verdict"] == "INFRA" and f["class"] is None, (f["verdict"], f["class"], f["reasons"])
    assert f["to"]["status"] == "not-run" and needle in f["reason"], f["reason"]
    with scene.env.active():
        regen = SB.SandboxRegenerator(scene.config(), url_map=scene.url_map)
        from nanosoc_rm.versioning import classify as cf
        sig = cf.consumer_signal("fakegen", scene.c["g0"], scene.c[to], regen)
    assert sig["status"] == "unmeasured" and sig["class"] is None, sig


def test_stored_signals_count_every_consumer_not_only_the_measured_ones(scene, monkeypatch):
    g0, feat = scene.c["g0"], scene.c["feat"]
    monkeypatch.setattr(SB.SandboxRegenerator, "consumers", lambda self, comp: ["soc-b", "soc-main"])
    obs = scene.measure("g0", "feat", store=True)
    with scene.env.active():
        cons, build = M.stored_signals("fakegen", g0, feat, config=scene.config())
    assert cons["status"] == "unmeasured" and "soc-b not measured" in cons["detail"], cons
    assert build["status"] == "unmeasured" and build["build_affecting"] is True, build
    # soc-b measured, but FAIL: one PASS and one non-PASS is still unmeasured
    bad = json.loads(json.dumps(scene.measure("fail", "g0")["fact"]))
    bad.update(configuration="soc-b")
    bad["from"]["commit"], bad["to"]["commit"] = g0, feat
    with scene.env.active():
        db.put(M.observe(bad, [], "test"), config=scene.config())
        cons, build = M.stored_signals("fakegen", g0, feat, config=scene.config())
    assert cons["status"] == "unmeasured" and "soc-b FAIL" in cons["detail"], cons
    assert cons["class"] == "feature" and build["status"] == "unmeasured"
    monkeypatch.setattr(SB.SandboxRegenerator, "consumers", lambda self, comp: ["soc-main"])
    with scene.env.active():
        cons, build = M.stored_signals("fakegen", g0, feat, config=scene.config())
    assert cons["status"] == "unmeasured"                                # the stored soc-b FAIL still counts
    assert obs["fact"]["verdict"] == "PASS"


def test_keep_work_under_a_read_only_root_is_refused_before_any_mkdir(scene, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    scene.settings({"work_dir": str(scene.h.root / "regen-work"), "read_only_roots": [str(ro)]})
    with pytest.raises(S.RegenUsage, match="read-only root"):
        scene.render("g0", keep_work=ro / "kept" / "deeper")
    assert not (ro / "kept").exists()


def test_a_component_that_is_not_an_id_is_refused_and_writes_nothing(scene, tmp_path):
    from click.testing import CliRunner
    from nanosoc_rm.cli import main
    esc = "../../../../../../../../.." + str(tmp_path / "escaped")
    with scene.env.active():
        with pytest.raises(S.RegenUsage, match="not a component id"):
            SB.render("soc-main", esc, scene.c["g0"], config=scene.config(), paths=["gen", "vendor/gen"],
                      url_map=scene.url_map, fresh=True)
    assert not (tmp_path / "escaped").exists()
    r = CliRunner().invoke(main, ["regen", "run", "soc-main", "--component", "../x", "--commit", scene.c["g0"],
                                  "--at", "gen"], env=scene.env.env)
    assert r.exit_code == 3 and "not a component id" in r.output, r.output


def test_a_binary_file_the_move_removes_is_breaking(scene, monkeypatch):
    def fake(configuration, component, commit, **kw):
        r = SB.Render(configuration, component, commit, "c" * 40, ["gen"], "ok", "PASS",
                      outputs={"rtl/top.v": "module top; endmodule\n"},
                      binary=["rom.bin"] if commit == "a" * 40 else [])
        r.digest = SB._digest(r.outputs, r.binary)
        return r
    monkeypatch.setattr(SB, "render", fake)
    monkeypatch.setattr(SB, "request_for", lambda c, cfg: ({"commit": "c" * 40}, []))
    f = fact(M.measure("soc-main", "fakegen", "a" * 40, "b" * 40, config=None))
    assert f["class"] == "breaking" and any("binary" in x for x in f["reasons"]), f


def test_the_live_build_signal_is_unmeasured_when_a_render_fails(scene):
    from nanosoc_rm.versioning import classify as cf
    with scene.env.active():
        regen = SB.SandboxRegenerator(scene.config(), url_map=scene.url_map)
        b = cf.build_signal("fakegen", scene.c["g0"], scene.c["fail"], regen)
    assert b["status"] == "unmeasured" and b["build_affecting"] is None, b
