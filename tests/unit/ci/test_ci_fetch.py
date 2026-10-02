"""fetch_pending(), coverage() and the stored bundle (interfaces.md section 10 Fetch; A4, D2, D5, D7, D11).

nanosoc_rm.t0 (gates, tiers) is built on another branch. The accepted path is tested here with a stand-in that
applies README section 6 through the acceptance file's own reference oracles (reference_rollup,
reference_apply_waivers, reference_run_verdict), so it shares no logic with the code under test."""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.ci import fetch, verify as verify_mod
from nanosoc_rm.ci.client import FakeCI
from nanosoc_rm.ci.fetch import FetchErrors, coverage, fetch_pending, items
from nanosoc_rm.ci.verify import RunRecord

import ci_helpers

# fixtures (ci_helpers docstring)
ce, sw, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.sw, ci_helpers.ci_config, ci_helpers.scanner_clean


class FakeT0:
    """README section 6 via the acceptance file's oracles, in the shapes interfaces.md section 9 contracts."""

    def __init__(self, ce):
        self.ce = ce

    def rollup(self, rows, scope):
        declared = self.ce.CONFIG_GATES if scope == "configuration" else self.ce.REGISTRY_GATES
        detail = {r["gate"]: r["detail"] for r in rows}
        return [{"id": g, "verdict": v, "control": c, "detail": detail.get(g) or "no row"}
                for g, (v, c) in self.ce.reference_rollup(rows, declared).items()]

    def apply_waivers(self, gates, item_map, waivers):
        rolled = {g["id"]: (g["verdict"], g["control"]) for g in gates}
        out, unused = self.ce.reference_apply_waivers(rolled, item_map, waivers)
        res = []
        for g in gates:
            g = dict(g)
            if out[g["id"]][0] == "WAIVED":
                bad = [i for i in item_map[g["id"]] if i["result"] != "pass"]
                direct = {w["tree_path"]: w for w in waivers if w["gate"] == g["id"]}
                g["raw_verdict"], g["verdict"] = g["verdict"], "WAIVED"
                g["waived"] = []
                for i in bad:
                    w = direct.get(i["tree_path"]) or next(direct[t] for t in direct if i["tree_path"].startswith(t + "/"))
                    g["waived"].append({"tree_path": i["tree_path"], "result": i["result"], "covered_by": w["tree_path"],
                                        "reason": w["reason"], "approved_by": w["approved_by"]})
            res.append(g)
        return res, unused

    def run_verdict(self, gates):
        return self.ce.reference_run_verdict({g["id"]: (g["verdict"], g["control"]) for g in gates})

    def check_budget(self, tier, started, finished):
        s = int((clock.parse(finished) - clock.parse(started)).total_seconds())
        return SimpleNamespace(ok=s <= 120, seconds=s, budget_s=120)


@pytest.fixture
def t0(monkeypatch, ce):
    fake = FakeT0(ce)
    monkeypatch.setattr(fetch, "_t0", lambda: (fake, fake))
    return fake


def example_item_files(ce, drop_tree_path=None):
    out = {}
    for gate, lines in ce.example_items().items():
        lines = [i for i in lines if i["tree_path"] != drop_tree_path]
        out["items/%s.jsonl" % gate] = "".join(json.dumps(i) + "\n" for i in lines)
    return out


def setup_run(ce, root, case=None, files=None, **fake_kw):
    case = case or ce.genuine_case()
    bundle = ce.write_bundle(root, case.prov, case.verdicts, files)
    db.put_run(RunRecord(**case.run))
    fake = FakeCI(pipelines={case.pipeline["id"]: case.pipeline}, jobs={case.pipeline["id"]: [case.job]},
                  variables={case.pipeline["id"]: case.variables}, artifacts={case.job["id"]: bundle}, **fake_kw)
    return case, fake


def at(text):
    return clock.parse(text)


NOW = at("2026-10-01T02:10:00Z")


