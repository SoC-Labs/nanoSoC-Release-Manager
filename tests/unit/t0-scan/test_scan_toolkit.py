"""nanosoc_rm.scan.toolkit: the toolkit scanner as the second engine (interfaces.md section 11, GATE2 Q4).

The real toolkit scanner is private, so these tests drive a FAKE with the same interface: `--rev REV --fast`,
its own rows in $CI_VERDICT_DIR/vendor/verdicts.tsv, findings in $VENDOR_CHECK_FINDINGS_OUT as
`rule <TAB> path <TAB> line <TAB> why`, exit 0 clean and 1 on a finding or an unverifiable run. Its mode comes
from lib.sh beside it, because the engine hands the scanner an environment built from nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nanosoc_rm.gitrun import run_git
from nanosoc_rm.scan import scan_dir, scan_tree
from nanosoc_rm.scan.rules import RulesError, specimen
from nanosoc_rm.scan.toolkit import TOOLKIT_ENV, toolkit

SECRET_WHY = "SECRET-WHY-TEXT"           # the fake's `why` column: must never come back
VALUE = "FAKE-FOUNDRY-VALUE"            # the fake's value-shaped rule fires on this token

FAKE = r"""#!/usr/bin/env bash
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/lib.sh"
rev=""
while [ $# -gt 0 ]; do case "$1" in --rev) rev="$2"; shift ;; esac; shift; done
mkdir -p "$CI_VERDICT_DIR/vendor"
v="$CI_VERDICT_DIR/vendor/verdicts.tsv"
rec() { printf '2026-01-01T00:00:00Z\t%s\t%s\t%s\n' "$1" "$2" "$3" >> "$v"; }
[ -n "${ENVDUMP:-}" ] && env > "$ENVDUMP"
case "$MODE" in
    unarmed) rec UNVERIFIED vendor.arm "a rule did not fire on its specimen"; exit 1 ;;
    crash)   exit 2 ;;
esac
rec PASS vendor.arm "fake rules armed"
git rev-parse --show-toplevel >/dev/null 2>&1 || { rec UNVERIFIED vendor.corpus "not a work tree"; exit 1; }
: > "$VENDOR_CHECK_FINDINGS_OUT"
n=0
allow=""
[ -n "${VC_PROJECT_ALLOWLIST:-}" ] && allow="$(cat "$VC_PROJECT_ALLOWLIST")"
while IFS= read -r -d '' path; do
    n=$((n + 1))
    printf '%s\n' "$allow" | grep -qxF "$path" && continue          # the project waives this path
    case "$path" in *.lef) printf 'file.ext\t%s\t0\t%s\n' "$path" "SECRET-WHY-TEXT" >> "$VENDOR_CHECK_FINDINGS_OUT" ;; esac
    git cat-file -p "$rev:$path" | grep -n 'FAKE-FOUNDRY-VALUE' | cut -d: -f1 | while read -r ln; do
        printf 'value.fake\t%s\t%s\t%s\n' "$path" "$ln" "SECRET-WHY-TEXT" >> "$VENDOR_CHECK_FINDINGS_OUT"
    done
