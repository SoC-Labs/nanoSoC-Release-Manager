"""`matrix --run --wait` works only the caller's own jobs (integration review 2026-10-16).

It runs tier cells where the person runs it (Worker(run_tiers=True)). Before this, its worker claimed the oldest
queued job of ANY kind: run on the service host with the service stopped, it would have run other people's queued
test-bumps, unsandboxed, as that person. `Worker(only=ids)` claims just those ids."""
from __future__ import annotations

import pytest

from nanosoc_rm import jobs
from nanosoc_rm.config import load_config

pytestmark = [pytest.mark.requirement("D3"), pytest.mark.requirement("D1")]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "data"))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def test_a_worker_given_ids_claims_only_those(cfg):
    other = jobs.submit(cfg, "walk", {"configuration": "someone-elses"}, "bob")
    mine = jobs.submit(cfg, "walk", {"configuration": "mine"}, "ann")
    w = jobs.Worker(cfg.path, run_tiers=True, only=[mine["id"]])
    got = w._claim(cfg)
    assert got["id"] == mine["id"] and got["state"] == "running"
    assert w._claim(cfg) is None                                  # bob's older job is never claimed
    assert jobs.get(cfg, other["id"])["state"] == "queued"
    assert jobs.Worker(cfg.path)._claim(cfg)["id"] == other["id"]    # the service's own worker still takes it


def test_matrix_wait_hands_its_worker_only_the_cells_it_queued(cfg, monkeypatch):
    from nanosoc_rm.matrix import commands as MC
    seen = {}

    class Spy(jobs.Worker):
        def __init__(self, *a, **k):
            seen.update(k)
            super().__init__(*a, **k)
    monkeypatch.setattr(jobs, "Worker", Spy)
    a = jobs.submit(cfg, "walk", {"configuration": "c"}, "ann")
    jobs._update(cfg, a["id"], state="done")                      # already finished: _wait returns at once
    MC._wait([a["id"]], cfg)
    assert seen.get("run_tiers") is True and list(seen.get("only") or []) == [a["id"]]
