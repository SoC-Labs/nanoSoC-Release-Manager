"""The `org-scan` job kind: the anonymous org and group listing as a local job (interfaces.md section 23.2).

The worker runs `nanosoc-rm catalogue org-scan --store --json` as a child process, so a page that wants a fresh
new-repo queue queues it (POST) and never lists the hosts inside a request. The job's verdict is PASS when every
host was listed (whether or not anything new was seen) and UNVERIFIED when a listing failed; its result counts the
new repos.
"""
from __future__ import annotations

from nanosoc_rm.jobs import JobUsage, register_kind

__all__ = ["params", "cli_args", "outcome", "KIND"]

KIND = "org-scan"
_HOSTS = ("github", "gitlab")


def params(p: dict) -> dict:
    """Checked parameters: an optional `host` (github or gitlab); none scans every configured host."""
    host = p.get("host") or None
    if host is not None and host not in _HOSTS:
        raise JobUsage("%r is not a host (github or gitlab)" % str(host)[:60])
    return {"host": host}


def cli_args(p: dict) -> list[str]:
    return ["catalogue", "org-scan"] + (["--host", p["host"]] if p.get("host") else []) + ["--store", "--json"]


def outcome(docs: list[dict], last_line: str) -> dict:
    seen = [d for d in docs if d.get("kind") == "repo_seen"]
    failed = "could not list" in (last_line or "")
    return {"state": "done", "verdict": "UNVERIFIED" if failed else "PASS", "reason": last_line or None,
            "result": {"new": len(seen), "repos": [d["subject"].split(":", 1)[1] for d in seen][:100]}}


register_kind(KIND, params, cli_args, outcome=outcome)
