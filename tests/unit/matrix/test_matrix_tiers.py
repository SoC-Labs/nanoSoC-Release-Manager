"""Tier cells of the matrix (D3 with T1, D4 moved pins and ip_hash; CR-WD-matrix-t1, interfaces.md section 18
item 8): a matrix column set may add tier t1. The cell runs T0 and the registry's tiers.yaml t1 command in a
sandbox checkout of the same tree; the checkout is re-read after the command (a moved pin is UNVERIFIED); the IP
hash is the hash of the file list the tier declares; no command is NOT-RUN and a tool this host lacks is INFRA
"tool not on this host" with nothing run.

Real runs on the hermetic scene (conftest.py); the tier commands are small shell strings."""
from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from click.testing import CliRunner

from nanosoc_rm import contracts, db, jobs
from nanosoc_rm import matrix as M
from nanosoc_rm.analysis import tierrun as T
from nanosoc_rm.cli import main
from nanosoc_rm.config import load_config
from nanosoc_rm.matrix import cells as K
from nanosoc_rm.matrix import jobkind as J
from nanosoc_rm.matrix import provenance as P
from nanosoc_rm.matrix import tiers as TI

pytestmark = [pytest.mark.requirement("D3"), pytest.mark.requirement("D4"), pytest.mark.requirement("D1")]


def set_tiers(scene, t1: dict | None) -> None:
    """tiers.yaml with this t1 spec for top-main; None: only a t2 command (no t1)."""
    confs = {"top-main": {"t1": dict({"cwd": ".", "budget_s": 60}, **t1)}} if t1 else \
        {"top-main": {"t2": {"command": "true", "cwd": ".", "budget_s": 60}}}
    (scene.registry / "tiers.yaml").write_text(yaml.safe_dump({"schema": 1, "configurations": confs},
                                                               sort_keys=False))


@pytest.fixture
def tiered(clean):
    yield clean
    p = clean.registry / "tiers.yaml"
    if p.exists():
        p.unlink()


def valid(docs):
    problems = contracts.validate_observation(docs[0])
    assert problems == [], problems
    return docs[0]["fact"]


def gates(attempt):
    return {g["id"]: g["verdict"] for g in attempt["gates"]}


class NeverCompat:
    def __call__(self, *a, **kw):
        raise AssertionError("compat must not run when the tier cannot run here")


# ---------------------------------------------------------------------------------------------------------
# A t1 cell that runs
# ---------------------------------------------------------------------------------------------------------
def test_t1_cell_runs_the_tier_reads_the_checkout_and_hashes_the_declared_files(tiered):
    s = tiered
    set_tiers(s, {"command": "printf 'a/rtl/a.v\\nREADME\\n' > files.lst; echo linted", "ip_files": "files.lst"})
    f = valid(s.run_cell("pinned", store=True, by="alice", tier="t1"))
    a = f["attempts"][0]
    assert f["verdict"] == "PASS" and f["tier"] == "t1" and f["force_tier"] is False and len(f["attempts"]) == 1
    assert a["tier"]["verdict"] == "PASS" and a["tier"]["exit_code"] == 0 and "linted" in a["tier"]["log_tail"]
    assert gates(a)["tier.t1"] == "PASS" and gates(a)["t0.pins.fetchable"] == "PASS"
    pv = f["provenance"]
    assert "T0 + t1" in pv["harness"]
    co = {p["tree_path"]: p["commit"] for p in pv["checkout"]["pins"]}
    assert co == {"a": s.a[0], "b": s.b1} and pv["checkout"]["superproject_commit"] == s.top and pv["moved"] == []
    # the hash, recomputed from the committed contents the tier read
    lines = ["%s  %s\n" % (hashlib.sha256(b"top\n").hexdigest(), "README"),
             "%s  %s\n" % (hashlib.sha256(b"module a0; endmodule\n").hexdigest(), "a/rtl/a.v")]
    want = hashlib.sha256("".join(sorted(lines, key=lambda x: x.split("  ", 1)[1])).encode()).hexdigest()
    assert pv["ip_hash"] == want and "2 files" in pv["ip_hash_note"]
    assert s.run_cell("pinned", tier="t1")[0]["subject"].startswith("matrix_cell:top-main:-:pinned:")

    # a T0 pinned run as well: the in-range t1 cell's D9 baseline is the pinned cell of ITS tier
    t0 = valid(s.run_cell("pinned", store=True, by="alice"))
    cell = valid(s.run_cell("in-range", "alpha", store=True, by="alice", tier="t1"))
    assert cell["verdict"] == "PASS" and cell["overrides"] == [{"tree_path": "a", "commit": s.a[1]}]
    assert {p["tree_path"]: p["commit"] for p in cell["provenance"]["checkout"]["pins"]} == {"a": s.a[1], "b": s.b1}
    assert cell["baseline"]["nonce"] == f["nonce"] != t0["nonce"]
    assert "tier.t1" in {r["gate"] for r in cell["comparison"]["rows"]}
    with s.env.active():
        cfg = load_config()
        assert K.latest_cells("top-main", cfg)[(None, "pinned")]["fact"]["nonce"] == t0["nonce"]      # T0 only
        assert K.latest_cells("top-main", cfg, tier="t1")[(None, "pinned")]["fact"]["nonce"] == f["nonce"]
        assert M.bump_cell("top-main", {"a": s.a[1]}, config=cfg) is None    # a tier cell is not the move's T0 cell


