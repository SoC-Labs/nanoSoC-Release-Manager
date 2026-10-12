"""Q2: each finding kind fires on a planted fixture and stays quiet on its clean twin; the patch applies (Q5)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, quality
from nanosoc_rm.cli import main
from qfix import commit, git, make_repo

pytestmark = pytest.mark.requirement("Q2")

# kind -> (planted files, clean twin files)
CASES = {
    "commented_out_code": (
        {"a.py": "def f(x):\n    # y = compute(x)\n    # return y * 2\n    return x\n",
         "m.sv": "module m(input a, output b);\n  // assign b = a & mask;\n  // always @(posedge clk) q <= d;\n"
                 "  assign b = a;\nendmodule\n",
         "run.sh": "#!/bin/sh\n# export BUILD_DIR=/tmp/out\n# rm -rf \"$BUILD_DIR\"\necho hi\n",
         "blk.c": "int f(int a) {\n/*\n  int b = a * 2;\n  return b;\n*/\n  return a;\n}\n"},
        {"a.py": "def f(x):\n    # Callers rely on identity, so the input comes back unchanged.\n    return x\n",
         "m.sv": "module m(input a, output b);\n  // The mask gates the output while the link is down.\n"
                 "  assign b = a;\nendmodule\n",
         "run.sh": "#!/bin/sh\n# Print a greeting so the CI log shows the job started.\necho hi\n",
         "blk.c": "int f(int a) {\n/*\n  Callers pass a value already scaled by the ADC gain.\n*/\n  return a;\n}\n"}),
    "restates_code": (
        {"a.py": "def f(counter):\n    # increment the counter\n    counter += 1\n    return counter\n",
         "b.c": "int g(int result) {\n    // return the result\n    return result;\n}\n"},
        {"a.py": "def f(counter):\n    # retries restart the backoff window, so count from here\n    counter += 1\n"
                 "    return counter\n",
         "b.c": "int g(int result) {\n    // callers ignore negative values (spec 4.2)\n    return result;\n}\n"}),
    "banner": (
        {"a.py": "# ------------------------------------------------\n# Helpers\n"
                 "# ------------------------------------------------\nX = 1\n",
         "m.sv": "// ===== Setup =====\nmodule m;\nendmodule\n"},
        {"a.py": "# Helpers\nX = 1\n", "m.sv": "// Setup\nmodule m;\nendmodule\n"}),
    "boilerplate": (
        {"tool.py": "# File: tool.py\n# Author: someone\n# Date: 2019-01-01\n# Copyright 2024 SoC Labs\nX = 1\n"},
        {"tool.py": "# Copyright 2024 SoC Labs\n# Converts the flist into a Makefile include.\nX = 1\n"}),
    "unknown_identifier": (
        {"calc.py": "def compute_total(values):\n    # compute_totl() adds the values; `old_helper()` used to round them\n"
                    "    return sum(values)\n"},
        {"calc.py": "def compute_total(values):\n    # compute_total() adds the values without rounding\n"
                    "    return sum(values)\n"}),
}


def _iso(days_ago: int) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("kind", sorted(CASES))
def test_kind_fires_on_planted_and_is_quiet_on_clean_twin(kind, tmp_path):
    planted, clean = CASES[kind]
    p = make_repo(tmp_path / "planted", planted)
    c = make_repo(tmp_path / "clean", clean)
    fp = quality.comments(None, path=p)["fact"]
    fc = quality.comments(None, path=c)["fact"]
    assert contracts.validate_observation(quality.comments(None, path=p)) == []
    fired = [x for x in fp["findings"] if x["kind"] == kind]
    assert fired, "%s did not fire on its planted fixture: %s" % (kind, fp["findings"])
    assert {x["path"] for x in fired} == set(planted), "every planted file fires"
    assert all(x["suggestion"] and x["line"] >= 1 for x in fired)
    assert fc["findings"] == [], "the clean twin of %s must be quiet: %s" % (kind, fc["findings"])


def test_unknown_identifier_suggests_the_close_match(tmp_path):
    p = make_repo(tmp_path / "p", CASES["unknown_identifier"][0])
    fired = {x["identifier"]: x for x in quality.comments(None, path=p)["fact"]["findings"]}
    assert fired["compute_totl"]["did_you_mean"] == "compute_total" and fired["compute_totl"]["edit"]
    assert "old_helper" in fired and fired["old_helper"]["edit"] is None


def test_stale_todo_is_dated_by_blame(tmp_path):
    old = make_repo(tmp_path / "old", {"a.py": "X = 1\n# TODO: drop this shim\n"}, date=_iso(400))
    new = make_repo(tmp_path / "new", {"a.py": "X = 1\n# TODO: drop this shim\n"}, date=_iso(3))
    fo = [x for x in quality.comments(None, path=old)["fact"]["findings"] if x["kind"] == "stale_todo"]
    fn = [x for x in quality.comments(None, path=new)["fact"]["findings"] if x["kind"] == "stale_todo"]
    assert len(fo) == 1 and fo[0]["line"] == 2 and 399 <= fo[0]["age_days"] <= 401
    assert fn == [], "a 3-day-old TODO is not stale"
    # the age is the line's, not the file's: a TODO added recently to an old file is not stale
    commit(old, {"a.py": "X = 1\n# TODO: drop this shim\n# FIXME: and this one\n"}, _iso(1))
    ages = {x["line"]: x["age_days"] for x in quality.comments(None, path=old)["fact"]["findings"]
            if x["kind"] == "stale_todo"}
    assert set(ages) == {2}


def test_patch_applies_with_git_apply_and_clears_the_mechanical_findings(tmp_path):
    files = {}
    for kind in ("commented_out_code", "restates_code", "banner", "boilerplate"):
        for path, text in CASES[kind][0].items():
            files["%s/%s" % (kind, path)] = text
    repo = make_repo(tmp_path / "r", files)
    out = tmp_path / "fix.diff"
    res = CliRunner().invoke(main, ["quality", "comments", "--path", str(repo), "--patch", str(out)])
    assert res.exit_code == 1, res.output
    git("apply", "--check", str(out), cwd=repo)
    git("apply", str(out), cwd=repo)
    after = quality.comments(None, path=repo)["fact"]
    left = [x for x in after["findings"] if x["kind"] in ("commented_out_code", "restates_code", "banner", "boilerplate")]
    assert left == [], left
    assert "Copyright 2024 SoC Labs" in (repo / "boilerplate" / "tool.py").read_text(), "licence lines are kept"
    kept = out.read_text()
    again = CliRunner().invoke(main, ["quality", "comments", "--path", str(repo), "--patch", str(out)])
    assert again.exit_code == 3 and out.read_text() == kept, "an existing patch file is never overwritten"


def test_tools_never_write_to_the_repository(tmp_path):
    repo = make_repo(tmp_path / "r", CASES["banner"][0])
    before = git("status", "--porcelain", "--ignored", cwd=repo).stdout
    snap = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    for kind in ("density", "comments", "readme", "docs"):
        getattr(quality, kind)(None, path=repo)
    assert git("status", "--porcelain", "--ignored", cwd=repo).stdout == before
    assert snap == {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_cli_json_one_finding_per_line(tmp_path):
    repo = make_repo(tmp_path / "r", CASES["banner"][0])
    res = CliRunner().invoke(main, ["quality", "comments", "--path", str(repo), "--json", "--kind", "banner"])
    rows = [json.loads(x) for x in res.output.splitlines()]
    assert {r["kind"] for r in rows} == {"banner"} and len(rows) == 2