def test_accepted_bundle_rolls_up_with_the_waivers_and_is_kept(ce, ci_config, tmp_path, scanner_clean, t0):
    case, fake = setup_run(ce, tmp_path / "b", files=example_item_files(ce))
    (doc,) = fetch_pending(client=fake, now=NOW)
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"], f["reason"]) == ("accepted", "WAIVED", None)
    g = {x["id"]: x for x in f["gates"]}
    assert {k: (v["verdict"], v["control"]) for k, v in g.items()} == ce.EXPECTED_RC4_WAIVED_ROLLUP
    assert all(g[k]["raw_verdict"] == ce.EXPECTED_RC4_ROLLUP[k][0] for k in ce.RC4_WAIVED_GATES)
    assert (f["started_at"], f["finished_at"], f["over_budget"]) == (case.prov["started_at"], case.prov["finished_at"], False)
    assert f["runner"] == {"id": ce.RUNNER_ID, "description": "rm-hardened", "tags": []}
    assert f["bundle"] == {"store": "ci", "path": "jobs/%d/artifacts" % case.job["id"]}
    assert f["run"]["job_id"] == case.job["id"] and db.get_run(case.run["nonce"])["state"] == "accepted"
    assert not (ci_config.quarantine / case.run["nonce"]).exists()
    assert items(case.run["nonce"], "t0.pins.reachable") == ce.example_items()["t0.pins.reachable"]
    assert [a for a in db.alerts() if a["fact"]["alert_kind"] == "waiver_unused"] == []
    assert fetch_pending(client=fake, now=NOW) == []                       # final: never fetched again
    assert [d["fact"]["acceptance"] for d in db.latest("t0_result")] == ["accepted"]


def test_a_waiver_that_covered_nothing_raises_one_alert_naming_it(ce, ci_config, tmp_path, scanner_clean, t0):
    """GATE2 Q2: once e908ac8 is on GitHub its item passes, and the reachable waiver is unused."""
    case, fake = setup_run(ce, tmp_path / "b", files=example_item_files(ce, ce.AHB_QSPI_TREE_PATH))
    (doc,) = fetch_pending(client=fake, now=NOW)
    assert doc["fact"]["verdict"] == "WAIVED"
    (alert,) = db.alerts()
    a = alert["fact"]
    assert a["alert_kind"] == "waiver_unused" and a["about"] == doc["subject"]
    assert "t0.pins.reachable" in a["message"] and ce.AHB_QSPI_TREE_PATH in a["message"]


def test_without_items_nothing_is_waived(ce, ci_config, tmp_path, scanner_clean, t0):
    case, fake = setup_run(ce, tmp_path / "b")
    (doc,) = fetch_pending(client=fake, now=NOW)
    assert doc["fact"]["verdict"] == "FAIL" and "WAIVED" not in {g["verdict"] for g in doc["fact"]["gates"]}


def test_a_malformed_items_file_never_waives_its_gate(ce, ci_config, tmp_path, scanner_clean, t0):
    files = example_item_files(ce)
    files["items/t0.pins.fetchable.jsonl"] += '{"tree_path": "x", "result": "maybe", "why": ""}\n'
    case, fake = setup_run(ce, tmp_path / "b", files=files)
    g = {x["id"]: x for x in fetch_pending(client=fake, now=NOW)[0]["fact"]["gates"]}
    assert g["t0.pins.fetchable"]["verdict"] == "FAIL" and g["t0.pins.reachable"]["verdict"] == "WAIVED"
    assert db.alerts() == []          # its waivers could not be judged, so they are not reported unused either


def test_refused_bundle_leaves_nothing_behind(ce, ci_config, tmp_path, monkeypatch):
    secret = "STAND-IN VENDOR BANNER LINE"
    finding = SimpleNamespace(verdict="finding", findings=[{"rule": "arm.banner", "file": "logs/t0.log", "line": 3}],
                              files_scanned=3, armed={"arm.banner": True})
    monkeypatch.setattr(verify_mod, "_scan_dir", lambda: (lambda b: finding))
    case, fake = setup_run(ce, tmp_path / "b", files={"logs/t0.log": ce.T0_LOG + secret + "\n"})
    (doc,) = fetch_pending(client=fake, now=NOW)
    f = doc["fact"]
    assert (f["acceptance"], f["verdict"], f["bundle"]) == ("refused", "UNVERIFIED", None)
    assert "logs/t0.log:3" in f["reason"] and secret not in f["reason"]
    left = [p for p in ci_config.data_dir.rglob("*") if p.is_file() and secret in p.read_text(errors="replace")]
    assert left == []