def test_ip_hash_is_null_with_the_reason_unless_the_declared_list_is_whole(tiered):
    s = tiered
    set_tiers(s, {"command": "true"})
    pv = valid(s.run_cell("pinned", tier="t1"))["provenance"]
    assert pv["ip_hash"] is None and "declares no ip_files" in pv["ip_hash_note"]
    set_tiers(s, {"command": "true", "ip_files": "files.lst"})
    pv = valid(s.run_cell("pinned", tier="t1"))["provenance"]
    assert pv["ip_hash"] is None and "was not written by this run" in pv["ip_hash_note"]
    set_tiers(s, {"command": "printf 'README\\nno/such.v\\n' > files.lst", "ip_files": "files.lst"})
    pv = valid(s.run_cell("pinned", tier="t1"))["provenance"]
    assert pv["ip_hash"] is None and "no/such.v" in pv["ip_hash_note"]
    set_tiers(s, {"command": ": > files.lst", "ip_files": "files.lst"})
    pv = valid(s.run_cell("pinned", tier="t1"))["provenance"]
    assert pv["ip_hash"] is None and "lists no file" in pv["ip_hash_note"]
    t0 = valid(s.run_cell("pinned"))["provenance"]                      # a T0 cell: unchanged
    assert t0["ip_hash"] is None and "T0 reads no IP file list" in t0["ip_hash_note"] and "checkout" not in t0


def test_the_tier_runner_removes_a_stale_ip_file_list_before_the_command(tmp_path):
    tree = tmp_path / "tree"
    (tree / "out").mkdir(parents=True)
    (tree / "out" / "files.lst").write_text("stale\n")
    d = T.run_tier({"command": "test ! -e out/files.lst", "cwd": ".", "budget_s": 30, "ip_files": "out/files.lst"},
                   "t1", tree, tmp_path, "0123456789abcdef0123456789abcdef")
    assert d["verdict"] == "PASS", d


# ---------------------------------------------------------------------------------------------------------
# D4: a tier that moves a submodule
# ---------------------------------------------------------------------------------------------------------
def test_a_tier_that_moves_a_submodule_checkout_is_unverified(tiered):
    s = tiered
    set_tiers(s, {"command": "git -C a -c user.name=t -c user.email=t@t commit -q --allow-empty -m moved"})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED" and len(f["attempts"]) == 1, f["reason"]
    mv = f["provenance"]["moved"]
    assert [m["tree_path"] for m in mv] == ["a"] and mv[0]["requested"] == s.a[0] != mv[0]["captured"]
    assert "the t1 checkout" in f["reason"] and f["attempts"][0]["tier"]["verdict"] == "PASS"
    assert f["blocks_auto_merge"] is True


def test_a_tier_that_repoints_the_parent_gitlink_is_unverified(tiered):
    """The checkout's HEAD stays, but the parent's index now records another commit for `a`: moved."""
    s = tiered
    set_tiers(s, {"command": "git update-index --cacheinfo 160000,%s,a" % s.b1})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED", f["reason"]
    assert f["provenance"]["moved"] == [{"tree_path": "a", "requested": s.a[0], "captured": s.b1}]


