"""Build the two published records (CR-ART-publish): a release manifest from a local release draft, a tapeout
record from a tapeout draft. Pure apart from reading the draft files and, for referenced artefacts, the store
(GET only, through `resolve_artefact`).

    release_manifest(config, configuration, version, artefacts=(), store=None) -> (manifest, documents)
    tapeout_record(config, tapeout_id | path, store=None) -> record
    canonical(doc) -> bytes                        the exact bytes published and signed
    diff(old, new) -> [str]                        field-level changes, for the preview
    parse_artefact("ROLE=REPO/PATH") -> (role, path)
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from nanosoc_rm import __version__, contracts
from nanosoc_rm.config import Config
from nanosoc_rm.publish.core import PublishRefused, PublishUsage, TAPEOUT_STATES, settings

__all__ = ["release_manifest", "tapeout_record", "canonical", "diff", "parse_artefact", "resolve_artefact",
           "DOCUMENTS", "ARTEFACT_ROLES", "tapeout_draft_path", "pins_md5", "load_tapeout_draft"]

# The draft files uploaded beside a release manifest: the ones the draft's `vendor.scan` covers (D11). The
# generated-RTL archive copy is never uploaded from here: it is private (CR-R8), referenced by --artefact.
DOCUMENTS = ("release.lock.yaml", "ERRATA.md", "ERRATA.html", "RELEASE_NOTES.md", "GATES.md", "CHECKLIST.md",
             "build_soc.md5sums.txt")
ARTEFACT_ROLES = ("archive", "gds", "netlist", "bitstream", "logs", "flists", "signoff", "foundry-return", "doc",
                  "other")
RAW_MD5_PROPS = ("rm.raw_md5", "pnr.raw_md5", "file.raw_md5")
LAYOUT_PROPS = ("rm.layout_hash", "layout.canonical_hash")
_STORE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*(/[A-Za-z0-9_.+@=-][A-Za-z0-9_.+@=-]*)+\Z")
# A backfill draft's free-text tapeout_status, read as one of the four states only when it says exactly that.
_STATUS_WORDS = {"draft": "draft", "submitted": "submitted", "accepted": "accepted", "accepted-by-foundry": "accepted",
                 "fabricated": "fabricated"}
_FOUNDRY_VERDICTS = ("accepted", "accepted-with-findings", "rejected", "pending", "unknown")


def canonical(doc: dict) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _digest(data: bytes) -> dict:
    return {"md5": hashlib.md5(data).hexdigest(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def pins_md5(pins: Iterable[tuple[str, str]]) -> str:
    return hashlib.md5("".join("%s %s\n" % (p, c) for p, c in sorted(pins)).encode()).hexdigest()


def parse_artefact(text: str) -> tuple[str, str]:
    role, sep, path = (text or "").partition("=")
    role, path = role.strip(), path.strip().strip("/")
    if not sep or role not in ARTEFACT_ROLES:
        raise PublishUsage("--artefact %r: want ROLE=REPO/PATH with ROLE one of %s" % (text[:80], ", ".join(ARTEFACT_ROLES)))
    if not _STORE_PATH.match(path) or ".." in path.split("/"):
        raise PublishUsage("--artefact %r: %r is not a plain store path REPO/PATH" % (text[:80], path[:120]))
    return role, path


def resolve_artefact(store: Any, role: str, path: str, expected_raw_md5: str | None) -> dict:
    """One referenced artefact, read from the store (GET only): its stored checksums, and its raw md5 joined to
    the lock by the stored object's md5 or by its raw-md5 property, never by the compressed checksum alone."""
    st = store.storage(path)
    if st is None:
        raise PublishRefused("artefact %s (%s) is not in the store" % (path, role))
    sums = st.get("checksums") or {}
    sha256, md5 = sums.get("sha256"), sums.get("md5")
    if not (isinstance(sha256, str) and re.match(r"^[0-9a-f]{64}\Z", sha256) and isinstance(md5, str)
            and re.match(r"^[0-9a-f]{32}\Z", md5)):
        raise PublishRefused("artefact %s: the store gives no sha256 and md5 for it" % path)
    try:
        size = int(st.get("size"))
    except (TypeError, ValueError):
        raise PublishRefused("artefact %s: the store gives no size for it" % path) from None
    props = store.properties(path)
    prop_raw = next((v[0] for k in RAW_MD5_PROPS for v in [props.get(k) or []] if v), None)
    if expected_raw_md5:
        if md5 == expected_raw_md5:
            raw, how = md5, "lock"
        elif prop_raw == expected_raw_md5:
            raw, how = prop_raw, "property"
        else:
            raise PublishRefused("artefact %s (%s): the lock's raw md5 %s matches neither the stored object (%s) nor "
                                 "its raw-md5 property (%s)" % (path, role, expected_raw_md5, md5, prop_raw or "none"))
    elif prop_raw:
        raw, how = prop_raw, "property"
    elif not path.endswith((".zst", ".gz", ".xz", ".bz2")):
        raw, how = md5, "stored"
    else:
        raw, how = None, None
    return {"role": role, "path": path, "stored_sha256": sha256, "stored_md5": md5, "bytes": size, "raw_md5": raw,
            "raw_md5_from": how}