def test_rejected_bundle_is_deleted_from_quarantine(ce, ci_config, tmp_path):
    case = ce.genuine_case()
    case.prov["captured"]["superproject_commit"] = ce.RC4_PARENT
    case, fake = setup_run(ce, tmp_path / "b", case=case)
    (doc,) = fetch_pending(client=fake, now=NOW)
    assert doc["fact"]["acceptance"] == "rejected" and doc["fact"]["runner"]["id"] == ce.RUNNER_ID
    assert list(ci_config.quarantine.rglob("*")) == []
    with pytest.raises(FileNotFoundError):
        items(case.run["nonce"], "t0.pins.reachable")


def test_two_t0_jobs_are_rejected_at_check_1(ce, ci_config, tmp_path):
    case, fake = setup_run(ce, tmp_path / "b")
    fake.job_lists[case.pipeline["id"]] = [case.job, dict(case.job, id=2001)]
    f = fetch_pending(client=fake, now=NOW)[0]["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 1:") and "2 jobs" in f["reason"]


def test_an_unsafe_artifact_is_rejected_and_nothing_escapes(ce, ci_config, tmp_path):
    case, fake = setup_run(ce, tmp_path / "b")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("rm-bundle/../../escaped.txt", "x")
    fake.artifact_store[case.job["id"]] = buf.getvalue()
    f = fetch_pending(client=fake, now=NOW)[0]["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 3:") and "unsafe" in f["reason"]
    assert not list(tmp_path.rglob("escaped.txt"))


@pytest.mark.parametrize("job_change, now, want", [
    ({"status": "failed", "failure_reason": "runner_system_failure"}, NOW, ("no-bundle", "INFRA")),
    ({"status": "failed", "failure_reason": "script_failure"}, NOW, ("no-bundle", "UNVERIFIED")),
    ({"status": "failed", "failure_reason": "script_failure", "exit_code": 137}, NOW, ("no-bundle", "INFRA")),
    ({"status": "canceled", "failure_reason": None}, NOW, ("no-bundle", "UNVERIFIED")),
    ({"status": "running"}, NOW, None),
    ({"status": "running"}, at("2026-10-01T04:03:11Z"), ("no-bundle", "INFRA")),
])
def test_no_bundle_and_timeouts(ce, ci_config, tmp_path, job_change, now, want):
    case = ce.genuine_case()
    case.job.update(job_change)
    case, fake = setup_run(ce, tmp_path / "b", case=case)
    del fake.artifact_store[case.job["id"]]                    # the job left no artifact
    out = fetch_pending(client=fake, now=now)
    if want is None:
        assert out == [] and db.get_run(case.run["nonce"])["state"] == "pending"
        return
    (doc,) = out
    assert contracts.validate_observation(doc) == []
    assert (doc["fact"]["acceptance"], doc["fact"]["verdict"]) == want and doc["fact"]["reason"]


def test_an_unreadable_ci_api_leaves_the_run_pending_until_the_timeout(ce, ci_config, tmp_path):
    case, fake = setup_run(ce, tmp_path / "b")
    fake.pipelines.clear()
    assert fetch_pending(client=fake, now=NOW) == []
    (doc,) = fetch_pending(client=fake, now=at("2026-10-01T05:00:00Z"))
    assert (doc["fact"]["acceptance"], doc["fact"]["verdict"]) == ("no-bundle", "INFRA")


def test_a_roll_up_that_cannot_run_keeps_the_run_pending(ce, ci_config, tmp_path, scanner_clean, monkeypatch):
    def missing():
        raise ImportError("no nanosoc_rm.t0 here")
    monkeypatch.setattr(fetch, "_t0", missing)
    case, fake = setup_run(ce, tmp_path / "b")
    with pytest.raises(FetchErrors, match=case.run["nonce"]):
        fetch_pending(client=fake, now=NOW)
    assert db.get_run(case.run["nonce"])["state"] == "pending"
    assert list(ci_config.quarantine.rglob("*")) == []


