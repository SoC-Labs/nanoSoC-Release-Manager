"""The catalogue `vendor_allowlist` (D2, D11).

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

The vendor gates' split adds `foundry_rules`, the severity of a foundry-family rule (nanosoc_rm.scan.families):

    foundry_rules:
      - rule: path                                               # a rule id as the scanner reports it
        severity: fail                                           # fail | warn (warn is the default for any rule)
        reason: "..."
        approved_by: David
        approved_on: "2026-10-14"

`apply_allowlist(..., uncovered=)` takes the severity of a finding no entry covers: `fail` (the default, and
always for the arm family) keeps it a finding; `warn` moves it to `warnings`, named as the default it is.

This is a separate list from the scanner's own `allow` entries (nanosoc_rm.scan.rules): those belong to the
private rule file and silence a finding before it exists; these belong to the registry, are approved by name
and date, and keep the finding visible as `allowed` or `warn`.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from nanosoc_rm.contracts import load_yaml

__all__ = ["VendorAllow", "vendor_allowlist", "load_vendor_allowlist", "apply_allowlist", "SEVERITIES",
           "FoundryRule", "foundry_rules", "load_foundry_rules", "RULE_SEVERITIES", "DEFAULT_FOUNDRY_SEVERITY"]

SEVERITIES = ("allow", "warn")
RULE_SEVERITIES = ("fail", "warn")         # catalogue foundry_rules[].severity
DEFAULT_FOUNDRY_SEVERITY = "warn"          # a foundry-family rule the catalogue does not name


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


@dataclass(frozen=True)
class FoundryRule:
    rule: str
    severity: str                   # fail | warn
    reason: str
    approved_by: str
    approved_on: str

    def describe(self) -> str:
        return "severity %s for rule %s by %s %s: %s" % (self.severity, self.rule, self.approved_by,
                                                          self.approved_on, self.reason)


def foundry_rules(catalogue: Mapping | None) -> dict[str, FoundryRule]:
    """rule id -> its catalogue severity entry (schema-validated elsewhere). An entry without a rule id or with an
    unknown severity is skipped, and so is one naming an arm-family rule (the schema refuses it; an arm finding is
    never downgraded by severity); of two entries for one rule, `fail` wins."""
    from nanosoc_rm.scan.families import ARM, family
    out: dict[str, FoundryRule] = {}
    for raw in (catalogue or {}).get("foundry_rules") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("rule"), str) or not raw["rule"]:
            continue
        if family(raw["rule"]) == ARM:
            continue
        sev = raw.get("severity")
        if sev not in RULE_SEVERITIES:
            continue
        prev = out.get(raw["rule"])
        if prev is not None and prev.severity == "fail":
            continue                                   # two entries for one rule: the stricter one stands
        out[raw["rule"]] = FoundryRule(raw["rule"], sev, str(raw.get("reason") or ""),
                                       str(raw.get("approved_by") or ""), str(raw.get("approved_on") or ""))
    return out


def load_foundry_rules(registry_dir: str | os.PathLike | None) -> dict[str, FoundryRule]:
    """The foundry_rules of the registry checkout's catalogue.yaml; {} when there is no checkout or no file."""
    if registry_dir is None:
        return {}
    path = Path(registry_dir) / "catalogue.yaml"
    if not path.is_file():
        return {}
    return foundry_rules(load_yaml(path.read_text()))


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


def apply_allowlist(res, entries: Sequence[VendorAllow], component: str | None, shas: Mapping[str, str] | None = None,
                    uncovered: Callable[[str], tuple[str, str]] | None = None):
    """A new ScanResult with `res.findings` sorted into findings (uncovered), warnings and allowed, each covered
    one carrying the entry that covers it (`by`: its description). `shas` maps a finding's file to its blob id
    (scan_tree collects them); without it, `file_sha` entries never match. `uncovered(rule) -> (severity, by)`
    (the foundry gate): the severity of a finding no entry covers; `warn` moves it to `warnings` with that
    `by`, `fail` keeps it a finding. Default: every uncovered finding is a finding. Pure: `res` is not changed."""
    shas = shas or {}
    keep, warn, allow = [], [], []
    for f in res.findings:
        e = _covering(entries, f, component, shas) if f.get("rule") != "scan.unscannable" else None
        if e is None:
            sev, by = uncovered(f["rule"]) if uncovered is not None else ("fail", "")
            if sev == "warn":
                warn.append(dict(f, by=by))
            else:
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
