"""A page never fetches (integration review 2026-10-16).

The mirrors are partial clones: reading a blob they lack makes git fetch it from the host, with the read credential
when one is configured. The Capabilities pages and Home's panel read manifests through tree_view, which now reads
with fetch=False: lazy fetching off and every transport refused. The CLI (`capabilities show`) and a test-bump
still fetch."""
from __future__ import annotations

import subprocess

import pytest

from nanosoc_rm.capabilities import manifest as MF
from nanosoc_rm.config import load_config
from nanosoc_rm.mirror import layout

pytestmark = [pytest.mark.requirement("D1"), pytest.mark.requirement("H1")]

DOC = "schema: 1\ncomponent: demo\ncapabilities:\n  - {id: lint, kind: lint, tier: t1, run: make lint}\n"


def _git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


@pytest.fixture
def partial(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / ".nanosoc").mkdir(parents=True)
    (src / ".nanosoc" / "capabilities.yaml").write_text(DOC)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "x")
    _git(src, "config", "uploadpack.allowFilter", "true")
    commit = _git(src, "rev-parse", "HEAD")
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    cfg = load_config(p)
    mirror = layout.mirror_path(cfg, "demo")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--bare", "--filter=blob:none", "file://%s" % src, str(mirror))
    MF._SETTLED.clear()
    return cfg, commit, mirror


def _has_blob(mirror, commit) -> bool:
    return subprocess.run(["git", "-c", "protocol.allow=never", "cat-file", "-e", "%s:.nanosoc/capabilities.yaml"
                           % commit], cwd=mirror, env={"PATH": "/usr/bin:/bin", "GIT_NO_LAZY_FETCH": "1"},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def test_a_page_read_never_fetches_a_missing_blob(partial):
    cfg, commit, mirror = partial
    assert not _has_blob(mirror, commit)                               # the partial clone lacks the blob
    r = MF.Reader(cfg, fetch=False).at("demo", commit)
    assert r.status == "unreadable", r
    assert not _has_blob(mirror, commit), "a page read fetched the blob"
    ok = MF.Reader(cfg).at("demo", commit)                             # the control: the CLI's read fetches it
    assert ok.status == "ok" and ok.capabilities()[0]["id"] == "lint"
    assert _has_blob(mirror, commit)
    MF._SETTLED.clear()
    assert MF.Reader(cfg, fetch=False).at("demo", commit).status == "ok"   # once fetched, a page reads it


def test_tree_view_defaults_to_no_fetch_and_the_cli_fetches():
    import inspect

    from nanosoc_rm.capabilities import commands, view
    assert inspect.signature(view.tree_view).parameters["fetch"].default is False
    assert "tree_view(config, configuration, fetch=True)" in inspect.getsource(commands)
