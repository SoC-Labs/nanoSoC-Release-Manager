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
                                      draft_path, exit_for, ladder, md5_file, parse_version,
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


def _lock_of(d: Path) -> dict | None:
    for name in (LOCK, DRAFT_LOCK):
        if (d / name).is_file():
            try:
                return contracts.load_yaml((d / name).read_text())
            except (OSError, ValueError):
                return None
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
    parse_version(version)
    reg = _registry(config)
    cfg = reg.configuration(configuration)
    if cfg is None:
        raise ReleaseUsage("no registered configuration %r" % configuration)
    if kind not in (None, "fix", "feature", "tapeout"):
        raise ReleaseUsage("--kind must be fix, feature or tapeout")
    silicon = cfg.get("silicon")
    kind = kind or ("tapeout" if silicon else "feature")
    for label, p in (("--archive", archive), ("--gds", gds), ("--netlist", netlist)):
        if p is not None and not Path(p).is_file():
            raise ReleaseUsage("%s %s: no such file" % (label, p))
    for k, p in artefacts:
        if k not in ARTEFACT_KINDS:
            raise ReleaseUsage("--artefact kind %r is not one of %s" % (k, ", ".join(ARTEFACT_KINDS)))
        if not Path(p).is_file():
            raise ReleaseUsage("--artefact %s=%s: no such file" % (k, p))
    target = Path(out) if out is not None else draft_path(config, configuration, version)
    if target.exists() and any(target.iterdir()) and not replace:
        raise ReleaseUsage("%s already holds a draft; pass --replace to rebuild it" % target)
    waivers = [dict(w) for w in waivers]
    comp = cfg["component"]
    canon = _repo(reg.canonical(comp))
    started = clock.iso()

    docs = (walk or _walk)(configuration, config)
    commit = _root_commit(config, cfg, canon, docs)
    tag = "v%s" % version
    release = {"component": comp, "configuration": configuration, "version": version, "tag": tag, "commit": commit,
               "kind": kind, "silicon": bool(silicon)}
    checks: list[dict] = []
    steps: list[dict] = []
    pending: list[dict] = []
    sources = [{"kind": "derived", "command": command or "nanosoc-rm release draft %s %s" % (configuration, version)}]

    # -- pins (G3) and protection (B5, G2) ------------------------------------------------------------------
    pins, unjudged = lockmod.lock_pins(docs)
    if unjudged:
        pending.append({"field": "pins", "reason": "%d pin(s) without a state: %s" % (len(unjudged), "; ".join(unjudged))})
        checks.append(check("lock.pins-stated", "FAIL", "; ".join(unjudged), requirement="G3"))
    else:
        checks.append(check("lock.pins-stated", "PASS", "%d pin(s), each with its host and state" % len(pins),
                            requirement="G3"))
    unprotected = [p["tree_paths"][0] for p in pins if (p.get("protection") or {}).get("by", "none") == "none"]
    protected_where = ", ".join("%s %d" % (b, n) for b, n in sorted(
        _count(p.get("protection", {}).get("by", "none") for p in pins).items()))
    strict = bool(silicon or cfg.get("frozen"))
    checks.append(check("lock.pins-protected", ("FAIL" if strict else "WARN") if unprotected else "PASS",
                        ("unprotected on the pinned host: %s" % ", ".join(unprotected)) if unprotected else
                        "every pin protected on its pinned host (%s)" % protected_where, requirement="G3"))
    gaps = sorted({"%s (%s: %s)" % (p["tree_paths"][0], h, a) for p in pins
                   for h, a in (p.get("protection") or {}).get("hosts", {}).items()
                   if a in ("none", "not-held") and h != p["repo"]["host"]})
    checks.append(check("lock.pins-canonical", "WARN" if gaps else "PASS",
                        ("canonical host does not protect: %s (B5: push the commit there, then keep-tag it)"
                         % "; ".join(gaps)) if gaps else "every pin is also protected on its canonical host",
                        requirement="G2"))
    untagged = [p for p in pins if (p.get("protection") or {}).get("by") not in ("release-tag", "keep-tag")
                and not _waived(p, waivers)]
    waived_n = sum(1 for p in pins if (p.get("protection") or {}).get("by") not in ("release-tag", "keep-tag")
                   and _waived(p, waivers))
    checks.append(check("lock.pins-tagged", "FAIL" if untagged else ("WAIVED" if waived_n else "PASS"),
                        ("%d pin(s) neither tagged nor keep-tagged nor waived: %s" % (
                            len(untagged), ", ".join(p["tree_paths"][0] for p in untagged))) if untagged else
                        "every pin tagged or keep-tagged%s" % (" (%d covered by a waiver)" % waived_n if waived_n else ""),
                        requirement="G2"))
    steps += _keep_tag_steps(config, reg, configuration, untagged)

    # -- tags (C1, C10) ---------------------------------------------------------------------------------------
    tags = lockmod.repo_tags(config, comp, canon)
    sources.append({"kind": "git", "command": "git -C <mirrors>/%s.git for-each-ref %s/tags" % (
        comp, ref_prefix(canon["host"], canon["path"]))})
    checks.append(_tag_check("release.tag", tag, commit, tags, "C10"))
    newer = [t for t in tags if RELEASE_TAG.match(t) and tags[t] != commit
             and parse_version(t[1:]) >= parse_version(version)]
    checks.append(check("release.version-order", "FAIL" if newer else "PASS",
                        ("release tag(s) %s at other commits are not older than %s" % (", ".join(sorted(newer)), version))
                        if newer else "%s is newer than every release tag at another commit" % version,
                        requirement="C1"))
    if tags.get(tag) != commit:
        steps.append(_tag_step(config, canon, tag, commit, "%s %s (%s)" % (comp, version, configuration)))
    sil_tag = None
    if silicon:
        sil_tag = "silicon/%s-%s" % (silicon["chip"], silicon["revision"])
        checks.append(_tag_check("release.silicon-tag", sil_tag, commit, tags, "C10"))
        if tags.get(sil_tag) != commit:
            steps.append(_tag_step(config, canon, sil_tag, commit, "%s silicon %s" % (silicon["chip"], silicon["revision"])))
    if candidate is not None:
        steps.insert(0, _tag_step(config, canon, "%s-rc.%d" % (tag, candidate), commit, "candidate %d" % candidate))

    # -- errata (F4) ------------------------------------------------------------------------------------------
    earlier = _earlier(config, configuration, version)
    edoc = errmod.document(reg, configuration, release=release)
    checks.append(errmod.revision_check(edoc, [dict(e["errata"], version=e["version"]) for e in earlier]))
    cat_a = [e["id"] for e in edoc.records if e.get("category") == "A" and e["status"] in errmod.OPEN_STATUSES]
    undecided = [e["id"] for e in edoc.records if e.get("category") is None and e["status"] in errmod.OPEN_STATUSES]
    if cat_a:
        checks.append(check("errata.category-a", "FAIL", "Category A erratum open: %s; each needs David's recorded "
                            "decision before release" % ", ".join(cat_a), requirement="G2"))
    else:
        checks.append(check("errata.category-a", "WARN" if undecided else "PASS",
                            ("no Category A erratum; category undecided for %s" % ", ".join(undecided)) if undecided
                            else "no open Category A erratum", requirement="G2"))
    sources.append({"kind": "registry", "command": "errata records of %s in the registry at %s" % (
        configuration, reg.commit[:12] if reg.commit else "an uncommitted checkout")})
    errata_changed = bool(earlier) and all(e["errata"]["revision"] != edoc.revision for e in earlier)

    # -- generated RTL (G7) and silicon outputs (G3) ---------------------------------------------------------
    files: dict[str, bytes] = {}
    scan_items: list[tuple[str, bytes | None]] = []
    copy_archive = None
    man = None
    generated_rtl = None
    if archive is not None:
        try:
            man = archmod.manifest(archive, keep_members=True)
        except archmod.ArchiveError as e:
            raise ReleaseUsage(str(e)) from None
        files[MD5S] = man.text.encode()
        scan_items += [("archive:%s" % n, b if len(b) <= MAX_SCAN_BYTES else None) for n, b in man.members]
        name = _safe_name(Path(archive).name)
        if man.bytes <= MAX_ARCHIVE_COPY:
            copy_archive = (Path(archive), name)
        gen = _generator_pin(pins, generator)
        if gen is None:
            pending.append({"field": "generated_rtl", "reason": "no generator pin (%s) in the tree to record"
                            % (generator or "component nanosoc_gen")})
        else:
            generated_rtl = {"committed": bool(rtl_committed),
                             "generator": {"component": gen["component"], "commit": gen["commit"],
                                           "tree_path": gen["tree_paths"][0]},
                             "archive": {"store": "disk", "path": name, "md5": man.md5, "bytes": man.bytes},
                             "manifest": {"store": "disk", "path": MD5S, "md5": man.manifest_md5}, "files": man.files}
            if man.files < 1:
                pending.append({"field": "generated_rtl", "reason": "the archive holds no regular file"})
                generated_rtl = None
        checks.append(check("rtl.archive", "PASS" if generated_rtl else "UNVERIFIED",
                            "%s: %d file(s), md5 %s, manifest md5 %s" % (name, man.files, man.md5, man.manifest_md5),
                            requirement="G7"))
    else:
        pending.append({"field": "generated_rtl", "reason": "no generated-RTL archive given (--archive): G7 needs "
                        "build_soc/ archived with an md5 manifest"})
        checks.append(check("rtl.archive", "UNVERIFIED", "no archive given: run the draft again with --archive "
                            "<build_soc.tar.gz>", person=True, requirement="G7"))
    outputs = None
    if silicon:
        if gds is not None and netlist is not None:
            outputs = {}
            for key, p in (("gds", gds), ("pnr_netlist", netlist)):
                h, n = md5_file(Path(p))
                outputs[key] = {"file": Path(p).name, "md5": h, "bytes": n}
            checks.append(check("outputs.hashes", "PASS", "GDS md5 %s (%d B), netlist md5 %s (%d B)" % (
                outputs["gds"]["md5"], outputs["gds"]["bytes"], outputs["pnr_netlist"]["md5"],
                outputs["pnr_netlist"]["bytes"]), requirement="G3"))
        else:
            pending.append({"field": "outputs", "reason": "a silicon release records the GDS and P&R netlist hashes "
                            "(--gds, --netlist)"})
            checks.append(check("outputs.hashes", "UNVERIFIED", "silicon release without --gds and --netlist",
                                person=True, requirement="G3"))
    arts = []
    for k, p in artefacts:
        h, n = md5_file(Path(p))
        arts.append({"kind": k, "store": "disk", "path": _safe_name(Path(p).name), "md5": h, "bytes": n})
        scan_items.append(("artefact:%s" % Path(p).name, Path(p).read_bytes() if n <= MAX_SCAN_BYTES else None))

    # -- evidence (G2) ------------------------------------------------------------------------------------------
    tchecks, facts = cl.tier_checks(config, configuration, commit, kind)
    checks += tchecks
    checks += cl.clone_checks(facts, cfg.get("upgrade_from"))
    checks.append(check("ledger.signed", "UNVERIFIED", "GATES.md is unsigned: a person signs it once every gate has "
                        "evidence", person=True, requirement="G2"))

    # -- the lock -----------------------------------------------------------------------------------------------
    generated_at = clock.iso()
    lock: dict[str, Any] = {"schema": 1, "release": {"component": comp, "configuration": configuration,
                                                       "version": version, "tag": tag, "commit": commit},
                            "generated_at": generated_at,
                            "generated_by": {"tool": "nanosoc-release-manager", "version": __version__},
                            "sources": _lock_sources(docs, config), "pins": pins, "waivers": waivers}
    if silicon:
        lock["release"]["silicon"] = {"chip": silicon["chip"], "revision": silicon["revision"], "tag": sil_tag}
    if generated_rtl:
        lock["generated_rtl"] = generated_rtl
    if outputs:
        lock["outputs"] = outputs
    lock.update(errata={"file": "ERRATA.md", "revision": edoc.revision}, gate_ledger={"file": "GATES.md"},
                artefacts=arts)
    if not pins:
        pending.append({"field": "pins", "reason": "the tree has no pin"})
    complete = not pending
    if complete:
        problems = contracts.validate("release-lock", lock)
    else:
        lock["pending"] = pending
        problems = contracts.validate("release-lock", lock, "/$defs/draft_lock")
    if problems:
        raise ReleaseError("the lock the draft built does not meet its contract: %s" % "; ".join(problems[:5]))
    checks.append(check("lock.complete", "PASS" if complete else "UNVERIFIED",
                        "release.lock.yaml written" if complete else "pending: %s" % "; ".join(
                            "%s (%s)" % (p["field"], p["reason"]) for p in pending), requirement="G3"))
    lock_name = LOCK if complete else DRAFT_LOCK
    files[lock_name] = ("# %s\n# %s\n" % (PUBLISH_NOTE, "Generated by nanosoc-rm release draft; never edit by hand.")
                        + yaml.safe_dump(lock, sort_keys=False, width=120)).encode()

    # -- train (G4), notes, checklist, ledger ---------------------------------------------------------------------
    train = _train(reg, pins)
    if train:
        steps.append({"step": "Release train (G4): finalise these %d component(s) bottom-up, deepest first, before "
                              "this release: %s" % (len(train), ", ".join("%s@%s" % (t["component"], t["commit"][:7])
                                                                          for t in train)),
                      "who": "the owners of each component, approved once by David"})
    steps += cl.approvals(kind, bool(silicon), errata_changed)
    steps.append({"step": "Sign GATES.md once every gate has its evidence", "who": "the approvers above"})
    steps.append({"step": "Upload the lock, ERRATA, GATES, the archive and its manifest to the release store (D12) "
                          "only after the vendor scan below is clean", "who": "the release approver"})
    notes = _notes(config, release, docs, pins, tags, since, earlier, comp)
    files["RELEASE_NOTES.md"] = notesmod.render(release, notes, edoc.revision, len(edoc.records),
                                                ["%s: %s" % (p["field"], p["reason"]) for p in pending]).encode()
    files["ERRATA.md"] = edoc.markdown.encode()
    files["ERRATA.html"] = edoc.html.encode()
    unclassed = sum(1 for e in notes["root"] + [x for c in notes["changed"] for x in c["entries"]]
                    if e["class"] == "unclassed")
    checks.append(check("notes.classes", "WARN" if unclassed else "PASS",
                        "%d change(s) without a change class (C2)" % unclassed if unclassed else
                        "every listed change has a class", requirement="C2"))

    # -- vendor scan (D11): the files meant for sharing must be clean; the archive and artefacts are private -----
    checks.append(_scan_check("vendor.scan", [(n, b) for n, b in files.items()], gds is not None or netlist is not None))
    if scan_items:
        checks.append(_scan_check("vendor.scan-private", scan_items, False))
    files["GATES.md"] = cl.ledger(release, checks).encode()
    files["CHECKLIST.md"] = cl.checklist(release, checks, steps).encode()
    files["README.txt"] = _readme(release, complete).encode()

    verdict = ladder(c["verdict"] for c in checks)
    machine_ok = all(c["verdict"] in ("PASS", "WARN", "WAIVED", "NOT-RUN") for c in checks if not c["person"])
    ready = complete and machine_ok and verdict not in ("FAIL", "INFRA")
    fact = {"configuration": configuration, "component": comp, "version": version, "tag": tag, "commit": commit,
            "kind": kind, "silicon": bool(silicon), "generated_at": generated_at,
            "lock": {"file": lock_name, "complete": complete, "pins": len(pins), "unprotected": unprotected,
                     "pending": ["%s: %s" % (p["field"], p["reason"]) for p in pending]},
            "errata": {"revision": edoc.revision, "records": len(edoc.records), "digest": edoc.digest},
            "archive": None if man is None else {"md5": man.md5, "bytes": man.bytes, "files": man.files,
                                                 "manifest_md5": man.manifest_md5},
            "checks": checks, "verdict": verdict, "ready": ready, "person_steps": steps, "train": train,
            "files": [], "published": False}
    times = [d["observed_at"] for d in docs] + [d["observed_at"] for d in facts] + [started]
    obs = {"schema": 1, "kind": "release_draft", "subject": "release_draft:%s:%s" % (configuration, version),
           "observed_at": min(times, key=clock.parse), "sources": sources, "fact": fact}
    _write(target, files, copy_archive, obs, replace)
    if store:
        from nanosoc_rm import db
        db.put(obs, config=config)
    return obs


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
        if target.exists():
            if not replace and any(target.iterdir()):
                raise ReleaseUsage("%s already holds a draft; pass --replace to rebuild it" % target)
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
    checks, drift = [], []
    # files as recorded
    bad = []
    for x in f["files"]:
        p = d / x["name"]
        if not p.is_file():
            bad.append("%s is missing" % x["name"])
        elif md5_file(p)[0] != x["md5"]:
            bad.append("%s changed since the draft (md5)" % x["name"])
    extra = sorted(p.name for p in d.iterdir() if p.name != DRAFT_FILE and p.name not in {x["name"] for x in f["files"]})
    bad += ["%s was added after the draft" % n for n in extra]
    drift += bad
    checks.append(check("files.md5", "FAIL" if bad else "PASS", "; ".join(bad) or "%d file(s) as drafted" % len(f["files"])))
    # pins
    reg = _registry(config)
    cfg = reg.configuration(cid)
    if cfg is None:
        raise ReleaseError("configuration %s is no longer registered" % cid)
    docs = (walk or _walk)(cid, config)
    lock = _lock_of(d) or {}
    now_pins, _ = lockmod.lock_pins(docs)
    now_root = _root_commit(config, cfg, _repo(reg.canonical(f["component"])), docs)
    if now_root != commit:
        checks.append(check("release.commit", "WARN", "the configuration's ref now resolves to %s, not the drafted %s: "
                            "the draft still describes %s; draft again to release the new commit" % (
                                now_root[:12], commit[:12], commit[:12])))
        pin_checks = []
    else:
        checks.append(check("release.commit", "PASS", "the configuration still resolves to %s" % commit[:12]))
        pin_checks = _pin_drift(lock.get("pins") or [], now_pins)
        drift += pin_checks
        checks.append(check("lock.pins", "FAIL" if pin_checks else "PASS",
                            "; ".join(pin_checks) or "every lock pin has the same commit, state and protection now"))
    # tags
    tags = lockmod.repo_tags(config, f["component"], _repo(reg.canonical(f["component"])))
    for name in [f["tag"]] + ([lock["release"]["silicon"]["tag"]] if lock.get("release", {}).get("silicon") else []):
        at = tags.get(name)
        if at is not None and at != commit:
            drift.append("%s now exists at %s, not the drafted commit %s" % (name, at[:12], commit[:12]))
            checks.append(check("tag.%s" % name.replace("/", "-"), "FAIL", drift[-1]))
        else:
            checks.append(check("tag.%s" % name.replace("/", "-"), "PASS" if at else "NOT-RUN",
                                "%s %s" % (name, "points at the drafted commit" if at else "is not created yet")))
    # errata
    edoc = errmod.document(reg, cid, release={"version": version, "tag": f["tag"], "commit": commit})
    if edoc.digest != f["errata"]["digest"] or edoc.revision != f["errata"]["revision"]:
        drift.append("the errata records changed since the draft (revision %s -> %s): draft again" % (
            f["errata"]["revision"], edoc.revision))
        checks.append(check("errata.digest", "FAIL", drift[-1], requirement="F4"))
    else:
        checks.append(check("errata.digest", "PASS", "the registry renders the same errata (revision %d)" % edoc.revision,
                            requirement="F4"))
    # archive (G7)
    if f["archive"]:
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
