"""nanosoc_rm.cli: section 0.2 exit codes, --json lines, the plug-in seam, and `alerts`."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import click
import pytest
from click.testing import CliRunner

from nanosoc_rm import cli, db
from nanosoc_rm.cliutil import emit, subgroup, table
from nanosoc_rm.config import ConfigError


def invoke(*args, **kw):
    return CliRunner().invoke(cli.main, list(args), **kw)


def test_console_script_is_on_path():
    exe = shutil.which("nanosoc-rm")
    assert exe, "nanosoc-rm is not on PATH: run scripts/dev-env.sh and put .venv/bin first"
    p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "Commands:" in p.stdout and "  alerts " in p.stdout
    assert subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120).stdout.strip().endswith(
        "0.1.0.dev0")


def test_usage_and_config_errors_exit_3(tmp_path, monkeypatch):
    assert invoke("no-such-command").exit_code == 3
    assert invoke("alerts", "--no-such-option").exit_code == 3
    assert invoke("alerts", "clear", "alert:x:y").exit_code == 3                  # --by is required
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    r = invoke("alerts")
    assert r.exit_code == 3 and "NANOSOC_RM_CONFIG" in r.output
    bad = tmp_path / "bad.toml"
    bad.write_text("[service]\ndata_dir = 7\n")
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(bad))
    assert invoke("alerts").exit_code == 3


def _group_with(fn):
    @click.group(cls=cli.RMGroup)
    def g():
        pass

    g.command("x")(fn)
    return g


def test_return_value_is_the_exit_code_and_a_crash_is_2():
    for code in (0, 1, 2, 3):
        g = _group_with(lambda code=code: code)
        assert CliRunner().invoke(g, ["x"]).exit_code == code
    assert CliRunner().invoke(_group_with(lambda: None), ["x"]).exit_code == 0

    def boom():
        raise RuntimeError("bug")
    r = CliRunner().invoke(_group_with(boom), ["x"])
    assert r.exit_code == 2 and "internal error" in r.output                # never 1 (a finding) or 0

    def cfg():
        raise ConfigError("[ci] project_id is required")
    assert CliRunner().invoke(_group_with(cfg), ["x"]).exit_code == 3

    def broken_registry():                         # integration: a registry that does not load is missing data
        from nanosoc_rm.registry import Problem, RegistryError
        raise RegistryError([Problem("eligibility.yaml", "/configurations/0/component", "rule-2", "planted")])
    r = CliRunner().invoke(_group_with(broken_registry), ["x"])
    assert r.exit_code == 2 and "registry does not load" in r.output and "planted [rule-2]" in r.output
    assert "internal error" not in r.output and "Traceback" not in r.output


def test_emit_json_lines_and_table(capsys):
    docs = [{"subject": "a:1", "observed_at": "2026-10-01T00:00:00Z"}, {"subject": "a:2", "observed_at": "x"}]
    emit(docs, as_json=True)
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in out] == docs
    emit(docs, as_json=False)
    assert capsys.readouterr().out.splitlines()[0].split() == ["SUBJECT", "OBSERVED_AT"]
    assert table([], ["a"]) == "A"


def test_plugin_seam(tmp_path, monkeypatch):
    pkg = tmp_path / "fakerm"
    (pkg / "withcmds").mkdir(parents=True)
    (pkg / "nocmds").mkdir()
    (pkg / "shared").mkdir()
    (pkg / "broken").mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "withcmds" / "__init__.py").write_text("")
    (pkg / "withcmds" / "commands.py").write_text(
        "import click\nfrom nanosoc_rm.cliutil import subgroup\n"
        "def register(main):\n"
        "    @main.command('walk')\n    def walk():\n        return 1\n"
        "    subgroup(main, 't0').command('trigger')(lambda: 0)\n")
    (pkg / "shared" / "__init__.py").write_text("")
    (pkg / "shared" / "commands.py").write_text(
        "from nanosoc_rm.cliutil import subgroup\n"
        "def register(main):\n    subgroup(main, 't0').command('run')(lambda: 2)\n")
    (pkg / "plain.py").write_text(
        "def register(main):\n    main.command('plain')(lambda: 0)\n")
    (pkg / "quiet.py").write_text("X = 1\n")                                # a module with no commands
    (pkg / "nocmds" / "__init__.py").write_text("")
    (pkg / "broken" / "__init__.py").write_text("")
    (pkg / "broken" / "commands.py").write_text("import nanosoc_rm.not_built_sibling\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    @click.group(cls=cli.RMGroup)
    def g():
        pass

    loaded = cli.load_plugins(g, ["withcmds", "shared", "plain", "quiet", "nocmds", "absent"], package="fakerm")
    assert loaded == ["withcmds", "shared", "plain"]
    assert sorted(g.commands) == ["plain", "t0", "walk"]
    assert sorted(g.commands["t0"].commands) == ["run", "trigger"]            # one group, two modules
    assert CliRunner().invoke(g, ["walk"]).exit_code == 1
    assert CliRunner().invoke(g, ["t0", "run"]).exit_code == 2
    # A module that exists but cannot import is an error, never silently skipped.
    with pytest.raises(ModuleNotFoundError, match="not_built_sibling"):
        cli.load_plugins(g, ["broken"], package="fakerm")
    with pytest.raises(RuntimeError):
        subgroup(g, "walk")


def test_real_main_registers_no_later_phase_command():
    # AT-A7: no Phase 2-5 command before its entry gate.
    assert not {"matrix", "release", "changelog", "bump", "lock", "forks"} & set(cli.main.commands)
    assert "lock" not in cli.PLUGIN_MODULES
    # In a fresh interpreter: in the merged suite other tests import the runner into this process (AT-A4).
    probe = subprocess.run([sys.executable, "-c", "import sys, nanosoc_rm.cli, nanosoc_rm.web; "
                            "sys.exit(1 if 'nanosoc_rm.t0.runner' in sys.modules else 0)"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


def test_alerts_command(rm_config):
    src = [{"kind": "derived", "command": "test"}]
    a = db.raise_alert("tag_moved", "eth-rc4", "v1.0.0 moved", src, at="2026-10-01T02:00:00Z")
    r = invoke("alerts", "--json")
    assert r.exit_code == 1, r.output                                          # an open alert is a finding
    assert [json.loads(line)["subject"] for line in r.output.splitlines()] == [a["subject"]]
    assert invoke("alerts", "clear", a["subject"], "--by", "alice").exit_code == 0
    assert invoke("alerts", "--json").output == "" and invoke("alerts").exit_code == 0
    r = invoke("alerts", "--all", "--json")
    assert json.loads(r.output)["fact"]["cleared_by"] == "alice" and r.exit_code == 0
    assert invoke("alerts", "clear", "alert:rewrite:nothing", "--by", "alice").exit_code == 3
    assert [x["action"] for x in db.actions()] == ["clear_alert"]