def test_a_tier_that_moves_the_superproject_is_unverified(tiered):
    s = tiered
    set_tiers(s, {"command": "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m moved"})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED" and f["provenance"]["moved"][0]["tree_path"] == "", f["reason"]


def test_capture_checkout_reads_head_and_index(tmp_path, tiered):
    """The capture alone, on a laid-out tree kept from a real compat --tier run."""
    from nanosoc_rm.analysis.compat import compat
    s = tiered
    set_tiers(s, {"command": "true", "pins": ["b"]})
    with s.env.active():
        compat("top-main", keep_work=tmp_path / "w", url_map=s.url_map, tier="t1")
    req = [{"tree_path": "a", "commit": s.a[0]}, {"tree_path": "b", "commit": s.b1}]
    cap = P.capture_checkout(tmp_path / "w" / "tree", s.top, req)
    assert cap["superproject_commit"] == s.top
    assert {p["tree_path"]: p["commit"] for p in cap["pins"]} == {"a": None, "b": s.b1}     # a: not laid out
    assert P.moved(req, cap) == []


# ---------------------------------------------------------------------------------------------------------
# Preflight: no command, no tool
# ---------------------------------------------------------------------------------------------------------
def test_no_t1_command_is_not_run_and_runs_nothing(tiered):
    s = tiered
    set_tiers(s, None)
    f = valid(s.run_cell("pinned", tier="t1", compat_fn=NeverCompat()))
    assert f["verdict"] == "NOT-RUN" and f["attempts"] == [] and "declares no t1 command" in f["reason"]


@pytest.mark.parametrize("spec, says", [
    ({"command": "make lint", "env": {"VERILATOR": "/nonexistent/bin/verilator"}},
     "VERILATOR=/nonexistent/bin/verilator does not exist"),
    ({"command": "no-such-lint-tool-xyz --lint"}, "no-such-lint-tool-xyz is not on the tier's PATH"),
    ({"command": ["/nonexistent/lint", "--all"]}, "/nonexistent/lint does not exist"),
])
def test_a_tool_not_on_this_host_is_infra_and_runs_nothing(tiered, spec, says):
    s = tiered
    set_tiers(s, spec)
    f = valid(s.run_cell("pinned", tier="t1", compat_fn=NeverCompat(), store=True))
    assert f["verdict"] == "INFRA" and len(f["attempts"]) == 1 and f["attempts"][0]["gates"] == []
    assert "tool not on this host" in f["reason"] and says in f["reason"]
    assert K.exit_for(f["verdict"]) == 2


def test_preflight_ready_and_program_rules(tiered):
    s = tiered
    with s.env.active():
        cfg = load_config()
        set_tiers(s, {"command": "make lint", "env": {"VERILATOR": "/bin/sh"}})
        assert TI.preflight("top-main", "t1", config=cfg).ready
        set_tiers(s, {"command": "VAR=1 ./scripts/lint.sh"})            # not a plain program word: not judged
        assert TI.preflight("top-main", "t1", config=cfg).ready
        set_tiers(s, {"command": "true", "regenerates": True})
        r = TI.preflight("top-main", "t1", config=cfg, environ={})
        assert r.verdict == "NOT-RUN" and "NANOSOC_GEN_OK" in r.reason
    with pytest.raises(ValueError):
        TI.parse_tiers("t1,t2")
    assert TI.parse_tiers("t1") == ["t1"] and TI.parse_tiers(None) == []


def test_force_tier_runs_past_a_t0_fail_and_needs_a_tier(tiered):
    s = tiered
    with pytest.raises(K.CellUsage):
        s.run_cell("pinned", force_tier=True)
    with pytest.raises(K.CellUsage):
        s.run_cell("pinned", tier="t2")


