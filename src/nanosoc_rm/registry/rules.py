"""The registry loader's cross-file rules (docs/contracts/README.md section 3, rules 1 to 15).

JSON Schema checks one document; these check what one document cannot see. Every rule runs on every parsed
document, schema-valid or not, and reads it defensively: a record that breaks its schema AND names a detector
its defect lacks gets both problems (AT-F2's control relies on this), and a broken field never crashes a rule.

Each problem is (file, JSON pointer, rule id, message). Rule ids are `rule-1` .. `rule-15` as numbered in the
README, plus `unique-id` for a configuration or erratum id used twice (no numbered rule covers it, and the
pages, the walker and `imports.yaml` records all address records by id).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = ["Docs", "check_all", "WAIVABLE_GATES", "CONFIGURATION_GATES", "SOC_CLASSES", "free_text_tokens",
           "structured_commits", "escape"]

# interfaces.md section 9, the configuration job's gates (`Gate.scope == "configuration"` in t0/gates.py).
# Rule 13 reads the declared set from here so the registry loads without importing the T0 package; a unit test
# holds this tuple equal to t0.gates.GATES once that module exists.
CONFIGURATION_GATES = ("t0.infra.probe", "t0.runner.hardened", "t0.pins.fetchable", "t0.pins.reachable",
                       "t0.urls.no-ssh-public", "t0.manifest.valid", "t0.clone.upgrade-path", "t0.vendor.no-arm-ip",
                       "t0.vendor.no-foundry-data")
# The schema already refuses a waiver on these two (they judge the job, not the frozen tree).
_JOB_GATES = ("t0.infra.probe", "t0.runner.hardened")
WAIVABLE_GATES = tuple(g for g in CONFIGURATION_GATES if g not in _JOB_GATES)

SOC_CLASSES = ("soc_ref", "example")          # rule 2: the classes a configuration may name

# Rule 9: 7 to 40 lowercase hex characters, not inside a longer hex run, with at least one digit.
_HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")
_MD5_BEFORE = re.compile(r"md5\s*[:=]?\s*$")


def escape(token: Any) -> str:
    """One JSON-pointer reference token (RFC 6901)."""
    return str(token).replace("~", "~0").replace("/", "~1")


def ptr(*tokens: Any) -> str:
    return "".join("/" + escape(t) for t in tokens)


@dataclass
class Docs:
    """The parsed registry: each file's document (None when it did not parse), by registry-relative path."""
    catalogue: tuple[str, Any] | None = None
    eligibility: tuple[str, Any] | None = None
    imports: tuple[str, Any] | None = None
    tiers: tuple[str, Any] | None = None                              # CR-12: tiers.yaml (optional)
    defects: list[tuple[str, Any]] = field(default_factory=list)
    errata: list[tuple[str, Any]] = field(default_factory=list)       # file = errata/<dir>/<name>


Problem = tuple[str, str, str, str]


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _items(v: Any) -> Iterator[tuple[int, dict]]:
    for i, x in enumerate(_list(v)):
        if isinstance(x, dict):
            yield i, x


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _repo_key(v: Any) -> tuple[str, str] | None:
    d = _dict(v)
    host, path = _str(d.get("host")), _str(d.get("path"))
    return (host, path.lower()) if host and path else None


# ---------------------------------------------------------------------------------------------------------
# Index of the catalogue and eligibility, built defensively
# ---------------------------------------------------------------------------------------------------------
@dataclass
class _Index:
    components: dict[str, dict] = field(default_factory=dict)      # id -> entry (first occurrence)
    configurations: dict[str, tuple[int, dict]] = field(default_factory=dict)
    defects: dict[str, dict] = field(default_factory=dict)          # id -> doc (first occurrence)
    errata_ids: set[str] = field(default_factory=set)


