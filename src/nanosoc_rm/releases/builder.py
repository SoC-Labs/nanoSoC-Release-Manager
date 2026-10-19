"""`nanosoc-rm release draft CONFIG VERSION` and `release verify DIR` (G2, G3, G4, G7, F4; CR-R1).

    draft(configuration, version, ...) -> release_draft observation   builds the draft folder
    verify(path, ...) -> release_verify observation                    re-checks a draft against the mirrors now

A draft is a folder, `<data_dir>/releases/<configuration>/<version>/` unless `out` names another:

    release.lock.yaml          the release lock (release-lock.schema.json), when nothing is pending; otherwise
    release.lock.draft.yaml    the same fields with `pending` naming what is missing ($defs/draft_lock)
    ERRATA.md, ERRATA.html     the configuration's errata document with its revision (F4)
    RELEASE_NOTES.md           classed changes since the previous release (C2 classes)
    CHECKLIST.md, GATES.md     the person-only steps (tags, approvals per G5) and the gate ledger to sign (G2)
    build_soc.md5sums.txt      the generated-RTL md5 manifest (G7), with a copy of the archive when one is given
    draft.json                 the release_draft observation: every check, file md5, person step
    README.txt                 what this folder is, and that nothing was published

Inputs are read-only: a fresh walk of the configuration over the mirrors (not stored, no alert), the registry's
errata, the tags in the mirrors, stored compat/T0 results for the evidence. The tool never tags, pushes or
uploads: each such action is a person step with its command.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import yaml

from nanosoc_rm import __version__, clock, contracts
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.mirror.layout import mirror_path, ref_prefix, run_in
from nanosoc_rm.releases import archive as archmod
from nanosoc_rm.releases import checklist as cl
from nanosoc_rm.releases import errata as errmod
from nanosoc_rm.releases import lock as lockmod
from nanosoc_rm.releases import notes as notesmod
from nanosoc_rm.releases.core import (DRAFT_FILE, PUBLISH_NOTE, RELEASE_TAG, ReleaseError, ReleaseUsage, check,
                                      draft_path, exit_for, ladder, md5_bytes, md5_file, parse_version,
                                      read_json, releases_dir)

__all__ = ["draft", "verify", "drafts", "read_draft", "find_draft", "parse_waiver", "ARTEFACT_KINDS",
           "MAX_ARCHIVE_COPY", "exit_for"]

ARTEFACT_KINDS = ("flists", "bitstream", "logs", "source-archive")
MAX_ARCHIVE_COPY = 256 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
LOCK, DRAFT_LOCK, MD5S = "release.lock.yaml", "release.lock.draft.yaml", "build_soc.md5sums.txt"


# ---------------------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------------------
def _registry(config: Config):
    """The registry checkout, loaded whole (RegistryError when any file has a problem: never partial)."""
    from nanosoc_rm.registry import load_registry
    path = Path(config.require("registry", "path"))
    if not path.is_dir():
        raise ReleaseError("registry path %s is not a directory" % path)
    return load_registry(path)


def _walk(configuration: str, config: Config) -> list[dict]:
    from nanosoc_rm.walker import UnknownConfiguration, WalkError, walk
    try:
        return walk(configuration, config=config, store=False, alerts=False)
    except UnknownConfiguration:
        raise ReleaseUsage("no registered configuration %r" % configuration) from None
    except WalkError as e:
        raise ReleaseError("the walk of %s could not answer: %s" % (configuration, e)) from None


def _walk_at(configuration: str, config: Config, commit: str) -> list[dict]:
    """The tree of `commit` (the drafted root), judged on the hosts now: a what-if walk, never stored."""
    from nanosoc_rm.walker import UnknownConfiguration, WalkError, walk
    try:
        return walk(configuration, config=config, store=False, alerts=False, at=commit)
    except UnknownConfiguration:
        raise ReleaseUsage("no registered configuration %r" % configuration) from None
    except WalkError as e:
        raise ReleaseError("the walk of %s at the drafted commit %s could not answer: %s" % (
            configuration, commit[:12], e)) from None


def _repo(r: Any) -> dict:
    return {"host": r.host, "path": r.path} if hasattr(r, "host") else {"host": r["host"], "path": r["path"]}


def _root_commit(config: Config, cfg: dict, repo: dict, docs: list[dict]) -> str:
    top = [d["fact"]["parent"]["commit"] for d in docs if d["fact"]["depth"] == 1]
    if top:
        return top[0]
    if cfg.get("commit"):
        return cfg["commit"]
    ref = cfg["ref"]
    name = "heads/%s" % ref["branch"] if "branch" in ref else "tags/%s" % ref["tag"]
    r = run_in(mirror_path(config, cfg["component"]), "rev-parse", "--verify", "--quiet",
               "%s/%s^{commit}" % (ref_prefix(repo["host"], repo["path"]), name))
    if not r.ok or not r.stdout.strip():
        raise ReleaseError("%s: %s is not in the mirror of %s" % (cfg["id"], name, cfg["component"]))
    return r.stdout.strip()


def parse_waiver(text: str) -> dict:
    """`--waiver PATHS=REASON`: PATHS is `*` or comma-separated tree paths (release-lock `waivers`)."""
    paths, sep, reason = text.partition("=")
    paths = [p.strip() for p in paths.split(",") if p.strip()]
    if not sep or not paths or not reason.strip():
        raise ReleaseUsage("--waiver %r is not PATHS=REASON (PATHS: * or tree paths separated by commas)" % text)
    return {"tree_paths": paths, "reason": reason.strip()}


def _waived(pin: dict, waivers: list[dict]) -> bool:
    return any("*" in w["tree_paths"] or set(w["tree_paths"]) & set(pin["tree_paths"]) for w in waivers)


def drafts(config: Config | None = None, configuration: str | None = None) -> list[dict]:
    """Every draft's release_draft fact (with `dir`), newest generated first. A folder whose draft.json does not
    read or validate is listed with its problem."""
    config = config if config is not None else load_config()
    root = releases_dir(config)
    out = []
    for p in sorted(root.glob("*/*/%s" % DRAFT_FILE)) if root.is_dir() else []:
        if configuration and p.parent.parent.name != configuration:
            continue
        try:
            doc = read_json(p)
            problems = contracts.validate_observation(doc)
        except (OSError, ValueError) as e:
            doc, problems = None, [str(e)]
        out.append({"dir": str(p.parent), "configuration": p.parent.parent.name, "version": p.parent.name,
                    "doc": doc if not problems else None, "problems": problems})
    return sorted(out, key=lambda r: (r["doc"] or {}).get("fact", {}).get("generated_at", ""), reverse=True)


def find_draft(config: Config, ref: str) -> Path:
    """A draft folder from a path, or from `<configuration>/<version>` under the releases directory."""
    p = Path(ref)
    if (p / DRAFT_FILE).is_file():
        return p
    parts = ref.strip("/").split("/")
    if len(parts) == 2:
        cand = draft_path(config, parts[0], parts[1])
        if (cand / DRAFT_FILE).is_file():
            return cand
    raise ReleaseUsage("no release draft at %s (give a draft folder or CONFIGURATION/VERSION)" % ref)


def read_draft(path: Path) -> dict:
    doc = read_json(Path(path) / DRAFT_FILE)
    problems = contracts.validate_observation(doc)
    if problems or doc.get("kind") != "release_draft":
        raise ReleaseError("%s is not a valid release draft: %s" % (path, "; ".join(problems) or doc.get("kind")))
    return doc


def _earlier(config: Config, configuration: str, version: str) -> list[dict]:
    """release_draft facts of other drafts of this configuration (for F4 numbering and the pins that moved)."""
    out = []
    for r in drafts(config, configuration):
        if r["doc"] and r["version"] != version:
            out.append(dict(r["doc"]["fact"], dir=r["dir"]))
    return out


def _issued_errata(config: Config, configuration: str, earlier: list[dict], target: Path) -> list[dict]:
    """Every errata block ({revision, digest, version}) an earlier draft of this configuration issued, for F4: the
    other drafts on disk, the draft this one replaces (same version), and every release_draft the store holds for
    the configuration (history survives a deleted or replaced folder)."""
    out = [dict(e["errata"], version=e["version"]) for e in earlier]
    if (target / DRAFT_FILE).is_file():
        try:
            f = read_draft(target)["fact"]
            if f["configuration"] == configuration:
                out.append(dict(f["errata"], version="%s (the draft being replaced)" % f["version"]))
        except (OSError, ValueError, ReleaseError):
            pass
    try:
        from nanosoc_rm import db
        for latest in db.latest("release_draft", "release_draft:%s:" % configuration, config=config):
            for d in db.history(latest["subject"], config=config):
                f = d["fact"]
                if f.get("configuration") == configuration and f.get("errata"):
                    out.append(dict(f["errata"], version="%s (stored %s)" % (f["version"], f["generated_at"])))
    except Exception:                                    # noqa: BLE001 - no store yet: the folders are the memory
        pass
    seen, uniq = set(), []
    for e in out:
        key = (e.get("revision"), e.get("digest"))
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def _lock_of(d: Path) -> dict | None:
    for name in (LOCK, DRAFT_LOCK):
        if (d / name).is_file():
            try:
                lock = contracts.load_yaml((d / name).read_text())
            except (OSError, ValueError, yaml.YAMLError):          # unreadable or not YAML: no lock (ROB-coverage-4)
                return None
            return lock if isinstance(lock, dict) else None
    return None


# ---------------------------------------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------------------------------------
def draft(configuration: str, version: str, *, config: Config | str | None = None, kind: str | None = None,
          archive: str | os.PathLike | None = None, generator: str | None = None, rtl_committed: bool = False,
          gds: str | os.PathLike | None = None, netlist: str | os.PathLike | None = None,
          artefacts: Sequence[tuple[str, str]] = (), waivers: Sequence[dict] = (), since: str | None = None,
          candidate: int | None = None, out: str | os.PathLike | None = None, replace: bool = False,
          walk: Callable[[str, Config], list[dict]] | None = None, command: str | None = None,
          store: bool = True) -> dict:
    """Build the draft folder and return its release_draft observation (also written as draft.json, and stored as
    an event observation unless `store` is False).
    ReleaseUsage (exit 3) before any work for a bad request; ReleaseError (exit 2) when it cannot answer."""
    config = config if isinstance(config, Config) else load_config(config)
    reg, cfg, kind = _draft_request(config, configuration, version, kind, archive, gds, netlist, artefacts)
    silicon = cfg.get("silicon")
    target = Path(out) if out is not None else draft_path(config, configuration, version)
    _check_target(target, replace)
    waivers = [dict(w) for w in waivers]
    comp = cfg["component"]
    canon = _repo(reg.canonical(comp))
    started = clock.iso()

    docs = (walk or _walk)(configuration, config)
    commit = _root_commit(config, cfg, canon, docs)
    tag = "v%s" % version
    release = {"component": comp, "configuration": configuration, "version": version, "tag": tag, "commit": commit,
               "kind": kind, "silicon": bool(silicon)}
    acc = _Draft(sources=[{"kind": "derived",
                           "command": command or "nanosoc-rm release draft %s %s" % (configuration, version)}])

    pins, unprotected = _pin_checks(acc, config, reg, configuration, cfg, docs, waivers)
    tags = lockmod.repo_tags(config, comp, canon)
    sil_tag = _tag_checks(acc, config, canon, release, tags, silicon, candidate)
    earlier = _earlier(config, configuration, version)
    edoc, errata_changed = _errata_checks(acc, config, reg, release, earlier, target)
    copy_archive, man, generated_rtl = _rtl_archive(acc, archive, generator, rtl_committed, pins)
    outputs = _silicon_outputs(acc, silicon, gds, netlist)
    arts = _artefacts(acc, artefacts)
    tchecks, facts = cl.tier_checks(config, configuration, commit, kind)
    acc.checks += tchecks
    acc.checks += cl.clone_checks(facts, cfg.get("upgrade_from"))
    acc.checks.append(check("ledger.signed", "UNVERIFIED", "GATES.md is unsigned: a person signs it once every gate "
                            "has evidence", person=True, requirement="G2"))

    generated_at = clock.iso()
    lock = {"schema": 1, "release": {"component": comp, "configuration": configuration, "version": version,
                                     "tag": tag, "commit": commit},
            "generated_at": generated_at, "generated_by": {"tool": "nanosoc-release-manager", "version": __version__},
            "sources": _lock_sources(docs, config), "pins": pins, "waivers": waivers}
    if silicon:
        lock["release"]["silicon"] = {"chip": silicon["chip"], "revision": silicon["revision"], "tag": sil_tag}
    if generated_rtl:
        lock["generated_rtl"] = generated_rtl
    if outputs:
        lock["outputs"] = outputs
    lock.update(errata={"file": "ERRATA.md", "revision": edoc.revision}, gate_ledger={"file": "GATES.md"},
                artefacts=arts)
    complete, lock_name = _finish_lock(acc, lock, pins)

    train = _train(reg, pins)
    _closing_steps(acc, train, kind, silicon, errata_changed)
    notes = _notes(config, release, docs, pins, tags, since, earlier, comp)
    _release_texts(acc, release, notes, edoc)
    _vendor_scans(acc, release, complete, gds is not None or netlist is not None)

    verdict = ladder(c["verdict"] for c in acc.checks)
    machine_ok = all(c["verdict"] in ("PASS", "WARN", "WAIVED", "NOT-RUN") for c in acc.checks if not c["person"])
    ready = complete and machine_ok and verdict not in ("FAIL", "INFRA")
    fact = {"configuration": configuration, "component": comp, "version": version, "tag": tag, "commit": commit,
            "kind": kind, "silicon": bool(silicon), "generated_at": generated_at,
            "lock": {"file": lock_name, "complete": complete, "pins": len(pins), "unprotected": unprotected,
                     "pending": ["%s: %s" % (p["field"], p["reason"]) for p in acc.pending]},
            "errata": {"revision": edoc.revision, "records": len(edoc.records), "digest": edoc.digest},
            "archive": None if man is None else {"md5": man.md5, "bytes": man.bytes, "files": man.files,
                                                 "manifest_md5": man.manifest_md5},
            "checks": acc.checks, "verdict": verdict, "ready": ready, "person_steps": acc.steps, "train": train,
            "files": [], "published": False}
    times = [d["observed_at"] for d in docs] + [d["observed_at"] for d in facts] + [started]
    obs = {"schema": 1, "kind": "release_draft", "subject": "release_draft:%s:%s" % (configuration, version),
           "observed_at": min(times, key=clock.parse), "sources": acc.sources, "fact": fact}
    _write(target, acc.files, copy_archive, obs, replace)
    if store:
        from nanosoc_rm import db
        db.put(obs, config=config)
    return obs


@dataclass
class _Draft:
    """What the draft's steps add to, in order: the order of `checks` and `steps` is the order a person reads."""
    checks: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    scan_items: list[tuple[str, bytes | None]] = field(default_factory=list)

    def pend(self, field_name: str, reason: str) -> None:
        self.pending.append({"field": field_name, "reason": reason})