# ---------------------------------------------------------------------------------------------------------
# Release manifest
# ---------------------------------------------------------------------------------------------------------
def _draft(config: Config, configuration: str, version: str) -> tuple[Path, dict, dict, bytes]:
    from nanosoc_rm.releases.builder import find_draft, read_draft
    from nanosoc_rm.releases.core import ReleaseError, ReleaseUsage
    try:
        d = find_draft(config, "%s/%s" % (configuration, version))
        doc = read_draft(d)
    except ReleaseUsage as e:
        raise PublishUsage(str(e)) from None
    except (ReleaseError, OSError, ValueError) as e:
        raise PublishRefused("the release draft does not read: %s" % e) from None
    f = doc["fact"]
    if not f["lock"]["complete"] or not (d / "release.lock.yaml").is_file():
        raise PublishRefused("the draft's lock is not complete (pending: %s): draft it again with every input"
                             % ("; ".join(f["lock"]["pending"]) or "release.lock.yaml missing"))
    if not f["ready"]:
        bad = [c["id"] for c in f["checks"] if not c["person"] and c["verdict"] not in ("PASS", "WARN", "WAIVED",
                                                                                        "NOT-RUN")]
        raise PublishRefused("the draft is not ready (verdict %s; checks the tool makes that are not clear: %s)"
                             % (f["verdict"], ", ".join(bad) or "none"))
    lock_bytes = (d / "release.lock.yaml").read_bytes()
    lock = contracts.load_yaml(lock_bytes.decode("utf-8"))
    problems = contracts.validate("release-lock", lock)
    if problems:
        raise PublishRefused("release.lock.yaml does not meet its contract: %s" % "; ".join(problems[:3]))
    rel = lock["release"]
    if (rel["configuration"], rel["version"], rel["commit"]) != (configuration, version, f["commit"]):
        raise PublishRefused("release.lock.yaml names %s %s %s, the draft %s %s %s" % (
            rel["configuration"], rel["version"], rel["commit"], configuration, version, f["commit"]))
    files = {x["name"]: x["md5"] for x in f.get("files") or []}
    for name in DOCUMENTS:
        p = d / name
        if p.is_file() and name in files and hashlib.md5(p.read_bytes()).hexdigest() != files[name]:
            raise PublishRefused("%s changed since the draft was written (md5 differs from draft.json): run "
                                 "`nanosoc-rm release verify %s/%s`" % (name, configuration, version))
    return d, doc, lock, lock_bytes