def _index(docs: Docs) -> _Index:
    ix = _Index()
    if docs.catalogue:
        for _i, c in _items(_dict(docs.catalogue[1]).get("components")):
            cid = _str(c.get("id"))
            if cid and cid not in ix.components:
                ix.components[cid] = c
    if docs.eligibility:
        for i, c in _items(_dict(docs.eligibility[1]).get("configurations")):
            cid = _str(c.get("id"))
            if cid and cid not in ix.configurations:
                ix.configurations[cid] = (i, c)
    for _f, d in docs.defects:
        did = _str(_dict(d).get("id"))
        if did and did not in ix.defects:
            ix.defects[did] = _dict(d)
    for _f, e in docs.errata:
        eid = _str(_dict(e).get("id"))
        if eid:
            ix.errata_ids.add(eid)
    return ix


# ---------------------------------------------------------------------------------------------------------
# Structured citations and free-text tokens (rule 9; also what T0's refs-resolve reads, F3)
# ---------------------------------------------------------------------------------------------------------
def structured_commits(record: dict) -> list[tuple[str, dict]]:
    """(pointer, {component, commit}) for every structured commit citation of a defect or an erratum:
    `cites`, `origin`, `fixed_in`, `candidate_fix`, detector `source`, `regression_test`, workaround
    `implementations`, `exposure.run.detector_source` (README section 3 rule 9)."""
    out: list[tuple[str, dict]] = []
    rec = _dict(record)
    for i, c in _items(rec.get("cites")):
        out.append((ptr("cites", i), c))
    for i, c in _items(rec.get("origin")):
        out.append((ptr("origin", i), c))
    for i, f in _items(rec.get("forms")):
        for key in ("fixed_in", "candidate_fix"):
            for j, c in _items(f.get(key)):
                out.append((ptr("forms", i, key, j), c))
    for i, d in _items(rec.get("detectors")):
        if isinstance(d.get("source"), dict):
            out.append((ptr("detectors", i, "source"), d["source"]))
    if isinstance(rec.get("regression_test"), dict):
        out.append((ptr("regression_test"), rec["regression_test"]))
    for i, c in _items(_dict(rec.get("workaround")).get("implementations")):
        out.append((ptr("workaround", "implementations", i), c))
    run = _dict(_dict(rec.get("exposure")).get("run"))
    if isinstance(run.get("detector_source"), dict):
        out.append((ptr("exposure", "run", "detector_source"), run["detector_source"]))
    return out


def _free_text_fields(record: dict) -> list[tuple[str, str]]:
    """(pointer, text) of a defect's or erratum's free text: title, trigger, effect texts, conditions,
    exposure evidence, workaround rules."""
    rec = _dict(record)
    out = []
    for key in ("title", "trigger", "conditions"):
        if isinstance(rec.get(key), str):
            out.append((ptr(key), rec[key]))
    for i, e in _items(rec.get("effects")):
        if isinstance(e.get("text"), str):
            out.append((ptr("effects", i, "text"), e["text"]))
    ev = _dict(rec.get("exposure")).get("evidence")
    if isinstance(ev, str):
        out.append((ptr("exposure", "evidence"), ev))
    rules = _dict(rec.get("workaround")).get("rules")
    if isinstance(rules, str):
        out.append((ptr("workaround", "rules"), rules))
    return out


def free_text_tokens(text: str) -> list[str]:
    """Rule 9's candidate commit tokens in `text`: 7 to 40 lowercase hex characters with at least one digit,
    skipping a token directly after `md5` (a file hash)."""
    out = []
    for m in _HEX.finditer(text):
        tok = m.group(0)
        if not any(ch.isdigit() for ch in tok):
            continue
        if _MD5_BEFORE.search(text[: m.start()]):
            continue
        out.append(tok)
    return out