def _draft_request(config: Config, configuration: str, version: str, kind: str | None, archive, gds, netlist,
                   artefacts: Sequence[tuple[str, str]]) -> tuple[Any, dict, str]:
    """(registry, configuration entry, kind) of a well-formed request; ReleaseUsage before any work otherwise."""
    parse_version(version)
    reg = _registry(config)
    cfg = reg.configuration(configuration)
    if cfg is None:
        raise ReleaseUsage("no registered configuration %r" % configuration)
    if kind not in (None, "fix", "feature", "tapeout"):
        raise ReleaseUsage("--kind must be fix, feature or tapeout")
    for label, p in (("--archive", archive), ("--gds", gds), ("--netlist", netlist)):
        if p is not None and not Path(p).is_file():
            raise ReleaseUsage("%s %s: no such file" % (label, p))
    for k, p in artefacts:
        if k not in ARTEFACT_KINDS:
            raise ReleaseUsage("--artefact kind %r is not one of %s" % (k, ", ".join(ARTEFACT_KINDS)))
        if not Path(p).is_file():
            raise ReleaseUsage("--artefact %s=%s: no such file" % (k, p))
    return reg, cfg, kind or ("tapeout" if cfg.get("silicon") else "feature")


def _by(p: dict) -> str | None:
    return (p.get("protection") or {}).get("by")


