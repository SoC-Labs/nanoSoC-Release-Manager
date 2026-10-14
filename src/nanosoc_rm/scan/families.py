"""Rule families: which T0 gate judges a scanner finding (CR-6; D2, D11; interfaces.md sections 9 and 11).

The scanner's two engines report one list of `{rule, file, line}` findings. Two configuration gates read it:

- `t0.vendor.no-arm-ip` judges the **arm** family: Arm source in a tree. The rules are `arm.banner` (the Arm
  confidential-and-proprietary header, this package's own rule), any private rule whose id starts `arm.`, the
  toolkit's `file.sha` (a known vendor file keyed by its sha256, so a rename does not evade it), and
  `scan.unscannable` (a file this package could not read, so could not clear of a banner). An uncovered arm finding
  is FAIL, as before.
- `t0.vendor.no-foundry-data` judges the **foundry** family: every other rule, which in practice is the toolkit's
  foundry and site-inventory rules (foundry file types, transcribed layer values, site paths, revision-coded
  release names, licence servers, big files) and any private rule not named `arm.*`. Its findings are WARN by
  default (amber: listed, never FAIL); a rule the catalogue's `foundry_rules` marks `severity: fail` FAILs when
  uncovered.

`split(res, family)` gives one family's view of a raw ScanResult (no catalogue entry applied yet). An
`unverified` scan stays unverified in both views: an engine that could not vouch for the tree vouches for neither
family.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Literal

__all__ = ["ARM", "FOUNDRY", "FAMILIES", "ARM_RULES", "family", "split"]

ARM = "arm"
FOUNDRY = "foundry"
FAMILIES = (ARM, FOUNDRY)
# Rule ids of the arm family besides the `arm.` prefix: the toolkit's content-hash rule and this package's
# could-not-read finding (nanosoc_rm.scan.scan.UNSCANNABLE).
ARM_RULES = frozenset({"arm.banner", "file.sha", "scan.unscannable"})


def family(rule_id: str) -> Literal["arm", "foundry"]:
    """The family of a scanner rule id (module docstring)."""
    return ARM if rule_id in ARM_RULES or rule_id.startswith("arm.") else FOUNDRY


def split(res, fam: str):
    """A new ScanResult holding only `fam`'s findings (and warnings and allowed, if any were sorted already). The
    verdict follows: `unverified` is kept; otherwise `finding` when a finding is left, `warn` when only warnings
    are, else `clean`. Pure: `res` is not changed."""
    if fam not in FAMILIES:
        raise ValueError("unknown rule family %r (one of %s)" % (fam, ", ".join(FAMILIES)))
    keep = [dict(f) for f in res.findings if family(f["rule"]) == fam]
    warn = [dict(f) for f in res.warnings if family(f["rule"]) == fam]
    allow = [dict(f) for f in res.allowed if family(f["rule"]) == fam]
    if res.verdict == "unverified":
        verdict = "unverified"
    elif keep:
        verdict = "finding"
    elif warn:
        verdict = "warn"
    else:
        verdict = "clean"
    return replace(res, verdict=verdict, findings=keep, warnings=warn, allowed=allow,
                   flagged_files=list(dict.fromkeys(f["file"] for f in keep)))