def test_trigger_then_fetch_end_to_end(ce, sw, ci_config, tmp_path, scanner_clean, t0):
    """The pipeline the trigger started, finished by the fake with a bundle built for its request."""
    from nanosoc_rm.ci.trigger import trigger
    fake = FakeCI(project_id=ce.CI_PROJECT_ID)
    doc = trigger("eth-rc4", by="alice", kind="manual", client=fake)
    nonce, pid = doc["fact"]["run"]["nonce"], doc["fact"]["run"]["pipeline_id"]
    req = json.loads(db.get_run(nonce)["request"])
    prov = ce.example_provenance()
    prov.update(run_nonce=nonce, request={k: req[k] for k in ("configuration", "repo", "commit", "upgrade_from",
                                                                "canonical_urls")})
    prov["ci"].update(pipeline_id=pid, job_id=pid + 1000, harness_sha=fake.pipelines[pid]["sha"])
    rows = [dict(r, nonce=nonce) for r in ce.parse_rows(ce.example_verdicts())]
    bundle = ce.write_bundle(tmp_path / "job", prov, ce.format_rows(rows), example_item_files(ce))
    fake.pipelines[pid].update(status="success")
    fake.job_lists[pid] = [{"id": pid + 1000, "name": "t0", "ref": "main", "status": "success",
                            "started_at": "2026-10-01T02:03:28Z", "finished_at": "2026-10-01T02:05:04Z",
                            "runner": {"id": ce.RUNNER_ID, "description": "rm-hardened"}, "tag_list": ["rm-hardened"]}]
    fake.artifact_store[pid + 1000] = bundle
    (final,) = fetch_pending(client=fake)                      # now: after the real trigger time
    assert final["subject"] == doc["subject"]
    assert (final["fact"]["acceptance"], final["fact"]["verdict"]) == ("accepted", "WAIVED")
    assert final["fact"]["runner"]["tags"] == ["rm-hardened"]
    run = db.latest("t0_result")[0]
    # AT-A4's own checker on the stored result and the CI API's record of it
    assert sw.check_a4(run, fake.pipelines[pid], fake.job_lists[pid], fake.variable_lists[pid],
                       configuration="eth-rc4", commit=ce.RC4, login="alice") == []


# ---- coverage (D2) ------------------------------------------------------------------------------------------------
def stored_result(ce, cfg, nonce, *, trigger="schedule", at_="2026-10-01T02:00:05Z", acceptance="accepted"):
    verdict = {"accepted": "PASS", "no-bundle": "INFRA", "rejected": "UNVERIFIED", "pending": None}[acceptance]
    gates = [{"id": "t0.infra.probe", "verdict": "PASS", "control": "fired", "detail": "x"}] if acceptance == "accepted" else []
    doc = {"schema": 1, "kind": "t0_result", "subject": "t0_result:%s:%s" % (cfg, nonce), "observed_at": at_,
           "sources": [{"kind": "ci_api", "command": "GET x"}],
           "fact": {"configuration": cfg, "commit": ce.RC4,
                    "run": {"nonce": nonce, "trigger": trigger, "triggered_by": "schedule" if trigger == "schedule" else "alice",
                            "triggered_at": at_, "pipeline_id": 1 if acceptance != "no-bundle" else None, "job_id": None},
                    "acceptance": acceptance, "reason": None if acceptance in ("accepted", "pending") else "why",
                    "verdict": verdict, "gates": gates,
                    "bundle": {"store": "ci", "path": "p"} if acceptance == "accepted" else None,
                    "started_at": at_ if acceptance == "accepted" else None,
                    "finished_at": at_ if acceptance == "accepted" else None,
                    "over_budget": False if acceptance == "accepted" else None, "runner": None}}
    db.put(doc)


def test_coverage_names_every_configuration_without_an_accepted_scheduled_run(ce, ci_config):
    night = dt.date(2026, 10, 1)
    want = ["eth-rc4", "eth-main", "multicore-master", "@registry"]
    assert [r["configuration"] for r in coverage(night)] == want
    stored_result(ce, "eth-rc4", "1" * 32)
    stored_result(ce, "eth-main", "2" * 32, acceptance="no-bundle")
    stored_result(ce, "multicore-master", "3" * 32, trigger="manual")
    stored_result(ce, "@registry", "4" * 32, at_="2026-09-30T02:00:05Z")
    rows = {r["configuration"]: r for r in coverage(night)}
    assert sorted(rows) == ["@registry", "eth-main", "multicore-master"]
    assert "no-bundle" in rows["eth-main"]["reason"] and rows["eth-main"]["runs"][0]["nonce"] == "2" * 32
    assert rows["multicore-master"]["reason"] == "no scheduled run was triggered"
    assert [r["configuration"] for r in coverage(dt.date(2026, 9, 30))] == ["eth-rc4", "eth-main", "multicore-master"]