def _pin_checks(acc: _Draft, config: Config, reg: Any, configuration: str, cfg: dict, docs: list[dict],
                waivers: list[dict]) -> tuple[list[dict], list[str]]:
    """G3 and G2 over the pins: stated, protected, protected on the canonical host, tagged. (pins, unprotected)."""
    pins, unjudged = lockmod.lock_pins(docs)
    if unjudged:
        acc.pend("pins", "%d pin(s) without a state: %s" % (len(unjudged), "; ".join(unjudged)))
        acc.checks.append(check("lock.pins-stated", "FAIL", "; ".join(unjudged), requirement="G3"))
    else:
        acc.checks.append(check("lock.pins-stated", "PASS", "%d pin(s), each with its host and state" % len(pins),
                                requirement="G3"))
    unprotected = [p["tree_paths"][0] for p in pins if (p.get("protection") or {}).get("by", "none") == "none"]
    acc.checks.append(_protected_check(pins, unprotected, strict=bool(cfg.get("silicon") or cfg.get("frozen"))))
    gaps = sorted({"%s (%s: %s)" % (p["tree_paths"][0], h, a) for p in pins
                   for h, a in (p.get("protection") or {}).get("hosts", {}).items()
                   if a in ("none", "not-held") and h != p["repo"]["host"]})
    acc.checks.append(check("lock.pins-canonical", "WARN" if gaps else "PASS",
                            ("canonical host does not protect: %s (B5: push the commit there, then keep-tag it)"
                             % "; ".join(gaps)) if gaps else "every pin is also protected on its canonical host",
                            requirement="G2"))
    loose = [p for p in pins if _by(p) not in ("release-tag", "keep-tag")]
    untagged = [p for p in loose if not _waived(p, waivers)]
    waived_n = len(loose) - len(untagged)
    if untagged:
        detail = "%d pin(s) neither tagged nor keep-tagged nor waived: %s" % (
            len(untagged), ", ".join(p["tree_paths"][0] for p in untagged))
    else:
        detail = "every pin tagged or keep-tagged%s" % (" (%d covered by a waiver)" % waived_n if waived_n else "")
    acc.checks.append(check("lock.pins-tagged", "FAIL" if untagged else ("WAIVED" if waived_n else "PASS"), detail,
                            requirement="G2"))
    acc.steps += _keep_tag_steps(config, reg, configuration, untagged)
    return pins, unprotected


