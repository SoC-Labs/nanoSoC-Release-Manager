"""Helpers for the registry module's unit tests: a registry directory built from the contract examples.
A module of its own, not a conftest.py: the foundation's unit tests `from conftest import ...`, which a
second conftest.py under tests/unit would shadow. Tests import the fixtures below by name."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from nanosoc_rm import contracts

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"

LAYOUT = (("component-catalogue.example.yaml", "catalogue.yaml"),
          ("eligibility.example.yaml", "eligibility.yaml"),
          ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
          ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
          ("import-ledger.example.yaml", "imports.yaml"))

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd=None):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV}, timeout=120)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def write_example_registry(root: Path) -> Path:
    for src, dst in LAYOUT:
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, root / dst)
    return root


def load(path: Path):
    return contracts.load_yaml(Path(path).read_text())


def dump(path: Path, doc) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def edit(path: Path, fn) -> None:
    doc = load(path)
    fn(doc)
    dump(path, doc)


@pytest.fixture
def example_registry(tmp_path):
    """The contract examples laid out as a registry (not a git checkout)."""
    return write_example_registry(tmp_path / "registry")


@pytest.fixture
def git_registry(tmp_path):
    """The example registry committed in its own git repo (clean)."""
    root = write_example_registry(tmp_path / "registry-git")
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "registry", cwd=root)
    return root