# ---- review CI-R1 / CI-R2: no run stays pending for ever ----------------------------------------------------------
LATE = at("2026-10-01T06:00:00Z")                               # past the 120-minute timeout of the 02:03:10Z trigger


def assert_run_is_enabled_again(case):
    from nanosoc_rm.ci.trigger import trigger
    assert db.get_run(case.run["nonce"])["state"] != "pending"
    assert trigger("eth-rc4", by="alice", kind="manual", client=FakeCI())["fact"]["acceptance"] == "pending"


def test_a_run_whose_trigger_never_finished_is_final_at_the_timeout(ce, ci_config):
    """The process stopped during the POST: a pending run with no pipeline id."""
    case = ce.genuine_case()
    db.put_run(RunRecord(**dict(case.run, pipeline_id=None)))
    fake = FakeCI()
    assert fetch_pending(client=fake, now=NOW) == [] and db.get_run(case.run["nonce"])["state"] == "pending"
    (doc,) = fetch_pending(client=fake, now=LATE)
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"], f["run"]["pipeline_id"]) == ("no-bundle", "INFRA", None)
    assert "trigger never completed" in f["reason"] and doc["sources"][0]["command"].startswith("POST ")
    assert_run_is_enabled_again(case)


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def _bad_utf8(case, bundle_root):
    case.verdicts = case.verdicts.encode() + b"\xff\xfe\n"


def _bad_month(case, bundle_root):
    rows = case.verdicts.split("\n")
    rows[0] = "2026-13-01" + rows[0][len("2026-10-01"):]
    case.verdicts = "\n".join(rows)


def _bad_started_at(case, bundle_root):
    case.prov["started_at"] = "2026-10-32T02:03:30Z"


@pytest.mark.parametrize("label, change, check, words", [
    ("verdicts.tsv is not UTF-8", _bad_utf8, 6, "not UTF-8"),
    ("a row stamped in month 13", _bad_month, 6, "not a real time"),
    ("provenance started on October 32", _bad_started_at, 3, "not a real time"),
])
def test_a_malformed_bundle_is_rejected_at_once(ce, ci_config, tmp_path, scanner_clean, t0, label, change, check,
                                                words):
    """The job is the untrusted party (D5): what it writes is rejected, never a crash that keeps the run pending."""
    case = ce.genuine_case()
    change(case, tmp_path)
    if isinstance(case.verdicts, bytes):                       # write_bundle writes text: put the bytes in after
        raw, case.verdicts = case.verdicts, case.verdicts.decode(errors="replace")
        case, fake = setup_run(ce, tmp_path / "b", case=case)
        (fake.artifact_store[case.job["id"]] / "verdicts.tsv").write_bytes(raw)
    else:
        case, fake = setup_run(ce, tmp_path / "b", case=case)
    (doc,) = fetch_pending(client=fake, now=NOW)
    f = doc["fact"]
    assert (f["acceptance"], f["verdict"]) == ("rejected", "UNVERIFIED"), (label, f["reason"])
    assert f["reason"].startswith("check %d:" % check) and words in f["reason"], (label, f["reason"])
    assert list(ci_config.quarantine.rglob("*")) == []
    assert_run_is_enabled_again(case)


@pytest.mark.parametrize("label, entries, words", [
    ("a file and a directory of one name", [("rm-bundle/a", "x"), ("rm-bundle/a/b", "y")], "cannot be unpacked"),
    ("a directory and a file of one name", [("rm-bundle/a/b", "y"), ("rm-bundle/a", "x")], "cannot be unpacked"),
])
def test_an_artifact_that_cannot_be_unpacked_is_rejected_at_check_3(ce, ci_config, tmp_path, label, entries, words):
    case, fake = setup_run(ce, tmp_path / "b")
    fake.artifact_store[case.job["id"]] = _zip(entries)
    (doc,) = fetch_pending(client=fake, now=NOW)
    f = doc["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 3:") and words in f["reason"], f["reason"]
    assert list(ci_config.quarantine.rglob("*")) == []
    assert_run_is_enabled_again(case)