# ---------------------------------------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------------------------------------
def _rule1(docs: Docs) -> Iterator[Problem]:
    """A component id used twice, or a repo that appears in two places."""
    if not docs.catalogue:
        return
    file, cat = docs.catalogue[0], _dict(docs.catalogue[1])
    ids: dict[str, str] = {}
    repos: dict[tuple[str, str], str] = {}

    def seen_id(cid: Any, where: str) -> Iterator[Problem]:
        if isinstance(cid, str):
            if cid in ids:
                yield (file, where, "rule-1", "id %s is used twice (first at %s)" % (cid, ids[cid]))
            else:
                ids[cid] = where

    def seen_repo(repo: Any, where: str) -> Iterator[Problem]:
        key = _repo_key(repo)
        if key is not None:
            if key in repos:
                yield (file, where, "rule-1", "repo %s:%s appears in two places (first at %s)"
                       % (repo["host"], repo["path"], repos[key]))
            else:
                repos[key] = where

    for i, c in _items(cat.get("components")):
        yield from seen_id(c.get("id"), ptr("components", i, "id"))
        yield from seen_repo(c.get("canonical"), ptr("components", i, "canonical"))
        for key in ("copies", "aliases"):
            for j, r in enumerate(_list(c.get(key))):
                yield from seen_repo(r, ptr("components", i, key, j))
    for i, x in _items(cat.get("external")):
        yield from seen_id(x.get("id"), ptr("external", i, "id"))
        yield from seen_repo(x.get("repo"), ptr("external", i, "repo"))
    for i, x in _items(cat.get("ignored")):
        yield from seen_repo(x.get("repo"), ptr("ignored", i, "repo"))


def _rule2(docs: Docs, ix: _Index) -> Iterator[Problem]:
    """A configuration whose component is missing or is not class soc_ref or example."""
    if not docs.eligibility:
        return
    file = docs.eligibility[0]
    seen: dict[str, int] = {}
    for i, c in _items(_dict(docs.eligibility[1]).get("configurations")):
        cid = _str(c.get("id"))
        if cid is not None:
            if cid in seen:
                yield (file, ptr("configurations", i, "id"), "unique-id",
                       "configuration id %s is used twice (first at %s)" % (cid, ptr("configurations", seen[cid])))
            else:
                seen[cid] = i
        comp = c.get("component")
        if not isinstance(comp, str) or not docs.catalogue:
            continue
        entry = ix.components.get(comp)
        if entry is None:
            yield (file, ptr("configurations", i, "component"), "rule-2",
                   "configuration %s names component %s, which the catalogue does not hold" % (cid, comp))
        elif entry.get("class") not in SOC_CLASSES:
            yield (file, ptr("configurations", i, "component"), "rule-2",
                   "configuration %s names component %s of class %r; a configuration must be a soc_ref or example "
                   "component (a legacy component is listed, never tested: B1)" % (cid, comp, entry.get("class")))


def _component_refs(record: dict, kind: str) -> list[tuple[str, Any]]:
    """(pointer, component id) of every component a defect or erratum names."""
    out = [(p + "/component", _dict(c).get("component")) for p, c in structured_commits(record)]
    if kind == "defect" and "component" in _dict(record):
        out.insert(0, (ptr("component"), record["component"]))
    return out


def _rule3(docs: Docs, ix: _Index) -> Iterator[Problem]:
    """A commit_ref, cites entry, regression_test, workaround or detector source naming an unknown component;
    an import source naming one; an import item whose `record` does not exist."""
    if not docs.catalogue:
        return
    for kind, files in (("defect", docs.defects), ("erratum", docs.errata)):
        for file, doc in files:
            for where, comp in _component_refs(_dict(doc), kind):
                if isinstance(comp, str) and comp not in ix.components:
                    yield (file, where, "rule-3", "component %s is not in the catalogue" % comp)
    if docs.imports:
        file = docs.imports[0]
        for i, s in _items(_dict(docs.imports[1]).get("sources")):
            comp = _dict(s.get("doc")).get("component")
            if isinstance(comp, str) and comp not in ix.components:
                yield (file, ptr("sources", i, "doc", "component"), "rule-3", "component %s is not in the catalogue"
                       % comp)
            for j, it in _items(s.get("items")):
                rec = it.get("record")
                if isinstance(rec, str) and rec not in ix.defects and rec not in ix.errata_ids:
                    yield (file, ptr("sources", i, "items", j, "record"), "rule-3",
                           "record %s is neither a defect nor an erratum in this registry" % rec)


def _ids(defect: dict, key: str) -> set[str]:
    return {x["id"] for _i, x in _items(defect.get(key)) if isinstance(x.get("id"), str)}