def _protected_check(pins: list[dict], unprotected: list[str], strict: bool) -> dict:
    """lock.pins-protected: an unprotected pin FAILs a silicon or frozen release (else WARN); an unreadable host
    leaves the pin unjudged."""
    protected_where = ", ".join("%s %d" % (b, n) for b, n in sorted(
        _count(p.get("protection", {}).get("by", "none") for p in pins).items()))
    unread = [p["tree_paths"][0] for p in pins if _by(p) == "unreadable"]
    if unprotected:
        verdict, detail = ("FAIL" if strict else "WARN"), "unprotected on the pinned host: %s" % ", ".join(unprotected)
        if unread:
            detail += "; not judged (unreadable): %s" % ", ".join(unread)
    elif unread:
        verdict, detail = "UNVERIFIED", ("not judged, the host refused the read (a private repo read without a "
                                         "token): %s" % ", ".join(unread))
    else:
        verdict, detail = "PASS", "every pin protected on its pinned host (%s)" % protected_where
    return check("lock.pins-protected", verdict, detail, requirement="G3")


def _tag_checks(acc: _Draft, config: Config, canon: dict, release: dict, tags: dict[str, str], silicon: dict | None,
                candidate: int | None) -> str | None:
    """C1 and C10: the release tag, its order among the release tags, the silicon tag. Returns the silicon tag."""
    comp, version, tag, commit = release["component"], release["version"], release["tag"], release["commit"]
    acc.sources.append({"kind": "git", "command": "git -C <mirrors>/%s.git for-each-ref %s/tags" % (
        comp, ref_prefix(canon["host"], canon["path"]))})
    acc.checks.append(_tag_check("release.tag", tag, commit, tags, "C10"))
    newer = [t for t in tags if RELEASE_TAG.match(t) and tags[t] != commit
             and parse_version(t[1:]) >= parse_version(version)]
    acc.checks.append(check("release.version-order", "FAIL" if newer else "PASS",
                            ("release tag(s) %s at other commits are not older than %s"
                             % (", ".join(sorted(newer)), version))
                            if newer else "%s is newer than every release tag at another commit" % version,
                            requirement="C1"))
    if tags.get(tag) != commit:
        acc.steps.append(_tag_step(config, canon, tag, commit, "%s %s (%s)" % (comp, version,
                                                                              release["configuration"])))
    sil_tag = None
    if silicon:
        sil_tag = "silicon/%s-%s" % (silicon["chip"], silicon["revision"])
        acc.checks.append(_tag_check("release.silicon-tag", sil_tag, commit, tags, "C10"))
        if tags.get(sil_tag) != commit:
            acc.steps.append(_tag_step(config, canon, sil_tag, commit,
                                       "%s silicon %s" % (silicon["chip"], silicon["revision"])))
    if candidate is not None:
        acc.steps.insert(0, _tag_step(config, canon, "%s-rc.%d" % (tag, candidate), commit,
                                      "candidate %d" % candidate))
    return sil_tag


def _errata_checks(acc: _Draft, config: Config, reg: Any, release: dict, earlier: list[dict],
                   target: Path) -> tuple[Any, bool]:
    """F4 and G2: the errata revision, and no open Category A erratum. (errata document, errata changed)."""
    configuration = release["configuration"]
    edoc = errmod.document(reg, configuration, release=release)
    acc.checks.append(errmod.revision_check(edoc, _issued_errata(config, configuration, earlier, target)))
    open_records = [e for e in edoc.records if e["status"] in errmod.OPEN_STATUSES]
    cat_a = [e["id"] for e in open_records if e.get("category") == "A"]
    undecided = [e["id"] for e in open_records if e.get("category") is None]
    if cat_a:
        acc.checks.append(check("errata.category-a", "FAIL", "Category A erratum open: %s; each needs David's "
                                "recorded decision before release" % ", ".join(cat_a), requirement="G2"))
    else:
        acc.checks.append(check("errata.category-a", "WARN" if undecided else "PASS",
                                ("no Category A erratum; category undecided for %s" % ", ".join(undecided))
                                if undecided else "no open Category A erratum", requirement="G2"))
    acc.sources.append({"kind": "registry", "command": "errata records of %s in the registry at %s" % (
        configuration, reg.commit[:12] if reg.commit else "an uncommitted checkout")})
    return edoc, bool(earlier) and all(e["errata"]["revision"] != edoc.revision for e in earlier)


def _rtl_archive(acc: _Draft, archive, generator: str | None, rtl_committed: bool,
                 pins: list[dict]) -> tuple[tuple[Path, str] | None, Any, dict | None]:
    """G7: the generated-RTL archive's md5 manifest and its generator pin. (archive to copy, manifest, record)."""
    if archive is None:
        acc.pend("generated_rtl", "no generated-RTL archive given (--archive): G7 needs build_soc/ archived with an "
                 "md5 manifest")
        acc.checks.append(check("rtl.archive", "UNVERIFIED", "no archive given: run the draft again with --archive "
                                "<build_soc.tar.gz>", person=True, requirement="G7"))
        return None, None, None
    try:
        man = archmod.manifest(archive, keep_members=True)
    except archmod.ArchiveError as e:
        raise ReleaseUsage(str(e)) from None
    acc.files[MD5S] = man.text.encode()
    acc.scan_items += [("archive:%s" % n, b if len(b) <= MAX_SCAN_BYTES else None) for n, b in man.members]
    name = _safe_name(Path(archive).name)
    copy_archive = (Path(archive), name) if man.bytes <= MAX_ARCHIVE_COPY else None
    gen = _generator_pin(pins, generator)
    generated_rtl = None
    if gen is None:
        acc.pend("generated_rtl", "no generator pin (%s) in the tree to record" % (generator or "component nanosoc_gen"))
    elif man.files < 1:
        acc.pend("generated_rtl", "the archive holds no regular file")
    else:
        generated_rtl = {"committed": bool(rtl_committed),
                         "generator": {"component": gen["component"], "commit": gen["commit"],
                                       "tree_path": gen["tree_paths"][0]},
                         "archive": {"store": "disk", "path": name, "md5": man.md5, "bytes": man.bytes},
                         "manifest": {"store": "disk", "path": MD5S, "md5": man.manifest_md5}, "files": man.files}
    acc.checks.append(check("rtl.archive", "PASS" if generated_rtl else "UNVERIFIED",
                            "%s: %d file(s), md5 %s, manifest md5 %s" % (name, man.files, man.md5, man.manifest_md5),
                            requirement="G7"))
    return copy_archive, man, generated_rtl


