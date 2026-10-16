"""The executor task and result contract (interfaces.md section 27.2; schemas/executor.schema.json).

    check_task(doc) -> list[str]          problems with a task (schema, plus the rules a schema cannot say)
    check_result(doc, nonce) -> list[str] problems with a posted result, for the task whose nonce is `nonce`
    command_refusal(command) -> str|None  why a command (or control) is never run

Both sides call these: the service before it queues a task and before it accepts a result, the agent before it
runs a claimed task (a task the service should never have queued is still refused at the point of execution).
"""
from __future__ import annotations

import re
from typing import Any

from nanosoc_rm import contracts

__all__ = ["check_task", "check_result", "command_refusal", "RESERVED_ENV", "SCHEMA"]

SCHEMA = "executor"
# Set by the sandbox itself; a task may not override them.
RESERVED_ENV = ("TREE", "HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND",
                "LD_PRELOAD", "LD_LIBRARY_PATH")
_RESERVED_PREFIX = "NANOSOC_RM_"
# A tier command never sources a set_env.sh (the same rule as analysis/tierrun.py and the registry schema).
_SOURCES_SET_ENV = re.compile(r"(^|[;&|(`]|\s)(source|\.)\s+[^;&|]*set_env")


def _schema(doc: Any, pointer: str) -> list[str]:
    return contracts.validate(SCHEMA, doc, pointer)


def command_refusal(command: Any) -> str | None:
    parts = [command] if isinstance(command, str) else list(command or [])
    if any(isinstance(p, str) and _SOURCES_SET_ENV.search(p) for p in parts):
        return "the command sources a set_env.sh; executor commands never do (declare `env` instead)"
    return None


def check_task(doc: Any) -> list[str]:
    probs = _schema(doc, "/$defs/task")
    if probs:
        return probs
    run = doc["run"]
    for what in ("command", "control"):
        if what in run:
            why = command_refusal(run[what])
            if why:
                probs.append("/run/%s: %s" % (what, why))
    for k in (run.get("env") or {}):
        if k in RESERVED_ENV or k.startswith(_RESERVED_PREFIX):
            probs.append("/run/env: %s is set by the sandbox and may not be given" % k)
    paths = [p["path"] for p in doc["pins"]]
    if len(set(paths)) != len(paths):
        probs.append("/pins: a tree path is given twice")
    return probs


def check_result(doc: Any, nonce: str, task_id: int | None = None) -> list[str]:
    probs = _schema(doc, "/$defs/result")
    if probs:
        return probs
    if doc["nonce"] != nonce:
        probs.append("/nonce: not this task's nonce")
    if task_id is not None and doc["task_id"] != task_id:
        probs.append("/task_id: not this task")
    bad = [i for i, r in enumerate(doc["rows"]) if r.get("nonce") != nonce]
    if bad:
        probs.append("/rows: %d row(s) do not carry this task's nonce (first: /rows/%d)" % (len(bad), bad[0]))
    if doc["finished_at"] < doc["started_at"]:
        probs.append("/finished_at: before started_at")
    if doc["verdict"] == "PASS" and not any(r["status"] == "PASS" for r in doc["rows"]):
        probs.append("/verdict: PASS with no PASS row")
    if doc["verdict"] == "PASS" and any(r["status"] in ("FAIL", "UNVERIFIED", "INFRA") for r in doc["rows"]):
        probs.append("/verdict: PASS over a non-pass row")
    return probs
