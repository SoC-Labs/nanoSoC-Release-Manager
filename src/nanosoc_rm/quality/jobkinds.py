"""The quality job kind: a page that has no stored result queues `nanosoc-rm quality all TARGET --store`
in the local job worker instead of analysing inside the request (interfaces.md section 14a, item 8)."""
from __future__ import annotations

import re

from nanosoc_rm.jobs import JobUsage, register_kind

__all__ = ["params", "args"]

_TARGET = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@+-]{0,199}\Z")


def params(p: dict) -> dict:
    target, ref = p.get("target"), p.get("ref") or None
    if not isinstance(target, str) or not _TARGET.match(target):
        raise JobUsage("%r is not a quality target" % str(target)[:60])
    if ref is not None and (not isinstance(ref, str) or not _REF.match(ref) or ".." in ref):
        raise JobUsage("%r is not a ref" % str(ref)[:60])
    return {"target": target, "ref": ref}


def args(p: dict) -> list[str]:
    return ["quality", "all", p["target"]] + (["--ref", p["ref"]] if p.get("ref") else []) + ["--store", "--json"]


register_kind("quality", params, args)