def _silicon_outputs(acc: _Draft, silicon: dict | None, gds, netlist) -> dict | None:
    """G3: a silicon release records the GDS and P&R netlist hashes."""
    if not silicon:
        return None
    if gds is None or netlist is None:
        acc.pend("outputs", "a silicon release records the GDS and P&R netlist hashes (--gds, --netlist)")
        acc.checks.append(check("outputs.hashes", "UNVERIFIED", "silicon release without --gds and --netlist",
                                person=True, requirement="G3"))
        return None
    outputs = {}
    for key, p in (("gds", gds), ("pnr_netlist", netlist)):
        h, n = md5_file(Path(p))
        outputs[key] = {"file": Path(p).name, "md5": h, "bytes": n}
    acc.checks.append(check("outputs.hashes", "PASS", "GDS md5 %s (%d B), netlist md5 %s (%d B)" % (
        outputs["gds"]["md5"], outputs["gds"]["bytes"], outputs["pnr_netlist"]["md5"],
        outputs["pnr_netlist"]["bytes"]), requirement="G3"))
    return outputs


def _artefacts(acc: _Draft, artefacts: Sequence[tuple[str, str]]) -> list[dict]:
    """G3's other artefacts (file lists, bitstream, logs, source archive): hashed, and scanned as private."""
    missing = [k for k in ("flists", "bitstream", "logs") if k not in {a for a, _ in artefacts}]
    acc.checks.append(check("lock.artefacts", "WARN" if missing else "PASS",
                            ("G3 also lists %s: none given (--artefact KIND=PATH); the lock does not carry them"
                             % ", ".join(missing)) if missing else "file lists, bitstream and logs recorded",
                            requirement="G3"))
    arts = []
    for k, p in artefacts:
        h, n = md5_file(Path(p))
        arts.append({"kind": k, "store": "disk", "path": _safe_name(Path(p).name), "md5": h, "bytes": n})
        acc.scan_items.append(("artefact:%s" % Path(p).name, Path(p).read_bytes() if n <= MAX_SCAN_BYTES else None))
    return arts


def _finish_lock(acc: _Draft, lock: dict, pins: list[dict]) -> tuple[bool, str]:
    """Validate the lock (a draft lock with `pending` when anything is missing) and add its file. (complete, name)."""
    if not pins:
        acc.pend("pins", "the tree has no pin")
    complete = not acc.pending
    if complete:
        problems = contracts.validate("release-lock", lock)
    else:
        lock["pending"] = acc.pending
        problems = contracts.validate("release-lock", lock, "/$defs/draft_lock")
    if problems:
        raise ReleaseError("the lock the draft built does not meet its contract: %s" % "; ".join(problems[:5]))
    acc.checks.append(check("lock.complete", "PASS" if complete else "UNVERIFIED",
                            "release.lock.yaml written" if complete else "pending: %s" % "; ".join(
                                "%s (%s)" % (p["field"], p["reason"]) for p in acc.pending), requirement="G3"))
    lock_name = LOCK if complete else DRAFT_LOCK
    acc.files[lock_name] = ("# %s\n# %s\n" % (PUBLISH_NOTE, "Generated by nanosoc-rm release draft; never edit by "
                                                            "hand.")
                            + yaml.safe_dump(lock, sort_keys=False, width=120)).encode()
    return complete, lock_name


def _closing_steps(acc: _Draft, train: list[dict], kind: str, silicon: dict | None, errata_changed: bool) -> None:
    """The person steps after the checks: the release train (G4), the approvals (G5), signing and uploading."""
    if train:
        acc.steps.append({"step": "Release train (G4): finalise these %d component(s) bottom-up, deepest first, "
                                  "before this release: %s" % (len(train), ", ".join(
                                      "%s@%s" % (t["component"], t["commit"][:7]) for t in train)),
                          "who": "the owners of each component, approved once by David"})
    acc.steps += cl.approvals(kind, bool(silicon), errata_changed)
    acc.steps.append({"step": "Sign GATES.md once every gate has its evidence", "who": "the approvers above"})
    acc.steps.append({"step": "Upload the lock, ERRATA, GATES, the archive and its manifest to the release store "
                              "(D12) only after the vendor scan below is clean", "who": "the release approver"})


def _release_texts(acc: _Draft, release: dict, notes: dict, edoc: Any) -> None:
    """RELEASE_NOTES.md, ERRATA.md and ERRATA.html, and the C2 check that every listed change has a class."""
    acc.files["RELEASE_NOTES.md"] = notesmod.render(release, notes, edoc.revision, len(edoc.records),
                                                    ["%s: %s" % (p["field"], p["reason"])
                                                     for p in acc.pending]).encode()
    acc.files["ERRATA.md"] = edoc.markdown.encode()
    acc.files["ERRATA.html"] = edoc.html.encode()
    unclassed = sum(1 for e in notes["root"] + [x for c in notes["changed"] for x in c["entries"]]
                    if e["class"] == "unclassed")
    acc.checks.append(check("notes.classes", "WARN" if unclassed else "PASS",
                            "%d change(s) without a change class (C2)" % unclassed if unclassed else
                            "every listed change has a class", requirement="C2"))


def _vendor_scans(acc: _Draft, release: dict, complete: bool, outputs_given: bool) -> None:
    """D11: the files meant for sharing must be clean; the archive and artefacts are private.

    GATES.md, CHECKLIST.md and README.txt list the checks, so they are rendered once without the scan rows, scanned
    with the rest, then rendered again with them (the rows add only this tool's own check text)."""
    if acc.scan_items:
        acc.checks.append(_scan_check("vendor.scan-private", acc.scan_items, False))
    shared = dict(acc.files)
    shared.update(_person_files(release, acc.checks, acc.steps, complete))
    acc.checks.insert(len(acc.checks) - (1 if acc.scan_items else 0),
                      _scan_check("vendor.scan", list(shared.items()), outputs_given))
    acc.files.update(_person_files(release, acc.checks, acc.steps, complete))


def _person_files(release: dict, checks: list[dict], steps: list[dict], complete: bool) -> dict[str, bytes]:
    return {"GATES.md": cl.ledger(release, checks).encode(),
            "CHECKLIST.md": cl.checklist(release, checks, steps).encode(),
            "README.txt": _readme(release, complete).encode()}


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _safe_name(name: str) -> str:
    s = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).lstrip(".")
    return s or "file"


def _generator_pin(pins: list[dict], tree_path: str | None) -> dict | None:
    if tree_path:
        return next((p for p in pins if tree_path in p["tree_paths"]), None)
    gens = [p for p in pins if p["component"] == "nanosoc_gen"]
    return min(gens, key=lambda p: (p["depth"], p["tree_paths"][0])) if gens else None