def _rule4_6_10_11_12(docs: Docs, ix: _Index) -> Iterator[Problem]:
    """Erratum against its configuration and defect: rules 4, 6, 10, 11 (the erratum's half) and 12."""
    for file, doc in docs.errata:
        e = _dict(doc)
        cfg_id, def_id = e.get("configuration"), e.get("defect")
        cfg = ix.configurations.get(cfg_id)[1] if isinstance(cfg_id, str) and cfg_id in ix.configurations else None
        if isinstance(cfg_id, str) and cfg is None and docs.eligibility:
            yield (file, ptr("configuration"), "rule-4", "configuration %s is not in eligibility.yaml" % cfg_id)
        defect = ix.defects.get(def_id) if isinstance(def_id, str) else None
        if isinstance(def_id, str) and defect is None:
            yield (file, ptr("defect"), "rule-4", "defect %s does not exist" % def_id)
        if defect is not None:
            forms, effects, detectors = _ids(defect, "forms"), _ids(defect, "effects"), _ids(defect, "detectors")
            for i, f in enumerate(_list(e.get("forms_present"))):
                if isinstance(f, str) and f not in forms:
                    yield (file, ptr("forms_present", i), "rule-4",
                           "form %s is not a form of %s (forms: %s)" % (f, def_id, ", ".join(sorted(forms))))
            for i, f in enumerate(_list(e.get("effects_present"))):
                if isinstance(f, str) and f not in effects:
                    yield (file, ptr("effects_present", i), "rule-4",
                           "effect %s is not an effect of %s (effects: %s)" % (f, def_id, ", ".join(sorted(effects))))
            exp = _dict(e.get("exposure"))
            det = exp.get("detector")
            if isinstance(det, str) and det not in detectors:
                yield (file, ptr("exposure", "detector"), "rule-4",
                       "detector %s is not a detector of %s (detectors: %s)"
                       % (det, def_id, ", ".join(sorted(detectors)) or "none"))
            # rule 12: the run's detector_source is the defect detector's source (null for a pattern detector)
            run = exp.get("run")
            if isinstance(det, str) and det in detectors and isinstance(run, dict) and "detector_source" in run:
                d = next(x for _i, x in _items(defect.get("detectors")) if x.get("id") == det)
                want = d.get("source") if isinstance(d.get("source"), dict) else None
                if run["detector_source"] != want:
                    yield (file, ptr("exposure", "run", "detector_source"), "rule-12",
                           "the run's detector_source %r differs from detector %s's source %r in %s"
                           % (run["detector_source"], det, want, def_id))
        if cfg is not None:
            status = e.get("status")
            silicon = isinstance(cfg.get("silicon"), dict)
            if status == "permanent" and not silicon:
                yield (file, ptr("status"), "rule-6",
                       "status permanent on configuration %s, which has no silicon block: only silicon errata are "
                       "permanent" % cfg_id)
            if status == "fixed" and silicon:
                yield (file, ptr("status"), "rule-10",
                       "status fixed on silicon configuration %s: silicon is never fixed, only open, permanent, "
                       "wont-fix or not-affected" % cfg_id)
            prefix = cfg.get("errata_prefix")
            eid = e.get("id")
            if isinstance(prefix, str) and isinstance(eid, str) and eid.rsplit("-ERR-", 1)[0] != prefix:
                yield (file, ptr("id"), "rule-11",
                       "erratum id %s does not start with configuration %s's errata_prefix %s-ERR-"
                       % (eid, cfg_id, prefix))


def _rule11_prefix(docs: Docs, ix: _Index) -> Iterator[Problem]:
    """A configuration with errata and no errata_prefix."""
    if not docs.eligibility:
        return
    by_cfg: dict[str, list[str]] = {}
    for file, doc in docs.errata:
        c = _dict(doc).get("configuration")
        if isinstance(c, str):
            by_cfg.setdefault(c, []).append(file)
    for cfg_id, files in sorted(by_cfg.items()):
        if cfg_id in ix.configurations:
            i, cfg = ix.configurations[cfg_id]
            if not isinstance(cfg.get("errata_prefix"), str):
                yield (docs.eligibility[0], ptr("configurations", i), "rule-11",
                       "configuration %s has errata (%s) and no errata_prefix" % (cfg_id, ", ".join(sorted(files))))


