"""One matrix cell: a local compat T0 run with one component moved (D3), its provenance and capture (D4), INFRA
classification (D7), the re-run and quarantine (D8), and the same-night baseline comparison (D9).

Real runs use the hermetic scene (conftest.py). The D7/D8/D9 rules are also driven through `compat_fn` and
`capture_fn` (run_cell's test seams), so each rule is shown on exactly the input that must trip it."""
from __future__ import annotations

import datetime as dt
import secrets

import pytest

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.matrix import cells as K
from nanosoc_rm.matrix import provenance as P

pytestmark = [pytest.mark.requirement("D3"), pytest.mark.requirement("D4"), pytest.mark.requirement("D7"),
              pytest.mark.requirement("D8"), pytest.mark.requirement("D9")]

GATES_OK = [("t0.infra.probe", "PASS"), ("t0.runner.hardened", "NOT-RUN"), ("t0.pins.fetchable", "PASS"),
            ("t0.pins.reachable", "PASS"), ("t0.vendor.no-arm-ip", "PASS")]


def fake_result(scene, verdict, gates=GATES_OK, details=None):
    details = details or {}
    nonce = secrets.token_hex(16)
    return {"subject": "compat_result:top-main:%s" % nonce,
            "fact": {"commit": scene.top, "verdict": verdict, "reason": None, "not_run": 1, "over_budget": False,
                     "unjudged_overrides": [],
                     "gates": [{"id": g, "verdict": v, "detail": details.get(g, "detail of %s" % g)} for g, v in gates],
                     "pins": [{"tree_paths": ["a"], "commit": scene.a[0]}, {"tree_paths": ["b"], "commit": scene.b1}]}}


