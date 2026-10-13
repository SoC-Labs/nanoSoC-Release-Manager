"""The gates of a SoC release (G2), its approvers (G5) and the person-only steps, as checks and two files.

    tier_checks(config, configuration, commit, kind) -> [release_check]   evidence per tier, from stored results
    approvals(kind, silicon, errata_changed) -> [person step]             G5
    ledger(release, checks) -> str                                        GATES.md, the gate ledger to sign
    checklist(release, checks, steps) -> str                              CHECKLIST.md

G2 gates by release kind: fix needs T0 to T3; feature adds T4 (FPGA build); tapeout adds T6 (ASIC flow). Plus a
clean clone and the upgrade-path clone, every pin tagged or keep-tagged, no Category A erratum without a decision,
and a signed gate ledger. The evidence for T0, T1 and T2 is a stored result for this exact commit (a local compat
run with --store, or a CI T0 run); a tier with no stored result, and T3, T4 and T6 (no runner for them here), are
UNVERIFIED person checks: a person attaches the evidence to the gate ledger. Nothing here runs a tier.
"""
from __future__ import annotations

from nanosoc_rm import db
from nanosoc_rm.config import Config
from nanosoc_rm.releases.core import PUBLISH_NOTE, check

__all__ = ["TIERS_FOR", "tier_checks", "clone_checks", "approvals", "ledger", "checklist"]

TIERS_FOR = {"fix": ("t0", "t1", "t2", "t3"), "feature": ("t0", "t1", "t2", "t3", "t4"),
             "tapeout": ("t0", "t1", "t2", "t3", "t4", "t6")}
_NAMES = {"t0": "T0 resolve", "t1": "T1 static and unit", "t2": "T2 smoke simulation", "t3": "T3 regression",
          "t4": "T4 FPGA build", "t6": "T6 ASIC flow"}


def _stored(config: Config, configuration: str, commit: str) -> list[dict]:
    """Stored compat_result and t0_result facts of this configuration at this commit, newest last."""
    out = []
    for kind in ("compat_result", "t0_result"):
        try:
            docs = db.latest(kind, "%s:%s:" % (kind, configuration), config=config)
        except Exception:                                   # noqa: BLE001 - no store yet is no evidence, not a crash
            docs = []
        out += [d for d in docs if d["fact"].get("commit") == commit and d["fact"].get("configuration") == configuration]
    return sorted(out, key=lambda d: d["observed_at"])


def tier_checks(config: Config, configuration: str, commit: str, kind: str) -> tuple[list[dict], list[dict]]:
    """(checks, the stored results used). One check per tier the release kind needs."""
    facts = _stored(config, configuration, commit)
    checks = []
    for tier in TIERS_FOR[kind]:
        name = "tier.%s" % tier
        if tier == "t0":
            hit = facts[-1] if facts else None
            if hit:
                f = hit["fact"]
                checks.append(check(name, f["verdict"], "%s: %s %s at %s (%s)" % (
                    _NAMES[tier], hit["kind"], f["verdict"], f.get("finished_at") or hit["observed_at"], hit["subject"]),
                    requirement="G2"))
                continue
        elif tier in ("t1", "t2"):
            hit = next((d for d in reversed(facts) if d["kind"] == "compat_result"
                        and (d["fact"].get("tier") or {}).get("tier") == tier), None)
            if hit:
                t = hit["fact"]["tier"]
                verdict = "UNVERIFIED" if t.get("forced") else t["verdict"]
                checks.append(check(name, verdict, "%s: %s%s (%s)" % (
                    _NAMES[tier], t["verdict"], ", forced past a T0 FAIL: not evidence for a release" if t.get("forced")
                    else "", hit["subject"]), requirement="G2"))
                continue
        cmd = ("nanosoc-rm compat %s --store%s" % (configuration, "" if tier == "t0" else " --tier %s" % tier)
               if tier in ("t0", "t1", "t2") else None)
        checks.append(check(name, "UNVERIFIED", "%s: no stored result for commit %s. %s" % (
            _NAMES[tier], commit[:12], ("Run `%s`." % cmd) if cmd else
            "No runner for this tier here: a person runs it and attaches the evidence to GATES.md."),
            person=cmd is None, requirement="G2"))
    return checks, facts


