"""The `freshness-scan` job kind (interfaces.md section 24.4): `nanosoc-rm freshness scan [CONFIG] --store --json`
as a child process of the local worker. Queued by the scheduler's `freshness_scan` job after each poll's walks, and
by POST /freshness/scan (local deployments only)."""
from __future__ import annotations

import re

from nanosoc_rm.jobs import register_kind

__all__ = ["KIND", "LIMIT_S", "params", "cli_args", "outcome", "limit_s"]

KIND = "freshness-scan"
LIMIT_S = 900                     # about 1,000 local git reads for four trees; minutes at most
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def params(p: dict) -> dict:
    extra = set(p) - {"configuration"}
    if extra:
        raise ValueError("freshness-scan takes no parameter %s" % ", ".join(sorted(map(str, extra))))
    cid = p.get("configuration") or None
    if cid is not None and not _ID.match(str(cid)):
        raise ValueError("%r is not a configuration id" % (cid,))
    return {"configuration": cid}


def cli_args(p: dict) -> list[str]:
    return ["freshness", "scan"] + ([p["configuration"]] if p.get("configuration") else []) + ["--store", "--json"]


def outcome(docs: list[dict], last_line: str) -> dict:
    trees = [d for d in docs if d.get("kind") == "freshness" and d["fact"].get("level") == "tree"]
    if not trees:
        return {"state": "done", "verdict": "UNVERIFIED", "reason": last_line or "the scan printed no tree"}
    unwalked = [d["fact"]["configuration"] for d in trees if d["fact"].get("not_measured")]
    pins = [d for d in docs if d.get("kind") == "freshness" and d["fact"].get("level") == "pin"]
    return {"state": "done", "verdict": "UNVERIFIED" if unwalked else "PASS",
            "reason": ("no stored walk: %s" % ", ".join(unwalked)) if unwalked else None,
            "result": {"trees": {d["fact"]["configuration"]: d["fact"]["state"] for d in trees}, "pins": len(pins),
                       "not_measured": sum(1 for d in pins if d["fact"]["behind"] is None)}}


def limit_s(config, job: dict) -> float:
    return float(LIMIT_S)


register_kind(KIND, params, cli_args, outcome=outcome, limit_s=limit_s)