def test_a_corrupt_entry_is_rejected_at_check_3(ce, ci_config, tmp_path):
    case, fake = setup_run(ce, tmp_path / "b")
    data = bytearray(_zip([("rm-bundle/verdicts.tsv", "PAYLOAD-PAYLOAD-PAYLOAD")]))
    i = data.index(b"PAYLOAD-PAYLOAD")
    data[i:i + 7] = b"XXXXXXX"                                 # the CRC no longer matches
    fake.artifact_store[case.job["id"]] = bytes(data)
    f = fetch_pending(client=fake, now=NOW)[0]["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 3:") and "BadZipFile" in f["reason"]


def test_any_processing_error_is_retried_then_final_at_the_timeout(ce, ci_config, tmp_path, scanner_clean,
                                                                  monkeypatch):
    """The backstop: an error the code does not foresee keeps the run pending (and is reported) until the timeout,
    then the run is no-bundle / INFRA, naming the error's type only (its message may quote the bundle, D11)."""
    def broken(*a, **k):
        raise RuntimeError("VENDOR TEXT FROM THE BUNDLE")
    monkeypatch.setattr(fetch, "verify", broken)
    case, fake = setup_run(ce, tmp_path / "b")
    with pytest.raises(FetchErrors, match=case.run["nonce"]):
        fetch_pending(client=fake, now=NOW)
    assert db.get_run(case.run["nonce"])["state"] == "pending"
    with pytest.raises(FetchErrors):                           # reported once more, and now final
        fetch_pending(client=fake, now=LATE)
    (doc,) = [d for d in db.latest("t0_result") if d["fact"]["run"]["nonce"] == case.run["nonce"]]
    f = doc["fact"]
    assert contracts.validate_observation(doc) == []
    assert (f["acceptance"], f["verdict"]) == ("no-bundle", "INFRA") and "RuntimeError" in f["reason"]
    assert "VENDOR TEXT" not in f["reason"]
    assert fetch_pending(client=fake, now=LATE) == []          # final: nothing left to report
    assert list(ci_config.quarantine.rglob("*")) == []
    assert_run_is_enabled_again(case)


def test_the_backstop_never_overwrites_a_run_that_became_final(ce, ci_config, tmp_path, scanner_clean, t0,
                                                               monkeypatch):
    def alert_fails(*a, **k):
        raise RuntimeError("the alert could not be stored")
    monkeypatch.setattr(fetch, "_waiver_alert", alert_fails)
    case, fake = setup_run(ce, tmp_path / "b", files=example_item_files(ce, ce.AHB_QSPI_TREE_PATH))
    with pytest.raises(FetchErrors):
        fetch_pending(client=fake, now=LATE)
    assert db.get_run(case.run["nonce"])["state"] == "accepted"
    assert [d["fact"]["acceptance"] for d in db.latest("t0_result")] == ["accepted"]


# ---- review CI-T2: the artifact guards ----------------------------------------------------------------------------
def test_an_oversize_artifact_is_rejected_at_check_3(ce, ci_config, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BUNDLE_BYTES", 64)
    case, fake = setup_run(ce, tmp_path / "b")
    f = fetch_pending(client=fake, now=NOW)[0]["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 3:") and "larger than 64" in f["reason"]
    assert list(ci_config.quarantine.rglob("*")) == []


def test_a_symbolic_link_in_the_artifact_is_rejected_at_check_3(ce, ci_config, tmp_path, scanner_clean, t0):
    case, fake = setup_run(ce, tmp_path / "b")
    genuine = fake.artifacts(case.job["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(genuine)) as src, zipfile.ZipFile(buf, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info))
        link = zipfile.ZipInfo("rm-bundle/logs/passwd")
        link.external_attr = (0o120777 << 16)
        z.writestr(link, "/etc/passwd")
    fake.artifact_store[case.job["id"]] = buf.getvalue()
    f = fetch_pending(client=fake, now=NOW)[0]["fact"]
    assert f["acceptance"] == "rejected" and f["reason"].startswith("check 3:") and "symbolic link" in f["reason"]
