"""Job kinds of the Publish pages (CR-ART-publish, through nanosoc_rm.jobs.register_kind): a preview or a confirmed
publish runs as `nanosoc-rm publish-release ...` / `register-tapeout ...` in the local job worker, a child process,
never inside a web request (the web process refuses every store write: client.refuse_writes_here)."""
from __future__ import annotations

import re

from nanosoc_rm.jobs import JobUsage, register_kind
from nanosoc_rm.publish.manifest import parse_artefact
from nanosoc_rm.publish.core import PublishUsage

__all__ = ["release_params", "release_args", "tapeout_params", "tapeout_args"]

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _common(p: dict) -> dict:
    confirm = p.get("confirm") or None
    if confirm is not None and not _SHA256.match(str(confirm)):
        raise JobUsage("confirm must be the record's 64-hex sha256")
    by = str(p.get("by") or "")
    if not _LOGIN.match(by):
        raise JobUsage("a publish job needs the signed-in login (by)")
    return {"confirm": confirm, "by": by}


def release_params(p: dict) -> dict:
    conf, version = str(p.get("configuration") or ""), str(p.get("version") or "")
    if not _ID.match(conf) or not _SEMVER.match(version):
        raise JobUsage("configuration and version must be an id and MAJOR.MINOR.PATCH")
    arts = []
    for a in p.get("artefacts") or []:
        try:
            role, path = parse_artefact(str(a))
        except PublishUsage as e:
            raise JobUsage(str(e)) from None
        arts.append("%s=%s" % (role, path))
    return dict(_common(p), configuration=conf, version=version, artefacts=arts)


def _tail(p: dict) -> list[str]:
    mode = ["--confirm", p["confirm"]] if p.get("confirm") else ["--dry-run"]
    return mode + ["--by", p["by"]]


def release_args(p: dict) -> list[str]:
    args = ["publish-release", "%s/%s" % (p["configuration"], p["version"])]
    for a in p.get("artefacts") or []:
        args += ["--artefact", a]
    return args + _tail(p) + ["--json"]


def tapeout_params(p: dict) -> dict:
    tid = str(p.get("tapeout_id") or "")
    if not _ID.match(tid):
        raise JobUsage("%r is not a tapeout id" % tid[:60])
    return dict(_common(p), tapeout_id=tid)


def tapeout_args(p: dict) -> list[str]:
    return ["register-tapeout", p["tapeout_id"]] + _tail(p) + ["--json"]


register_kind("publish-release", release_params, release_args, action=None)
register_kind("register-tapeout", tapeout_params, tapeout_args, action=None)
