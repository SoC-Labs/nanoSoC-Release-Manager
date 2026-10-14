"""Robustness (soak angle): every command that takes a CONFIGURATION treats an unknown one the same way.

Found in a read-only soak on 2026-10-14. The demo data was used, and each command was run once with the
configuration `nosuch`. Most commands exit 3 (usage) and name the problem. Three commands exit 0 and print an
empty answer instead, so a mistyped configuration looks like a clean "nothing here": `release list nosuch`,
`bumps queue nosuch` and `matrix quarantine nosuch`. `matrix nightly nosuch` exits 3. `compat nosuch` exits 3,
but its message is only "nanosoc-rm: nosuch".

The contract is cli.py's module docstring and interfaces.md section 0.2: exit 3 means a usage or config error,
and an unknown configuration is a usage error (analysis/commands.py names it). Hermetic: the registry is the
contract examples (four configurations: eth-rc4, eth-main, multicore-master, quickstart-main), with an empty
data directory. Nothing is fetched. Each command below fails on the name before it reads anything else.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from nanosoc_rm.cli import main

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"


@pytest.fixture
def registry_config(tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    for src, dst in (("component-catalogue.example.yaml", "catalogue.yaml"),
                     ("eligibility.example.yaml", "eligibility.yaml"),
                     ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
                     ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
                     ("import-ledger.example.yaml", "imports.yaml")):
        (reg / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, reg / dst)
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "rm.toml"
    cfg.write_text("\n".join([
        "[service]", 'data_dir = "%s"' % data, "[registry]", 'path = "%s"' % reg,
        "[hosts.github]", 'web = "https://github.com"', 'api = "https://api.github.com.invalid"',
        'ssh = "git@github.com:"', 'org = "SoC-Labs"',
        "[hosts.gitlab]", 'web = "https://gitlab.example.invalid"', 'api = "https://gitlab.example.invalid/api/v4"',
        'ssh = "git@gitlab.example.invalid:"', 'group = "soclabs"', ""]))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    return cfg


def _run(*args):
    return CliRunner().invoke(main, list(args))


# Controls: these commands already answer exit 3 for an unknown configuration (the soak saw each of them).
CONSISTENT = [("walk", "nosuch"), ("pins", "nosuch"), ("silicon-protection", "nosuch"),
              ("matrix", "show", "nosuch"), ("matrix", "nightly", "nosuch"), ("bumps", "scan", "nosuch"),
              ("release", "draft", "nosuch", "1.0.0"), ("compat", "nosuch")]


@pytest.mark.parametrize("argv", CONSISTENT, ids=lambda a: " ".join(a))
def test_unknown_configuration_is_usage(registry_config, argv):
    r = _run(*argv)
    assert r.exit_code == 3, (r.exit_code, r.output)
    assert "nosuch" in r.output


def test_known_configuration_is_not_usage(registry_config):
    """Control for the control: the same commands with a registered name do not exit 3 on the name."""
    for argv in (("matrix", "quarantine", "eth-rc4"), ("bumps", "queue", "eth-rc4"), ("release", "list", "eth-rc4")):
        r = _run(*argv)
        assert r.exit_code != 3, (argv, r.exit_code, r.output)


@pytest.mark.parametrize("argv", [("release", "list", "nosuch"), ("bumps", "queue", "nosuch"),
                                  ("matrix", "quarantine", "nosuch")], ids=lambda a: " ".join(a))
def test_unknown_configuration_is_usage_everywhere(registry_config, argv):
    """ROB-soak-4 (fixed): a listing of nothing for a name the registry does not know is a usage error."""
    r = _run(*argv)
    assert r.exit_code == 3, (r.exit_code, r.output)
    assert "nosuch" in r.output, r.output


def test_compat_unknown_configuration_says_why(registry_config):
    """ROB-soak-5 (fixed): `compat nosuch` used to print only 'nanosoc-rm: nosuch'."""
    r = _run("compat", "nosuch")
    assert r.exit_code == 3
    msg = r.output.strip().splitlines()[-1]
    assert msg != "nanosoc-rm: nosuch" and ("registered" in msg or "unknown" in msg), msg


# ------------------------------------------------------------------------------------------------------------
# The command each "no mirror" message tells the person to run must itself be a valid command line. The soak found
# two spellings: `branches` advises `mirror sync --component X`, which is valid. `version tags|next`,
# `changelog lint` and `quality` advise `mirror sync X`, and `mirror sync` takes no argument.
def _advised_sync(output: str) -> list[str]:
    import re
    m = re.search(r"`nanosoc-rm (mirror sync[^`]*)`", output)
    assert m, output
    return m.group(1).split()


def _parses(argv: list[str]) -> bool:
    import click
    sync = main.commands["mirror"].commands["sync"]
    try:
        sync.make_context("sync", argv[2:])      # parse only: nothing is fetched
    except click.UsageError:
        return False
    return True


def test_branches_no_mirror_hint_is_a_valid_command(registry_config):
    r = _run("branches", "ahb_qspi")
    assert r.exit_code == 2, r.output
    assert _parses(_advised_sync(r.output)), r.output


@pytest.mark.parametrize("argv", [("version", "tags", "ahb_qspi"), ("version", "next", "ahb_qspi"),
                                  ("changelog", "lint", "ahb_qspi")], ids=lambda a: " ".join(a))
def test_versioning_no_mirror_hint_is_a_valid_command(registry_config, argv):
    """ROB-soak-7 (fixed): the hint used to say `mirror sync <component>`, which click refuses."""
    r = _run(*argv)
    assert r.exit_code == 2, r.output
    assert _parses(_advised_sync(r.output)), r.output


def test_a_retired_configuration_with_stored_drafts_still_lists_them(registry_config, monkeypatch):
    """The bound of ROB-soak-4: the name check runs only when the listing is empty, so drafts kept for a
    configuration the registry no longer has are still listed, exit 0 (review: no test held this)."""
    from nanosoc_rm import releases
    row = {"configuration": "retired-x", "version": "1.0.0", "dir": "releases/retired-x/1.0.0",
           "doc": {"fact": {"verdict": "PASS", "ready": True}}}
    monkeypatch.setattr(releases, "drafts", lambda configuration=None, **_: [row])
    r = _run("release", "list", "retired-x")
    assert r.exit_code == 0, (r.exit_code, r.output)
    assert "retired-x" in r.output and "1.0.0" in r.output, r.output


def test_an_unreadable_registry_never_turns_a_listing_into_usage(registry_config):
    """With no registry to ask, an empty listing is still an answer (exit 0), not exit 3 (review)."""
    from nanosoc_rm.cliutil import unregistered_configuration
    from nanosoc_rm.config import load_config
    text = registry_config.read_text()
    reg = text.split('[registry]\npath = "', 1)[1].split('"', 1)[0]
    registry_config.write_text(text.replace(reg, reg + "-missing"))
    assert unregistered_configuration("nosuch", load_config(registry_config)) is None
    r = _run("release", "list", "nosuch")
    assert r.exit_code == 0, (r.exit_code, r.output)
