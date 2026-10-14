"""Scanner rules (interfaces.md section 11; D11, D2).

This public package carries exactly one rule, `arm.banner`: the shape of the Arm confidential-and-proprietary
file header. Its specimen and counter-specimen are INVENTED here; no vendor text is ever written into this repo.
The lab's real rule set (foundry file, value and inventory shapes, Arm product codes) stays private (GATE2 Q4):
compat-ci carries a pinned copy and passes its path with `--rules PATH` or `$RM_SCAN_RULES`.

Every rule is armed before a scan (the toolkit scanner's method): it must fire on its specimen and stay quiet on
each counter-specimen (the near miss). A rule that is not armed makes the scan `unverified`.

Private rule file (YAML or JSON):

    schema: 1
    rules:
      - id: foundry.example                 # gate-id style: lower case, dot separated
        patterns: ['regex', ...]            # Python re; any one matching is a finding
        flags: [i, s, m, x]                 # optional
        specimen: "invented text the rule must fire on"
        counter_specimens: ["a near miss it must not fire on"]
    allow:                                  # optional; every entry names its reason and owner
      - {rule: foundry.example, path: "docs/*.md", reason: "...", owner: "<login>"}
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from nanosoc_rm.contracts import load_yaml

__all__ = ["Rule", "AllowEntry", "RuleSet", "RulesError", "BUILTIN_RULES", "RULES_ENV", "specimen", "load_rules",
           "rule_set", "arm"]

RULES_ENV = "RM_SCAN_RULES"           # path of the private rule file (compat-ci); optional
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+\Z")
_FLAGS = {"i": re.I, "s": re.S, "m": re.M, "x": re.X}


class RulesError(ValueError):
    """A rule file that cannot be used: unreadable, malformed, a duplicate id, or an allow entry without its
    reason or owner. The CLI turns it into exit 3."""


@dataclass(frozen=True)
class Rule:
    id: str
    patterns: tuple[re.Pattern, ...]
    specimen: str
    counter_specimens: tuple[str, ...] = ()
    description: str = ""

    def matches(self, text: str) -> list[int]:
        """Start offsets of every match of any pattern in `text`, ascending, without duplicates."""
        starts = set()
        for p in self.patterns:
            starts.update(m.start() for m in p.finditer(text))
        return sorted(starts)


@dataclass(frozen=True)
class AllowEntry:
    rule: str
    path: str               # fnmatch glob on the scanned file's relative path
    reason: str
    owner: str

    def covers(self, rule_id: str, rel_path: str) -> bool:
        return self.rule == rule_id and fnmatch.fnmatchcase(rel_path, self.path)


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    allow: tuple[AllowEntry, ...] = field(default=())
    sources: tuple[str, ...] = ("builtin",)

    def allowed(self, rule_id: str, rel_path: str) -> bool:
        return any(a.covers(rule_id, rel_path) for a in self.allow)

    def describe(self) -> str:
        return "nanosoc-rm scan (rules: %s; %d rule%s)" % (
            " + ".join(self.sources), len(self.rules), "" if len(self.rules) == 1 else "s")


# ---------------------------------------------------------------------------------------------------------
# arm.banner. The pattern asks for the confidentiality wording and the vendor's company name within a few
# lines of each other; the match starts at the confidentiality wording, so a finding's line is the banner's
# first line. Open-source Arm copyright headers (Apache-2.0 CMSIS files) carry no such wording and do not match.
# The specimen is invented and is matched on its FIRST line, so a planted specimen is reported at the line it
# starts on (AT-D11: logs/t0.log:3).
# ---------------------------------------------------------------------------------------------------------
_ARM_BANNER = Rule(
    id="arm.banner",
    patterns=(re.compile(r"\bconfidential\s+and\s+proprietary\b.{0,600}?\bArm\s+Limited\b", re.I | re.S),),
    specimen=("// INVENTED SPECIMEN (nanosoc-rm arm.banner): this confidential and proprietary notice names "
              "Arm Limited\n// and is not copied from any vendor file.\n"),
    counter_specimens=(
        "// near miss: a confidential and proprietary notice of an invented company, Limb Limited\n",
        "// near miss: Arm Limited named in an ordinary open-source copyright line\n",
    ),
    description="the Arm confidential-and-proprietary file header (D11)",
)

BUILTIN_RULES: tuple[Rule, ...] = (_ARM_BANNER,)


def specimen(rule_id: str) -> str:
    """The invented text rule `rule_id` is armed on (built-in rules, plus the private file named by
    $RM_SCAN_RULES if set)."""
    for r in rule_set().rules:
        if r.id == rule_id:
            return r.specimen
    raise KeyError("no scanner rule %r" % rule_id)


def _compile(where: str, raw: dict) -> Rule:
    if not isinstance(raw, dict):
        raise RulesError("%s: a rule must be a mapping" % where)
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID.match(rid):
        raise RulesError("%s: rule id %r is not a dotted lower-case id" % (where, rid))
    pats = raw.get("patterns")
    if isinstance(pats, str):
        pats = [pats]
    if not pats or not all(isinstance(p, str) and p for p in pats):
        raise RulesError("%s: rule %s has no patterns" % (where, rid))
    flags = 0
    for f in raw.get("flags") or []:
        if f not in _FLAGS:
            raise RulesError("%s: rule %s has unknown flag %r" % (where, rid, f))
        flags |= _FLAGS[f]
    try:
        compiled = tuple(re.compile(p, flags) for p in pats)
    except re.error as e:
        raise RulesError("%s: rule %s: bad pattern: %s" % (where, rid, e)) from None
    spec = raw.get("specimen")
    if not isinstance(spec, str) or not spec:
        raise RulesError("%s: rule %s has no specimen (every rule is armed before a scan)" % (where, rid))
    counters = raw.get("counter_specimens") or []
    if not isinstance(counters, list) or not all(isinstance(c, str) for c in counters):
        raise RulesError("%s: rule %s: counter_specimens must be a list of strings" % (where, rid))
    return Rule(rid, compiled, spec, tuple(counters), str(raw.get("description") or ""))


def load_rules(path: str | os.PathLike) -> RuleSet:
    """The rules and allowlist of one private rule file. RulesError on any defect; never partial."""
    path = Path(path)
    try:
        doc = load_yaml(path.read_text())
    except OSError as e:
        raise RulesError("cannot read rule file %s: %s" % (path, e.strerror or e)) from None
    except Exception as e:                                   # YAML syntax, duplicate key
        raise RulesError("rule file %s: %s" % (path, e)) from None
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise RulesError("rule file %s: expected a mapping with schema: 1" % path)
    rules = tuple(_compile("%s rules[%d]" % (path, n), r) for n, r in enumerate(doc.get("rules") or []))
    allow = []
    for n, a in enumerate(doc.get("allow") or []):
        where = "%s allow[%d]" % (path, n)
        if not isinstance(a, dict):
            raise RulesError("%s: an allow entry must be a mapping" % where)
        missing = [k for k in ("rule", "path", "reason", "owner") if not (isinstance(a.get(k), str) and a[k].strip())]
        if missing:
            raise RulesError("%s: allow entry lacks %s (each entry carries a reason and an owner)"
                             % (where, ", ".join(missing)))
        allow.append(AllowEntry(a["rule"], a["path"], a["reason"], a["owner"]))
    return RuleSet(rules, tuple(allow), (str(path),))


def rule_set(rules_file: str | os.PathLike | None = None) -> RuleSet:
    """The built-in rules plus the private file (`rules_file`, else $RM_SCAN_RULES when set). A private rule
    may not reuse a built-in id, and an allow entry must name a known rule."""
    path = rules_file if rules_file is not None else (os.environ.get(RULES_ENV) or None)
    if not path:
        return RuleSet(BUILTIN_RULES)
    extra = load_rules(path)
    ids = [r.id for r in BUILTIN_RULES + extra.rules]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise RulesError("duplicate rule id(s): %s" % ", ".join(dup))
    unknown = sorted({a.rule for a in extra.allow} - set(ids))
    if unknown:
        raise RulesError("allow entries name unknown rule(s): %s" % ", ".join(unknown))
    return RuleSet(BUILTIN_RULES + extra.rules, extra.allow, ("builtin",) + extra.sources)


def arm(rules: Iterable[Rule]) -> dict[str, bool]:
    """rule id -> armed: fires on its specimen and on none of its counter-specimens."""
    return {r.id: bool(r.matches(r.specimen)) and not any(r.matches(c) for c in r.counter_specimens)
            for r in rules}
