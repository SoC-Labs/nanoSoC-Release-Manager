"""The `regen-class` job kind: one class measurement as a local job (interfaces.md section 20).

The worker runs `nanosoc-rm regen class CONFIG --component C --from SHA --to SHA --store --json` as a child
process: two sandbox renders, never inside the service. The job's verdict is the measurement's.
"""
from __future__ import annotations

import re

from nanosoc_rm.jobs import register_kind

__all__ = ["params", "cli_args", "outcome", "limit_s", "LIMIT_S"]

LIMIT_S = 3600                    # two renders, each a walk, a checkout and the generator
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")
_SHA = re.compile(r"^[0-9a-f]{40}\Z")


def params(p: dict) -> dict:
    """Checked parameters; ValueError when the CLI would exit 3."""
    cfg, comp, frm, to = p.get("configuration"), p.get("component"), p.get("from"), p.get("to")
    for what, v in (("configuration", cfg), ("component", comp)):
        if not isinstance(v, str) or not _ID.match(v):
            raise ValueError("%r is not a %s id" % (str(v)[:60], what))
    for what, v in (("from", frm), ("to", to)):
        if not isinstance(v, str) or not _SHA.match(v):
            raise ValueError("%s must be a full 40-hex commit id" % what)
    return {"configuration": cfg, "component": comp, "from": frm, "to": to}


def cli_args(p: dict) -> list[str]:
    return ["regen", "class", p["configuration"], "--component", p["component"], "--from", p["from"], "--to", p["to"],
            "--store", "--json"]


def outcome(docs: list[dict], last_line: str) -> dict:
    doc = next((d for d in reversed(docs) if d.get("kind") == "regen_class"), None)
    if doc is None:
        return {"state": "done", "verdict": "UNVERIFIED", "reason": last_line or "regen class printed no result"}
    f = doc["fact"]
    return {"state": "done", "verdict": f["verdict"], "reason": f.get("reason"),
            "result": {"subject": doc["subject"], "class": f["class"], "verdict": f["verdict"],
                       "build_affecting": f["build_affecting"]}}


def limit_s(config, job: dict) -> float:
    return float(LIMIT_S)


register_kind("regen-class", params, cli_args, outcome=outcome, limit_s=limit_s)
