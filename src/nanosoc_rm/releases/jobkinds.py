"""Job kinds of the release pages (CR-R2, through nanosoc_rm.jobs.register_kind): a draft, a verify and a fork
listing each run as `nanosoc-rm ...` in the local job worker, never inside a web request."""
from __future__ import annotations

import os
import re

from nanosoc_rm.jobs import JobUsage, register_kind
from nanosoc_rm.releases.core import SEMVER, is_safe_id

__all__ = ["draft_params", "draft_args", "verify_params", "verify_args", "forks_params", "forks_args"]

_KINDS = ("fix", "feature", "tapeout")
_WAIVER = re.compile(r"^[^=\n]+=[^\n]+\Z")


def _id(value, what: str) -> str:
    if not is_safe_id(value):
        raise JobUsage("%r is not a %s id" % (str(value)[:60], what))
    return value


def _file(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isfile(value):
        raise JobUsage("%r is not an existing file" % str(value)[:120])
    return value


def draft_params(p: dict) -> dict:
    version = str(p.get("version") or "")
    if not SEMVER.match(version):
        raise JobUsage("version %r is not MAJOR.MINOR.PATCH" % version[:40])
    kind = p.get("kind") or None
    if kind not in (None,) + _KINDS:
        raise JobUsage("kind must be fix, feature or tapeout")
    waivers = [str(w).strip() for w in p.get("waivers") or [] if str(w).strip()]
    for w in waivers:
        if not _WAIVER.match(w):
            raise JobUsage("waiver %r is not PATHS=REASON" % w[:80])
    return {"configuration": _id(p.get("configuration"), "configuration"), "version": version, "kind": kind,
            "archive": _file(p.get("archive")), "gds": _file(p.get("gds")), "netlist": _file(p.get("netlist")),
            "waivers": waivers, "replace": bool(p.get("replace"))}


def draft_args(p: dict) -> list[str]:
    args = ["release", "draft", p["configuration"], p["version"]]
    for opt in ("kind", "archive", "gds", "netlist"):
        if p.get(opt):
            args += ["--%s" % opt, p[opt]]
    for w in p.get("waivers") or []:
        args += ["--waiver", w]
    return args + (["--replace"] if p.get("replace") else []) + ["--json"]


def verify_params(p: dict) -> dict:
    version = str(p.get("version") or "")
    if not SEMVER.match(version):
        raise JobUsage("version %r is not MAJOR.MINOR.PATCH" % version[:40])
    return {"configuration": _id(p.get("configuration"), "configuration"), "version": version}


def verify_args(p: dict) -> list[str]:
    return ["release", "verify", "%s/%s" % (p["configuration"], p["version"]), "--json"]


def forks_params(p: dict) -> dict:
    comp = p.get("component") or None
    return {"component": _id(comp, "component") if comp else None}


def forks_args(p: dict) -> list[str]:
    return ["forks"] + ([p["component"]] if p.get("component") else []) + ["--store", "--json"]


register_kind("release-draft", draft_params, draft_args)
register_kind("release-verify", verify_params, verify_args)
register_kind("forks", forks_params, forks_args)