class FakeCompat:
    """compat_fn seam: hands out canned results in order, or raises a given exception."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, configuration, overrides, **kw):
        self.calls.append(dict(overrides or {}))
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def same_capture(run_dir, root, requested, overrides, scratch):
    return {"superproject_commit": root, "pins": [{"tree_path": r["tree_path"], "commit": r["commit"]}
                                                   for r in requested]}


def facts(docs):
    assert contracts.validate_observation(docs[0]) == [], contracts.validate_observation(docs[0])
    return docs[0]["fact"]


# ---------------------------------------------------------------------------------------------------------
# Real runs (hermetic hosts)
# ---------------------------------------------------------------------------------------------------------
def test_pinned_then_in_range_real_runs_with_provenance_and_baseline(clean):
    scene = clean
    pinned = scene.run_cell("pinned", store=True, by="alice")
    p = facts(pinned)
    assert p["verdict"] == "PASS" and len(p["attempts"]) == 1 and p["overrides"] == [] and p["component"] is None
    pv = p["provenance"]
    assert pv["requested"]["superproject_commit"] == scene.top and pv["captured"]["superproject_commit"] == scene.top
    assert {x["tree_path"]: x["commit"] for x in pv["captured"]["pins"]} == {"a": scene.a[0], "b": scene.b1}
    assert pv["moved"] == [] and set(pv["tools"]) >= {"git", "python", "nanosoc_rm"}
    assert pv["host"]["cpus"] >= 1 and pv["host"]["load_start"] is not None and pv["host"]["load_end"] is not None
    assert pinned[0]["subject"] == "matrix_cell:top-main:-:pinned:%s" % p["nonce"]

    cell = scene.run_cell("in-range", "alpha", store=True, by="alice")
    f = facts(cell)
    assert f["verdict"] == "PASS" and f["target"]["ref"] == "v1.0.1" and f["target"]["range"] == "~1.0"
    assert f["overrides"] == [{"tree_path": "a", "commit": scene.a[1]}]
    cap = {x["tree_path"]: x["commit"] for x in f["provenance"]["captured"]["pins"]}
    assert cap == {"a": scene.a[1], "b": scene.b1}                 # the moved pin, as the run read it
    assert f["baseline"]["nonce"] == p["nonce"]                    # D9 names the same night's pinned run
    assert {r["change"] for r in f["comparison"]["rows"]} == {"same"}
    with scene.env.active():
        assert {d["subject"] for d in db.latest("matrix_cell")} == {pinned[0]["subject"], cell[0]["subject"]}


def test_no_target_and_same_as_pinned_run_nothing(clean):
    for column in ("latest-tag", "head"):
        docs = clean.run_cell(column, "beta", compat_fn=FakeCompat())   # FakeCompat() with no result: never called
        f = facts(docs)
        assert f["verdict"] == "NOT-RUN" and f["attempts"] == [] and K.exit_for(f["verdict"]) == 2
    assert "same as pinned" in facts(clean.run_cell("head", "beta", compat_fn=FakeCompat()))["reason"]


def test_capture_reads_what_the_run_fetched_and_a_moved_pin_is_found(clean, tmp_path):
    """D4 on real objects: the capture re-reads the tree from the run's own fetches. Asked for alpha@a1 at `a`
    while the run fetched the tree with `a` -> a2: the capture says a2, so `a` is MOVED."""
    from nanosoc_rm.analysis.compat import compat
    with clean.env.active():
        compat("top-main", {"a": clean.a[1]}, keep_work=tmp_path / "w", url_map=clean.url_map)
    requested = [{"tree_path": "a", "commit": clean.a[1]}, {"tree_path": "b", "commit": clean.b1}]
    cap = P.capture(tmp_path / "w" / "run", clean.top, requested, {"a": clean.a[1]}, tmp_path / "s")
    assert cap["superproject_commit"] == clean.top and P.moved(requested, cap) == []
    wrong = [{"tree_path": "a", "commit": clean.a[0]}, {"tree_path": "b", "commit": clean.b1}]
    assert P.moved(wrong, cap) == [{"tree_path": "a", "requested": clean.a[0], "captured": clean.a[1]}]
    # without the override top's tree names a1 at `a`, but this run fetched a2 there, never a1: not read (review
    # item 3: a gitlink alone is not a read)
    cap2 = P.capture(tmp_path / "w" / "run", clean.top, wrong, {}, tmp_path / "s2")
    assert {x["tree_path"]: x["commit"] for x in cap2["pins"]} == {"a": None, "b": clean.b1}
    # a pin asked for at another commit than its parent's fetched tree holds is read at what the tree holds
    other_b = [{"tree_path": "a", "commit": clean.a[1]}, {"tree_path": "b", "commit": clean.a[0]}]
    cap3 = P.capture(tmp_path / "w" / "run", clean.top, other_b, {"a": clean.a[1]}, tmp_path / "s3")
    assert P.moved(other_b, cap3) == [{"tree_path": "b", "requested": clean.a[0], "captured": clean.b1}]


def test_capture_reads_only_the_pin_fetches_and_an_unfetched_pin_is_not_read(clean, tmp_path):
    """Review item 3: the capture borrows objects from the run's pin fetches (`fetch-*` slots) only, never from the
    reachability clones (`refs-*`), the negative controls or the upgrade work. A pin the run did not fetch is
    captured as null (not read), even when a refs clone holds its commit. The capture says T0 cannot move a pin."""
    import shutil as sh

    from nanosoc_rm.analysis.compat import compat
    from nanosoc_rm.gitrun import run_git
    with clean.env.active():
        compat("top-main", {"a": clean.a[1]}, keep_work=tmp_path / "w", url_map=clean.url_map)
    git_dir = tmp_path / "w" / "run" / "git"
    requested = [{"tree_path": "a", "commit": clean.a[1]}, {"tree_path": "b", "commit": clean.b1}]
    cap = P.capture(tmp_path / "w" / "run", clean.top, requested, {"a": clean.a[1]}, tmp_path / "s0")
    assert {x["tree_path"]: x["commit"] for x in cap["pins"]} == {"a": clean.a[1], "b": clean.b1}
    assert "T0" in cap["note"] and "cannot move" in cap["note"]
    # the run's fetch of alpha@a2 is gone (as when `t0.pins.fetchable` says "not fetched"); the refs clone of alpha
    # still holds a2
    gone = [d for d in git_dir.glob("fetch-*")
            if run_git("rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}", cwd=d).stdout.strip() == clean.a[1]]
    assert gone, sorted(p.name for p in git_dir.iterdir())
    for d in gone:
        sh.rmtree(d)
    assert any(run_git("cat-file", "-e", clean.a[1] + "^{commit}", cwd=d).ok for d in git_dir.glob("refs-*"))
    cap = P.capture(tmp_path / "w" / "run", clean.top, requested, {"a": clean.a[1]}, tmp_path / "s1")
    got = {x["tree_path"]: x["commit"] for x in cap["pins"]}
    assert got == {"a": None, "b": clean.b1}, got                  # not read, not "captured equals requested"
    assert P.moved(requested, cap) == []


def test_a_deliberately_moved_submodule_makes_the_cell_unverified(clean):
    """D4 done-when: the run's captured commits differ from the requested ones -> UNVERIFIED, never PASS."""
    def moving_capture(run_dir, root, requested, overrides, scratch):
        cap = same_capture(run_dir, root, requested, overrides, scratch)
        cap["pins"][0]["commit"] = clean.a[2]                     # a setup step moved `a`
        return cap
    f = facts(clean.run_cell("in-range", "alpha", compat_fn=FakeCompat(fake_result(clean, "PASS")),
                             capture_fn=moving_capture))
    assert f["verdict"] == "UNVERIFIED" and "D4" in f["reason"]
    assert f["provenance"]["moved"] == [{"tree_path": "a", "requested": clean.a[0], "captured": clean.a[2]}]