def _lock_sources(docs: list[dict], config: Config) -> list[dict]:
    seen, out = set(), []
    for d in docs:
        for s in list(d["sources"]) + [s for st in d["fact"].get("hosts", {}).values() for s in st.get("sources", [])]:
            s = {"kind": s["kind"], "command": lockmod.public_command(s["command"], config)}
            key = (s["kind"], s["command"])
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out or [{"kind": "derived", "command": "walker.walk (no pin read)"}]


def _tag_check(cid: str, tag: str, commit: str, tags: dict[str, str], req: str) -> dict:
    at = tags.get(tag)
    if at is None:
        return check(cid, "UNVERIFIED", "%s does not exist yet: a person creates it (CHECKLIST.md)" % tag, person=True,
                     requirement=req)
    if at == commit:
        return check(cid, "PASS", "%s already points at %s" % (tag, commit[:12]), requirement=req)
    return check(cid, "FAIL", "%s exists at %s, not %s: tags are never moved (C10); choose another version" % (
        tag, at[:12], commit[:12]), requirement=req)


def _push_url(config: Config, repo: dict) -> str:
    return "%s%s.git" % (config.host(repo["host"])["ssh"], repo["path"])


def _tag_step(config: Config, repo: dict, tag: str, commit: str, message: str) -> dict:
    annotated = not tag.startswith("keep/")
    cmd = ("git tag %s%s %s%s\ngit push %s refs/tags/%s" % (
        "-a " if annotated else "", tag, commit, ' -m "%s"' % message if annotated else "", _push_url(config, repo), tag))
    return {"step": "Create tag %s on %s:%s at %s (in a checkout that holds the commit)" % (
        tag, repo["host"], repo["path"], commit[:12]), "who": "a person with tag rights on %s" % repo["host"],
        "command": cmd}


def _keep_tag_steps(config: Config, reg: Any, configuration: str, untagged: list[dict]) -> list[dict]:
    steps, used, done = [], {}, set()
    for p in untagged:
        canon = _repo(reg.canonical(p["component"])) if reg.component(p["component"]) else p["repo"]
        held = (p.get("protection") or {}).get("hosts", {}).get(canon["host"]) != "not-held"
        name = "keep/%s/%s" % (configuration, p["component"])
        if name in used and used[name] != p["commit"]:
            name = "keep/%s/%s" % (configuration, p["tree_paths"][0].rsplit("/", 1)[-1])
        if used.get(name) == p["commit"] and name in done:
            continue                                  # the same commit reached through another parent
        used.setdefault(name, p["commit"])
        done.add(name)
        if not held:
            steps.append({"step": "Push %s %s to its canonical host %s:%s first (B5 decision R1), then keep-tag it"
                                  % (p["component"], p["commit"][:12], canon["host"], canon["path"]),
                          "who": "the component's owner"})
        steps.append(_tag_step(config, canon, name, p["commit"], ""))
    return steps


def _train(reg: Any, pins: list[dict]) -> list[dict]:
    """G4: catalogued components whose pinned commit has no release tag, deepest first."""
    seen: dict[tuple[str, str], dict] = {}
    for p in pins:
        if (p.get("protection") or {}).get("by") == "release-tag" or reg.component(p["component"]) is None:
            continue
        row = seen.setdefault((p["component"], p["commit"]), {"component": p["component"], "commit": p["commit"],
                                                             "depth": p["depth"], "tree_paths": []})
        row["depth"] = max(row["depth"], p["depth"])
        row["tree_paths"] += p["tree_paths"]
    return sorted(seen.values(), key=lambda r: (-r["depth"], r["component"], r["commit"]))


def _notes(config, release, docs, pins, tags, since, earlier, comp) -> dict:
    prev_tag = since
    if prev_tag is None:
        older = [t for t in tags if RELEASE_TAG.match(t) and parse_version(t[1:]) < parse_version(release["version"])]
        prev_tag = max(older, key=lambda t: parse_version(t[1:])) if older else None
    old = tags.get(prev_tag) if prev_tag else None
    root, src = notesmod.entries(config, comp, old, release["commit"])
    notes = {"since": prev_tag, "root": root, "changed": [], "pins": len(pins), "first": prev_tag is None,
             "sources": [src]}
    prev = [e for e in earlier if parse_version(e["version"]) < parse_version(release["version"])]
    if prev:
        p = max(prev, key=lambda e: parse_version(e["version"]))
        lock = _lock_of(Path(p["dir"])) or {}
        before = {tp: x["commit"] for x in lock.get("pins") or [] for tp in x["tree_paths"]}
        notes["since"] = notes["since"] or "draft %s" % p["version"]
        notes["first"] = False
        for x in pins:
            tp = x["tree_paths"][0]
            if before.get(tp) != x["commit"]:
                ents, s = notesmod.entries(config, x["component"], before.get(tp), x["commit"])
                notes["changed"].append({"component": x["component"], "tree_path": tp, "old": before.get(tp),
                                         "new": x["commit"], "entries": ents[:50]})
                notes["sources"].append(s)
    return notes


def _scan_check(cid: str, items: list[tuple[str, bytes | None]], outputs_given: bool) -> dict:
    """D11. `vendor.scan`: the generated files meant for sharing (lock, errata, notes, checklist, ledger, manifest);
    a finding FAILs the draft. `vendor.scan-private`: the generated-RTL archive's members and the artefacts, which
    go only to the private release store (D12): a finding is a WARN that says so, never a reason to drop G7."""
    from nanosoc_rm.scan import scan_texts
    res = scan_texts(items)
    tail = "; the GDS and netlist are hashed, not scanned (foundry outputs are never uploaded publicly)" \
        if outputs_given else ""
    private = cid == "vendor.scan-private"
    what = "archive member(s) and artefact(s)" if private else "release file(s)"
    if res.verdict == "clean":
        return check(cid, "PASS", "%d %s scanned, no vendor content%s" % (res.files_scanned, what, tail),
                     requirement="D11")
    if res.verdict == "warn":
        return check(cid, "WARN", "%d file(s) hold allow-listed warn content%s" % (len(res.warnings), tail),
                     requirement="D11")
    if res.verdict == "finding":
        flagged = res.flagged_files
        listed = ", ".join(flagged[:10]) + (" ..." if len(flagged) > 10 else "")
        rules = ", ".join(sorted({f["rule"] for f in res.findings}))
        if private:
            return check(cid, "WARN", "%d of %d %s hold vendor content (rules %s): %s. Store them only in the "
                         "private release store (D12); never publish them." % (len(flagged), res.files_scanned, what,
                                                                                rules, listed), requirement="D11")
        return check(cid, "FAIL", "%d of %d %s hold vendor content (rules %s): %s%s. Never upload them." % (
            len(flagged), res.files_scanned, what, rules, listed, tail), requirement="D11")
    return check(cid, "UNVERIFIED", "scan could not vouch: %s" % res.why, requirement="D11")


