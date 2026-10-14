"""Coverage angle (rob/coverage): verify's individual checks as functions (ci/verify.py, interfaces.md section 10).

The genuine-bundle and tamper tests (test_ci_verify.py) drive verify() end to end; these reach the branches a
tamper of the example bundle does not: the job's own pipeline record, RM_REQUEST delivered twice or not as a
string, provenance that is not an object, verdicts.tsv that is not UTF-8 or has a blank line, a job without times,
and a scanner that could not run."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanosoc_rm.ci import verify as V

CI = {"project": "grp/ci", "project_id": 7, "allowed_refs": ["main"], "job": "t0", "allowed_runner_ids": [3]}


def _run(**kw):
    d = dict(nonce="ab" * 16, configuration="eth", commit="c" * 40, request=b'{"schema":1}', trigger="manual",
             triggered_by="ann", triggered_at="2026-10-14T00:00:00Z", pipeline_id=11, job_id=22, state="pending")
    d.update(kw)
    return V.RunRecord(**d)


def _api(**over):
    pipeline = {"id": 11, "project_id": 7, "source": "trigger", "ref": "main", "sha": "h" * 40}
    job = {"id": 22, "name": "t0", "ref": "main", "runner": {"id": 3}, "pipeline": {"id": 11, "project_id": 7}}
    for k, v in over.items():
        where, key = k.split("__")
        target = pipeline if where == "p" else job if where == "j" else job["pipeline"]
        target[key] = v
    return pipeline, job


def _check1(**over):
    p, j = _api(**over)
    with pytest.raises(V._Fail) as ei:
        V._check_api(_run(), p, j, CI)
    assert ei.value.check == 1
    return ei.value.reason


def test_check_1_accepts_the_genuine_record():
    p, j = _api()
    V._check_api(_run(), p, j, CI)


@pytest.mark.parametrize("over,words", [
    ({"jp__project_id": 8}, "job belongs to project 8"),
    ({"j__ref": "dev"}, "job ref 'dev' differs"),
    ({"jp__ref": "dev"}, "job's pipeline ref 'dev' differs"),
    ({"jp__id": 12}, "belongs to pipeline 12"),
    ({"j__runner": None}, "runner none"),
    ({"p__ref": None}, "is not in ci.allowed_refs"),
    ({"p__id": 99}, "not the run's pipeline 11"),
])
def test_check_1_branches(over, words):
    assert words in _check1(**over)


def test_check_2_rm_request_must_be_delivered_once_as_the_stored_string():
    run = _run()
    V._check_variable(run, [{"key": "OTHER", "value": "x"}, {"key": "RM_REQUEST", "value": '{"schema":1}'}])
    for variables, words in (
        (None, "not delivered"),
        ([{"key": "RM_REQUEST", "value": "a"}, {"key": "RM_REQUEST", "value": "a"}], "2 RM_REQUEST"),
        ([{"key": "RM_REQUEST", "value": 5}], "differs"),
        ([{"key": "RM_REQUEST", "value": '{"schema":1} '}], "differs"),
    ):
        with pytest.raises(V._Fail, match=words) as ei:
            V._check_variable(run, variables)
        assert ei.value.check == 2


def test_read_provenance_failures_are_check_3(tmp_path):
    with pytest.raises(V._Fail, match="no readable provenance.json"):
        V.read_provenance(tmp_path)
    (tmp_path / "provenance.json").write_bytes(b"\xff\xfe")
    with pytest.raises(V._Fail, match="not JSON"):
        V.read_provenance(tmp_path)
    (tmp_path / "provenance.json").write_text("[1, 2]")
    with pytest.raises(V._Fail, match="not a JSON object"):
        V.read_provenance(tmp_path)


def test_read_rows_utf8_blank_lines_and_the_final_newline(tmp_path):
    with pytest.raises(V._Fail, match="no readable verdicts.tsv"):
        V.read_rows(tmp_path)
    (tmp_path / "verdicts.tsv").write_bytes(b"\xff")
    with pytest.raises(V._Fail, match="not UTF-8 \\(byte 0\\)"):
        V.read_rows(tmp_path)
    (tmp_path / "verdicts.tsv").write_text("")
    assert V.read_rows(tmp_path) == []
    row = "2026-10-14T00:00:00Z\tPASS\tt0.infra.probe\tok\t%s" % ("ab" * 16)
    (tmp_path / "verdicts.tsv").write_text(row + "\n")
    assert len(V.read_rows(tmp_path)) == 1
    (tmp_path / "verdicts.tsv").write_text(row + "\n\n")                 # a blank row is not a row
    with pytest.raises(V._Fail, match="line 2"):
        V.read_rows(tmp_path)


def test_check_6_needs_rows_and_the_jobs_times(tmp_path):
    (tmp_path / "verdicts.tsv").write_text("")
    with pytest.raises(V._Fail, match="holds no rows"):
        V._check_rows(_run(), {}, tmp_path)
    (tmp_path / "verdicts.tsv").write_text("2026-10-14T00:00:00Z\tPASS\tt0.infra.probe\tok\t%s\n" % ("ab" * 16))
    for job in ({}, {"started_at": "x", "finished_at": "y"}, {"started_at": "2026-10-14T00:00:00Z"}):
        with pytest.raises(V._Fail, match="no start and finish time"):
            V._check_rows(_run(), job, tmp_path)
    ok = {"started_at": "2026-10-14T00:00:30Z", "finished_at": "2026-10-14T00:05:00Z"}
    V._check_rows(_run(), ok, tmp_path)                                   # 30 s early: inside the 60 s slack
    late = {"started_at": "2026-10-14T00:01:01Z", "finished_at": "2026-10-14T00:05:00Z"}
    with pytest.raises(V._Fail, match="outside the job's run"):
        V._check_rows(_run(), late, tmp_path)
    with pytest.raises(V._Fail, match="carries nonce"):
        V._check_rows(_run(nonce="cd" * 16), ok, tmp_path)


def test_a_job_with_null_times_is_rejected_at_check_6_not_a_crash(tmp_path):
    (tmp_path / "verdicts.tsv").write_text("2026-10-14T00:00:00Z\tPASS\tt0.infra.probe\tok\t%s\n" % ("ab" * 16))
    with pytest.raises(V._Fail, match="no start and finish time"):
        V._check_rows(_run(), {"started_at": None, "finished_at": None}, tmp_path)


def test_check_7_reasons_name_rules_and_never_text(tmp_path):
    V._check_scan(tmp_path, lambda b: SimpleNamespace(verdict="clean"))
    found = SimpleNamespace(verdict="finding", findings=[{"rule": "arm.banner", "file": "a.v", "line": 3,
                                                          "text": "SECRET VENDOR TEXT"}])
    with pytest.raises(V._Fail) as ei:
        V._check_scan(tmp_path, lambda b: found)
    assert ei.value.check == 7 and "arm.banner at a.v:3" in ei.value.reason and "SECRET" not in ei.value.reason
    unarmed = SimpleNamespace(verdict="error", armed={"arm.banner": False, "tsmc": True, "arm.x": False})
    with pytest.raises(V._Fail, match="rules not armed on their specimens: arm.banner, arm.x"):
        V._check_scan(tmp_path, lambda b: unarmed)
    with pytest.raises(V._Fail, match="0 files scanned"):
        V._check_scan(tmp_path, lambda b: None)                           # no result at all: refused


def test_a_refused_bundle_is_refused_not_rejected(tmp_path, monkeypatch):
    calls = []
    for name in ("_check_api", "_check_variable", "_check_provenance", "_check_commit", "_check_rows"):
        monkeypatch.setattr(V, name, lambda *a, n=name: calls.append(n))
    monkeypatch.setattr(V, "read_provenance", lambda b: {})
    monkeypatch.setattr(V, "_check_identity", lambda *a: {})
    cfg = {"ci": {"project": "grp/ci", "project_id": 7, "allowed_refs": ["main"], "allowed_runner_ids": [3]}}

    class Cfg(dict):
        def require(self, *keys):
            v = self
            for k in keys:
                v = v[k]
            return v
    out = V.verify(_run(), {}, {}, [], tmp_path, config=Cfg(cfg),
                   scanner=lambda b: SimpleNamespace(verdict="finding", findings=[{"rule": "r", "file": "f", "line": 1}]))
    assert (out.acceptance, out.check) == ("refused", 7) and len(calls) == 5
    ok = V.verify(_run(), {}, {}, [], tmp_path, config=Cfg(cfg), scanner=lambda b: SimpleNamespace(verdict="clean"))
    assert (ok.acceptance, ok.check, ok.reason) == ("accepted", None, None)
    assert V.ci_settings(Cfg(cfg))["job"] == "t0"
