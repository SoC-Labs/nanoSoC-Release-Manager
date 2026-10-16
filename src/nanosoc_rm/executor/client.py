"""The agent's HTTP client for the executor API (interfaces.md section 27.5).

Its one POST call site goes through `check_path`, which allows exactly the six executor API paths, before any
I/O (WRITE_CALLS effect `executor`). It sends the bearer token in a header only (never a URL or a log line),
follows no redirect, verifies TLS (the configured `ca_file`, else the system store), and never reads a cookie.

    ExecutorClient(cfg, token, http=None)
        .claim(labels) -> dict | None
        .heartbeat(task_id, lease) -> dict
        .upload_log(task_id, lease, data) -> dict
        .post_result(task_id, lease, result) -> dict
        .job_heartbeat(job_id, lease) -> dict
        .finish_job(job_id, lease, exit_code, stdout, log) -> dict
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from nanosoc_rm.executor.agentconfig import AgentConfig

__all__ = ["ExecutorClient", "ApiError", "LeaseGone", "check_path", "TIMEOUT_S"]

TIMEOUT_S = 60
_PATHS = re.compile(r"^/executor/v1/(claim|tasks/[1-9][0-9]{0,11}/(heartbeat|log|result)|"
                    r"jobs/[1-9][0-9]{0,11}/(heartbeat|result))\Z")


class ApiError(RuntimeError):
    """The service answered with an error (status kept); the message never carries the token."""

    def __init__(self, status: int, text: str):
        self.status = status
        super().__init__("executor API %d: %s" % (status, text[:300]))


class LeaseGone(ApiError):
    """409: the task is no longer leased to this agent. Stop the run; never post its result."""


def check_path(path: str) -> str:
    if not _PATHS.match(path):
        raise PermissionError("the executor client posts only to the executor API, not %r" % path[:80])
    return path


class ExecutorClient:
    def __init__(self, cfg: AgentConfig, token: str, http: Any = None):
        self.cfg = cfg
        self._auth = {"Authorization": "Bearer " + token}
        self.http = http if http is not None else httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=False, trust_env=False,
            verify=str(cfg.ca_file) if cfg.ca_file else True)

    def _post(self, path: str, *, json: Any = None, content: bytes | None = None,
              headers: dict | None = None) -> httpx.Response:
        url = self.cfg.server + check_path(path)
        h = dict(headers or {})
        h.update(self._auth)
        try:
            r = self.http.post(url, json=json, content=content, headers=h)      # WRITE_CALLS: executor
        except httpx.HTTPError as e:
            raise ApiError(0, "the service did not answer (%s)" % type(e).__name__) from None
        if r.status_code == 409:
            raise LeaseGone(409, _error(r))
        if r.status_code >= 300:
            raise ApiError(r.status_code, _error(r))
        return r

    def claim(self, labels) -> dict | None:
        r = self._post("/executor/v1/claim", json={"labels": list(labels)})
        return None if r.status_code == 204 else r.json()

    def heartbeat(self, task_id: int, lease: str) -> dict:
        return self._post("/executor/v1/tasks/%d/heartbeat" % task_id, json={"lease": lease}).json()

    def upload_log(self, task_id: int, lease: str, data: bytes) -> dict:
        return self._post("/executor/v1/tasks/%d/log" % task_id, content=bytes(data),
                          headers={"X-Executor-Lease": lease, "Content-Type": "text/plain; charset=utf-8"}).json()

    def post_result(self, task_id: int, lease: str, result: dict) -> dict:
        return self._post("/executor/v1/tasks/%d/result" % task_id, json={"lease": lease, "result": result}).json()

    def job_heartbeat(self, job_id: int, lease: str) -> dict:
        return self._post("/executor/v1/jobs/%d/heartbeat" % job_id, json={"lease": lease}).json()

    def finish_job(self, job_id: int, lease: str, exit_code: int | None, stdout: str, log: str) -> dict:
        return self._post("/executor/v1/jobs/%d/result" % job_id,
                          json={"lease": lease, "exit_code": exit_code, "stdout": stdout, "log": log}).json()


def _error(r: httpx.Response) -> str:
    try:
        d = r.json()
        msg = str(d.get("error", "")) + ("".join("; " + str(p) for p in d.get("problems", [])[:5]))
    except Exception:                                     # noqa: BLE001 - a non-JSON answer
        msg = r.text[:200]
    return msg or "HTTP %d" % r.status_code