# ---------------------------------------------------------------------------------------------------------
# The view, the queue, the job kind, the CLI and the pages
# ---------------------------------------------------------------------------------------------------------
def test_view_with_t1_shows_tier_cells_and_queues_only_runnable_ones(tiered):
    s = tiered
    set_tiers(s, {"command": "make lint", "env": {"VERILATOR": "/nonexistent/verilator"}})
    with s.env.active():
        cfg = load_config()
        doc = M.matrix("top-main", config=cfg, tiers=["t1"])
        assert contracts.validate_observation(doc) == []
        v = doc["fact"]
        assert v["tiers"][0]["ready"] is False and v["tiers"][0]["verdict"] == "INFRA"
        assert v["pinned_tiers"]["t1"]["status"] == "no-tool" and v["pinned_tiers"]["t1"]["verdict"] == "INFRA"
        alpha = next(r for r in v["rows"] if r["component"] == "alpha")
        assert alpha["tier_cells"]["t1"]["in-range"]["status"] == "no-tool"
        assert "tool not on this host" in alpha["tier_cells"]["t1"]["in-range"]["reason"]
        assert all(len(c) == 2 for c in M.cell_requests(doc))                   # no tier cell is queued
        assert "tiers" not in M.matrix("top-main", config=cfg)["fact"]          # T0 view unchanged

        set_tiers(s, {"command": "true"})
        queued = M.queue("top-main", by="alice", config=cfg, tiers=["t1"], components=["alpha"])
        got = [(j["params"]["component"], j["params"]["column"], j["params"].get("tier")) for j in queued]
        assert got[:4] == [(None, "pinned", None), ("alpha", "in-range", None), ("alpha", "latest-tag", None),
                           ("alpha", "head", None)]
        assert got[4:] == [(None, "pinned", "t1"), ("alpha", "in-range", "t1"), ("alpha", "latest-tag", "t1"),
                           ("alpha", "head", "t1")]
        assert queued[4]["command"] == "nanosoc-rm matrix cell top-main --column pinned --tier t1 --store"
        v = M.matrix("top-main", config=cfg, tiers=["t1"])["fact"]
        assert v["pinned_tiers"]["t1"]["status"] == "queued" and v["pinned"]["status"] == "queued"
        assert M.queue("top-main", by="alice", config=cfg, tiers=["t1"], components=["alpha"]) == []


def test_job_kind_carries_the_tier(tiered):
    p = J.params({"configuration": "top-main", "column": "pinned", "tier": "t1", "force_tier": True})
    assert p == {"configuration": "top-main", "component": None, "column": "pinned", "tier": "t1",
                 "force_tier": True}
    assert J.cli_args(p)[-5:] == ["--tier", "t1", "--force-tier", "--store", "--json"]
    for bad in ({"tier": "t2"}, {"force_tier": True}, {"tier": "t1", "force_tier": "yes"}):
        with pytest.raises(ValueError):
            J.params(dict({"configuration": "top-main", "column": "pinned"}, **bad))
    assert J.params({"configuration": "top-main", "column": "pinned"}) == {
        "configuration": "top-main", "component": None, "column": "pinned"}             # T0 job unchanged
    set_tiers(tiered, {"command": "true", "budget_s": 100})
    with tiered.env.active():
        cfg = load_config()
        assert J.limit_s(cfg, {"params": p}) == J.LIMIT_S + 2 * 150
        assert J.limit_s(cfg, {"params": {"configuration": "top-main", "column": "pinned"}}) == J.LIMIT_S


def test_cli_view_and_cell_with_t1(tiered):
    s = tiered
    set_tiers(s, {"command": "echo linted"})
    with s.env.active():
        r = CliRunner().invoke(main, ["matrix", "top-main", "--tiers", "t1", "--component", "alpha"],
                               catch_exceptions=False)
        assert "tier t1: ready on this host" in r.output and "+ t1" in r.output, r.output
        assert CliRunner().invoke(main, ["matrix", "top-main", "--tiers", "t9"]).exit_code == 3
        assert CliRunner().invoke(main, ["matrix", "cell", "top-main", "--column", "pinned",
                                         "--force-tier"]).exit_code == 3
        set_tiers(s, {"command": "make lint", "env": {"VERILATOR": "/nonexistent/verilator"}})
        r = CliRunner().invoke(main, ["matrix", "cell", "top-main", "--column", "pinned", "--tier", "t1",
                                      "--json"], catch_exceptions=False)
        doc = json.loads(r.output.splitlines()[0])
        assert r.exit_code == 2 and doc["fact"]["verdict"] == "INFRA" and doc["fact"]["tier"] == "t1"