done < <(git ls-tree -r -z --name-only "$rev")
[ "$n" -gt 0 ] || { rec UNVERIFIED vendor.corpus.rev "0 files"; exit 1; }
rec PASS vendor.corpus.rev "$n file(s)"
[ -s "$VENDOR_CHECK_FINDINGS_OUT" ] && exit 1
exit 0
"""


def fake_toolkit(d: Path, mode: str = "normal", envdump: Path | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "check-vendor-collateral.sh").write_text(FAKE)
    (d / "lib.sh").write_text("MODE=%s\n%s" % (mode, "ENVDUMP=%s\n" % envdump if envdump else ""))
    return d / "check-vendor-collateral.sh"


def bundle(d: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def listing(d: Path) -> list[str]:
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))


def test_no_toolkit_unless_named(monkeypatch):
    monkeypatch.delenv(TOOLKIT_ENV, raising=False)
    assert toolkit() is None


def test_a_copy_without_lib_sh_or_a_missing_script_is_refused(tmp_path):
    s = fake_toolkit(tmp_path / "tk")
    (tmp_path / "tk" / "lib.sh").unlink()
    with pytest.raises(RulesError, match="lib.sh"):
        toolkit(s)
    with pytest.raises(RulesError, match="not a file"):
        toolkit(tmp_path / "absent.sh")


def test_toolkit_findings_are_merged_as_locations_only_and_the_bundle_is_untouched(tmp_path):
    tk = toolkit(fake_toolkit(tmp_path / "tk"))
    b = bundle(tmp_path / "b", {"logs/t0.log": "one\n%s here\n" % VALUE, "cells.lef": "x\n", "ok.json": "{}\n"})
    before = listing(b)
    r = scan_dir(b, toolkit=tk)
    assert r.verdict == "finding" and r.armed == {"arm.banner": True, "toolkit": True}
    assert sorted(r.findings, key=lambda f: f["file"]) == [
        {"rule": "file.ext", "file": "cells.lef", "line": None},          # line 0: a file-shaped finding
        {"rule": "value.fake", "file": "logs/t0.log", "line": 2}]
    assert sorted(r.flagged_files) == ["cells.lef", "logs/t0.log"]
    doc = json.dumps(r.as_doc())
    assert SECRET_WHY not in doc and VALUE not in doc
    assert tk.sha256[:12] in r.tool
    assert listing(b) == before                                            # no .git, nothing written


def test_a_clean_bundle_is_clean_with_both_engines_armed(tmp_path):
    r = scan_dir(bundle(tmp_path / "b", {"a.txt": "nothing here\n"}), toolkit=fake_toolkit(tmp_path / "tk"))
    assert (r.verdict, r.findings, r.armed) == ("clean", [], {"arm.banner": True, "toolkit": True})


@pytest.mark.parametrize("mode, why", [("unarmed", "did not arm"), ("crash", "did not arm")])
def test_a_toolkit_that_cannot_vouch_makes_the_scan_unverified(tmp_path, mode, why):
    r = scan_dir(bundle(tmp_path / "b", {"cells.lef": "x\n"}), toolkit=fake_toolkit(tmp_path / "tk", mode))
    assert r.verdict == "unverified" and r.armed["toolkit"] is False and why in r.why


def test_the_native_finding_still_counts_when_the_toolkit_is_clean(tmp_path):
    r = scan_dir(bundle(tmp_path / "b", {"logs/t0.log": "a\n" + specimen("arm.banner")}),
                 toolkit=fake_toolkit(tmp_path / "tk"))
    assert r.verdict == "finding" and r.findings == [{"rule": "arm.banner", "file": "logs/t0.log", "line": 2}]


def test_the_env_names_the_toolkit_and_a_broken_one_is_unverified_not_a_crash(tmp_path, monkeypatch):
    b = bundle(tmp_path / "b", {"cells.lef": "x\n"})
    monkeypatch.setenv(TOOLKIT_ENV, str(fake_toolkit(tmp_path / "tk")))
    assert scan_dir(b).verdict == "finding"
    monkeypatch.setenv(TOOLKIT_ENV, str(tmp_path / "absent.sh"))
    r = scan_dir(b)
    assert r.verdict == "unverified" and "not a file" in r.why
    assert scan_dir(b, toolkit=None).verdict == "clean"                    # explicitly none: native only


def test_the_toolkit_gets_an_environment_built_from_nothing(tmp_path, monkeypatch):
    dump = tmp_path / "env.txt"
    monkeypatch.setenv("CI_JOB_TOKEN", "must-not-reach-the-scanner")
    monkeypatch.setenv("VC_SIZE_LIMIT", "999999999")                       # would weaken file.size if inherited
    monkeypatch.setenv("VENDOR_CHECK_BYPASS", "1")
    scan_dir(bundle(tmp_path / "b", {"a": "a\n"}), toolkit=fake_toolkit(tmp_path / "tk", envdump=dump))
    env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert "CI_JOB_TOKEN" not in env and "VC_SIZE_LIMIT" not in env and "VENDOR_CHECK_BYPASS" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] != str(Path.home()) and not Path(env["HOME"]).exists()   # private, and deleted after


def test_scan_tree_runs_the_toolkit_in_place_with_the_prefix(tmp_path):
    repo = tmp_path / "fetched"
    run_git("init", "-q", repo).check()
    (repo / "rtl").mkdir()
    (repo / "rtl" / "x.v").write_text("// %s\n" % VALUE)
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@invalid"}
    run_git("add", "-A", cwd=repo).check()
    run_git("commit", "-q", "-m", "c", cwd=repo, env=ident).check()
    sha = run_git("rev-parse", "HEAD", cwd=repo).check().stdout.strip()
    r = scan_tree(repo, sha, prefix="sub/a", toolkit=fake_toolkit(tmp_path / "tk"))
    assert r.verdict == "finding" and r.findings == [{"rule": "value.fake", "file": "sub/a/rtl/x.v", "line": 1}]


def _committed_repo(d: Path, files: dict[str, str]) -> tuple[Path, str]:
    run_git("init", "-q", d).check()
    bundle(d, files)
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@invalid"}
    run_git("add", "-A", cwd=d).check()
    run_git("commit", "-q", "-m", "c", cwd=d, env=ident).check()
    return d, run_git("rev-parse", "HEAD", cwd=d).check().stdout.strip()


def test_a_tree_s_own_vendor_allowlist_applies_but_a_bundle_never_waives_itself(tmp_path):
    tk = fake_toolkit(tmp_path / "tk")
    repo, sha = _committed_repo(tmp_path / "fetched", {"cells.lef": "x\n", "VENDOR_ALLOWLIST": "cells.lef\n"})
    assert scan_tree(repo, sha, toolkit=tk).verdict == "clean"            # the project reviewed it
    b = bundle(tmp_path / "b", {"cells.lef": "x\n", "VENDOR_ALLOWLIST": "cells.lef\n"})
    r = scan_dir(b, toolkit=tk)
    assert r.verdict == "finding" and {"rule": "file.ext", "file": "cells.lef", "line": None} in r.findings


def test_finalise_bundle_deletes_what_the_toolkit_flags(at, tmp_path, runner_module):
    b = at.write_bundle(tmp_path / "job", at.example_provenance(), at.example_verdicts(),
                        {"logs/t0.log": "x\n%s\n" % VALUE, "items/t0.pins.fetchable.jsonl": ""})
    res = runner_module.finalise_bundle(b, toolkit=toolkit(fake_toolkit(tmp_path / "tk")))
    assert res.verdict == "finding" and not (b / "logs" / "t0.log").exists()
    assert (b / "verdicts.tsv").exists()
    scan = json.loads((b / "provenance.json").read_text())["scan"]
    assert scan["findings"] == [{"rule": "value.fake", "file": "logs/t0.log", "line": 2}]
    assert VALUE not in json.dumps(scan) and SECRET_WHY not in json.dumps(scan)


def test_finalise_bundle_keeps_only_provenance_when_the_toolkit_cannot_vouch(at, tmp_path, runner_module):
    b = at.write_bundle(tmp_path / "job", at.example_provenance(), at.example_verdicts(), {"logs/t0.log": "x\n"})
    res = runner_module.finalise_bundle(b, toolkit=toolkit(fake_toolkit(tmp_path / "tk", "unarmed")))
    assert res.verdict == "unverified"
    assert sorted(p.name for p in b.rglob("*") if p.is_file()) == ["provenance.json"]
    assert json.loads((b / "provenance.json").read_text())["scan"]["verdict"] == "unverified"


def test_finalise_bundle_with_an_unusable_toolkit_keeps_only_provenance(at, tmp_path, monkeypatch, runner_module):
    b = at.write_bundle(tmp_path / "job", at.example_provenance(), at.example_verdicts(), {"logs/t0.log": "x\n"})
    monkeypatch.setenv(TOOLKIT_ENV, str(tmp_path / "absent.sh"))
    res = runner_module.finalise_bundle(b)
    assert res.verdict == "unverified" and res.files_scanned >= 3
    assert not (b / "verdicts.tsv").exists() and not (b / "logs" / "t0.log").exists()
    assert json.loads((b / "provenance.json").read_text())["scan"]["verdict"] == "unverified"


def test_vendor_gate_uses_the_toolkit_and_a_broken_one_is_unverified(at, tmp_path, monkeypatch):
    from nanosoc_rm.t0 import gates
    bare = tmp_path / "rm-tk.git"
    sha = at.fx_commit(bare, files={"lib/cells.lef": "x\n", "rtl/a.v": "module a; endmodule\n"}, ref="refs/heads/main")
    req = {"configuration": "rm-tk", "repo": {"host": "github", "path": "SoC-Labs/rm-tk"}, "commit": sha,
           "upgrade_from": None, "canonical_urls": {}}
    ctx = gates.Context.for_tests(nonce="3" * 32, workdir=tmp_path / "w", request=req,
                                  url_map={"https://github.com/SoC-Labs/rm-tk.git": str(bare)})
    monkeypatch.setenv(TOOLKIT_ENV, str(fake_toolkit(tmp_path / "tk")))
    status, detail = gates.GATES["t0.vendor.no-arm-ip"].check(ctx)
    assert status == "FAIL" and "1 with findings" in detail, detail
    (item,) = ctx.items("t0.vendor.no-arm-ip")
    assert item["tree_path"] == "." and "file.ext at lib/cells.lef:-" in item["why"]
    monkeypatch.setenv(TOOLKIT_ENV, str(tmp_path / "absent.sh"))
    status, detail = gates.GATES["t0.vendor.no-arm-ip"].check(ctx)
    assert status == "UNVERIFIED" and "not a file" in detail


def test_cli_scan_bundle_toolkit_option(tmp_path):
    from nanosoc_rm.cli import main
    b = bundle(tmp_path / "b", {"cells.lef": "x\n"})
    run = lambda *a: CliRunner().invoke(main, list(a), standalone_mode=False)
    res = run("scan", "bundle", str(b), "--toolkit", str(fake_toolkit(tmp_path / "tk")), "--json")
    assert res.return_value == 1 and json.loads(res.output)["armed"]["toolkit"] is True
    assert run("scan", "bundle", str(b), "--toolkit", str(tmp_path / "absent.sh")).return_value == 3