def _readme(release: dict, complete: bool) -> str:
    return ("Release draft: %(component)s %(version)s (%(configuration)s), commit %(commit)s\n\n" % release
            + PUBLISH_NOTE + "\n\n"
            + "Files: %s, ERRATA.md/.html, RELEASE_NOTES.md, CHECKLIST.md (what a person does next), GATES.md (the "
              "gate ledger to sign), draft.json (every check, as the tool saw it), %s when an archive was given.\n"
            % (LOCK if complete else DRAFT_LOCK + " (not complete: see its `pending`)", MD5S)
            + "Re-check it later with: nanosoc-rm release verify %(configuration)s/%(version)s\n" % release)


def _check_target(target: Path, replace: bool) -> None:
    """The draft folder may be absent or empty; with `replace` it may also hold an earlier draft (a draft.json).
    Anything else is refused: --replace swaps the folder out and deletes it, so it must never meet a directory the
    tool did not write (a mistyped --out naming a checkout)."""
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ReleaseUsage("%s is not a directory the release manager wrote; choose another --out" % target)
    if not target.exists() or not any(target.iterdir()):
        return
    if not replace:
        raise ReleaseUsage("%s already holds a draft; pass --replace to rebuild it" % target)
    if not (target / DRAFT_FILE).is_file():
        raise ReleaseUsage("%s is not empty and holds no %s: --replace only rebuilds an earlier release draft, it "
                           "never deletes another directory" % (target, DRAFT_FILE))


def _write(target: Path, files: dict[str, bytes], copy_archive, obs: dict, replace: bool) -> None:
    """Write the folder next to its final place, then swap it in, so a reader never sees half a draft."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".%s." % target.name, dir=target.parent))
    try:
        for name, data in files.items():
            (tmp / name).write_bytes(data)
        if copy_archive is not None:
            shutil.copyfile(copy_archive[0], tmp / copy_archive[1])
        listed = []
        for p in sorted(tmp.iterdir()):
            h, n = md5_file(p)
            listed.append({"name": p.name, "md5": h, "bytes": n})
        obs["fact"]["files"] = listed
        problems = contracts.validate_observation(obs)
        if problems:
            raise ReleaseError("the release_draft document does not meet its contract: %s" % "; ".join(problems[:5]))
        (tmp / DRAFT_FILE).write_text(json.dumps(obs, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        old = None
        if target.exists() or target.is_symlink():
            _check_target(target, replace)
            old = target.with_name(".%s.old-%d" % (target.name, os.getpid()))
            os.replace(target, old)
        os.replace(tmp, target)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------------------------------------
def verify(ref: str | os.PathLike, *, config: Config | str | None = None, against: str | os.PathLike | None = None,
           walk: Callable[[str, Config], list[dict]] | None = None, command: str | None = None,
           store: bool = True) -> dict:
    """Re-check a draft against the mirrors and the registry now (release_verify observation, stored unless
    `store` is False). `against`: a
    directory holding build_soc/ to compare with the archive's manifest file for file (G7 done-when)."""
    config = config if isinstance(config, Config) else load_config(config)
    d = find_draft(config, str(ref))
    doc = read_draft(d)
    f = doc["fact"]
    cid, version, commit = f["configuration"], f["version"], f["commit"]
    started = clock.iso()
    checks: list[dict] = []
    drift: list[str] = []
    _verify_files(checks, drift, d, f)
    reg = _registry(config)
    cfg = reg.configuration(cid)
    if cfg is None:
        raise ReleaseError("configuration %s is no longer registered" % cid)
    lock = _lock_of(d) or {}
    docs = _verify_pins(checks, drift, config, reg, cfg, f, lock, walk)
    tags = lockmod.repo_tags(config, f["component"], _repo(reg.canonical(f["component"])))
    _verify_tags(checks, drift, tags, f, lock)
    edoc = errmod.document(reg, cid, release={"version": version, "tag": f["tag"], "commit": commit})
    if edoc.digest != f["errata"]["digest"] or edoc.revision != f["errata"]["revision"]:
        drift.append("the errata records changed since the draft (revision %s -> %s): draft again" % (
            f["errata"]["revision"], edoc.revision))
        checks.append(check("errata.digest", "FAIL", drift[-1], requirement="F4"))
    else:
        checks.append(check("errata.digest", "PASS", "the registry renders the same errata (revision %d)"
                            % edoc.revision, requirement="F4"))
    if f["archive"]:
        _verify_archive(checks, drift, d, f, lock, against)
    elif against is not None:
        raise ReleaseUsage("--against needs a draft made with --archive")
    verdict = ladder(c["verdict"] for c in checks)
    if drift and verdict in ("PASS", "WARN"):
        verdict = "FAIL"
    fact = {"configuration": cid, "version": version, "draft_generated_at": f["generated_at"], "checks": checks,
            "drift": drift, "verdict": verdict}
    times = [d_["observed_at"] for d_ in docs] + [started]
    obs = {"schema": 1, "kind": "release_verify", "subject": "release_verify:%s:%s" % (cid, version),
           "observed_at": min(times, key=clock.parse),
           "sources": [{"kind": "derived", "command": command or "nanosoc-rm release verify %s/%s" % (cid, version)}],
           "fact": fact}
    problems = contracts.validate_observation(obs)
    if problems:
        raise ReleaseError("the release_verify document does not meet its contract: %s" % "; ".join(problems[:5]))
    if store:
        from nanosoc_rm import db
        db.put(obs, config=config)
    return obs