def _rule5(docs: Docs) -> Iterator[Problem]:
    """A defect whose effects or detectors name a form it does not define."""
    for file, doc in docs.defects:
        d = _dict(doc)
        forms = _ids(d, "forms")
        for key in ("effects", "detectors"):
            for i, x in _items(d.get(key)):
                for j, f in enumerate(_list(x.get("forms"))):
                    if isinstance(f, str) and f not in forms:
                        yield (file, ptr(key, i, "forms", j), "rule-5",
                               "%s %s names form %s, which %s does not define" % (key[:-1], x.get("id"), f, d.get("id")))


_DEFECT_FILE = re.compile(r"^defects/(?P<name>[^/]+)\.yaml$")
_ERRATUM_FILE = re.compile(r"^errata/(?P<dir>[^/]+)/ERR-(?P<num>[0-9]{3})\.yaml$")


def _rule7(docs: Docs) -> Iterator[Problem]:
    """A file name or directory that disagrees with the id inside it."""
    for file, doc in docs.defects:
        did = _dict(doc).get("id")
        m = _DEFECT_FILE.match(file)
        if not m:
            yield (file, "", "rule-7", "a defect lives at defects/<id>.yaml")
        elif isinstance(did, str) and m.group("name") != did:
            yield (file, ptr("id"), "rule-7", "defect %s is in a file named %s.yaml" % (did, m.group("name")))
    for file, doc in docs.errata:
        e = _dict(doc)
        m = _ERRATUM_FILE.match(file)
        if not m:
            yield (file, "", "rule-7", "an erratum lives at errata/<configuration>/ERR-NNN.yaml")
            continue
        cfg, eid = e.get("configuration"), e.get("id")
        if isinstance(cfg, str) and cfg != m.group("dir"):
            yield (file, ptr("configuration"), "rule-7",
                   "erratum of configuration %s is in directory errata/%s" % (cfg, m.group("dir")))
        if isinstance(eid, str) and not eid.endswith("-ERR-" + m.group("num")):
            yield (file, ptr("id"), "rule-7", "erratum %s is in a file named ERR-%s.yaml" % (eid, m.group("num")))


def _rule9(docs: Docs) -> Iterator[Problem]:
    """A commit-like token in free text that no structured citation of the same record matches."""
    for files in (docs.defects, docs.errata):
        for file, doc in files:
            rec = _dict(doc)
            cited = [c.get("commit") for _p, c in structured_commits(rec) if isinstance(c.get("commit"), str)]
            for where, text in _free_text_fields(rec):
                for tok in free_text_tokens(text):
                    if not any(c.startswith(tok) for c in cited):
                        yield (file, where, "rule-9",
                               "commit %s is named in free text but not cited structurally (add it to `cites` as "
                               "{component, commit} so T0 can prove it, F3)" % tok)


def _rule13(docs: Docs) -> Iterator[Problem]:
    """Two waivers of one configuration with the same gate, tree_path and result; a waiver whose gate is not a
    declared configuration gate."""
    if not docs.eligibility:
        return
    file = docs.eligibility[0]
    for i, c in _items(_dict(docs.eligibility[1]).get("configurations")):
        seen: dict[tuple, int] = {}
        for j, w in _items(c.get("t0_waivers")):
            key = (w.get("gate"), w.get("tree_path"), w.get("result"))
            if all(isinstance(k, str) for k in key):
                if key in seen:
                    yield (file, ptr("configurations", i, "t0_waivers", j), "rule-13",
                           "waiver duplicates %s (gate %s, tree_path %s, result %s)"
                           % (ptr("configurations", i, "t0_waivers", seen[key]), *key))
                else:
                    seen[key] = j
            gate = w.get("gate")
            if isinstance(gate, str) and gate not in WAIVABLE_GATES:
                yield (file, ptr("configurations", i, "t0_waivers", j, "gate"), "rule-13",
                       "gate %s is not a waivable configuration gate (%s)" % (gate, ", ".join(WAIVABLE_GATES)))


