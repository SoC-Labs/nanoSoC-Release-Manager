"""analysis.detect.run (F2, interfaces.md section 6a): pattern and command detectors, input identity, exits."""
from __future__ import annotations

import hashlib
import json
import tarfile

import pytest
import yaml
from click.testing import CliRunner

from nanosoc_rm import cli, contracts
from nanosoc_rm.analysis import UsageProblem
from nanosoc_rm.analysis.detect import DetectorError, input_md5, md5_manifest, run

from analysis_fixtures import git, load_at, route_hosts, write_config

CI = load_at("test_ci_errata")


def cli_run(*args):
    r = CliRunner().invoke(cli.main, list(args))
    return r.exit_code, [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")], r


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return write_config(tmp_path, monkeypatch)


def test_pattern_detector_fires_and_stays_clean(cfg, tmp_path):
    """AT-F2 control's detector half: SL-DEF-0001 D2 on the AT's own synthetic netlists."""
    fired, clean = CI.synthetic_netlists(tmp_path / "netlists")
    hit, miss = run("SL-DEF-0001", "D2", fired), run("SL-DEF-0001", "D2", clean)
    assert (hit.result, hit.forms) == ("fired", ["A"])
    assert (miss.result, miss.forms) == ("clean", [])
    assert hit.input["md5"] == hashlib.md5(fired.read_bytes()).hexdigest() != miss.input["md5"]
    assert hit.detector_source is None                                         # a pattern lives in the record
    assert hit.command == "nanosoc-rm detect SL-DEF-0001 D2 --input %s" % fired
    for r in (hit, miss):
        assert contracts.validate_detector_run(r.as_doc()) == []
    assert "fired.v:2" in hit.log


def test_directory_and_archive_inputs(cfg, tmp_path):
    out = tmp_path / "build_soc"
    (out / "rtl" / "sub").mkdir(parents=True)
    (out / "rtl" / "top.v").write_text("module top; cpu_ss u (.cpu_ss_hsel(cpu_ss_hsel_w)); endmodule\n")
    (out / "rtl" / "sub" / "net.v").write_text("x .cpu_ss_hsel(UNCONNECTED_3) y\n")
    manifest = md5_manifest(out)
    assert manifest.splitlines()[0].endswith("  rtl/sub/net.v") and len(manifest.splitlines()) == 2
    assert input_md5(out) == hashlib.md5(manifest.encode()).hexdigest()
    assert run("SL-DEF-0001", "D2", out).result == "fired"
    tar = tmp_path / "build_soc.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        t.add(out, arcname="build_soc")
    r = run("SL-DEF-0001", "D2", tar)
    assert (r.result, r.input["md5"]) == ("fired", hashlib.md5(tar.read_bytes()).hexdigest())
    (out / "rtl" / "sub" / "net.v").write_text("x .cpu_ss_hsel(hsel_w) y\n")
    assert run("SL-DEF-0001", "D2", out).result == "clean"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DetectorError, match="no file"):                     # a scan of nothing proves nothing
        run("SL-DEF-0001", "D2", empty)


def test_unknown_defect_detector_or_input(cfg, tmp_path):
    with pytest.raises(UsageProblem, match="has no detector D9"):
        run("SL-DEF-0001", "D9", tmp_path)
    with pytest.raises(UsageProblem, match="unknown defect"):
        run("SL-DEF-9999", "D1", tmp_path)
    with pytest.raises(UsageProblem, match="does not exist"):
        run("SL-DEF-0001", "D2", tmp_path / "nope.v")


# ---------------------------------------------------------------------------------------------------------
# Command detectors: the source is fetched at its pinned commit from the canonical host (routed to a local
# bare repo by url.insteadOf), and run as `python -m <module> {dir}`.
# ---------------------------------------------------------------------------------------------------------
LINT = '''import sys
from pathlib import Path
bad = [p for p in Path(sys.argv[1]).rglob("*.v") if "UNCONNECTED" in p.read_text()]
print("lint: %d finding(s)" % len(bad))
sys.exit(1 if bad else 0)
'''
CRASH = "import no_such_module_anywhere\n"


