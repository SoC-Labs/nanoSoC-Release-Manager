"""`nanosoc-rm compat` (CR-11, MVP S2; interfaces.md section 9a): the walk with pin overrides, the configuration T0
gates run LOCALLY (run mode `local`), the roll-up with waivers, the compat_result document and the exit codes.

Hermetic: the acceptance file's LocalHosts (bare repos behind url.insteadOf) feed the walker's mirrors; the gates
reach the same bare repos through the test-only url_map seam, so they run exactly the git commands of a real run.
"""
from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path

import pytest
from analysis_fixtures import load_at
from click.testing import CliRunner

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.analysis import compat as C
from nanosoc_rm.t0 import gates as G
from nanosoc_rm.t0.local import run_local

GH = "github"
NO_SUCH = "0123456789abcdef0123456789abcdef01234567"     # a commit no repo holds


@pytest.fixture(scope="module")
def at_pins():
    return load_at("test_pins")


@pytest.fixture(autouse=True)
def _developer_env(monkeypatch):
    """Run as a developer would: no CI job, no private scan rules or toolkit."""
    for k in ("CI_JOB_ID", "CI_SERVER_URL", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT", "RM_PROBE_URLS"):
        monkeypatch.delenv(k, raising=False)


class Scene:
    """github SoC-Labs/top pins `a` -> alpha@a1 and `b` -> beta@b1; beta@b1 pins `deps/gamma` -> gamma@g1.
    alpha@a2 (branch `next`) pins `vendor/gamma` -> gamma@g2: overriding `a` with it adds a second gamma (B7).
    SoC-Labs/frozen pins `s` -> alpha@a1 by ssh URL: frozen, with t0_waivers covering exactly that failure."""

    def __init__(self, at, h):
        self.h = h
        self.g = at._chain(h, GH, "SoC-Labs/gamma", 2)
        self.a1 = h.commit(GH, "SoC-Labs/alpha", files={"rtl/a.v": "module a; endmodule\n"})
        h.push(GH, "SoC-Labs/alpha", self.a1, "main")
        self.a2 = h.commit(GH, "SoC-Labs/alpha", parents=[self.a1], files={"rtl/a.v": "module a2; endmodule\n"},
                           gitlinks={"vendor/gamma": self.g[1]},
                           gitmodules=[{"path": "vendor/gamma", "url": h.url(GH, "SoC-Labs/gamma")}])
        h.push(GH, "SoC-Labs/alpha", self.a2, "next")
        self.b1 = h.commit(GH, "SoC-Labs/beta", files={"rtl/b.v": "module b; endmodule\n"},
                           gitlinks={"deps/gamma": self.g[0]},
                           gitmodules=[{"path": "deps/gamma", "url": h.url(GH, "SoC-Labs/gamma")}])
        h.push(GH, "SoC-Labs/beta", self.b1, "main")
        self.top = h.commit(GH, "SoC-Labs/top", files={"README": "top\n"}, gitlinks={"a": self.a1, "b": self.b1},
                            gitmodules=[{"path": "a", "url": h.url(GH, "SoC-Labs/alpha")},
                                        {"path": "b", "url": h.url(GH, "SoC-Labs/beta")}])
        h.push(GH, "SoC-Labs/top", self.top, "main")
        self.frozen = h.commit(GH, "SoC-Labs/frozen", files={"README": "frozen\n"}, gitlinks={"s": self.a1},
                               gitmodules=[{"path": "s", "url": h.url(GH, "SoC-Labs/alpha", ssh=True)}])
        h.push(GH, "SoC-Labs/frozen", self.frozen, "main")
        h.tag(GH, "SoC-Labs/frozen", "v1", self.frozen)
        comps = ["top", "alpha", "beta", "gamma", "frozen"]
        catalogue = {"schema": 1, "components": [
            {"id": c, "class": "soc_ref" if c in ("top", "frozen") else "ip_tech",
             "canonical": {"host": GH, "path": "SoC-Labs/%s" % c}} for c in comps]}
        waiver = lambda gate, result: {"gate": gate, "tree_path": "s", "result": result, "approved_by": "member-a",
                                       "approved_on": "2026-10-11", "reason": "frozen tree pins alpha by ssh URL"}
        eligibility = {"schema": 1, "configurations": [
            {"id": "top-main", "component": "top", "ref": {"branch": "main"}},
            {"id": "frozen-v1", "component": "frozen", "ref": {"tag": "v1"}, "commit": self.frozen, "frozen": True,
             "t0_waivers": [waiver("t0.pins.fetchable", "fail"), waiver("t0.urls.no-ssh-public", "fail"),
                            waiver("t0.vendor.no-arm-ip", "not-reached")]},
            {"id": "frozen-nowaiver", "component": "frozen", "ref": {"tag": "v1"}, "commit": self.frozen,
             "frozen": True}]}
        self.registry = at.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
        self.env = h.tool_env(self.registry, "compat")
        with self.env.active():
            for c in comps:
                at.rm("mirror.sync_component")(c)
        # the gates' routes: every host repo by its https URL (the seam the runner refuses under CI_JOB_ID)
        self.url_map = {"https://github.com/SoC-Labs/%s.git" % c: str(h.bare(GH, "SoC-Labs/%s" % c)) for c in comps}

    def run(self, configuration, overrides=None, **kw):
        with self.env.active():
            return C.compat(configuration, overrides, url_map=self.url_map, **kw)


@pytest.fixture(scope="module")
def scene(at_pins, tmp_path_factory):
    h = at_pins.LocalHosts(tmp_path_factory.mktemp("compat"))
    yield Scene(at_pins, h)
    h.api.close()


def gates_of(doc):
    return {g["id"]: g for g in doc["fact"]["gates"]}


def pins_of(doc):
    return {p["tree_path"]: p for p in doc["fact"]["pins"]}


# ---------------------------------------------------------------------------------------------------------
def test_clean_tree_passes_with_runner_hardened_not_run(scene):
    doc = scene.run("top-main")
    assert contracts.validate_observation(doc) == [], contracts.validate_observation(doc)
    f, g = doc["fact"], gates_of(doc)
    assert f["verdict"] == "PASS" and C.exit_for(f["verdict"]) == 0, g
    # README section 6: NOT-RUN never blocks PASS, and the run still needs a PASS gate (it has five)
    assert g["t0.runner.hardened"] == {"id": "t0.runner.hardened", "verdict": "NOT-RUN", "control": "not-applicable",
                                       "detail": "local run"}
    assert sum(1 for x in g.values() if x["verdict"] == "PASS") >= 1
    assert {k for k, x in g.items() if x["verdict"] == "PASS"} == {
        "t0.infra.probe", "t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public", "t0.vendor.no-arm-ip"}
    assert all(x["control"] == "fired" for x in g.values() if x["verdict"] == "PASS")
    assert f["not_run"] == 4 and f["mode"] == "local"          # CR-6: + no-foundry-data, no foundry rule here and f["overrides"] == [] and f["splits"] == []
    p = pins_of(doc)
    assert sorted(p) == ["a", "b", "b/deps/gamma"]
    assert p["b/deps/gamma"]["items"] == {"fetchable": "pass", "reachable": "pass", "no_ssh": "pass", "vendor": "pass"}
    assert p["a"]["state"] == "ON_DEFAULT" and not p["a"]["overridden"] and p["a"]["replaces"] is None
    # a what-if is not stored: nothing reached the store
    with scene.env.active():
        assert db.latest("compat_result") == []


def test_override_to_a_nonexistent_commit_fails_reachable_and_fetchable_at_that_path(scene):
    doc = scene.run("top-main", {"b": NO_SUCH})
    assert contracts.validate_observation(doc) == []
    f, g, p = doc["fact"], gates_of(doc), pins_of(doc)
    assert f["verdict"] == "FAIL" and C.exit_for(f["verdict"]) == 1
    assert f["overrides"] == [{"tree_path": "b", "commit": NO_SUCH, "replaces": scene.b1}]
    assert p["b"]["overridden"] and p["b"]["commit"] == NO_SUCH and p["b"]["replaces"] == scene.b1
    assert p["b"]["items"]["reachable"] == "fail" and p["b"]["items"]["fetchable"] == "fail"
    assert g["t0.pins.reachable"]["verdict"] == "FAIL" and ("b %s" % NO_SUCH[:7]) in g["t0.pins.reachable"]["detail"]
    assert g["t0.pins.fetchable"]["verdict"] == "FAIL" and "b" in g["t0.pins.fetchable"]["detail"]
    assert p["b"]["state"] == "MISSING"                      # the walker judged the override on its host
    assert "b/deps/gamma" not in p                            # the subtree is the override's own (none)
    assert p["a"]["items"]["reachable"] == "pass"            # the rest of the tree is unchanged
    assert g["t0.runner.hardened"]["verdict"] == "NOT-RUN" and f["unjudged_overrides"] == []


def test_override_adding_a_second_copy_of_a_component_is_flagged_b7(scene):
    doc = scene.run("top-main", {"a": scene.a2})
    assert contracts.validate_observation(doc) == []
    f, p = doc["fact"], pins_of(doc)
    assert sorted(p) == ["a", "a/vendor/gamma", "b", "b/deps/gamma"]
    assert p["a/vendor/gamma"]["split"] and p["b/deps/gamma"]["split"] and not p["a"]["split"]
    assert f["splits"] == [{"component": "gamma", "pins": [{"tree_path": "a/vendor/gamma", "commit": scene.g[1]},
                                                          {"tree_path": "b/deps/gamma", "commit": scene.g[0]}]}]
    # the gates judged the overridden subtree too, and the override is valid: the run passes
    assert p["a/vendor/gamma"]["items"]["reachable"] == "pass" and f["verdict"] == "PASS"
    out = _cli(scene, "compat", "top-main", "--pin", "a=%s" % scene.a2)
    assert out.exit_code == 0, out.output
    assert "B7 split: gamma pinned at 2 commits: a/vendor/gamma@%s, b/deps/gamma@%s" % (scene.g[1][:7], scene.g[0][:7]) \
        in out.output
    assert "override a: %s -> %s" % (scene.a1[:12], scene.a2[:12]) in out.output


def test_a_waiver_covered_failure_rolls_up_waived(scene):
    raw = scene.run("frozen-nowaiver")
    assert raw["fact"]["verdict"] == "FAIL"
    doc = scene.run("frozen-v1")
    assert contracts.validate_observation(doc) == []
    f, g = doc["fact"], gates_of(doc)
    assert f["verdict"] == "WAIVED" and C.exit_for("WAIVED") == 0 and f["unused_waivers"] == 0
    for gid, rv in (("t0.pins.fetchable", "FAIL"), ("t0.urls.no-ssh-public", "FAIL"), ("t0.vendor.no-arm-ip", "UNVERIFIED")):
        assert (g[gid]["verdict"], g[gid]["raw_verdict"]) == ("WAIVED", rv), g[gid]
        assert [w["tree_path"] for w in g[gid]["waived"]] == ["s"]
    assert g["t0.pins.reachable"]["verdict"] == "PASS"       # ssh-pinned, but reachable on its canonical https URL
    assert pins_of(doc)["s"]["url_kind"] == "ssh"


def test_a_moved_pin_that_was_never_fetched_is_unverified_not_a_judged_fail(scene):
    """Evaluation 2026-10-16 (report action 3): `s` is pinned by an ssh URL, so the anonymous gates walk the move
    but never fetch or scan the moved commit. The what-if must read UNVERIFIED (it did not look), not FAIL; the
    gates' own FAIL stays visible in the gates table."""
    doc = scene.run("frozen-nowaiver", {"s": scene.a2})
    assert contracts.validate_observation(doc) == [], contracts.validate_observation(doc)
    f, g, p = doc["fact"], gates_of(doc), pins_of(doc)
    assert p["s"]["overridden"] and p["s"]["items"]["fetchable"] == "fail"
    assert g["t0.pins.fetchable"]["verdict"] == "FAIL"                       # hygiene: still reported
    assert f["verdict"] == "UNVERIFIED" and C.exit_for(f["verdict"]) == 2, (f["verdict"], f["reason"])
    assert f["unjudged_overrides"] == ["s"] and "s not fetched (fetchable fail)" in f["reason"]
    # the control: the same move at a path the gates fetch is judged (PASS), and a move to a commit the host
    # does not hold stays a judged FAIL (reachable proves it absent)
    ok = scene.run("top-main", {"a": scene.a2})["fact"]
    assert ok["verdict"] == "PASS" and ok["unjudged_overrides"] == []
    gone = scene.run("top-main", {"b": NO_SUCH})["fact"]
    assert gone["verdict"] == "FAIL" and gone["unjudged_overrides"] == []


def test_unjudged_reads_each_gate_item():
    class P:
        override_of = "x" * 40
    judged = {"a": P(), "b": P(), "c": P(), "d": P()}
    items = {"t0.pins.fetchable": [{"tree_path": "a", "result": "pass"}, {"tree_path": "b", "result": "fail"},
                                   {"tree_path": "c", "result": "pass"}, {"tree_path": "d", "result": "fail"}],
             "t0.pins.reachable": [{"tree_path": "d", "result": "fail"}],
             "t0.vendor.no-arm-ip": [{"tree_path": "a", "result": "pass"}, {"tree_path": "c", "result": "not-reached"}]}
    out, why = C._unjudged({"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}, judged, items)
    assert out == ["b", "c", "e"]
    assert why == ["b not fetched (fetchable fail)", "c fetched but not scanned",
                   "e not reached by the gates' anonymous walk"]


def test_override_at_a_path_with_no_pin_is_a_usage_error(scene):
    from nanosoc_rm.walker import OverrideError
    with pytest.raises(OverrideError):
        scene.run("top-main", {"nope": scene.a2})
    with pytest.raises(OverrideError):                       # exists only below an override that removes it
        scene.run("top-main", {"b": NO_SUCH, "b/deps/gamma": scene.g[1]})
    for args in (("compat", "top-main", "--pin", "nope=%s" % scene.a2),
                 ("compat", "top-main", "--pin", "a=abc123"),
                 ("compat", "top-main", "--pin", "a"),
                 ("compat", "top-main", "--pin", "a=%s" % scene.a2, "--store"),
                 ("compat", "no-such-config")):
        r = _cli(scene, *args)
        assert r.exit_code == 3, (args, r.output)


def test_nested_override_below_an_override_is_walked_from_the_new_commit(scene):
    doc = scene.run("top-main", {"a": scene.a2, "a/vendor/gamma": scene.g[0]})
    p = pins_of(doc)
    assert p["a/vendor/gamma"]["overridden"] and p["a/vendor/gamma"]["commit"] == scene.g[0]
    assert p["a/vendor/gamma"]["replaces"] == scene.g[1]
    assert not p["a/vendor/gamma"]["split"] and doc["fact"]["splits"] == []   # both gammas at g1 now
    assert doc["fact"]["verdict"] == "PASS"


def test_store_records_only_a_real_run(scene):
    doc = scene.run("top-main", store=True)
    with scene.env.active():
        stored = db.latest("compat_result")
    assert [d["subject"] for d in stored] == [doc["subject"]]
    with pytest.raises(C.CompatUsage):
        scene.run("top-main", {"a": scene.a2}, store=True)


def test_json_output_is_one_compat_result(scene):
    r = _cli(scene, "compat", "top-main", "--json")
    assert r.exit_code == 0, r.output
    (doc,) = [json.loads(x) for x in r.stdout.splitlines()]
    assert doc["kind"] == "compat_result" and contracts.validate_observation(doc) == []


def test_keep_work_keeps_the_isolated_home(scene, tmp_path):
    keep = tmp_path / "work"
    doc = scene.run("top-main", keep_work=keep)
    assert doc["fact"]["work_dir"] == str(keep)
    assert (keep / "run" / "home").is_dir() and list((keep / "run" / "home").iterdir()) == []
    with pytest.raises(C.CompatUsage):
        scene.run("top-main", keep_work=keep)                # not empty any more


def _cli(scene, *args):
    """The CLI in-process, with compat() given the test's url_map (the seam the CLI never exposes)."""
    real = C.compat
    C.compat = lambda *a, **kw: real(*a, url_map=scene.url_map, **kw)
    try:
        with scene.env.active():
            return CliRunner().invoke(cli.main, list(args))
    finally:
        C.compat = real


# ---------------------------------------------------------------------------------------------------------
# Local mode itself
# ---------------------------------------------------------------------------------------------------------
def test_local_rollup_never_reports_runner_hardened_pass():
    """A local run's t0.runner.hardened row must be the one SKIP 'local run' row; anything else is UNVERIFIED."""
    skip = [{"utc": "2026-10-11T10:00:00Z", "status": "SKIP", "gate": "t0.runner.hardened", "detail": "local run",
             "nonce": "ab" * 16}]
    passed = [dict(skip[0], status="PASS", detail="ok"),
              dict(skip[0], gate="t0.runner.hardened.negctl", status="PASS", detail="fired")]
    get = lambda rows, mode: next(g for g in G.rollup(rows, "configuration", mode=mode) if g["id"] == "t0.runner.hardened")
    assert get(skip, "local") == {"id": "t0.runner.hardened", "verdict": "NOT-RUN", "control": "not-applicable",
                                  "detail": "local run"}
    assert get(passed, "local")["verdict"] == "UNVERIFIED"
    assert get(skip, "ci")["verdict"] == "UNVERIFIED"         # a CI bundle may never skip it (README section 6)
    assert get(passed, "ci")["verdict"] == "PASS"


def test_run_local_never_runs_the_hardened_check(tmp_path, monkeypatch):
    called = []
    gate = G.GATES["t0.runner.hardened"]
    monkeypatch.setitem(G.GATES, "t0.runner.hardened",
                        G.Gate(gate.id, gate.scope, gate.reqs, gate.may_skip,
                               lambda ctx: called.append(1) or ("PASS", "x"), gate.control))
    ctx = G.Context.for_local(nonce="cd" * 16, workdir=tmp_path / "w", request=None, host_bases={})
    rows, _ = run_local(ctx)
    assert not called
    assert [r for r in rows if r["gate"].startswith("t0.runner.hardened")] == [
        {"utc": rows[1]["utc"], "status": "SKIP", "gate": "t0.runner.hardened", "detail": "local run",
         "nonce": "cd" * 16}]
    with pytest.raises(ValueError):
        run_local(G.Context.for_tests(nonce="cd" * 16, workdir=tmp_path / "ci"))      # a ci-mode Context


class _AuthServer:
    """An http git 'host' that answers every request 401 and records the Authorization header of each."""

    def __init__(self):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="rm"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_port

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_local_mode_never_reads_netrc(tmp_path, monkeypatch):
    """The user's ~/.netrc holds a password for the host. git in local mode never sends it; the control (the
    same call with the user's HOME) does, so the test can fail."""
    from nanosoc_rm.gitrun import run_git
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".netrc").write_text("machine 127.0.0.1 login someone password not-for-rm\n")
    os.chmod(home / ".netrc", 0o600)
    monkeypatch.setenv("HOME", str(home))
    srv = _AuthServer()
    try:
        url = "%s/SoC-Labs/private.git" % srv.base
        assert not run_git("ls-remote", url, isolate=True, timeout=60).ok
        assert any(a and a.startswith("Basic ") for a in srv.seen), "control: git with the user's HOME used .netrc"
        srv.seen.clear()
        request = {"configuration": "top-main", "repo": {"host": "github", "path": "SoC-Labs/private"},
                   "commit": NO_SUCH, "upgrade_from": None, "canonical_urls": {}}
        ctx = G.Context.for_local(nonce="ef" * 16, workdir=tmp_path / "w", request=request,
                                  host_bases={"github": srv.base})
        row, _ = G.run_with_control("t0.infra.probe", ctx)     # the gate's own git call, local mode
        assert row["status"] == "FAIL", row
        assert ctx.git.run("ls-remote", url, timeout=60).ok is False
        assert srv.seen and all(a is None for a in srv.seen), srv.seen
        assert ctx.git.env["HOME"] == str(tmp_path / "w" / "home") and not any((tmp_path / "w" / "home").iterdir())
        assert Path(ctx.git.env["TMPDIR"]).parent == tmp_path / "w"
    finally:
        srv.close()