def release_manifest(config: Config, configuration: str, version: str, *, artefacts: Sequence[tuple[str, str]] = (),
                     store: Any = None) -> tuple[dict, dict[str, bytes]]:
    """(manifest, {document name: bytes}) for a ready local release draft. Referenced artefacts need `store`."""
    d, doc, lock, lock_bytes = _draft(config, configuration, version)
    f = doc["fact"]
    rel = lock["release"]
    from nanosoc_rm.releases.builder import _registry, _repo
    repo = _repo(_registry(config).canonical(rel["component"]))
    pins = [{"tree_path": p["tree_paths"][0], "component": p["component"], "repo": dict(p["repo"]),
             "commit": p["commit"]} for p in lock["pins"]]
    pins.sort(key=lambda p: p["tree_path"])
    docs = {name: (d / name).read_bytes() for name in DOCUMENTS if (d / name).is_file()}
    expected = {}
    if lock.get("generated_rtl"):
        expected["archive"] = lock["generated_rtl"]["archive"].get("md5")
    if lock.get("outputs"):
        expected["gds"] = lock["outputs"]["gds"]["md5"]
        expected["netlist"] = lock["outputs"]["pnr_netlist"]["md5"]
    arts = []
    seen = set()
    for role, path in artefacts:
        if path in seen:
            raise PublishUsage("--artefact %s is given twice" % path)
        seen.add(path)
        if store is None:
            raise PublishUsage("a referenced artefact (--artefact) needs the store")
        arts.append(resolve_artefact(store, role, path, expected.get(role)))
    arts.sort(key=lambda a: (a["role"], a["path"]))
    silicon = rel.get("silicon")
    manifest = {
        "schema": 1, "kind": "release-manifest", "project": settings(config).project(rel["component"]),
        "component": rel["component"], "configuration": configuration, "version": version, "tag": rel["tag"],
        "commit": rel["commit"], "release_kind": f["kind"],
        "silicon": dict(silicon) if silicon else None, "repo": repo, "pins": pins,
        "pins_md5": pins_md5((p["tree_path"], p["commit"]) for p in pins),
        "lock": dict(file="release.lock.yaml", **_digest(lock_bytes)),
        "errata": {"revision": f["errata"]["revision"], "records": f["errata"]["records"],
                   "digest": f["errata"]["digest"]},
        "gates": {"verdict": f["verdict"], "ready": True,
                  "checks": [{"id": c["id"], "verdict": c["verdict"]} for c in f["checks"]]},
        "artefacts": arts,
        "documents": sorted(({"name": n, **_digest(b)} for n, b in docs.items()), key=lambda x: x["name"]),
        "generated_by": {"tool": "nanosoc-release-manager", "version": __version__},
        "draft": {"generated_at": f["generated_at"], "observed_at": doc["observed_at"]},
    }
    if lock.get("outputs"):
        manifest["outputs"] = {"gds": dict(lock["outputs"]["gds"]), "pnr_netlist": dict(lock["outputs"]["pnr_netlist"])}
    problems = contracts.validate("release-manifest", manifest)
    if problems:
        raise PublishRefused("the manifest does not meet its contract: %s" % "; ".join(problems[:5]))
    return manifest, docs


# ---------------------------------------------------------------------------------------------------------
# Tapeout record
# ---------------------------------------------------------------------------------------------------------
def tapeout_draft_path(config: Config, tapeout_id: str) -> Path:
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,127}\Z", tapeout_id or ""):
        raise PublishUsage("%r is not a tapeout id" % str(tapeout_id)[:80])
    reg = Path(config.require("registry", "path"))
    return reg / "tapeouts" / ("%s.tapeout.yaml" % tapeout_id)


def load_tapeout_draft(path: Path) -> dict:
    try:
        doc = contracts.load_yaml(path.read_text(encoding="utf-8"))
    except OSError:
        raise PublishUsage("no tapeout draft at %s" % path) from None
    except ValueError as e:
        raise PublishRefused("the tapeout draft does not read: %s" % str(e)[:300]) from None
    problems = contracts.validate("tapeout-record", doc, "/$defs/tapeout_draft")
    if problems:
        raise PublishRefused("the tapeout draft does not meet its contract: %s" % "; ".join(problems[:5]))
    return doc


def _status(rec: dict) -> str:
    st = rec.get("status")
    if st in TAPEOUT_STATES:
        return st
    word = _STATUS_WORDS.get(str(rec.get("tapeout_status") or "").strip().lower())
    if word:
        return word
    raise PublishRefused("tapeout status %r is not one of %s: set record.status in the draft" % (
        rec.get("tapeout_status"), ", ".join(TAPEOUT_STATES)))


def _need(rec: dict, pointer: str) -> Any:
    node: Any = rec
    for part in pointer.strip("/").split("/"):
        node = node.get(part) if isinstance(node, dict) else None
    if node in (None, "", [], {}):
        raise PublishRefused("the tapeout draft has no %s (null means unknown; a published record needs it)" % pointer)
    return node


def _date(value: Any) -> str | None:
    v = str(value) if value is not None else None
    return v if v and re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", v) else None