# ---------------------------------------------------------------------------------------------------------
# D7
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raise_", [MemoryError("out of memory"), OSError(28, "No space left on device"),
                                    TimeoutError("the host stopped answering")])
def test_host_failures_are_infra_never_fail(clean, raise_):
    f = facts(clean.run_cell("pinned", compat_fn=FakeCompat(raise_), capture_fn=same_capture))
    assert f["verdict"] == "INFRA" and len(f["attempts"]) == 1 and "D7" in f["reason"]
    assert K.exit_for("INFRA") == 2


def test_a_fail_whose_every_failing_gate_timed_out_is_infra_and_not_rerun(clean):
    gates = GATES_OK[:4] + [("t0.vendor.no-arm-ip", "FAIL")]
    fc = FakeCompat(fake_result(clean, "FAIL", gates, {"t0.vendor.no-arm-ip": "git fetch timed out after 300 s"}))
    f = facts(clean.run_cell("pinned", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "INFRA" and len(fc.calls) == 1, f
    # the same FAIL with a real finding stays a FAIL
    fc = FakeCompat(*[fake_result(clean, "FAIL", gates, {"t0.vendor.no-arm-ip": "Arm banner in rtl/x.v"})] * 2)
    assert facts(clean.run_cell("pinned", compat_fn=fc, capture_fn=same_capture))["verdict"] == "FAIL"


# ---------------------------------------------------------------------------------------------------------
# D8
# ---------------------------------------------------------------------------------------------------------
RED = GATES_OK[:3] + [("t0.pins.reachable", "FAIL"), GATES_OK[4]]


def test_red_twice_is_fail_and_red_then_green_is_flaky(clean):
    fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "FAIL", RED))
    f = facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "FAIL" and len(f["attempts"]) == 2 and fc.calls == [{"a": clean.a[2]}] * 2
    assert f["blocks_auto_merge"] and K.exit_for("FAIL") == 1

    fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "PASS"))
    f = facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "FLAKY" and f["flaky_gates"] == ["t0.pins.reachable"] and f["blocks_auto_merge"]
    assert [a["verdict"] for a in f["attempts"]] == ["FAIL", "PASS"] and K.exit_for("FLAKY") == 1

    fc = FakeCompat(fake_result(clean, "FAIL", RED), MemoryError("oom"))
    assert facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))["verdict"] \
        == "UNVERIFIED"                                          # red, then no answer: neither FAIL nor FLAKY


