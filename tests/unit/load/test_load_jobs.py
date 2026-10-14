"""Load: the local job queue (nanosoc_rm.jobs; CR-17, interfaces.md section 13a) with 200 queued jobs and a long
finished history.

Measured on <host> (2026-10-14): 200 compat submits 31.5 s (158 ms each, most of it the `run_t0` action log);
with 5000 finished jobs carrying a 20 KB result each, jobs(limit=10, configuration=...) 1.6 s and jobs(limit=50)
0.9 s, because every row is read and decoded before the limit is applied.
"""
from __future__ import annotations

import json
import time

import pytest

from nanosoc_rm import jobs
from nanosoc_rm.config import load_config

import loadfix as L


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = L.store_config(tmp_path)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    return load_config(p)


def _history(cfg, n: int, result_bytes: int = 2000) -> None:
    """`n` finished jobs (compat and matrix-cell, over two configurations), as months of Run clicks leave them."""
    res = json.dumps({"docs": ["x" * 100] * (result_bytes // 100)})
    with jobs._connect(cfg) as con:
        con.execute("BEGIN")
        for i in range(n):
            params = {"configuration": "eth-rc4" if i % 4 else "eth-main", "pins": [], "tier": None,
                      "force_tier": False}
            con.execute("INSERT INTO jobs (kind, params, state, by, created_at, finished_at, command, result) "
                        "VALUES (?, ?, 'done', 'load', '2026-10-01T00:00:00Z', '2026-10-01T00:03:00Z', ?, ?)",
                        ("compat", json.dumps(params, sort_keys=True), "nanosoc-rm compat x --store", res))
        con.execute("COMMIT")


def test_200_queued_jobs_queue_in_order_and_quickly(cfg):
    """200 queued jobs: each submit answers within a generous 1 s on average, positions run 1..200 in submit
    order, the listing shows them newest first, and the worker would claim the oldest."""
    t = time.perf_counter()
    ids = [jobs.submit(cfg, "walk", {"configuration": "eth-rc4"}, "alice")["id"] for _ in range(200)]
    assert (time.perf_counter() - t) / 200 < 1.0
    assert [jobs.queue_position(cfg, i) for i in (ids[0], ids[99], ids[-1])] == [1, 100, 200]
    listed = jobs.jobs(cfg, limit=500, states=("queued",))
    assert [j["id"] for j in listed] == sorted(ids, reverse=True)
    claimed = jobs.Worker(cfg.path)._claim(cfg)
    assert claimed["id"] == ids[0] and claimed["state"] == "running"
    assert jobs.queue_position(cfg, ids[-1]) == 199


def test_listing_recent_jobs_does_not_read_the_whole_history(cfg, monkeypatch):
    """A configuration page asks for its 10 most recent jobs (views.configuration_view, jobs(limit=10,
    configuration=...)). With 2000 finished jobs that must not decode all 2000 rows."""
    _history(cfg, 2000)
    decoded = []
    real = jobs._row
    monkeypatch.setattr(jobs, "_row", lambda r: decoded.append(1) or real(r))
    got = jobs.jobs(cfg, limit=10, configuration="eth-rc4")
    assert len(got) == 10 and all(j["params"]["configuration"] == "eth-rc4" for j in got)
    assert len(decoded) <= 100, len(decoded)


def test_a_second_identical_run_while_one_is_queued_is_not_a_second_job(cfg):
    """ROB-load-5 (fixed). Run on a configuration that already has the same compat job queued gives back that job, as the quality
    pages (quality/web.py _queue) and the matrix (cell_requests skips queued cells) do. Today 200 clicks on Run
    queue 200 T0 runs: about 10 hours of the single worker at 3 minutes each, ahead of anything else."""
    first = jobs.submit(cfg, "compat", {"configuration": "eth-rc4"}, "alice")
    second = jobs.submit(cfg, "compat", {"configuration": "eth-rc4"}, "bob")
    assert second["id"] == first["id"]
    assert len(jobs.jobs(cfg, limit=50, states=("queued",))) == 1


def test_an_identical_run_after_the_queued_one_started_is_a_new_job(cfg):
    """ROB-load-5 boundary: only a QUEUED compat job is reused; once it is running (it may have read the tree before
    the change being asked about), or for different params, Run queues a fresh job."""
    first = jobs.submit(cfg, "compat", {"configuration": "eth-rc4"}, "alice")
    other = jobs.submit(cfg, "compat", {"configuration": "eth-rc4", "tier": "t1"}, "alice")
    assert other["id"] != first["id"]
    assert jobs.Worker(cfg.path)._claim(cfg)["id"] == first["id"]
    again = jobs.submit(cfg, "compat", {"configuration": "eth-rc4"}, "alice")
    assert again["id"] not in (first["id"], other["id"])