def tapeout_record(config: Config, tapeout_id: str | None = None, *, draft: str | Path | None = None,
                   store: Any = None) -> dict:
    """The tapeout record from its draft (`<registry>/tapeouts/<id>.tapeout.yaml`, or `draft`). With `store`, the
    stored GDS is read (GET only) and must agree with the draft's raw md5 and layout hash."""
    path = Path(draft) if draft is not None else tapeout_draft_path(config, tapeout_id or "")
    doc = load_tapeout_draft(path)
    rec = doc["record"]
    if tapeout_id and rec["tapeout_id"] != tapeout_id:
        raise PublishRefused("the draft at %s is tapeout %s, not %s" % (path.name, rec["tapeout_id"], tapeout_id))
    status = _status(rec)
    release = rec.get("release") or {}
    version = release.get("version") if isinstance(release, dict) else None
    if not version:
        raise PublishRefused("the tapeout draft names no release folder (record.release: {project, version})")
    src, repo, commit, pins = _source(rec)
    gds = rec.get("gds") or {}
    raw_md5 = _need(rec, "/gds/raw_md5")
    layout = gds.get("layout_hash") or gds.get("canonical_sha256")
    store_path = gds.get("store_path")
    stored_sha = gds.get("stored_sha256") or gds.get("stored_zst_sha256")
    raw_from = None
    if store is not None and store_path:
        raw_from, stored_sha, layout = _stored_gds(store, store_path, raw_md5, layout, stored_sha)
    elif status != "draft" and store is not None:
        raise PublishRefused("a %s tapeout record needs the GDS's store path (record.gds.store_path)" % status)
    foundry = _foundry(rec)
    dates = {k: _date(v) for k, v in (rec.get("submission") or rec.get("dates") or {}).items()}
    returns = [dict(r) for r in rec.get("foundry_returns") or [] if isinstance(r, dict)]
    cites = next((r.get("cites_raw_md5") for r in returns if r.get("cites_raw_md5")), None)
    signoff = _signoff_rows(rec.get("signoff") or [])
    errata = _errata(rec)
    pdk = rec.get("pdk") or {}
    record = {
        "schema": 1, "kind": "tapeout-record", "tapeout_id": rec["tapeout_id"], "project": rec["project"],
        "version": version, "block": rec.get("block"), "chip": rec["chip"],
        "silicon_revision": rec.get("silicon_revision"), "configuration": rec.get("configuration"), "status": status,
        "foundry": foundry, "pdk": {"name": _need(rec, "/pdk/name"), "version": pdk.get("version")},
        "libraries": _named(rec.get("libraries"), "name", lambda x: {"name": str(x.get("name")),
                                                                     "version": x.get("version")}),
        "submission_date": _submission_date(rec),
        "dates": dates,
        "foundry_verdict": _foundry_verdict(rec.get("foundry_verdict") or {}, status, dates, returns, cites),
        "source": {"repo": dict(repo), "tags": list(src.get("tags") or []), "commit": commit, "pins": dict(pins),
                   "build": src.get("build"), "silicon_equivalent": list(src.get("silicon_equivalent") or [])},
        "toolkit": rec.get("toolkit"),
        "gds": {"file": gds.get("file"), "raw_md5": raw_md5, "raw_sha256": gds.get("raw_sha256"),
                "bytes": gds.get("bytes"), "layout_hash": layout, "layout_method": _layout_method(gds),
                "store_path": store_path, "stored_sha256": stored_sha, "raw_md5_from": raw_from},
        "signoff": _named(signoff, "name", dict),
        "foundry_returns": returns, "errata": errata,
        "documents": _named(rec.get("documents"), "kind", dict),
        "release": {"project": release.get("project") or rec["project"], "version": version,
                    "manifest_sha256": release.get("manifest_sha256")},
        "generated_by": {"tool": "nanosoc-release-manager", "version": __version__},
    }
    if cites and cites != raw_md5:
        raise PublishRefused("the foundry return cites raw md5 %s, the draft's GDS is %s" % (cites, raw_md5))
    record["release"]["manifest_sha256"] = _release_join(config, rec, release, version, store)
    problems = contracts.validate("tapeout-record", record)
    if problems:
        raise PublishRefused("the tapeout record does not meet its contract: %s" % "; ".join(problems[:5]))
    return record


