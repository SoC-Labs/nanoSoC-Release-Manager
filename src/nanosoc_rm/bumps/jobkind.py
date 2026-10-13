"""The `bumps` job kind for the local job runner (nanosoc_rm.jobs.register_kind; interfaces.md section 19.7).

The /bumps page's Scan button queues `nanosoc-rm bumps [CONFIG] --store --json` as a job, and a mark queues
`nanosoc-rm bumps ledger --store --json`, so the mirror reads (which may fetch blobs anonymously) never run inside
a web request.
"""
from __future__ import annotations

import re

from nanosoc_rm.jobs import register_kind

__all__ = ["job_params", "job_args"]

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def job_params(params: dict) -> dict:
    """{"configuration": id or None} for a scan, {"ledger": True} for a ledger refresh. ValueError for anything the
    CLI would refuse."""
    if params.get("ledger"):
        if params.get("configuration"):
            raise ValueError("a ledger refresh takes no configuration")
        return {"configuration": None, "ledger": True}
    cid = params.get("configuration") or None
    if cid is not None and (not isinstance(cid, str) or not _ID.match(cid)):
        raise ValueError("%r is not a configuration id" % str(cid)[:60])
    return {"configuration": cid}


def job_args(params: dict) -> list[str]:
    if params.get("ledger"):
        return ["bumps", "ledger", "--store", "--json"]
    return ["bumps", "scan"] + ([params["configuration"]] if params.get("configuration") else []) + ["--store", "--json"]


register_kind("bumps", job_params, job_args)
