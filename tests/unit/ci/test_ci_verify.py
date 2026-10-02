"""verify(): the seven checks of interfaces.md section 10, in order (D5, D10, D11).

AT-D5 itself needs nanosoc_rm.scan (check 7 on the genuine bundle) and nanosoc_rm.web (its route table), both built
on other branches; these tests run its genuine case and all 13 tampers against verify() with a stand-in scanner,
using the acceptance file's own case builders (conftest `ce`)."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from nanosoc_rm.ci.verify import RunRecord, Verdict, verify
from nanosoc_rm.config import ConfigError

import ci_helpers

# fixtures (ci_helpers docstring)
ce, sw, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.sw, ci_helpers.ci_config, ci_helpers.scanner_clean


def run(ce, case, root, scanner, files=None):
    bundle = ce.write_bundle(root, case.prov, case.verdicts, files)
    return verify(RunRecord(**case.run), case.pipeline, case.job, case.variables, bundle, scanner=scanner)


def test_genuine_bundle_is_accepted(ce, ci_config, tmp_path, scanner_clean):
    v = run(ce, ce.genuine_case(), tmp_path / "g", scanner_clean)
    assert v == Verdict("accepted", None, None)


def test_every_tamper_is_rejected_at_the_check_that_owns_the_field(ce, ci_config, tmp_path, scanner_clean):
    assert len(ce.D5_TAMPERS) == 13
    for n, (label, check, mutate) in enumerate(ce.D5_TAMPERS):
        case = ce.genuine_case()
        mutate(case)
        v = run(ce, case, tmp_path / ("t%02d" % n), scanner_clean)
        assert (v.acceptance, v.check) == ("rejected", check), (label, v)
        assert v.reason.startswith("check %d: " % check), (label, v.reason)


def test_d5_control_names_the_field_and_both_commits(ce, ci_config, tmp_path, scanner_clean):
    case = ce.genuine_case()
    case.prov["captured"]["superproject_commit"] = ce.RC4_PARENT
    v = run(ce, case, tmp_path / "c", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 5)
    assert "superproject_commit" in v.reason and ce.RC4 in v.reason and ce.RC4_PARENT in v.reason


def test_the_checks_run_in_contract_order(ce, ci_config, tmp_path, scanner_clean):
    """Two tampers at once: the earlier check names the run (first failure wins)."""
    case = ce.genuine_case()
    ce._harness(case)                         # check 4
    ce._job_name(case)                        # check 1
    assert run(ce, case, tmp_path / "a", scanner_clean).check == 1
    case = ce.genuine_case()
    ce._empty_rows(case)                      # check 6
    case.prov["captured"]["superproject_commit"] = ce.RC4_PARENT     # check 5
    assert run(ce, case, tmp_path / "b", scanner_clean).check == 5


def test_runner_outside_the_allowlist_is_rejected_at_check_1(ce, ci_config, tmp_path, scanner_clean):
    case = ce.genuine_case()
    case.job["runner"]["id"] = ce.RUNNER_ID + 1
    v = run(ce, case, tmp_path / "r", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 1) and str(ce.RUNNER_ID + 1) in v.reason
    case = ce.genuine_case()
    del case.job["runner"]
    assert run(ce, case, tmp_path / "n", scanner_clean).check == 1


@pytest.mark.parametrize("change, words", [
    (lambda c: c.pipeline.update(source="push"), "source"),
    (lambda c: c.pipeline.pop("project_id"), "project"),
    (lambda c: c.job.update(ref="feat/x"), "job ref"),
    (lambda c: c.job["pipeline"].update(id=4), "pipeline 4"),
])
def test_other_check_1_facts(ce, ci_config, tmp_path, scanner_clean, change, words):
    case = ce.genuine_case()
    change(case)
    v = run(ce, case, tmp_path / "x", scanner_clean)
    assert v.check == 1 and words in v.reason, v


def test_missing_rm_request_names_the_variable(ce, ci_config, tmp_path, scanner_clean):
    case = ce.genuine_case()
    case.variables = [{"key": "OTHER", "value": "x"}]
    v = run(ce, case, tmp_path / "v", scanner_clean)
    assert v.check == 2 and "RM_REQUEST not delivered to the pipeline" in v.reason


def test_bundle_without_provenance_is_check_3(ce, ci_config, tmp_path, scanner_clean):
    case = ce.genuine_case()
    bundle = ce.write_bundle(tmp_path / "p", case.prov, case.verdicts)
    (bundle / "provenance.json").unlink()
    v = verify(RunRecord(**case.run), case.pipeline, case.job, case.variables, bundle, scanner=scanner_clean)
    assert v.check == 3 and "provenance.json" in v.reason


def test_scan_finding_refuses_with_locations_only(ce, ci_config, tmp_path):
    secret = "STAND-IN VENDOR TEXT"
    finding = SimpleNamespace(verdict="finding", findings=[{"rule": "arm.banner", "file": "logs/t0.log", "line": 3}],
                              files_scanned=3, armed={"arm.banner": True})
    v = run(ce, ce.genuine_case(), tmp_path / "f", lambda b: finding, {"logs/t0.log": ce.T0_LOG + secret})
    assert (v.acceptance, v.check) == ("refused", 7)
    assert "arm.banner" in v.reason and "logs/t0.log:3" in v.reason and secret not in v.reason


def test_a_scan_that_could_not_run_refuses(ce, ci_config, tmp_path):
    empty = SimpleNamespace(verdict="unverified", findings=[], files_scanned=0, armed={"arm.banner": True})
    v = run(ce, ce.genuine_case(), tmp_path / "e", lambda b: empty)
    assert (v.acceptance, v.check) == ("refused", 7) and "0 files" in v.reason
    unarmed = SimpleNamespace(verdict="unverified", findings=[], files_scanned=3, armed={"arm.banner": False})
    v = run(ce, ce.genuine_case(), tmp_path / "u", lambda b: unarmed)
    assert v.check == 7 and "arm.banner" in v.reason


def test_scanner_is_called_only_after_checks_1_to_6(ce, ci_config, tmp_path):
    calls = []
    case = ce.genuine_case()
    case.prov["captured"]["superproject_commit"] = ce.RC4_PARENT
    run(ce, case, tmp_path / "s", lambda b: calls.append(b))
    assert calls == []


def test_verify_needs_the_ci_project_id(ce, tmp_path, monkeypatch, scanner_clean):
    """Check 1 compares the API's project with ci.project_id: without it verify cannot answer (exit 3)."""
    path = ce.write_config(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace("project_id = %d\n" % ce.CI_PROJECT_ID, ""))
    with pytest.raises(ConfigError, match="project_id"):
        run(ce, ce.genuine_case(), tmp_path / "c", scanner_clean)