def _rule14(docs: Docs) -> Iterator[Problem]:
    """CR-12: tiers.yaml names only configurations registered in eligibility.yaml."""
    if not docs.tiers or not docs.eligibility:
        return
    known = {_str(c.get("id")) for _i, c in _items(_dict(docs.eligibility[1]).get("configurations"))}
    for cid in _dict(_dict(docs.tiers[1]).get("configurations")):
        if cid not in known:
            yield (docs.tiers[0], ptr("configurations", cid), "rule-14",
                   "tiers for configuration %s, which eligibility.yaml does not register" % cid)


def _rule15(docs: Docs) -> Iterator[Problem]:
    """CR-WF-catalogue: the characterisation fields agree across entries. Each `wraps` id is another catalogue
    component whose group the wrapper may wrap (model.WRAP_TARGETS, the target's group explicit or derived from
    its class); `group: legacy` exactly when `class: legacy` (watch-only stays one fact); `guessed` names only
    fields the entry has (`type`/`role` on a ladder group, `wraps` with wraps, `inside` on a soc or subsystem)."""
    from nanosoc_rm.catalogue.model import LADDER_GROUPS, WRAP_TARGETS, effective_group
    if not docs.catalogue:
        return
    file, doc = docs.catalogue
    comps = {_str(c.get("id")): c for _i, c in _items(_dict(doc).get("components"))}
    for i, c in _items(_dict(doc).get("components")):
        cid, group = _str(c.get("id")), _str(c.get("group"))
        if group is not None and (group == "legacy") != (c.get("class") == "legacy"):
            yield (file, ptr("components", i, "group"), "rule-15",
                   "group %s with class %s: group legacy goes with class legacy and only with it"
                   % (group, c.get("class")))
        mine = effective_group(c)
        for j, w in enumerate(_list(c.get("wraps"))):
            where = ptr("components", i, "wraps", j)
            if not isinstance(w, str):
                continue
            if w == cid:
                yield (file, where, "rule-15", "component %s wraps itself" % cid)
            elif w not in comps:
                yield (file, where, "rule-15", "component %s wraps %s, which is not a catalogue component" % (cid, w))
            else:
                theirs = effective_group(comps[w])
                allowed = WRAP_TARGETS.get(mine or "", ())
                if theirs not in allowed:
                    yield (file, where, "rule-15", "a %s wraps only %s; %s is group %s"
                           % (mine, " or ".join(allowed) or "nothing", w, theirs))
        for j, g in enumerate(_list(c.get("guessed"))):
            where = ptr("components", i, "guessed", j)
            if g in ("type", "role") and group not in LADDER_GROUPS:
                yield (file, where, "rule-15", "guessed %s on a %s entry, which has no %s" % (g, group or "derived-group", g))
            elif g == "wraps" and not c.get("wraps"):
                yield (file, where, "rule-15", "guessed wraps on an entry with no wraps")
            elif g == "inside" and group not in ("soc", "subsystem"):
                yield (file, where, "rule-15", "guessed inside (which project or SoC wraps it) on a %s entry"
                       % (group or "derived-group"))


def _unique_errata(docs: Docs) -> Iterator[Problem]:
    seen: dict[str, str] = {}
    for file, doc in docs.errata:
        eid = _dict(doc).get("id")
        if isinstance(eid, str):
            if eid in seen:
                yield (file, ptr("id"), "unique-id", "erratum id %s is used twice (first in %s)" % (eid, seen[eid]))
            else:
                seen[eid] = file


def check_all(docs: Docs) -> list[Problem]:
    """Every cross-file problem (README section 3 rules 1 to 7 and 9 to 15; rule 8 is the YAML reader's and the
    schema enforces the first half of rule 12)."""
    ix = _index(docs)
    out: list[Problem] = []
    for gen in (_rule1(docs), _rule2(docs, ix), _rule3(docs, ix), _rule4_6_10_11_12(docs, ix),
                _rule11_prefix(docs, ix), _rule5(docs), _rule7(docs), _rule9(docs), _rule13(docs),
                _rule14(docs), _rule15(docs), _unique_errata(docs)):
        out.extend(gen)
    return out