def _verify_files(checks: list[dict], drift: list[str], d: Path, f: dict) -> None:
    """files.md5 and ledger.signed: every drafted file is unchanged, except the signature cells of GATES.md."""
    bad = []
    signed: list[str] | None = None
    for x in f["files"]:
        p = d / x["name"]
        if not p.is_file():
            bad.append("%s is missing" % x["name"])
        elif md5_file(p)[0] != x["md5"]:
            if x["name"] == "GATES.md":
                # G2: a person signs the ledger after the draft. Only the signature cells may change: the ledger
                # with those cells blank must still be the drafted file byte for byte.
                text = p.read_text(encoding="utf-8", errors="replace")
                blank, signed = cl.unsigned(text)
                if md5_bytes(blank.encode()) == x["md5"]:
                    continue
            bad.append("%s changed since the draft (md5)" % x["name"])
    listed = {x["name"] for x in f["files"]}
    extra = sorted(p.name for p in d.iterdir() if p.name != DRAFT_FILE and p.name not in listed)
    bad += ["%s was added after the draft" % n for n in extra]
    drift += bad
    checks.append(check("files.md5", "FAIL" if bad else "PASS", "; ".join(bad) or "%d file(s) as drafted%s" % (
        len(f["files"]), " (GATES.md differs only in its signatures)" if signed is not None else "")))
    checks.append(check("ledger.signed", "PASS" if signed else "NOT-RUN",
                        ("GATES.md is signed by: %s" % ", ".join(signed)) if signed else
                        "GATES.md is not signed yet (a person signs it once every gate has evidence)",
                        person=not signed, requirement="G2"))


def _verify_pins(checks: list[dict], drift: list[str], config: Config, reg: Any, cfg: dict, f: dict, lock: dict,
                 walk: Callable[[str, Config], list[dict]] | None) -> list[dict]:
    """release.commit and lock.pins: the drafted commit's tree, judged on the hosts now. Returns the walk."""
    cid, commit = f["configuration"], f["commit"]
    docs = (walk or _walk)(cid, config)
    now_root = _root_commit(config, cfg, _repo(reg.canonical(f["component"])), docs)
    if now_root != commit:
        checks.append(check("release.commit", "WARN", "the configuration's ref now resolves to %s, not the drafted %s: "
                            "the draft still describes %s, whose tree is re-walked below; draft again to release the "
                            "new commit" % (now_root[:12], commit[:12], commit[:12])))
        # The lock describes the drafted commit's tree: re-judge THAT tree against the hosts now, never skip it.
        docs = _walk_at(cid, config, commit)
    else:
        checks.append(check("release.commit", "PASS", "the configuration still resolves to %s" % commit[:12]))
    now_pins, _ = lockmod.lock_pins(docs)
    pin_checks = _pin_drift(lock.get("pins") or [], now_pins)
    drift += pin_checks
    checks.append(check("lock.pins", "FAIL" if pin_checks else "PASS",
                        "; ".join(pin_checks) or "every lock pin has the same commit, state and protection now"))
    return docs


def _verify_tags(checks: list[dict], drift: list[str], tags: dict[str, str], f: dict, lock: dict) -> None:
    """tag.<name>: the release (and silicon) tag is absent or at the drafted commit; a tag the draft saw is kept."""
    commit = f["commit"]
    drafted = {c["id"]: c["verdict"] for c in f["checks"]}
    sil = (lock.get("release") or {}).get("silicon")
    for name, cid_ in [(f["tag"], "release.tag")] + ([(sil["tag"], "release.silicon-tag")] if sil else []):
        at = tags.get(name)
        if at is not None and at != commit:
            drift.append("%s now exists at %s, not the drafted commit %s" % (name, at[:12], commit[:12]))
            checks.append(check("tag.%s" % name.replace("/", "-"), "FAIL", drift[-1]))
        elif at is None and drafted.get(cid_) == "PASS":
            # Tags are permanent (C10), so losing one the draft saw at the commit is drift.
            drift.append("%s pointed at %s when drafted and is gone now: tags are never deleted (C10)" % (
                name, commit[:12]))
            checks.append(check("tag.%s" % name.replace("/", "-"), "FAIL", drift[-1], requirement="C10"))
        else:
            checks.append(check("tag.%s" % name.replace("/", "-"), "PASS" if at else "NOT-RUN",
                                "%s %s" % (name, "points at the drafted commit" if at else "is not created yet")))


def _verify_archive(checks: list[dict], drift: list[str], d: Path, f: dict, lock: dict, against) -> None:
    """G7: the archive copy still matches its manifest, and `against` reproduces it file for file."""
    rtl = lock.get("generated_rtl") or {}
    name = (rtl.get("archive") or {}).get("path")
    copy = d / name if name else None
    if copy and copy.is_file():
        man = archmod.manifest(copy)
        ok = man.md5 == f["archive"]["md5"] and man.text.encode() == (d / MD5S).read_bytes()
        if not ok:
            drift.append("the archive copy no longer matches its manifest")
        checks.append(check("rtl.archive", "PASS" if ok else "FAIL", "%s re-read: %d file(s), md5 %s" % (
            name, man.files, man.md5), requirement="G7"))
    else:
        checks.append(check("rtl.archive", "NOT-RUN", "no copy of the archive in the draft (over %d MB)" % (
            MAX_ARCHIVE_COPY >> 20), requirement="G7"))
    if against is not None:
        diffs = archmod.compare((d / MD5S).read_text(), against)
        drift += ["build_soc %s" % x for x in diffs[:20]]
        checks.append(check("rtl.reproduces", "FAIL" if diffs else "PASS", ("%d difference(s): %s" % (
            len(diffs), "; ".join(diffs[:10]))) if diffs else "%s reproduces the archive file for file (%d files)"
            % (against, f["archive"]["files"]), requirement="G7"))


def _pin_drift(old: list[dict], new: list[dict]) -> list[str]:
    o = {p["tree_paths"][0]: p for p in old}
    n = {p["tree_paths"][0]: p for p in new}
    out = ["pin %s is gone from the tree" % t for t in sorted(set(o) - set(n))]
    out += ["pin %s is new in the tree" % t for t in sorted(set(n) - set(o))]
    for t in sorted(set(o) & set(n)):
        a, b = o[t], n[t]
        if a["commit"] != b["commit"]:
            out.append("%s: commit %s -> %s" % (t, a["commit"][:12], b["commit"][:12]))
        elif a["state"] != b["state"]:
            out.append("%s: state %s -> %s" % (t, a["state"], b["state"]))
        elif (a.get("protection") or {}).get("by") != (b.get("protection") or {}).get("by"):
            out.append("%s: protection %s -> %s" % (t, (a.get("protection") or {}).get("by"),
                                                     (b.get("protection") or {}).get("by")))
    return out
