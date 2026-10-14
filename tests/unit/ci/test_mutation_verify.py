"""Mutation-killing tests for ci.verify (robustness angle: mutation).

A scripted AST mutation run over nanosoc_rm/ci/verify.py (2026-10-14) left 26 mutants alive against
test_ci_verify.py. Most were malformed-bundle paths: deleting the `raise` that turns a missing or broken file into
a check-3/check-6 rejection still passed every test, because no test fed such a bundle. Each test below names the
mutant it kills. A rejected bundle must come back as a Verdict, never as an exception out of verify()."""
from __future__ import annotations

import json

import pytest

from nanosoc_rm.ci.verify import ROW_SLACK_S, RunRecord, verify

import ci_helpers

ce, ci_config, scanner_clean = ci_helpers.ce, ci_helpers.ci_config, ci_helpers.scanner_clean


def _verify(ce, case, root, scanner, edit=None):
    bundle = ce.write_bundle(root, case.prov, case.verdicts)
    if edit:
        edit(bundle)
    return verify(RunRecord(**case.run), case.pipeline, case.job, case.variables, bundle, scanner=scanner)


@pytest.mark.parametrize("label, edit, words", [
    ("provenance not JSON", lambda b: (b / "provenance.json").write_text("{not json"), "is not JSON"),
    ("provenance a JSON list", lambda b: (b / "provenance.json").write_text("[1, 2]"), "not a JSON object"),
])
def test_unreadable_provenance_is_check_3(ce, ci_config, tmp_path, scanner_clean, label, edit, words):
    """read_provenance: kills `Raise -> pass` on the ValueError and not-a-dict branches, and `3 -> 4`."""
    v = _verify(ce, ce.genuine_case(), tmp_path / "b", scanner_clean, edit)
    assert (v.acceptance, v.check) == ("rejected", 3), (label, v)
    assert v.reason.startswith("check 3: ") and words in v.reason, (label, v.reason)


def test_provenance_time_that_matches_the_pattern_but_is_not_a_time_is_check_3(ce, ci_config, tmp_path,
                                                                                scanner_clean):
    """_check_provenance's RFC 3339 re-parse (Feb 30: the schema pattern admits it, RFC 3339 does not).
    Kills `Raise -> pass` on the re-parse."""
    case = ce.genuine_case()
    case.prov["started_at"] = "2026-02-30T02:03:30Z"
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 3), v
    assert v.reason == "check 3: provenance started_at 2026-02-30T02:03:30Z is not a real time", v.reason


@pytest.mark.parametrize("label, edit, words", [
    ("no verdicts.tsv", lambda b: (b / "verdicts.tsv").unlink(), "no readable verdicts.tsv"),
    ("verdicts.tsv not UTF-8", lambda b: (b / "verdicts.tsv").write_bytes(b"\xff\xfe\n"), "not UTF-8 (byte 0)"),
])
def test_unreadable_verdicts_is_check_6(ce, ci_config, tmp_path, scanner_clean, label, edit, words):
    """read_rows: kills `Raise -> pass` on OSError and UnicodeDecodeError, and `6 -> 7`."""
    v = _verify(ce, ce.genuine_case(), tmp_path / "b", scanner_clean, edit)
    assert (v.acceptance, v.check) == ("rejected", 6), (label, v)
    assert v.reason.startswith("check 6: ") and words in v.reason, (label, v.reason)


def test_a_blank_line_between_rows_is_rejected_with_its_line_number(ce, ci_config, tmp_path, scanner_clean):
    """Only the FINAL empty string (the trailing newline) is skipped. Kills `and -> or` in read_rows, which would
    skip every blank line AND stop reading at the last line whatever it holds."""
    case = ce.genuine_case()
    lines = case.verdicts.split("\n")
    case.verdicts = "\n".join(lines[:1] + [""] + lines[1:])
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 6) and "line 2:" in v.reason, v


def test_a_bad_last_row_without_a_trailing_newline_is_still_read(ce, ci_config, tmp_path, scanner_clean):
    case = ce.genuine_case()
    rows = ce.parse_rows(case.verdicts)
    rows[-1]["nonce"] = "f" * len(rows[-1]["nonce"])
    case.verdicts = ce.format_rows(rows).rstrip("\n")
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 6) and "line %d carries nonce" % len(rows) in v.reason, v


