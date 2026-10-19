"""The `test-bump` job kind (registered with nanosoc_rm.jobs.register_kind): one test-bump as a queued job.

It is an EXECUTOR kind: the service's local worker never claims it (unless the local fallback
is on, for the hermetic tests); the pull agent on the EDA host claims it and runs
`nanosoc-rm test-bump CONFIG PATH TO --json` there, with its own host id ($NANOSOC_RM_EXECUTOR_HOST or `[executor]
host`). The job's verdict is the test-bump's functional verdict; its result is the whole test_bump document, which
the bump gate and the Capabilities pages read.
"""
from __future__ import annotations

import re

from nanosoc_rm.jobs import JOB_LIMIT_S, register_kind

__all__ = ["params", "cli_args", "outcome", "limit_s", "LIMIT_S", "ACTION", "KIND"]

KIND = "test-bump"
ACTION = "run_t0"                  # a test-bump runs T0 on both sides: logged like Run (H2)
LIMIT_S = 2 * JOB_LIMIT_S + 2 * 4 * 3600       # two T0 runs, then the planned capabilities on both sides
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")
_PATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_.@+/-]{1,512}\Z")
_TO = re.compile(r"^(?!-)[A-Za-z0-9_.@+/-]{1,255}\Z")


def params(p: dict) -> dict:
    cfg, path, to = p.get("configuration"), p.get("path"), p.get("to")
    if not isinstance(cfg, str) or not _ID.match(cfg):
        raise ValueError("%r is not a configuration id" % str(cfg)[:60])
    if not isinstance(path, str) or not _PATH.match(path.strip("/")):
        raise ValueError("%r is not a tree path" % str(path)[:80])
    if not isinstance(to, str) or not _TO.match(to) or ".." in to:
        raise ValueError("%r is not a tag, branch or commit" % str(to)[:80])
    return {"configuration": cfg, "path": path.strip("/"), "to": to}


def cli_args(p: dict) -> list[str]:
    return ["test-bump", p["configuration"], p["path"], p["to"], "--json"]


def outcome(docs: list[dict], last_line: str) -> dict:
    doc = next((d for d in reversed(docs) if d.get("kind") == "test_bump"), None)
    if doc is None:
        return {"state": "done", "verdict": "UNVERIFIED", "reason": last_line or "test-bump printed no result"}
    f = doc["fact"]
    return {"state": "done", "verdict": f["verdict"], "reason": f.get("reason"), "result": doc}


def limit_s(config, job: dict) -> float:
    return float(LIMIT_S)


register_kind(KIND, params, cli_args, outcome=outcome, limit_s=limit_s, action=ACTION, executor=True)
