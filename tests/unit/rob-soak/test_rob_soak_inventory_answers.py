"""Robustness (soak angle): what the inventory says when a host listing fails.

Found in a read-only soak on 2026-10-14. The first `nanosoc-rm inventory` of the soak exited 2 with:
    gitlab listing failed at GET groups/soclabs/projects?include_subgroups=true&per_page=100: HTTP 200
Two minutes later the same command listed every project (exit 1, unclassified rows). The host had answered 200
with a body that was not a JSON list, and the message reports a success status as the failure. The walk has the
same gap: "GET .../merge_requests answered 200" (walker/states.py `_ask_host`).

Hermetic: `nanosoc_rm.mirror.inventory.api_get` is replaced with a stub.
"""
from __future__ import annotations

import importlib

import pytest

from nanosoc_rm.config import load_config
from nanosoc_rm.mirror.layout import ApiAnswer

inv = importlib.import_module("nanosoc_rm.mirror.inventory")   # the package re-exports a function of this name


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text("\n".join([
        "[service]", 'data_dir = "%s"' % (tmp_path / "data"),
        "[hosts.gitlab]", 'web = "https://gitlab.example.invalid"', 'api = "https://gitlab.example.invalid/api/v4"',
        'ssh = "git@gitlab.example.invalid:"', 'group = "soclabs"', ""]))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config()


def _answer(monkeypatch, status, body, headers=None):
    def fake(config, host, path, params=None, *, url=None, client=None):
        return ApiAnswer(status, body, headers or {}, {"kind": "gitlab_api", "command": "GET %s" % path})
    monkeypatch.setattr(inv, "api_get", fake)


def test_listing_of_one_page_is_read(cfg, monkeypatch):
    """Control: a 200 JSON list with no X-Next-Page is the whole listing."""
    _answer(monkeypatch, 200, [{"path_with_namespace": "soclabs/a"}, {"path_with_namespace": "soclabs/b"}])
    assert [i["path_with_namespace"] for i, _ in inv.list_host(cfg, "gitlab")] == ["soclabs/a", "soclabs/b"]


def test_listing_server_error_names_the_status(cfg, monkeypatch):
    """Control: a 502 says HTTP 502."""
    _answer(monkeypatch, 502, None)
    with pytest.raises(inv.InventoryError, match="HTTP 502"):
        inv.list_host(cfg, "gitlab")


def test_listing_200_not_a_list_says_what_was_wrong(cfg, monkeypatch):
    """ROB-soak-6 (fixed): a 200 whose body is not a JSON list used to read 'listing failed ...: HTTP 200'."""
    _answer(monkeypatch, 200, None, {"content-type": "text/html"})
    with pytest.raises(inv.InventoryError) as e:
        inv.list_host(cfg, "gitlab")
    assert not str(e.value).endswith("HTTP 200"), str(e.value)