def _source(rec: dict) -> tuple[dict, Any, str, Any]:
    """(source, repo, commit, pins) of the draft: the repo, the commit (or the tag's commit) and the pins are
    required."""
    src = rec.get("source") or {}
    repo = _need(rec, "/source/repo")
    commit = src.get("commit") or src.get("tag_commit")
    if not commit:
        raise PublishRefused("the tapeout draft has no /source/commit (or tag_commit)")
    return src, repo, commit, _need(rec, "/source/pins")


def _foundry(rec: dict) -> dict:
    foundry = dict(_need(rec, "/foundry"))
    foundry.setdefault("shuttle", None)
    return {k: (None if v is None else str(v)) for k, v in foundry.items()}


def _errata(rec: dict) -> dict:
    errata = dict(rec.get("errata") or {})
    errata.setdefault("ids", [])
    errata["revision"] = errata.pop("doc_revision", errata.get("revision"))
    return errata


def _named(items: Any, key: str, row) -> list[dict]:
    """`row(x)` of each mapping in `items` that has a `key`; anything else is dropped."""
    return [row(x) for x in items or [] if isinstance(x, dict) and x.get(key)]


def _submission_date(rec: dict) -> str | None:
    return _date((rec.get("submission") or {}).get("submitted_on")) or _date(rec.get("submission_date"))


def _layout_method(gds: dict) -> str | None:
    return gds.get("layout_method") or gds.get("canonical_method")


def _first_prop(props: dict, keys: Sequence[str]) -> str | None:
    return next((v[0] for k in keys for v in [props.get(k) or []] if v), None)


def _stored_gds(store: Any, store_path: str, raw_md5: str, layout: str | None,
                stored_sha: str | None) -> tuple[str, str | None, str | None]:
    """(raw_md5_from, stored sha256, layout hash) of the GDS in the store, which must agree with the draft. The
    join is by raw md5: the stored object's own md5, or its raw-md5 property when it is stored compressed."""
    st = store.storage(store_path)
    if st is None:
        raise PublishRefused("the GDS %s is not in the store" % store_path)
    sums = st.get("checksums") or {}
    props = store.properties(store_path)
    prop_raw = _first_prop(props, RAW_MD5_PROPS)
    prop_layout = _first_prop(props, LAYOUT_PROPS)
    if sums.get("md5") == raw_md5:
        raw_from = "stored"
    elif prop_raw == raw_md5:
        raw_from = "property"
    else:
        raise PublishRefused("the stored GDS %s: raw md5 %s matches neither the stored object (%s) nor its raw-md5 "
                             "property (%s); the join is by raw md5, never by the compressed checksum"
                             % (store_path, raw_md5, sums.get("md5"), prop_raw or "none"))
    if layout and prop_layout and prop_layout != layout:
        raise PublishRefused("the stored GDS's layout hash property %s differs from the draft's %s"
                             % (prop_layout, layout))
    if stored_sha and sums.get("sha256") != stored_sha:
        raise PublishRefused("the stored GDS's sha256 %s differs from the draft's %s" % (sums.get("sha256"), stored_sha))
    return raw_from, sums.get("sha256"), layout or prop_layout


def _signoff_rows(signoff: Any) -> Any:
    """A backfill draft's `signoff` mapping (name -> report path, or name -> verdict) as the record's rows."""
    if not isinstance(signoff, dict):
        return signoff
    return [{"name": k, "path": v if isinstance(v, str) else None,
             "verdict": None if isinstance(v, str) else (str(v) if v is not None else None)}
            for k, v in sorted(signoff.items())]


def _foundry_verdict(fv: Any, status: str, dates: dict, returns: list[dict], cites: str | None) -> dict:
    """The foundry's verdict: the draft's when it is one of the known words, else read from the status."""
    given = fv if isinstance(fv, dict) else {}
    verdict = given.get("verdict")
    if verdict not in _FOUNDRY_VERDICTS:
        verdict = "accepted" if status in ("accepted", "fabricated") else "unknown"
    return {"verdict": verdict, "date": _date(given.get("date")) or dates.get("foundry_graded_on"),
            "report_md5": next((r.get("final_report_md5") for r in returns if r.get("final_report_md5")), None),
            "cites_raw_md5": cites, "note": given.get("note")}