def publish_detector(tmp_path, monkeypatch, files):
    """A nanosoc_gen commit holding `files`, on the (local) GitLab host; returns its SHA."""
    hosts = route_hosts(tmp_path, monkeypatch)
    work = tmp_path / "gen-work"
    git("init", "--quiet", work)
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    git("-C", work, "add", "-A")
    git("-C", work, "commit", "--quiet", "-m", "detector")
    sha = git("-C", work, "rev-parse", "HEAD")
    bare = hosts / "gitlab" / "soclabs" / "nanosoc_gen.git"
    git("clone", "--quiet", "--bare", work, bare)
    git("--git-dir", bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return sha


def point_d1_at(registry, sha, command="python -m lintpkg.check {dir}"):
    f = registry / "defects" / "SL-DEF-0001.yaml"
    doc = yaml.safe_load(f.read_text())
    d1 = next(d for d in doc["detectors"] if d["id"] == "D1")
    d1.update(command=command, source={"component": "nanosoc_gen", "commit": sha})
    f.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_command_detector_runs_at_its_pinned_source(cfg, tmp_path, monkeypatch):
    _path, _data, registry = cfg
    sha = publish_detector(tmp_path, monkeypatch, {"lintpkg/__init__.py": "", "lintpkg/check.py": LINT})
    point_d1_at(registry, sha)
    out = tmp_path / "rtl"
    out.mkdir()
    (out / "a.v").write_text(".cpu_ss_hsel(UNCONNECTED_1)\n")
    r = run("SL-DEF-0001", "D1", out)
    assert (r.result, r.forms, r.detector_source) == ("fired", ["A", "B"], {"component": "nanosoc_gen", "commit": sha})
    assert "lint: 1 finding(s)" in r.log
    assert contracts.validate_detector_run(r.as_doc()) == []
    (out / "a.v").write_text(".cpu_ss_hsel(w)\n")
    assert (run("SL-DEF-0001", "D1", out).result, run("SL-DEF-0001", "D1", out).forms) == ("clean", [])
    rc, docs, res = cli_run("detect", "SL-DEF-0001", "D1", "--input", str(out), "--json")
    assert rc == 0 and docs[0]["result"] == "clean" and contracts.validate_detector_run(docs[0]) == [], res.output


def test_a_crashing_detector_is_could_not_run_never_fired(cfg, tmp_path, monkeypatch):
    """Python exits 1 on an uncaught exception, the same code as `fired`: the wrapper keeps them apart."""
    _path, _data, registry = cfg
    sha = publish_detector(tmp_path, monkeypatch, {"lintpkg/__init__.py": "", "lintpkg/check.py": CRASH})
    point_d1_at(registry, sha)
    with pytest.raises(DetectorError, match="without an answer"):
        run("SL-DEF-0001", "D1", tmp_path)
    rc, _docs, r = cli_run("detect", "SL-DEF-0001", "D1", "--input", str(tmp_path), "--json")
    assert rc == 2, r.output
    point_d1_at(registry, sha, command="python -m lintpkg.absent {dir}")      # a module the source lacks
    with pytest.raises(DetectorError):
        run("SL-DEF-0001", "D1", tmp_path)
    point_d1_at(registry, sha, command="sh -c 'exit 1' {dir}")               # not `python -m`: refused
    with pytest.raises(DetectorError, match="only `python -m"):
        run("SL-DEF-0001", "D1", tmp_path)


def test_unfetchable_source_is_could_not_run(cfg, tmp_path, monkeypatch):
    _path, _data, registry = cfg
    publish_detector(tmp_path, monkeypatch, {"lintpkg/__init__.py": "", "lintpkg/check.py": LINT})
    point_d1_at(registry, "f" * 40)                                         # a commit the host does not have
    with pytest.raises(DetectorError, match="cannot fetch"):
        run("SL-DEF-0001", "D1", tmp_path)


def test_cli_exit_codes(cfg, tmp_path):
    fired, clean = CI.synthetic_netlists(tmp_path / "n")
    rc, docs, r = cli_run("detect", "SL-DEF-0001", "D2", "--input", str(fired), "--json")
    assert rc == 1 and docs[0]["result"] == "fired" and contracts.validate_detector_run(docs[0]) == [], r.output
    rc, docs, r = cli_run("detect", "SL-DEF-0001", "D2", "--input", str(clean), "--json")
    assert rc == 0 and docs[0]["forms"] == []
    rc, _docs, r = cli_run("detect", "SL-DEF-0001", "D9", "--input", str(clean))
    assert rc == 3 and "no detector D9" in r.output
    rc, _docs, r = cli_run("detect", "SL-DEF-0001", "D2", "--input", str(clean))
    assert rc == 0 and "RESULT" in r.stdout and "pattern in the record" in r.stdout
