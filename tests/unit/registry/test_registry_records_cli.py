"""Registry facts (registry_record observations, A3) and the `nanosoc-rm registry` commands (PR4)."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner
from registry_helpers import edit, example_registry, git, git_registry

from nanosoc_rm import cli, contracts, db
from nanosoc_rm.registry import RegistryUncommitted, load_registry, registry_records, store_records


def invoke(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args])


# ---------------------------------------------------------------------------------------------------------
# registry_record observations
# ---------------------------------------------------------------------------------------------------------
def test_records_are_valid_storable_facts(git_registry):
    reg = load_registry(git_registry)
    docs = registry_records(reg, observed_at="2026-10-02T09:00:00Z")
    kinds = [d["fact"]["record_kind"] for d in docs]
    assert kinds.count("component") == 27 and kinds.count("configuration") == 3
    assert kinds.count("defect") == 1 and kinds.count("erratum") == 1 and kinds.count("import_source") == 2
    for d in docs:
        assert contracts.validate_observation(d) == [] and db.check_observation(d) == []
        f = d["fact"]
        assert f["commit"] == reg.commit and d["observed_at"] == "2026-10-02T09:00:00Z"
        cmd = d["sources"][0]["command"]
        assert d["sources"][0]["kind"] == "registry" and " show %s:%s" % (reg.commit, f["file"]) in cmd
    rc4 = next(d for d in docs if d["subject"] == "registry_record:eligibility.yaml#/configurations/0")
    assert rc4["fact"]["id"] == "eth-rc4" and rc4["fact"]["record"] == reg.configuration("eth-rc4")
    err = next(d for d in docs if d["fact"]["record_kind"] == "erratum")
    assert err["subject"] == "registry_record:errata/eth-rc4/ERR-001.yaml#"
    # the command really reproduces the record
    cmd = err["sources"][0]["command"].split()
    shown = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    assert contracts.load_yaml(shown) == err["fact"]["record"]


def test_uncommitted_registry_has_no_facts(example_registry):
    with pytest.raises(RegistryUncommitted):
        load_registry(example_registry).records()


def test_store_records_snapshots_per_file_and_drops_deleted_files(git_registry, rm_config):
    store_records(load_registry(git_registry))
    assert len(db.latest("registry_record")) == 34
    assert len(db.latest("registry_record", "registry_record:errata/")) == 1
    shutil.rmtree(git_registry / "errata")
    edit(git_registry / "imports.yaml", lambda d: d["sources"][0]["items"][0].update(disposition="dismissed",
                                                                                     reason="not a defect"))
    git("add", "-A", cwd=git_registry)
    git("commit", "-q", "-m", "drop the erratum", cwd=git_registry)
    store_records(load_registry(git_registry))
    current = db.latest("registry_record")
    assert len(current) == 33 and not [d for d in current if d["fact"]["record_kind"] == "erratum"]
    src = next(d for d in current if d["subject"] == "registry_record:imports.yaml#/sources/0")
    assert src["fact"]["record"]["items"][0]["disposition"] == "dismissed"


# ---------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------
def test_validate_ok_and_problems(example_registry):
    r = invoke("registry", "validate", example_registry)
    assert r.exit_code == 0, r.output
    edit(example_registry / "errata/eth-rc4/ERR-001.yaml", lambda e: e.update(status="fixed"))
    r = invoke("registry", "validate", example_registry)
    assert r.exit_code == 1
    assert "errata/eth-rc4/ERR-001.yaml:/status: status fixed on silicon" in r.output and "[rule-10]" in r.output
    r = invoke("registry", "validate", example_registry, "--json")
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    assert r.exit_code == 1 and {"file": "errata/eth-rc4/ERR-001.yaml", "pointer": "/status", "rule": "rule-10"}.items() \
        <= lines[0].items()


def test_validate_reads_the_configured_path(example_registry, tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[registry]\npath = "%s"\n' % (tmp_path / "d", example_registry))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    assert invoke("registry", "validate").exit_code == 0
    monkeypatch.delenv("NANOSOC_RM_CONFIG")
    assert invoke("registry", "validate").exit_code == 3             # no PATH and no config: usage error


def test_console_script(example_registry):
    exe = shutil.which("nanosoc-rm")
    assert exe
    p = subprocess.run([exe, "registry", "validate", str(example_registry), "--json"], capture_output=True,
                       text=True, timeout=120)
    assert p.returncode == 0 and p.stdout == "", p.stderr


@pytest.fixture
def ledger_host(tmp_path, example_registry, monkeypatch):
    """The example registry whose two import sources sit on a local 'github' at one commit."""
    work = tmp_path / "eth"
    (work / "docs/tapeout").mkdir(parents=True)
    (work / "docs/tapeout/11-known-issues.md").write_text("## a) one\n## b) two\n")
    (work / "docs/tapeout/16-open-defects.md").write_text("## 1 one\n")
    git("init", "-q", "-b", "main", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "registers", cwd=work)
    sha = git("rev-parse", "HEAD", cwd=work)
    base = tmp_path / "github"
    git("clone", "-q", "--bare", work, base / "SoC-Labs" / "NanoSoC-Ethernet-Chiplet.git")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[service]\ndata_dir = "%s"\n[hosts.github]\nweb = "https://github.invalid"\ngit = "file://%s"\n'
                   % (tmp_path / "data", base))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))

    def dispose(d):
        for s in d["sources"]:
            s["doc"]["commit"] = sha
            for it in s["items"]:
                it.update(disposition="dismissed", reason="unit test")
    edit(example_registry / "imports.yaml", dispose)
    return example_registry


def test_imports_done(ledger_host):
    r = invoke("registry", "imports", ledger_host, "--json")
    rows = {d["path"]: d for d in map(json.loads, r.stdout.splitlines())}
    assert r.exit_code == 0, r.output
    assert rows["docs/tapeout/11-known-issues.md"]["census"] == ["a)", "b)"] and all(d["done"] for d in rows.values())


def test_imports_pending_or_missing_is_a_finding(ledger_host):
    edit(ledger_host / "imports.yaml", lambda d: d["sources"][0]["items"].pop())
    r = invoke("registry", "imports", ledger_host, "--json")
    rows = [json.loads(x) for x in r.stdout.splitlines()]
    assert r.exit_code == 1 and rows[0]["missing"] == ["b)"]
    edit(ledger_host / "imports.yaml", lambda d: d["sources"][1]["items"][0].update(disposition="pending")
         or d["sources"][1]["items"][0].pop("reason"))
    assert invoke("registry", "imports", ledger_host).exit_code == 1


def test_imports_unreadable_source_is_could_not_answer(ledger_host):
    edit(ledger_host / "imports.yaml", lambda d: d["sources"][1]["doc"].update(commit="3" * 40))
    r = invoke("registry", "imports", ledger_host, "--json")
    rows = [json.loads(x) for x in r.stdout.splitlines()]
    assert r.exit_code == 2 and rows[1]["error"].startswith("cannot read") and rows[0]["done"]


def test_imports_without_ledger_or_with_broken_registry(example_registry, rm_config):
    (example_registry / "imports.yaml").unlink()
    assert invoke("registry", "imports", example_registry).exit_code == 1
    edit(example_registry / "catalogue.yaml", lambda c: c["components"][0].pop("class"))
    assert invoke("registry", "imports", example_registry).exit_code == 2


def test_records_command(git_registry, rm_config, example_registry):
    r = invoke("registry", "records", git_registry, "--json", "--store")
    docs = [json.loads(x) for x in r.stdout.splitlines()]
    assert r.exit_code == 0 and len(docs) == 34 and all(d["kind"] == "registry_record" for d in docs)
    assert len(db.latest("registry_record")) == 34
    assert invoke("registry", "records", example_registry).exit_code == 2     # not a git checkout


FIXTURES = (example_registry, git_registry)      # imported for pytest; named here for pyflakes