def test_pages_show_tier_rows_and_the_cell(tiered, tmp_path):
    from test_matrix_surfaces import web_client
    s = tiered
    set_tiers(s, {"command": "make lint", "env": {"VERILATOR": "/nonexistent/verilator"}})
    c, cfg = web_client(s, tmp_path)
    with s.env.active():
        r = c.get("/matrix/top-main?tiers=t1")
        assert r.status_code == 200, r.text
        assert 'data-tier-row="t1"' in r.text and 'data-cell="alpha/in-range@t1"' in r.text
        assert 'data-preflight="no-tool"' in r.text and 'data-tier-preflight="t1" data-ready="no"' in r.text
        assert 'data-cell="alpha/in-range"' in r.text                          # the T0 cell is still there
        assert 'data-tier-row' not in c.get("/matrix/top-main").text
        assert c.get("/matrix/top-main?tiers=t9").status_code == 400
        r = c.get("/matrix/top-main/cell?column=pinned&tier=t1")
        assert r.status_code == 200 and 'data-tier="t1"' in r.text and "tool not on this host" in r.text
        assert c.get("/matrix/top-main/cell?column=pinned&tier=t9").status_code == 400
        set_tiers(s, {"command": "true"})
        r = c.post("/matrix/top-main/run", data={"scope": "cell", "column": "pinned", "tier": "t1"},
                   follow_redirects=False)
        assert r.status_code == 303 and "queued=1" in r.headers["location"], r.text
        got = [(j["params"]["column"], j["params"].get("tier")) for j in jobs.jobs(load_config(cfg), kind="matrix-cell")]
        assert got == [("pinned", "t1")]
        r = c.post("/matrix/top-main/run", data={"scope": "row", "component": "alpha", "filter_tiers": "t1"},
                   follow_redirects=False)
        assert r.status_code == 303 and "queued=7" in r.headers["location"] and "tiers=t1" in r.headers["location"]


def test_stored_t1_cell_validates_and_shows_on_its_page(tiered, tmp_path):
    from test_matrix_surfaces import web_client
    s = tiered
    set_tiers(s, {"command": "git -C a -c user.name=t -c user.email=t@t commit -q --allow-empty -m moved"})
    valid(s.run_cell("pinned", tier="t1", store=True, by="alice"))
    c, _ = web_client(s, tmp_path)
    with s.env.active():
        r = c.get("/matrix/top-main/cell?column=pinned&tier=t1")
        assert r.status_code == 200 and "data-checkout" in r.text and 'data-attempt-tier="t1"' in r.text
        assert "UNVERIFIED" in r.text
        stored = db.latest("matrix_cell", "matrix_cell:top-main:-:pinned:")
        assert [d["fact"].get("tier") for d in stored] == ["t1"]


# ---------------------------------------------------------------------------------------------------------
# D4: a laid-out input that the command removed or changed is not a green capture (review R1, R4)
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("command, where", [
    ("rm -rf a/.git && echo 'module evil; endmodule' > a/rtl/a.v", "a"),
    ("rm -rf .git", "(superproject)"),
])
def test_a_laid_out_repo_the_tier_removed_is_unverified(tiered, command, where):
    s = tiered
    set_tiers(s, {"command": command})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED", f["reason"]
    assert "could not be re-read after the t1 command" in f["reason"] and where in f["reason"]
    assert where in [x or "(superproject)" for x in f["provenance"]["checkout"]["lost"]]


def test_a_tier_that_rewrites_a_tracked_file_in_a_pin_is_unverified(tiered):
    s = tiered
    set_tiers(s, {"command": "echo 'module a1_tampered; endmodule' > a/rtl/a.v; echo built > a/build.log"})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED", f["reason"]
    assert f["provenance"]["checkout"]["dirty"] == [{"tree_path": "a", "files": ["rtl/a.v"]}]
    assert "a/rtl/a.v" in f["reason"]


def test_a_rewritten_declared_output_file_is_not_dirty(tiered):
    """run_tier deletes a declared ip_files/verdicts file before the command, so its rewrite is not tampering."""
    s = tiered
    set_tiers(s, {"command": "printf 'README\\n' > README",
                  "ip_files": "README"})
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["provenance"]["checkout"]["dirty"] == [] and f["verdict"] == "PASS", f["reason"]


