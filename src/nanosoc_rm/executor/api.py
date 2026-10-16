"""The executor API: POST-only, bearer-token-only, at /executor/v1/ (interfaces.md section 27.5).

The web app's Guard hands every request under PREFIX to `serve()` BEFORE its session check, and nothing under
PREFIX is routed by the app. So:
  - a session cookie never reaches this API, and an executor token never reaches any other route;
  - nothing here renders a page, sets a cookie, or echoes a token, the path or the body.

    POST /executor/v1/claim                      {labels?}                 -> 200 claim | 204 nothing to run
    POST /executor/v1/tasks/<id>/heartbeat       {lease}                   -> 200 {cancel, lease_seconds}
    POST /executor/v1/tasks/<id>/log             raw log bytes; header X-Executor-Lease  -> 200 {stored}
    POST /executor/v1/tasks/<id>/result          {lease, result}           -> 200 {state, verdict}
    POST /executor/v1/jobs/<id>/heartbeat        {lease}                   -> 200 {cancel, lease_seconds}
    POST /executor/v1/jobs/<id>/result           {lease, exit_code, stdout, log}   -> 200 {state, verdict}

A claim hands out a capability TASK (executor.sqlite, `type: task`) first, else a tier JOB of the local job queue
(`type: job`, jobbridge.py) to an agent granted the `jobs` label.

Answers: 401 no or unknown/revoked token; 404 [executor] not configured, or an unknown path; 405 any method but
POST; 409 the lease is not the caller's; 413 body over the limit; 422 a body or result that breaks the contract.
A claim's labels are the INTERSECTION of what the agent asks with what its config entry grants: an agent cannot
claim a task for a tool the operator did not grant it.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.executor import jobbridge
from nanosoc_rm.executor import queue as Q
from nanosoc_rm.executor import tokens

__all__ = ["PREFIX", "serve", "MAX_BODY_BYTES", "MAX_JSON_BYTES"]

PREFIX = "/executor"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = Q.MAX_LOG_BYTES + 64 * 1024
_TASK = re.compile(r"^/executor/v1/(tasks|jobs)/([1-9][0-9]{0,11})/(heartbeat|log|result)\Z")
_LEASE = re.compile(r"^[0-9a-f]{32}\Z")
_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


def _json(payload: dict, status: int = 200) -> Response:
    return JSONResponse(payload, status, headers=_HEADERS)


async def _body(receive: Callable, limit: int) -> bytes | None:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


async def serve(scope: dict, receive: Callable, send: Callable, load: Callable[[], Config]) -> None:
    resp = await _answer(scope, receive, load)
    await resp(scope, receive, send)


async def _answer(scope: dict, receive: Callable, load: Callable[[], Config]) -> Response:
    path = scope.get("path", "")
    try:
        config = load()
    except ConfigError:
        return _json({"error": "the service config cannot be read"}, 503)
    if not isinstance(config.get("executor"), dict):
        return _json({"error": "not found"}, 404)
    if scope.get("method") != "POST":
        return Response(status_code=405, headers={**_HEADERS, "allow": "POST"})
    agent = tokens.identify(config, _header(scope, b"authorization"))
    if agent is None:
        return _json({"error": "executor token missing, unknown or revoked"}, 401)
    name = agent["name"]
    if path == "/executor/v1/claim":
        body = await _body(receive, MAX_JSON_BYTES)
        if body is None:
            return _json({"error": "body too large"}, 413)
        doc = _parse(body)
        if doc is None:
            return _json({"error": "the body is not a JSON object"}, 422)
        asked = doc.get("labels")
        granted = set(agent.get("labels") or [])
        labels = granted if asked is None else granted & {x for x in asked if isinstance(x, str)} \
            if isinstance(asked, list) else None
        if labels is None:
            return _json({"error": "labels must be a list"}, 422)
        claim = await run_in_threadpool(Q.claim, config, name, labels)
        if claim is not None:
            return _json(dict(claim, type="task"))
        claim = await run_in_threadpool(jobbridge.claim, config, name, labels)
        return Response(status_code=204, headers=_HEADERS) if claim is None else _json(claim)
    m = _TASK.match(path)
    if not m or (m.group(1) == "jobs" and m.group(3) == "log"):
        return _json({"error": "not found"}, 404)
    what, task_id, verb = m.group(1), int(m.group(2)), m.group(3)
    try:
        if what == "jobs":
            return await _job(receive, config, name, task_id, verb)
        if verb == "log":
            lease = _header(scope, b"x-executor-lease") or ""
            if not _LEASE.match(lease):
                return _json({"error": "X-Executor-Lease is required"}, 422)
            body = await _body(receive, MAX_BODY_BYTES)
            if body is None:
                return _json({"error": "log too large"}, 413)
            await run_in_threadpool(Q.store_log, config, task_id, name, lease, body)
            return _json({"stored": min(len(body), Q.MAX_LOG_BYTES)})
        body = await _body(receive, MAX_JSON_BYTES)
        if body is None:
            return _json({"error": "body too large"}, 413)
        doc = _parse(body)
        if doc is None or not isinstance(doc.get("lease"), str) or not _LEASE.match(doc["lease"]):
            return _json({"error": "the body must be a JSON object with the lease"}, 422)
        if verb == "heartbeat":
            return _json(await run_in_threadpool(Q.heartbeat, config, task_id, name, doc["lease"]))
        result = doc.get("result")
        if not isinstance(result, dict):
            return _json({"error": "result must be an object"}, 422)
        row = await run_in_threadpool(Q.complete, config, task_id, name, doc["lease"], result)
        return _json({"state": row["state"], "verdict": row["verdict"]})
    except Q.TaskRefused as e:
        return _json({"error": "the result breaks the contract", "problems": e.problems[:20]}, 422)
    except Q.QueueError as e:
        return _json({"error": str(e)[:300]}, e.status)


async def _job(receive: Callable, config: Config, name: str, job_id: int, verb: str) -> Response:
    if not jobbridge.available():
        return _json({"error": "not found"}, 404)
    body = await _body(receive, MAX_BODY_BYTES)
    if body is None:
        return _json({"error": "body too large"}, 413)
    doc = _parse(body)
    if doc is None or not isinstance(doc.get("lease"), str) or not _LEASE.match(doc["lease"]):
        return _json({"error": "the body must be a JSON object with the lease"}, 422)
    if verb == "heartbeat":
        return _json(await run_in_threadpool(jobbridge.heartbeat, config, job_id, name, doc["lease"]))
    done = await run_in_threadpool(jobbridge.finish, config, job_id, name, doc["lease"], doc.get("exit_code"),
                                   doc.get("stdout", ""), doc.get("log", ""))
    return _json(done)


def _parse(body: bytes) -> dict | None:
    try:
        doc = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None
