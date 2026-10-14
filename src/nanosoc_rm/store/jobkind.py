"""The `store-index` job kind (interfaces.md section 22): `nanosoc-rm store index --store --json` as a child
process of the local worker. Queued by the scheduler's `store_index` job and by the Store page's Refresh button
(POST /store/index, local deployments only). The job's verdict is the index's.
"""
from __future__ import annotations

from nanosoc_rm.jobs import register_kind

__all__ = ["params", "cli_args", "outcome", "limit_s", "LIMIT_S", "KIND"]

KIND = "store-index"
LIMIT_S = 900                     # one AQL call, a few dozen build records, the documents under the cache limit


def params(p: dict) -> dict:
    extra = set(p) - {"no_cache"}
    if extra:
        raise ValueError("store-index takes no parameter %s" % ", ".join(sorted(map(str, extra))))
    return {"no_cache": bool(p.get("no_cache"))}


def cli_args(p: dict) -> list[str]:
    return ["store", "index", "--store", "--json"] + (["--no-cache"] if p.get("no_cache") else [])


def outcome(docs: list[dict], last_line: str) -> dict:
    doc = next((d for d in reversed(docs) if d.get("kind") == "store_index"), None)
    if doc is None:
        return {"state": "done", "verdict": "INFRA", "reason": last_line or "store index printed no result"}
    f = doc["fact"]
    return {"state": "done", "verdict": f["verdict"], "reason": f.get("reason"),
            "result": {"subject": doc["subject"], "files": f["counts"]["files"], "artefacts": len(f["artefacts"]),
                       "builds": len(f["builds"]), "manifests": len(f["manifests"])}}


def limit_s(config, job: dict) -> float:
    return float(LIMIT_S)


register_kind(KIND, params, cli_args, outcome=outcome, limit_s=limit_s)