def _configured_project(config: Config, configuration: Any) -> str | None:
    """The store project a release of `configuration` is filed under (its component through `[stores.artifactory]
    projects`), or None when the registry does not name that configuration. Fails CLOSED (evaluation 2026-10-16):
    a registry that cannot be read (no [registry] path, not a directory, a file with a problem) refuses the
    publish, because without it the "filed under another project" check cannot run."""
    if not configuration:
        return None
    try:
        from nanosoc_rm.releases.builder import _registry
        cfg = _registry(config).configuration(str(configuration))
    except Exception as e:                                # noqa: BLE001 - any load error refuses, named
        raise PublishRefused("the registry cannot be read (%s: %s), so the check that configuration %s's releases "
                             "are filed under this record's project cannot run: fix [registry] path, then publish "
                             "again" % (type(e).__name__, str(e)[:300], configuration)) from None
    return settings(config).project(cfg["component"]) if cfg and cfg.get("component") else None


def _release_join(config: Config, rec: dict, release: dict, version: str, store: Any) -> str | None:
    """The tapeout record is filed in its release's folder `<release_repo>/<project>/<version>/`. Refuse a draft
    whose release names another project, whose configuration's release is filed under another project, or whose
    release manifest sha256 is not the one stored there. Returns the release manifest's sha256 (the stored one when
    the store has it; None when no release manifest is published there and the draft names none)."""
    from nanosoc_rm.publish.core import MANIFEST_NAME
    project = rec["project"]
    named = release.get("project")
    if named and named != project:
        raise PublishRefused("the draft's release is %s/%s but the record's project is %s: a tapeout record is filed "
                             "in its release's folder, so record.project and record.release.project must agree"
                             % (named, version, project))
    mapped = _configured_project(config, rec.get("configuration"))
    if mapped and mapped != project:
        raise PublishRefused("configuration %s releases are filed under project %s, not the draft's %s: set "
                             "[stores.artifactory] projects (or record.project) so both records share one folder"
                             % (rec.get("configuration"), mapped, project))
    claimed = release.get("manifest_sha256")
    if store is None:
        return claimed
    path = "%s/%s/%s/%s" % (settings(config).release_repo, project, version, MANIFEST_NAME)
    data = store.get_bytes(path)
    if data is None:
        if claimed:
            raise PublishRefused("the draft names release manifest sha256 %s, but %s is not in the store: publish "
                                 "the release first, or clear record.release.manifest_sha256" % (claimed, path))
        return None
    stored = hashlib.sha256(data).hexdigest()
    if claimed and claimed != stored:
        raise PublishRefused("the draft names release manifest sha256 %s, the stored %s is %s" % (claimed, path, stored))
    try:
        man = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PublishRefused("the stored %s does not read as JSON" % path) from None
    if not isinstance(man, dict) or (man.get("project"), man.get("version")) != (project, version):
        raise PublishRefused("the stored %s names %s/%s, not %s/%s" % (
            path, (man or {}).get("project") if isinstance(man, dict) else None,
            man.get("version") if isinstance(man, dict) else None, project, version))
    if rec.get("configuration") and man.get("configuration") and man["configuration"] != rec["configuration"]:
        raise PublishRefused("the stored %s is configuration %s, the tapeout draft's is %s"
                             % (path, man["configuration"], rec["configuration"]))
    return stored


# ---------------------------------------------------------------------------------------------------------
def _flat(node: Any, ptr: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k in sorted(node):
            out.update(_flat(node[k], "%s/%s" % (ptr, k)))
        return out or {ptr: {}}
    if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
        key = next((k for k in ("tree_path", "name", "path", "id") if all(k in x for x in node)), None)
        if key:
            out = {}
            for x in node:
                out.update(_flat(x, "%s[%s]" % (ptr, x[key])))
            return out
    return {ptr: node}


def diff(old: dict | None, new: dict) -> list[str]:
    """Field-level changes from `old` (the last published record of the project, or None) to `new`."""
    if old is None:
        return ["new: nothing of this project was published by this tool before"]
    a, b = _flat(old), _flat(new)
    out = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append("+ %s = %s" % (k, json.dumps(b[k])[:200]))
        elif k not in b:
            out.append("- %s (was %s)" % (k, json.dumps(a[k])[:200]))
        elif a[k] != b[k]:
            out.append("~ %s: %s -> %s" % (k, json.dumps(a[k])[:120], json.dumps(b[k])[:120]))
    return out
