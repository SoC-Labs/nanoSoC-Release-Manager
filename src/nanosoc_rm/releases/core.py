"""Shared pieces of the release modules: errors, the verdict ladder, where drafts live, safe names."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from nanosoc_rm.config import Config

__all__ = ["ReleaseUsage", "ReleaseError", "LADDER", "ladder", "exit_for", "releases_dir", "draft_path",
           "check", "md5_file", "md5_bytes", "SEMVER", "RELEASE_TAG", "parse_version", "is_safe_id",
           "read_json", "DRAFT_FILE", "PUBLISH_NOTE"]

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
RELEASE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
LADDER = ("FAIL", "UNVERIFIED", "INFRA", "WAIVED", "WARN", "PASS")
DRAFT_FILE = "draft.json"
# Every page and every generated file says this (hard rule: the tool never pushes, tags or publishes).
PUBLISH_NOTE = ("This is a local draft. The release manager never tags, pushes or publishes: every tag, approval and "
                "upload below is a step for a person.")


class ReleaseUsage(ValueError):
    """A request the CLI refuses before any work (exit 3): unknown configuration, bad version, a draft folder that
    already exists, a file argument that does not exist."""


class ReleaseError(RuntimeError):
    """The draft or check could not answer (exit 2): no mirror, a walk that failed, an unreadable draft."""


def ladder(verdicts: Iterable[str]) -> str:
    """README section 6 run ladder: FAIL > UNVERIFIED > INFRA > WAIVED > WARN > PASS; NOT-RUN is ignored. Nothing
    checked is UNVERIFIED, never PASS."""
    seen = [v for v in verdicts if v != "NOT-RUN"]
    for v in LADDER:
        if v in seen:
            return v
    return "UNVERIFIED"


def exit_for(verdict: str) -> int:
    """interfaces.md section 0.2: 0 PASS/WARN/WAIVED, 1 FAIL, 2 UNVERIFIED/INFRA."""
    return {"FAIL": 1, "UNVERIFIED": 2, "INFRA": 2}.get(verdict, 0)


def check(cid: str, verdict: str, detail: str, *, person: bool = False, requirement: str | None = None) -> dict:
    """One release_check row (observation.schema.json $defs/release_check)."""
    row = {"id": cid, "verdict": verdict, "detail": detail, "person": person}
    if requirement:
        row["requirement"] = requirement
    return row


def releases_dir(config: Config) -> Path:
    """Where drafts live: <data_dir>/releases/<configuration>/<version>/ (service's own disk only)."""
    return config.data_dir / "releases"


def is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.match(value)) and ".." not in value


def parse_version(version: str) -> tuple[int, int, int]:
    m = SEMVER.match(version or "")
    if not m:
        raise ReleaseUsage("version %r is not MAJOR.MINOR.PATCH (C1, C10: the tag is vX.Y.Z)" % version)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def draft_path(config: Config, configuration: str, version: str) -> Path:
    if not is_safe_id(configuration):
        raise ReleaseUsage("%r is not a configuration id" % configuration)
    parse_version(version)
    return releases_dir(config) / configuration / version


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def md5_file(path: Path, chunk: int = 1 << 20) -> tuple[str, int]:
    """(md5 hex, size) of a file, read in chunks (a GDS is hundreds of MB)."""
    h, n = hashlib.md5(), 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