def test_two_flaky_in_14_days_quarantine_with_owner_and_expire_on_their_own(clean, monkeypatch):
    t = [dt.datetime(2026, 10, 1, 2, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(clock, "now", lambda: t[0])

    def flaky(by):
        fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "PASS"))
        return clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture, store=True, by=by)

    assert flaky("alice")[1:] == []                                # one FLAKY: no quarantine
    t[0] += dt.timedelta(days=15)
    assert flaky("bob")[1:] == []                                  # the first is now older than 14 days
    t[0] += dt.timedelta(days=3)
    docs = flaky("carol")                                          # second FLAKY within 14 days
    assert len(docs) == 2
    q = docs[1]
    assert contracts.validate_observation(q) == []
    assert q["fact"]["test"] == "t0.pins.reachable" and q["fact"]["owner"] == "carol"
    assert clock.parse(q["fact"]["expires_at"]) - clock.parse(q["fact"]["since"]) == dt.timedelta(days=30)
    assert len(q["fact"]["flaky_cells"]) == 2
    with clean.env.active():
        from nanosoc_rm.config import load_config
        cfg = load_config()
        assert flaky("dave")[1:] == []                             # already quarantined: no second entry
        assert set(K.active_quarantine(K.quarantine("top-main", cfg))) == {"t0.pins.reachable"}
        K.set_owner("top-main", "t0.pins.reachable", "erin", "alice", config=cfg)
        cur = K.quarantine("top-main", cfg)[0]["fact"]
        assert cur["owner"] == "erin" and cur["set_by"] == "alice" and cur["expires_at"] == q["fact"]["expires_at"]
        later = clock.parse(q["fact"]["expires_at"]) + dt.timedelta(seconds=1)
        assert K.active_quarantine(K.quarantine("top-main", cfg), later) == {}   # expired: nobody acted