def clone_checks(facts: list[dict], upgrade_from: dict | None) -> list[dict]:
    """G2 clean-clone and upgrade-path tests, read from the stored T0 gates (t0.pins.fetchable is the anonymous
    clean clone of every pin; t0.clone.upgrade-path the upgrade clone)."""
    out = []
    gates = {}
    for d in facts:
        for g in d["fact"].get("gates") or []:
            gates[g["id"]] = (g, d["subject"])
    for cid, gate, what in (("clone.clean", "t0.pins.fetchable", "clean anonymous clone of every pin"),
                            ("clone.upgrade-path", "t0.clone.upgrade-path", "upgrade-path clone")):
        if gate in gates:
            g, subj = gates[gate]
            out.append(check(cid, g["verdict"], "%s: %s %s (%s)" % (what, gate, g["verdict"], subj), requirement="G2"))
        elif cid == "clone.upgrade-path" and not upgrade_from:
            out.append(check(cid, "NOT-RUN", "the configuration declares no upgrade_from", requirement="G2"))
        else:
            out.append(check(cid, "UNVERIFIED", "%s: no stored T0 result for this commit has %s" % (what, gate),
                             requirement="G2"))
    return out


def approvals(kind: str, silicon: bool, errata_revision_changed: bool) -> list[dict]:
    """G5 approvers of a SoC release, as person steps."""
    who = "David and a second reviewer" if (kind == "tapeout" or silicon) else "David"
    steps = [{"step": "Approve the release (G5: a SoC release is approved by %s)" % who, "who": who}]
    if errata_revision_changed:
        steps.append({"step": "Approve the new errata document revision (G5: an errata revision is approved by David "
                              "and a second reviewer)", "who": "David and a second reviewer"})
    return steps


def ledger(release: dict, checks: list[dict]) -> str:
    """GATES.md: one row per gate with its verdict now, and a signature line per approver (G2: a signed ledger)."""
    lines = ["# Gate ledger: %s %s (%s)" % (release["component"], release["version"], release["configuration"]), "",
             PUBLISH_NOTE, "",
             "Commit `%s`, release kind %s. A person signs below once every gate has evidence; a gate the draft "
             "could not check is marked UNVERIFIED and needs its evidence attached here." % (release["commit"],
                                                                                          release["kind"]), "",
             "| Gate | Verdict now | Evidence or what is missing | Person |", "|---|---|---|---|"]
    lines += ["| `%s` | %s | %s | %s |" % (c["id"], c["verdict"], c["detail"].replace("|", "/"),
                                          "yes" if c["person"] else "") for c in checks]
    lines += ["", "## Signatures", "", "| Role | Name | Date | Signature |", "|---|---|---|---|",
              "| Release approver | | | |", "| Second reviewer (tapeout, silicon or errata revision) | | | |", ""]
    return "\n".join(lines)


def checklist(release: dict, checks: list[dict], steps: list[dict]) -> str:
    """CHECKLIST.md: what a person does before and to make the release, in order."""
    lines = ["# Release checklist: %s %s (%s)" % (release["component"], release["version"], release["configuration"]),
             "", PUBLISH_NOTE, "",
             "## 1. Clear the open checks", ""]
    open_ = [c for c in checks if c["verdict"] not in ("PASS", "WARN", "NOT-RUN")]
    lines += ["- [ ] `%s` (%s): %s" % (c["id"], c["verdict"], c["detail"]) for c in open_] or ["- nothing open"]
    lines += ["", "## 2. Person-only steps", ""]
    for i, s in enumerate(steps, 1):
        lines.append("%d. [ ] %s (%s)" % (i, s["step"], s["who"]))
        if s.get("command"):
            lines += ["", "   ```", *("   " + ln for ln in s["command"].splitlines()), "   ```", ""]
    lines += ["", "## 3. Afterwards", "",
              "- [ ] Re-check the draft: `nanosoc-rm release verify %s/%s` (or Verify on the release page)."
              % (release["configuration"], release["version"]), ""]
    return "\n".join(lines)
