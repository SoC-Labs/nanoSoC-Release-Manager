"""The job card (_job.html) renders for EVERY job kind and every result shape a kind's outcome can produce
(evaluation 2026-10-16, report action 1: matrix-cell and store-index results had no `kinds` key, and six History
and Store links answered HTTP 500). The sample documents below are what each kind's child prints; a new kind added
to jobs.all_kinds() without a sample fails test_every_kind_has_a_sample, so the card is never untested for it."""
from __future__ import annotations

import json

import pytest

from nanosoc_rm import jobs
from nanosoc_rm.web.page import Page, env

_COMPAT = {"verdict": "FAIL", "run_verdict": "FAIL", "configuration": "eth-main", "commit": "a" * 40,
           "started_at": "2026-10-16T00:00:00Z", "finished_at": "2026-10-16T00:00:30Z", "nonce": "n" * 16,
           "gates": [{"id": "t0.pins.fetchable", "verdict": "FAIL", "control": "ok", "detail": "d"}],
           "pins": [{"tree_path": "tidelink", "component": "tidelink", "commit": "b" * 40, "state": "pinned",
                     "url_kind": "ssh", "items": {"fetchable": "fail"}}]}
SAMPLES = [
    {"kind": "compat_result", "subject": "eth-main", "fact": _COMPAT},
    {"kind": "matrix_cell", "subject": "eth-main:latest-tag:tidelink",
     "fact": {"verdict": "FAIL", "column": "latest-tag", "component": "tidelink", "attempts": [{}, {}],
              "flaky_gates": []}},
    {"kind": "store_index", "subject": "store", "fact": {"verdict": "PASS", "counts": {"files": 3},
                                                       "artefacts": [1], "builds": [], "manifests": []}},
    {"kind": "freshness", "subject": "eth-main", "fact": {"level": "tree", "configuration": "eth-main",
                                                        "state": "outdated"}},
    {"kind": "freshness", "subject": "eth-main:tidelink", "fact": {"level": "pin", "behind": None}},
    {"kind": "regen_class", "subject": "eth-main", "fact": {"verdict": "PASS", "class": "none",
                                                          "build_affecting": False}},
    {"kind": "repo_seen", "subject": "github:SoC-Labs/x", "fact": {}},
    {"kind": "mirror_status", "subject": "b", "fact": {"ok": False}},
]
STDOUT = "\n".join(json.dumps(d) for d in SAMPLES)
# The kinds the samples above exercise beyond the generic {documents, kinds} summary.
COVERED = {"compat", "matrix-cell", "store-index", "freshness-scan", "regen-class", "org-scan"}


def _card(job: dict) -> str:
    p = Page()
    return env.get_template("_job.html").render(job=job, log="", position=None, csrf="t", fact=p.fact, page=p,
                                                stored=None)


def _job(kind: str, outcome: dict) -> dict:
    return {"id": 4, "kind": kind, "params": {}, "state": outcome.get("state", "done"), "by": "ann",
            "created_at": "2026-10-16T00:00:00Z", "started_at": None, "finished_at": None, "cancel_by": None,
            "command": "nanosoc-rm %s" % kind, "verdict": outcome.get("verdict"), "reason": outcome.get("reason"),
            "result": outcome.get("result")}


def test_every_kind_has_a_sample():
    generic = {k for k in jobs.all_kinds()
               if (jobs.registered(k) is None or jobs.registered(k).outcome is None) and k != "compat"}
    assert set(jobs.all_kinds()) - generic <= COVERED, "add a sample document for %s" % sorted(
        set(jobs.all_kinds()) - generic - COVERED)


@pytest.mark.parametrize("kind", jobs.all_kinds())
@pytest.mark.parametrize("stdout", ["", STDOUT], ids=["no-output", "samples"])
@pytest.mark.parametrize("rc", [0, 1, 2, 3, None])
def test_the_job_card_renders_for_every_kind_and_outcome(kind, stdout, rc):
    o = jobs._outcome({"kind": kind, "params": {}}, rc, stdout, "last line\n")
    html = _card(_job(kind, o))
    assert 'id="job-4"' in html and "nanosoc-rm %s" % kind in html


@pytest.mark.parametrize("result", [
    {"subject": "eth-rc4", "verdict": "FAIL", "column": "pinned", "component": "tidelink", "attempts": 2,
     "flaky_gates": ["t0.pins.reachable"]},                                       # matrix cell (job 4..8 live)
    {"subject": "store", "files": 12, "artefacts": 3, "builds": 0, "manifests": 0},  # store index (job 140 live)
    {"trees": {"eth-main": "outdated"}, "pins": 3, "not_measured": 1},
    {"new": 0, "repos": []}, {"documents": 2, "kinds": None}, {"weird": {"a": {"b": 1}}}, [], "text", 0])
@pytest.mark.parametrize("kind", ["walk", "matrix-cell", "store-index", "compat"])
def test_the_job_card_renders_any_result_shape(kind, result):
    html = _card(_job(kind, {"result": result}))
    assert 'id="job-4"' in html


def test_a_field_result_shows_its_fields():
    html = _card(_job("store-index", {"result": {"subject": "store", "files": 12, "artefacts": 3}}))
    assert 'data-result="fields"' in html and "<th>files</th><td>12</td>" in html
    html = _card(_job("matrix-cell", {"result": {"verdict": "FAIL", "flaky_gates": ["a", "b"]}}))
    assert "2 item(s)" in html