def test_row_numbers_start_at_one(ce, ci_config, tmp_path, scanner_clean):
    """_check_rows enumerate(rows, 1). Kills `1 -> 2`."""
    case = ce.genuine_case()
    rows = ce.parse_rows(case.verdicts)
    rows[0]["nonce"] = "f" * len(rows[0]["nonce"])
    case.verdicts = ce.format_rows(rows)
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert v.check == 6 and "verdicts.tsv line 1 carries nonce" in v.reason, v


def test_a_job_without_times_is_check_6_not_a_crash(ce, ci_config, tmp_path, scanner_clean):
    """_check_rows: a job record with no started_at. Kills `Raise -> pass` (NameError on `lo`) and `6 -> 7`."""
    case = ce.genuine_case()
    del case.job["started_at"]
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 6) and "no start and finish time" in v.reason, v


def test_a_row_stamped_with_a_pattern_valid_non_time_is_check_6(ce, ci_config, tmp_path, scanner_clean):
    """Kills `Raise -> pass` and `6 -> 7` on the per-row clock.parse."""
    case = ce.genuine_case()
    rows = ce.parse_rows(case.verdicts)
    rows[0]["utc"] = "2026-13-01T02:04:00Z"
    case.verdicts = ce.format_rows(rows)
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance, v.check) == ("rejected", 6), v
    assert v.reason.startswith("check 6: verdicts.tsv line 1 ")


@pytest.mark.parametrize("utc, accepted", [
    ("2026-10-01T02:02:28Z", True),       # started_at 02:03:28 - 60 s: inclusive
    ("2026-10-01T02:02:27Z", False),
    ("2026-10-01T02:06:04Z", True),       # finished_at 02:05:04 + 60 s: inclusive
    ("2026-10-01T02:06:05Z", False),
])
def test_row_time_window_edges(ce, ci_config, tmp_path, scanner_clean, utc, accepted):
    """lo <= t <= hi with ROW_SLACK_S = 60. Kills `<= -> <` (both sides) and `60 -> 61`."""
    assert ROW_SLACK_S == 60
    case = ce.genuine_case()
    rows = ce.parse_rows(case.verdicts)
    rows[0]["utc"] = utc
    case.verdicts = ce.format_rows(rows)
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert (v.acceptance == "accepted") is accepted, v


def test_request_diff_names_only_the_fields_that_differ(ce, ci_config, tmp_path, scanner_clean):
    """_check_identity's diff list. Kills `!= -> ==` (which would name every equal field instead)."""
    case = ce.genuine_case()
    case.prov["request"]["upgrade_from"] = ce.RC4_PARENT
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert v.check == 4 and v.reason.endswith("differs from the stored request in upgrade_from"), v.reason


def test_runner_rejection_lists_the_allowlist(ce, ci_config, tmp_path, scanner_clean):
    """_ids renders the allowlist. Kills `return '[...]' -> None`."""
    case = ce.genuine_case()
    case.job["runner"]["id"] = 1
    v = _verify(ce, case, tmp_path / "b", scanner_clean)
    assert v.check == 1 and "ci.allowed_runner_ids [%d]" % ce.RUNNER_ID in v.reason, v.reason


def test_a_scan_result_without_a_file_count_says_zero(ce, ci_config, tmp_path):
    """getattr(result, 'files_scanned', 0). Kills `0 -> 1`."""
    class Bare:
        verdict = "unverified"
    v = _verify(ce, ce.genuine_case(), tmp_path / "b", lambda b: Bare())
    assert (v.acceptance, v.check) == ("refused", 7) and "(0 files scanned)" in v.reason, v.reason


def test_provenance_round_trip_unchanged_is_accepted(ce, ci_config, tmp_path, scanner_clean):
    """Control for the tests above: re-serialising provenance.json compactly changes nothing."""
    case = ce.genuine_case()
    v = _verify(ce, case, tmp_path / "b", scanner_clean,
                lambda b: (b / "provenance.json").write_text(json.dumps(case.prov)))
    assert v.acceptance == "accepted", v
