"""Mirror and poller (interfaces.md section 3; H6, A1, B8; B6 data).

    sync_component(component_id) -> list[dict]   mirror_status per repo (+ rewrite_alert, section 8); stored
    inventory() -> list[dict]                    repo_inventory for every repo in the org and the group; stored
    detect_rewrite(mirror, key, branch, old, new) -> dict | None     section 8, key = "<host>/<quote(path, safe='')>"
    mirror_status(component_id=None) -> list[dict]                   the latest mirror_status rows, from the store

Layout and namespace helpers (`repo_key`, `ref_prefix`, `mirror_path`, `default_branch`) are shared with the
walker and the analysis module through this package.
"""
from nanosoc_rm.mirror.catalogue import Catalogue, CatalogueError, Entry, load_catalogue
from nanosoc_rm.mirror.inventory import InventoryError, inventory
from nanosoc_rm.mirror.layout import default_branch, git_env, git_url, mirror_path, ref_prefix, repo_key, split_key
from nanosoc_rm.mirror.sync import detect_rewrite, mirror_status, sync_all, sync_component

__all__ = ["sync_component", "sync_all", "inventory", "detect_rewrite", "mirror_status", "InventoryError",
           "Catalogue", "CatalogueError", "Entry", "load_catalogue", "default_branch", "git_env", "git_url",
           "mirror_path", "ref_prefix", "repo_key", "split_key"]