# ---------------------------------------------------------------------------------------------------------
# ip_hash: an identity of the inputs, bounded (review R2, R3)
# ---------------------------------------------------------------------------------------------------------
def test_ip_hash_is_the_same_for_absolute_and_relative_listings_across_runs(tiered):
    s = tiered
    set_tiers(s, {"command": "printf '%s/README\\n' \"$TREE\" > files.lst", "ip_files": "files.lst"})
    h1 = valid(s.run_cell("pinned", tier="t1"))["provenance"]["ip_hash"]
    h2 = valid(s.run_cell("pinned", tier="t1"))["provenance"]["ip_hash"]
    set_tiers(s, {"command": "printf 'README\\n./README\\n' > files.lst", "ip_files": "files.lst"})
    h3 = valid(s.run_cell("pinned", tier="t1"))["provenance"]["ip_hash"]
    want = hashlib.sha256(("%s  README\n" % hashlib.sha256(b"top\n").hexdigest()).encode()).hexdigest()
    assert h1 == h2 == h3 == want


def test_ip_hash_refuses_a_listed_path_that_is_not_a_regular_file(tmp_path):
    import os
    t = tmp_path / "t"
    t.mkdir()
    os.mkfifo(t / "fifo")
    import signal

    def stuck(*_):
        raise AssertionError("ip_hash is still reading after 20 s")
    old = signal.signal(signal.SIGALRM, stuck)
    for name in ("/dev/zero", "fifo", "."):
        (t / "files.lst").write_text(name + "\n")
        signal.alarm(20)
        try:
            h, note, n = P.ip_hash(t, "files.lst")
        finally:
            signal.alarm(0)
        assert h is None and n == 0 and "not a regular file" in note, (name, note)
    signal.signal(signal.SIGALRM, old)


def test_ip_hash_refuses_a_list_above_the_byte_cap(tmp_path, monkeypatch):
    t = tmp_path / "t"
    t.mkdir()
    (t / "big.v").write_bytes(b"x" * 100)
    (t / "files.lst").write_text("big.v\n")
    assert P.ip_hash(t, "files.lst")[0] is not None
    monkeypatch.setattr(P, "IP_BYTES_MAX", 99)
    h, note, _ = P.ip_hash(t, "files.lst")
    assert h is None and "above 99 bytes" in note


def test_a_tier_run_without_a_layout_record_is_unverified(tiered, monkeypatch):
    """Without the record a removed repo looks like one not laid out: the capture is not trusted."""
    s = tiered
    set_tiers(s, {"command": "true"})

    def broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(T, "_record_layout", broken)
    f = valid(s.run_cell("pinned", tier="t1"))
    assert f["verdict"] == "UNVERIFIED" and "left no layout record" in f["reason"], f["reason"]
    assert f["provenance"]["checkout"]["laid_out"] is None


def test_a_change_the_layout_left_is_not_the_commands_but_a_further_change_is(tmp_path):
    """The record's before-state: a tracked file already changed when the command started (e.g. checkout
    normalisation) is not DIRTY unless the command changes it again."""
    import subprocess
    r = tmp_path / "r"
    r.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(r)]
    (r / "f.v").write_text("module f; endmodule\n")
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "f.v"], check=True)
    subprocess.run(git + ["commit", "-qm", "i"], check=True)
    head = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (r / "f.v").write_text("module f; endmodule\r\n")                  # changed by the layout, before the command
    T._record_layout(tmp_path, r, T.Layout(laid_out=[""]))
    rec = T.read_layout_record(tmp_path)
    assert rec["changes_before"] == {"": {"f.v": hashlib.sha256(b"module f; endmodule\r\n").hexdigest()}}
    co = P.capture_checkout(r, head, [], record=rec)
    assert co["dirty"] == [] and co["lost"] == [] and co["laid_out"] == [""]
    (r / "f.v").write_text("module tampered; endmodule\n")              # changed by the command
    assert P.capture_checkout(r, head, [], record=rec)["dirty"] == [{"tree_path": "", "files": ["f.v"]}]
