"""The `bumps` job kind for the local job runner (nanosoc_rm.jobs PLUGIN_KINDS; interfaces.md section 18.7).

The /bumps page's Scan button queues `nanosoc-rm bumps [CONFIG] --store --json` as a job, so the mirror reads
(which may fetch blobs anonymously) never run inside a web request.
"""
from __future__ import annotations

import re

__all__ = ["job_params", "job_args"]

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def job_params(params: dict) -> dict:
    """{"configuration": id or None}. ValueError for anything the CLI would refuse."""
    cid = params.get("configuration") or None
    if cid is not None and (not isinstance(cid, str) or not _ID.match(cid)):
        raise ValueError("%r is not a configuration id" % str(cid)[:60])
    return {"configuration": cid}


def job_args(params: dict) -> list[str]:
    return ["bumps", "scan"] + ([params["configuration"]] if params.get("configuration") else []) + ["--store", "--json"]