# ---------------------------------------------------------------------------------------------------------
# D9
# ---------------------------------------------------------------------------------------------------------
def test_comparison_is_gate_by_gate_against_the_same_nights_pinned_run_only(clean, monkeypatch):
    t = [dt.datetime(2026, 10, 2, 1, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    base = clean.run_cell("pinned", compat_fn=FakeCompat(fake_result(clean, "PASS")), capture_fn=same_capture,
                          store=True)
    t[0] += dt.timedelta(hours=3)
    bad = GATES_OK[:4] + [("t0.vendor.no-arm-ip", "FAIL")]
    fc = FakeCompat(fake_result(clean, "FAIL", bad), fake_result(clean, "FAIL", bad))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["baseline"]["subject"] == base[0]["subject"]
    rows = {r["gate"]: r["change"] for r in f["comparison"]["rows"]}
    assert rows["t0.vendor.no-arm-ip"] == "new-failure" and rows["t0.pins.fetchable"] == "same"
    # 13 hours later the only pinned run is another night's: no comparison, never against history
    t[0] += dt.timedelta(hours=13)
    fc = FakeCompat(fake_result(clean, "PASS"))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["baseline"] is None and f["comparison"]["rows"] == [] and "never with history" in f["comparison"]["note"]


# ---------------------------------------------------------------------------------------------------------
# Review fixes (2026-10-13): D7 signatures anchored, every failing gate, overloaded host; D9 baseline usable
# ---------------------------------------------------------------------------------------------------------
NO_ARM = GATES_OK[:4] + [("t0.vendor.no-arm-ip", "FAIL")]


@pytest.mark.parametrize("detail", ["Arm IP found in rtl/cmsdk_apb_watchdog_timeout.v (no waiver)",
                                    "Arm IP found in rtl/bloom_filter.v", "Arm IP found in rtl/timeout.v",
                                    "Arm banner in rtl/killed_flag.v", "Arm banner in rtl/oom_ctrl.sv",
                                    "1 tree with findings: tidelink (rtl/tl_backstop_timeout.sv)",
                                    "1 tree with findings: chiplet-room-ctrl (rtl/x.v)",
                                    "Arm banner in rtl/timeout_after_reset.v", "pin oom-killer/x not reachable",
                                    "Arm banner in rtl/nosigkill.v", "Arm banner in rtl/xtimed out.v"])
def test_a_real_finding_that_names_a_file_with_an_infra_word_stays_fail(clean, detail):
    """D7 must not turn a tree's failure into the host's: an infra word inside a file name is not a signature."""
    fc = FakeCompat(*[fake_result(clean, "FAIL", NO_ARM, {"t0.vendor.no-arm-ip": detail})] * 2)
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "FAIL" and len(fc.calls) == 2, (detail, f["reason"])


@pytest.mark.parametrize("detail", ["git fetch timed out after 300 s", "killed by signal 9 (out of memory)",
                                    "Cannot allocate memory", "licence checkout denied: no seats",
                                    "write failed: No space left on device", "OOM-killer stopped the job",
                                    "timeout after 600 s"])
def test_infra_signatures_still_match_the_hosts_messages(clean, detail):
    fc = FakeCompat(fake_result(clean, "FAIL", NO_ARM, {"t0.vendor.no-arm-ip": detail}))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "INFRA" and len(fc.calls) == 1, (detail, f["reason"])


def test_infra_only_when_every_failing_gate_is_the_hosts(clean):
    two = GATES_OK[:3] + [("t0.pins.reachable", "FAIL"), ("t0.vendor.no-arm-ip", "FAIL")]
    det = {"t0.pins.reachable": "git fetch timed out after 300 s", "t0.vendor.no-arm-ip": "Arm banner in rtl/x.v"}
    fc = FakeCompat(*[fake_result(clean, "FAIL", two, det)] * 2)
    assert facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture))["verdict"] == "FAIL"


def test_overloaded_host_red_then_green_is_infra_not_flaky(clean, monkeypatch):
    """D7 'overloaded host': a FAIL on a host above OVERLOAD_PER_CPU whose re-run is green is INFRA (the host,
    not the tree, and it never counts toward quarantine). Red twice is still FAIL, with the overload noted."""
    loads = iter([1.0, 16 * P.OVERLOAD_PER_CPU + 1] * 8)                 # quiet at start, busy by the end
    monkeypatch.setattr(P, "host_load", lambda: {"cpus": 16, "load": next(loads)})
    fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "PASS"))
    f = facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "INFRA" and f["flaky_gates"] == [] and "overloaded" in f["reason"], f["reason"]
    assert f["attempts"][0]["host"]["overloaded"] is True and K.exit_for(f["verdict"]) == 2
    fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "FAIL", RED))
    f = facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "FAIL" and "overloaded" in f["reason"]
    # just below the threshold it is an ordinary FLAKY
    monkeypatch.setattr(P, "host_load", lambda: {"cpus": 16, "load": 16 * P.OVERLOAD_PER_CPU})
    fc = FakeCompat(fake_result(clean, "FAIL", RED), fake_result(clean, "PASS"))
    f = facts(clean.run_cell("latest-tag", "alpha", compat_fn=fc, capture_fn=same_capture))
    assert f["verdict"] == "FLAKY" and f["attempts"][0]["host"]["overloaded"] is False