def test_run_record_has_the_runs_table_fields():
    from nanosoc_rm import db
    assert [f.name for f in dataclasses.fields(RunRecord)] == list(db._RUN_FIELDS)


# ---- review CI-T2: contracted facts the 13 AT-D5 tampers do not reach ---------------------------------------------
def _pretty_request(c):
    c.variables[0]["value"] = json.dumps(json.loads(c.variables[0]["value"]), indent=1)     # JSON-equal, not bytes


def _status_maybe(c):
    rows = c.verdicts.split("\n")
    f = rows[0].split("\t")
    f[1] = "MAYBE"                                           # 5 fields, right nonce and time; not a valid status
    rows[0] = "\t".join(f)
    c.verdicts = "\n".join(rows)


@pytest.mark.parametrize("label, check, change, words", [
    ("the job's pipeline is in another project", 1,
     lambda c: c.job["pipeline"].update(project_id=c.job["pipeline"]["project_id"] + 1), "job belongs to project"),
    ("RM_REQUEST equal as JSON, not as bytes", 2, _pretty_request, "differs from the stored request"),
    ("two RM_REQUEST variables", 2, lambda c: c.variables.append(dict(c.variables[0])), "2 RM_REQUEST"),
    ("ci.job_id differs from the API", 4, lambda c: c.prov["ci"].update(job_id=c.prov["ci"]["job_id"] + 1), "ci.job_id"),
    ("ci.project differs", 4, lambda c: c.prov["ci"].update(project="soclabs/other-ci"), "ci.project"),
    ("ci.job_name differs", 4, lambda c: c.prov["ci"].update(job_name="t0-debug"), "ci.job_name"),
    ("tier t1", 4, lambda c: c.prov.update(tier="t1"), "tier"),
    ("a row whose status is not one of the six", 6, _status_maybe, "not a valid row"),
])
def test_each_contracted_fact_is_checked(ce, ci_config, tmp_path, scanner_clean, label, check, change, words):
    case = ce.genuine_case()
    change(case)
    v = run(ce, case, tmp_path / "x", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", check), (label, v)
    assert v.reason.startswith("check %d: " % check) and words in v.reason, (label, v.reason)
