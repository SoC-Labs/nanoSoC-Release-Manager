"""The catalogue `vendor_allowlist` (CR-6a; D2, D11; David's U1 decision, 2026-10-10).

The registry owner records, per scanner rule, the findings that have been judged:

    vendor_allowlist:
      - rule: arm.banner
        match: {component: sldma230_tech}                       # component, path_glob, file_sha: all given must hold
        severity: allow                                          # allow: clean | warn: WARN, listed, never FAIL
        reason: "David 2026-10-10: DMA-230 cleared"
        approved_by: David
        approved_on: "2026-10-10"

`apply_allowlist` sorts a ScanResult's findings into three lists: `findings` (no entry covers them: FAIL),
`warnings` (a `warn` entry covers them: the gate row is WARN and names the item) and `allowed` (an `allow`
entry covers them: clean, recorded in the item's `why`). An `allow` entry wins over a `warn` entry for the same
finding. The verdict follows: `finding` > `warn` > `clean`; `unverified` is never changed by the allowlist, since
an unarmed rule or an empty corpus proves nothing whatever the registry says.

This is a separate list from the scanner's own `allow` entries (nanosoc_rm.scan.rules): those belong to the
private rule file and silence a finding before it exists; these belong to the registry, are approved by name
and date, and keep the finding visible as `allowed` or `warn`.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from nanosoc_rm.contracts import load_yaml

__all__ = ["VendorAllow", "vendor_allowlist", "load_vendor_allowlist", "apply_allowlist", "SEVERITIES"]

SEVERITIES = ("allow", "warn")


@dataclass(frozen=True)
class VendorAllow:
    rule: str
    severity: str                   # allow | warn
    reason: str
    approved_by: str
    approved_on: str
    component: str | None = None
    path_glob: str | None = None
    file_sha: str | None = None

    def covers(self, rule_id: str, component: str | None, rel_path: str, blob_sha: str | None) -> bool:
        """Does this entry cover a finding of `rule_id` at `rel_path` (blob `blob_sha`) in `component`'s tree?
        Every match key given must hold; a key the finding has no value for (an unknown component, no blob id)
        never matches, so an entry can only narrow, never widen."""
        if self.rule != rule_id:
            return False
        if self.component is not None and (component is None or self.component != component):
            return False
        if self.path_glob is not None and not fnmatch.fnmatchcase(rel_path, self.path_glob):
            return False
        if self.file_sha is not None and (blob_sha is None or self.file_sha != blob_sha):
            return False
        return True

    def describe(self) -> str:
        keys = [k for k in ("component", "path_glob", "file_sha") if getattr(self, k) is not None]
        return "%s %s{%s} by %s %s: %s" % (
            self.severity, self.rule, ", ".join("%s=%s" % (k, getattr(self, k)) for k in keys),
            self.approved_by, self.approved_on, self.reason)


def vendor_allowlist(catalogue: Mapping | None) -> tuple[VendorAllow, ...]:
    """The entries of a loaded catalogue document (schema-validated elsewhere; a malformed entry is skipped
    here rather than trusted, so a broken registry can only make the gate stricter)."""
    out = []
    for raw in (catalogue or {}).get("vendor_allowlist") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("match"), dict):
            continue
        if raw.get("severity") not in SEVERITIES or not isinstance(raw.get("rule"), str):
            continue
        m = raw["match"]
        out.append(VendorAllow(raw["rule"], raw["severity"], str(raw.get("reason") or ""),
                               str(raw.get("approved_by") or ""), str(raw.get("approved_on") or ""),
                               m.get("component"), m.get("path_glob"), m.get("file_sha")))
    return tuple(out)


def load_vendor_allowlist(registry_dir: str | os.PathLike | None) -> tuple[VendorAllow, ...]:
    """The allowlist of the registry checkout's catalogue.yaml; () when there is no checkout or no file."""
    if registry_dir is None:
        return ()
    path = Path(registry_dir) / "catalogue.yaml"
    if not path.is_file():
        return ()
    return vendor_allowlist(load_yaml(path.read_text()))


def _covering(entries: Iterable[VendorAllow], finding: dict, component: str | None,
              shas: Mapping[str, str]) -> VendorAllow | None:
    """The entry that decides this finding: an `allow` entry if any covers it, else the first `warn` that does."""
    hits = [e for e in entries if e.covers(finding["rule"], component, finding["file"], shas.get(finding["file"]))]
    for e in hits:
        if e.severity == "allow":
            return e
    return hits[0] if hits else None


def apply_allowlist(res, entries: Sequence[VendorAllow], component: str | None, shas: Mapping[str, str] | None = None):
    """A new ScanResult with `res.findings` sorted into findings (uncovered), warnings and allowed, each covered
    one carrying the entry that covers it (`by`: its description). `shas` maps a finding's file to its blob id
    (scan_tree collects them); without it, `file_sha` entries never match. Pure: `res` is not changed."""
    shas = shas or {}
    keep, warn, allow = [], [], []
    for f in res.findings:
        e = _covering(entries, f, component, shas) if f.get("rule") != "scan.unscannable" else None
        if e is None:
            keep.append(dict(f))
        elif e.severity == "allow":
            allow.append(dict(f, by=e.describe()))
        else:
            warn.append(dict(f, by=e.describe()))
    if res.verdict == "unverified":
        verdict = "unverified"
    elif keep:
        verdict = "finding"
    elif warn:
        verdict = "warn"
    else:
        verdict = "clean"
    flagged = list(dict.fromkeys(f["file"] for f in keep))
    return replace(res, verdict=verdict, findings=keep, warnings=list(res.warnings) + warn,
                   allowed=list(res.allowed) + allow, flagged_files=flagged)