def test_a_baseline_without_gate_results_is_not_compared(clean, monkeypatch):
    """D9: the same night's pinned run was INFRA (no gates): the cell is not compared, and says why, rather than
    calling every failing gate a new failure."""
    t = [dt.datetime(2026, 10, 2, 1, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    base = clean.run_cell("pinned", compat_fn=FakeCompat(MemoryError("oom")), capture_fn=same_capture, store=True)
    assert base[0]["fact"]["verdict"] == "INFRA"
    t[0] += dt.timedelta(hours=1)
    fc = FakeCompat(fake_result(clean, "FAIL", NO_ARM), fake_result(clean, "FAIL", NO_ARM))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["comparison"]["rows"] == [] and "no gate results" in f["comparison"]["note"], f["comparison"]
    assert f["baseline"]["subject"] == base[0]["subject"]                 # still named: the reader can open it
    # a usable pinned run later the same night is preferred to the INFRA one
    t[0] += dt.timedelta(hours=1)
    good = clean.run_cell("pinned", compat_fn=FakeCompat(fake_result(clean, "PASS")), capture_fn=same_capture,
                          store=True)
    t[0] += dt.timedelta(minutes=5)
    fc = FakeCompat(fake_result(clean, "FAIL", NO_ARM), fake_result(clean, "FAIL", NO_ARM))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["baseline"]["subject"] == good[0]["subject"]
    assert {r["gate"]: r["change"] for r in f["comparison"]["rows"]}["t0.vendor.no-arm-ip"] == "new-failure"
    # ...even when an unusable (INFRA) pinned run is nearer in time to the cell
    t[0] += dt.timedelta(minutes=5)
    clean.run_cell("pinned", compat_fn=FakeCompat(MemoryError("oom")), capture_fn=same_capture, store=True)
    t[0] += dt.timedelta(minutes=1)
    fc = FakeCompat(fake_result(clean, "FAIL", NO_ARM), fake_result(clean, "FAIL", NO_ARM))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["baseline"]["subject"] == good[0]["subject"] and f["comparison"]["note"] is None


def test_baseline_is_a_pinned_run_of_the_same_configuration_commit_only(clean, monkeypatch):
    t = [dt.datetime(2026, 10, 3, 1, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    other = fake_result(clean, "PASS")
    other["fact"]["commit"] = "f" * 40                                    # another superproject commit
    clean.run_cell("pinned", compat_fn=FakeCompat(other), capture_fn=same_capture, store=True)
    t[0] += dt.timedelta(hours=1)
    f = facts(clean.run_cell("head", "alpha", compat_fn=FakeCompat(fake_result(clean, "PASS")),
                             capture_fn=same_capture, store=True))
    assert f["baseline"] is None and f["comparison"]["rows"] == []


def test_a_moved_superproject_commit_makes_the_cell_unverified(clean):
    def other_root(run_dir, root, requested, overrides, scratch):
        cap = same_capture(run_dir, root, requested, overrides, scratch)
        cap["superproject_commit"] = "e" * 40
        return cap
    f = facts(clean.run_cell("head", "alpha", compat_fn=FakeCompat(fake_result(clean, "PASS")),
                             capture_fn=other_root))
    assert f["verdict"] == "UNVERIFIED" and "(superproject)" in f["reason"]


def test_a_pinned_run_that_read_moved_pins_is_not_a_baseline(clean, monkeypatch):
    t = [dt.datetime(2026, 10, 4, 1, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(clock, "now", lambda: t[0])

    def moving(run_dir, root, requested, overrides, scratch):
        cap = same_capture(run_dir, root, requested, overrides, scratch)
        cap["pins"][0]["commit"] = clean.a[2]
        return cap
    base = clean.run_cell("pinned", compat_fn=FakeCompat(fake_result(clean, "PASS")), capture_fn=moving, store=True)
    assert base[0]["fact"]["verdict"] == "UNVERIFIED" and base[0]["fact"]["attempts"][0]["gates"]
    t[0] += dt.timedelta(hours=1)
    fc = FakeCompat(fake_result(clean, "FAIL", NO_ARM), fake_result(clean, "FAIL", NO_ARM))
    f = facts(clean.run_cell("head", "alpha", compat_fn=fc, capture_fn=same_capture, store=True))
    assert f["comparison"]["rows"] == [] and "moved pins" in f["comparison"]["note"], f["comparison"]
