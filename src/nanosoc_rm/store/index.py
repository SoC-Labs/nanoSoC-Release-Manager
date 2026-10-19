"""The store index (interfaces.md section 22): what the artefact store holds, read once, stored as one fact.

    build_index(client, st, *, now=None) -> fact      read the store (AQL, build-info, manifests); pure over the client
    index(config, *, store=False, client=None) -> doc  build, cache collateral, and (with store) put the observation

One run, in order, each step probing the server feature it relies on first (a feature the server lacks is a
probe row NOT-RUN with its reason, and the sections that need it are NOT-RUN too; nothing is guessed):

    system.ping, system.version    GET api/system/ping, api/system/version (version and licence)
    aql.items                      POST api/search/aql: every file (repo, path, name, size, created, created_by,
                                   checksums, properties); AQL is the only read that returns properties in bulk
    build.list, build.info         GET api/build, api/build/<name>, api/build/<name>/<number>?started=<t>: every
                                   build record, including a number filed twice (the vcs block is not AQL-queryable,
                                   so it is fetched per build and joined here, client-side)
    storage.release_repo           GET api/storage/<release repo>: the manifest repository exists
    download.manifest              GET <release repo>/<path> of each manifest (JSON, at most 1 MiB each) and of its
                                   `.sig`, verified against the allowed signers (`_signature`): only a verified
                                   manifest no shared login uploaded is `trusted`

The fact keeps the rows a page needs, not the whole store: every stream (GDS), source archive, manifest,
signature and document (collateral) as a row; foundry returns one row per return folder; every other file only in
the counts by repository and role. Joins are by the RAW md5 properties (`rm.raw_md5`, `submitted.raw_md5`,
`pnr.raw_md5`, `file.raw_md5`), never by the stored object's checksum (a `.zst` checksum describes the compressed
bytes). Git identity is what the store records (`rm.repo`, `rm.commit`, `release.commit`, any `*.commit`): a row
without it says so (`commit: null`), it is never inferred from a path or a name.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from nanosoc_rm import clock
from nanosoc_rm.store.client import AQL_PATH, STORE, Settings, StoreClient, StoreError

__all__ = ["build_index", "index", "classify", "props_of", "role_of", "run_tag_of", "raw_md5_of", "commit_of",
           "collateral_kind", "repo_of_url", "parse_manifest", "subject", "MANIFEST_MAX", "CACHE_MAX", "CACHE_DIR",
           "INDEX_ROLES", "cache_collateral", "SECTION_VERDICTS"]

MANIFEST_MAX = 1 << 20                       # bytes read of one manifest
CACHE_MAX = 16 << 20                         # collateral larger than this is listed, not cached for download
CACHE_TOTAL = 256 << 20
CACHE_DIR = "store-cache"
INDEX_ROLES = ("stream", "source", "manifest", "signature", "doc", "foundry-return")
SECTION_VERDICTS = ("PASS", "NOT-RUN", "INFRA")
_SHA = re.compile(r"^[0-9a-f]{7,40}\Z")
_MD5 = re.compile(r"^[0-9a-f]{32}\Z")
_STREAM = re.compile(r"\.(gds|oas|oasis)(\.(zst|gz|xz|bz2))?$", re.I)
_ARCHIVE = re.compile(r"\.(tar\.(gz|zst|xz|bz2)|tgz|zip)$", re.I)
_SIG = re.compile(r"\.(minisig|sig|asc|sigstore\.json)$", re.I)
_DOC_NAME = re.compile(r"(datasheet|errat|manual|release[_-]?notes|guide|reference|(?<![a-z])trm(?![a-z]))", re.I)
_DOC_EXT = re.compile(r"\.(pdf|md|html?|txt|docx?)$", re.I)
_RAW_KEYS_STREAM = ("rm.raw_md5", "submitted.raw_md5", "pnr.raw_md5", "file.raw_md5")
_RAW_KEYS_OTHER = ("rm.raw_md5", "file.raw_md5")
_SUBJECT_KEYS = ("rm.subject_md5", "pnr.raw_md5", "submitted.raw_md5")
_COMMIT_KEYS = ("rm.commit", "rm.tag_commit", "release.commit", "vcs.revision", "report.project_head")
_FOUNDRY = re.compile(r"^(.*?/foundry/[^/]+)(/.*)?$")


def utc(s: Any) -> str | None:
    """A store time ("2026-08-24T13:54:14.737Z", "2026-08-25T14:14:02.000+0000") as YYYY-MM-DDTHH:MM:SSZ, or
    None when it does not parse."""
    if not isinstance(s, str) or not s:
        return None
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.strip())
    try:
        return clock.iso(dt.datetime.fromisoformat(t.replace("Z", "+00:00")).replace(microsecond=0))
    except ValueError:
        return None


def subject(store: str = STORE) -> str:
    return "store_index:%s" % store


# ---------------------------------------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------------------------------------
def props_of(row: dict) -> dict[str, str]:
    """AQL's [{key, value}] as {key: value}; a key given twice keeps its values joined with ','."""
    out: dict[str, str] = {}
    for p in row.get("properties") or []:
        k, v = p.get("key"), p.get("value")
        if not isinstance(k, str):
            continue
        v = "" if v is None else str(v)
        out[k] = v if k not in out else ",".join(sorted({*out[k].split(","), v}))
    return out


def _segments(row: dict) -> list[str]:
    path = row.get("path") or ""
    return [s for s in path.split("/") if s and s != "."]


def role_of(row: dict, props: dict[str, str], release_repo: str) -> str:
    """The artefact's role: its `rm.role` when the producer set one, else from its name and path."""
    if props.get("rm.role"):
        r = props["rm.role"].strip().lower()
        if r.startswith("stream"):
            return "stream"
        if r in ("doc", "docs", "collateral", "errata", "datasheet", "manual"):
            return "doc"
        if r in ("foundry-return", "foundry"):
            return "foundry-return"
        if r in ("source", "manifest", "signature", "evidence", "recipe", "firmware"):
            return r
        return "other"
    name, segs = row.get("name") or "", _segments(row)
    if row.get("repo") == release_repo:
        if _SIG.search(name):
            return "signature"
        if name.endswith(".json"):
            return "manifest"
    if (row.get("repo") or "").endswith("build-info"):
        return "build-info"
    if "foundry" in segs:
        return "foundry-return"
    if _STREAM.search(name):
        return "stream"
    if "source" in segs and _ARCHIVE.search(name):
        return "source"
    if "evidence" in segs or name.startswith("evidence_report"):
        return "evidence"                                         # a run's evidence is not release collateral
    if _DOC_NAME.search(name) and (_DOC_EXT.search(name) or _ARCHIVE.search(name)):
        return "doc"
    if name.lower().endswith(".pdf"):
        return "doc"
    return "other"


def run_tag_of(row: dict, props: dict[str, str]) -> tuple[str | None, str | None]:
    """(run tag, where it came from: property name or "path")."""
    for k in ("rm.run_tag", "run.tag", "build.number", "run"):
        if props.get(k):
            return props[k], k
    segs = _segments(row)
    if len(segs) >= 3 and (row.get("repo") or "").startswith("asic-"):
        return segs[2], "path"
    return None, None


def raw_md5_of(role: str, props: dict[str, str]) -> tuple[str | None, str | None]:
    """(raw md5 of this artefact's own payload, the property it came from). A stream's raw md5 may sit in
    `submitted.raw_md5` or `pnr.raw_md5`; on any other file `pnr.raw_md5` names the stream it describes, so it is
    that file's subject (subject_md5_of), not its own."""
    for k in (_RAW_KEYS_STREAM if role == "stream" else _RAW_KEYS_OTHER):
        v = (props.get(k) or "").strip().lower()
        if _MD5.match(v):
            return v, k
    return None, None


def subject_md5_of(role: str, props: dict[str, str]) -> str | None:
    if role == "stream":
        return None
    for k in _SUBJECT_KEYS:
        v = (props.get(k) or "").strip().lower()
        if _MD5.match(v):
            return v
    return None


def commit_of(props: dict[str, str]) -> tuple[str | None, str | None]:
    """(a git commit the store records for this file, the property it is in). The known keys first, then any
    key ending in `.commit`. Short ids are kept as given (a short id is what was recorded)."""
    for k in _COMMIT_KEYS:
        v = (props.get(k) or "").strip().lower()
        if _SHA.match(v):
            return v, k
    for k in sorted(props):
        if k.endswith(".commit"):
            v = (props.get(k) or "").strip().lower()
            if _SHA.match(v):
                return v, k
    return None, None


def collateral_kind(name: str) -> str:
    n = name.lower()
    for kind, rx in (("errata", r"errat"), ("datasheet", r"datasheet"), ("release-notes", r"release[_-]?notes"),
                     ("manual", r"manual|reference|(?<![a-z])trm(?![a-z])|guide")):
        if re.search(rx, n):
            return kind
    return "document"


def repo_of_url(url: str | None, hosts: dict[str, dict] | None = None) -> dict | None:
    """A VCS URL as {host, path}: host is a configured host key whose web or ssh address matches, "github" for
    github.com, else "other"; path is owner/name without ".git". None for no URL."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", u)              # scp form: git@host:owner/name.git
    if m:
        host, path = m.group(1), m.group(2)
    else:
        p = urlparse(u)
        host, path = (p.hostname or ""), p.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    key = "other"
    for k, h in (hosts or {}).items():
        for field in ("web", "ssh", "git"):
            v = (h or {}).get(field) or ""
            hv = urlparse(v).hostname if "://" in v else (v.split("@")[-1].split(":")[0] if v else "")
            if hv and hv == host:
                key = k
    if key == "other" and host == "github.com":
        key = "github"
    return {"host": key, "path": path} if path else None


def _project_block(row: dict, props: dict[str, str]) -> tuple[str | None, str | None]:
    segs = _segments(row)
    project = props.get("rm.project") or props.get("publish.project") or (segs[0] if segs else None)
    block = props.get("rm.block") or props.get("publish.block") or (segs[1] if len(segs) > 1 else None)
    return project, block


def classify(rows: Iterable[dict], st: Settings, hosts: dict | None = None) -> dict:
    """AQL rows -> {artefacts, foundry, counts}: the rows a page needs and the counts of everything."""
    artefacts, foundry, counts = [], {}, {}
    totals = {"files": 0, "bytes": 0, "with_properties": 0, "with_commit": 0, "with_repo": 0}
    for row in rows:
        props = props_of(row)
        role = role_of(row, props, st.release_repo)
        repo = row.get("repo") or "?"
        c = counts.setdefault(repo, {"files": 0, "bytes": 0, "roles": {}})
        c["files"] += 1
        c["bytes"] += int(row.get("size") or 0)
        c["roles"][role] = c["roles"].get(role, 0) + 1
        totals["files"] += 1
        totals["bytes"] += int(row.get("size") or 0)
        totals["with_properties"] += 1 if props else 0
        commit, commit_key = commit_of(props)
        totals["with_commit"] += 1 if commit else 0
        totals["with_repo"] += 1 if props.get("rm.repo") else 0
        path = "/".join(_segments(row))
        if role == "foundry-return":
            m = _FOUNDRY.match(path)
            folder = m.group(1) if m else path
            f = foundry.setdefault((repo, folder), {"repo": repo, "path": folder, "files": 0, "bytes": 0,
                                                    "created": utc(row.get("created")), "subject_md5": None})
            f["files"] += 1
            f["bytes"] += int(row.get("size") or 0)
            c = utc(row.get("created"))
            if c and (not f["created"] or c < f["created"]):
                f["created"] = c
            f["subject_md5"] = f["subject_md5"] or subject_md5_of(role, props) or (
                props.get("final-report-md5") if _MD5.match(props.get("final-report-md5") or "") else None)
            continue
        if role not in INDEX_ROLES:
            continue
        run_tag, run_from = run_tag_of(row, props)
        raw, raw_key = raw_md5_of(role, props)
        project, block = _project_block(row, props)
        build = ({"name": props["build.name"], "number": props.get("build.number")} if props.get("build.name")
                 else None)
        git_repo = None
        if props.get("rm.repo"):
            r = props["rm.repo"].strip().strip("/")
            git_repo = repo_of_url(r, hosts) if ("://" in r or "@" in r) else {"host": "github", "path": r}
        artefacts.append({
            "repo": repo, "path": path, "name": row.get("name") or "", "size": int(row.get("size") or 0),
            "created": utc(row.get("created")), "created_by": row.get("created_by"),
            "md5": (row.get("actual_md5") or None), "sha1": (row.get("actual_sha1") or None),
            "role": role, "project": project, "block": block, "run_tag": run_tag, "run_tag_from": run_from,
            "raw_md5": raw, "raw_md5_key": raw_key, "subject_md5": subject_md5_of(role, props),
            "build": build, "commit": commit, "commit_key": commit_key, "git_repo": git_repo,
            "dirty": (props.get("rm.dirty") or None),
            "tag": props.get("rm.tag") or props.get("release.tag") or None,
            "ships": (props.get("submitted.ships") or None),
            "confidentiality": props.get("rm.confidentiality") or props.get("confidentiality") or None,
            "kind": collateral_kind(row.get("name") or "") if role == "doc" else None,
        })
    artefacts.sort(key=lambda a: (a["repo"], a["path"], a["name"]))
    return {"artefacts": artefacts, "foundry": sorted(foundry.values(), key=lambda f: (f["repo"], f["path"])),
            "counts": {"repos": [dict(repo=k, **v) for k, v in sorted(counts.items())], **totals}}


# ---------------------------------------------------------------------------------------------------------
# Manifests (asic-release): read leniently, every gap named
# ---------------------------------------------------------------------------------------------------------
def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", [], {}):
            return cur
    return None


_KIND_ALIASES = {"release-manifest": "release", "tapeout-record": "tapeout", "release": "release", "tapeout": "tapeout"}


def _repo_field(repo: Any, hosts: dict | None) -> dict | None:
    if isinstance(repo, dict) and repo.get("path"):
        return {"host": str(repo.get("host") or "github"), "path": str(repo["path"])}
    if isinstance(repo, str) and repo:
        return repo_of_url(repo, hosts) if ("://" in repo or "@" in repo) else {"host": "github", "path": repo}
    return None


def parse_manifest(path: str, name: str, body: bytes, hosts: dict | None = None) -> dict:
    """One release manifest or tapeout record, as the publish service writes them (`release-manifest.schema.json`
    kind `release-manifest`, `tapeout-record.schema.json` kind `tapeout-record`), read leniently: known fields are
    read where they are, and whatever is missing is named in `problems`."""
    out: dict[str, Any] = {"path": path, "name": name, "kind": "tapeout" if name.startswith("tapeout") else "release",
                           "problems": []}
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        out["problems"].append("not JSON: %s" % type(e).__name__)
        return out
    if not isinstance(doc, dict):
        out["problems"].append("not a JSON object")
        return out
    declared = str(_first(doc, "kind") or "")
    out["kind"] = _KIND_ALIASES.get(declared, out["kind"])
    if declared and declared not in _KIND_ALIASES:
        out["problems"].append("kind %r is neither a release manifest nor a tapeout record" % declared)
    src = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    _identity(out, doc, src, hosts)
    _pins(out, doc.get("pins") if "pins" in doc else src.get("pins"))
    _tapeout(out, doc)
    _listed(out, doc)
    for key in ("tag", "commit", "repo"):
        if not out.get(key):
            out["problems"].append("no %s recorded" % key)
    if out["kind"] == "tapeout":
        for key in ("gds_raw_md5", "foundry", "date"):
            if not out.get(key):
                out["problems"].append("no %s recorded" % key.replace("_", " "))
    return out


def _identity(out: dict, doc: dict, src: dict, hosts: dict | None) -> None:
    """schema, project, configuration, component, version, tag(s), commit and repo, in that key order."""
    out["schema"] = _first(doc, "schema")
    out["project"] = _first(doc, "project")
    out["configuration"] = _first(doc, "configuration")
    out["component"] = _first(doc, "component")
    out["version"] = _first(doc, "version")
    tags = src.get("tags") if isinstance(src.get("tags"), list) else []
    out["tag"] = _first(doc, "tag") or (tags[0] if tags else None)
    out["tags"] = [str(t) for t in tags][:20] if tags else ([out["tag"]] if out["tag"] else [])
    commit = _first(doc, "commit") or src.get("commit")
    out["commit"] = str(commit).lower() if isinstance(commit, str) and _SHA.match(commit.lower()) else None
    out["repo"] = _repo_field(_first(doc, "repo") or src.get("repo"), hosts)


def _pins(out: dict, pins: Any) -> None:
    """A tapeout record's {tree path: commit}, or a release manifest's [{tree_path, component, commit}]."""
    out["pins"], out["pin_components"] = {}, {}
    if isinstance(pins, dict):
        out["pins"] = {str(k): str(v) for k, v in pins.items()}
    elif isinstance(pins, list):
        for p in pins:
            if isinstance(p, dict) and p.get("commit") and (p.get("tree_path") or p.get("component")):
                out["pins"][str(p.get("tree_path") or p.get("component"))] = str(p["commit"])
                if p.get("component"):
                    out["pin_components"][str(p["component"])] = str(p["commit"])


def _tapeout(out: dict, doc: dict) -> None:
    """tapeout_id, foundry, shuttle, date, status, foundry_verdict and the GDS's raw md5 and store path."""
    out["tapeout_id"] = _first(doc, "tapeout_id")
    fdry = doc.get("foundry")
    out["foundry"] = (fdry.get("foundry") if isinstance(fdry, dict) else fdry) or None
    out["shuttle"] = (fdry.get("shuttle") if isinstance(fdry, dict) else _first(doc, "shuttle")) or None
    out["date"] = _first(doc, "submission_date", "date")
    out["status"] = _first(doc, "status")
    verdict = doc.get("foundry_verdict")
    out["foundry_verdict"] = verdict.get("verdict") if isinstance(verdict, dict) else None
    gds = doc.get("gds") if isinstance(doc.get("gds"), dict) else ((doc.get("outputs") or {}).get("gds") or {})
    gds = gds if isinstance(gds, dict) else {}
    raw = str(gds.get("raw_md5") or gds.get("md5") or "").lower()
    out["gds_raw_md5"] = raw if _MD5.match(raw) else None
    out["gds_store_path"] = gds.get("store_path") or None


def _listed(out: dict, doc: dict) -> None:
    """documents (first 50) and artefacts (first 200)."""
    docs = doc.get("documents")
    out["documents"] = [{"kind": str(d.get("kind") or ""), "name": str(d.get("name") or d.get("path") or "")}
                        for d in (docs if isinstance(docs, list) else []) if isinstance(d, dict)][:50]
    arts = doc.get("artefacts")
    out["artefacts"] = [{"path": str(a.get("path") or ""), "role": a.get("role"),
                         "md5": (str(a.get("raw_md5")).lower() if a.get("raw_md5") else None),
                         "stored_md5": (str(a.get("stored_md5")).lower() if a.get("stored_md5") else None)}
                        for a in (arts if isinstance(arts, list) else []) if isinstance(a, dict)][:200]


# ---------------------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------------------
def _probe(probes: list, feature: str, verdict: str, detail: str) -> None:
    probes.append({"feature": feature, "verdict": verdict, "detail": detail[:300]})


def _absent(e: StoreError) -> bool:
    return e.cause == "absent"


def _aql_items(st: Settings) -> str:
    crit = '"type":"file"'
    if st.repos:
        crit += ',"$or":[%s]' % ",".join('{"repo":%s}' % json.dumps(r) for r in st.repos)
    return ('items.find({%s}).include("repo","path","name","size","created","created_by","actual_md5",'
            '"actual_sha1","property")' % crit)


def _builds(client: StoreClient, probes: list, hosts: dict | None) -> tuple[dict, list[dict]]:
    try:
        top = client.get_json("api/build")
    except StoreError as e:
        if e.status == 404 and "build" in e.detail.lower():
            _probe(probes, "build.list", "PASS", "no build records on the server")
            return {"verdict": "PASS", "reason": "no build records"}, []
        why = "build-info is not available on this server (%s)" % e if _absent(e) else str(e)
        _probe(probes, "build.list", "NOT-RUN" if _absent(e) else "FAIL", why)
        return {"verdict": "NOT-RUN" if _absent(e) else "INFRA", "reason": why}, []
    _probe(probes, "build.list", "PASS", "%d build name(s)" % len(top.get("builds") or []))
    out, failed = [], []
    for b in top.get("builds") or []:
        name = str(b.get("uri") or "").strip("/")
        if name:
            out += _build_numbers(client, name, failed, hosts)
    if failed:
        _probe(probes, "build.info", "FAIL", "%d record(s) unreadable: %s" % (len(failed), failed[0]))
    elif out:
        _probe(probes, "build.info", "PASS", "%d record(s) read" % len(out))
    else:
        _probe(probes, "build.info", "NOT-RUN", "no build record to read")
    sec = {"verdict": "PASS" if not failed else "INFRA",
           "reason": None if not failed else "%d build record(s) could not be read" % len(failed)}
    return sec, out


def _build_numbers(client: StoreClient, name: str, failed: list[str], hosts: dict | None) -> list[dict]:
    """Every build record filed under one build name; a record that cannot be read goes to `failed`."""
    try:
        nums = client.get_json("api/build/%s" % quote(name, safe=""))
    except StoreError as e:
        failed.append(str(e))
        return []
    entries = nums.get("buildsNumbers") or []
    seen: dict[str, int] = {}
    for n in entries:
        num = str(n.get("uri") or "").strip("/")
        seen[num] = seen.get(num, 0) + 1
    out = []
    for n in entries:
        num, started = str(n.get("uri") or "").strip("/"), n.get("started")
        if not num:
            continue
        # A number filed twice is told apart by its start time.
        params = {"started": started} if seen.get(num, 0) > 1 and started else None
        try:
            bi = (client.get_json("api/build/%s/%s" % (quote(name, safe=""), quote(num, safe="")), params)
                  or {}).get("buildInfo") or {}
        except StoreError as e:
            failed.append(str(e))
            continue
        out.append(_build_record(name, num, started, bi, seen.get(num, 1), hosts))
    return out


def _tristate(text: str) -> bool | None:
    return True if text in ("yes", "true", "1") else False if text in ("no", "false", "0") else None


def _build_record(name: str, num: str, started: Any, bi: dict, filed: int, hosts: dict | None) -> dict:
    p = bi.get("properties") or {}
    vcs = [dict(repo_of_url(v.get("url"), hosts) or {"host": "other", "path": ""},
                revision=(str(v.get("revision") or "").lower() or None), branch=v.get("branch"))
           for v in bi.get("vcs") or [] if isinstance(v, dict)]
    mods = bi.get("modules") or []
    md5s = sorted({str(a.get("md5")) for m in mods for a in (m.get("artifacts") or []) if a.get("md5")})
    return {"name": name, "number": num, "started": utc(bi.get("started") or started),
            "agent": (bi.get("agent") or {}).get("name"), "vcs": vcs,
            "dirty": _tristate(str(p.get("vcs.worktree_dirty") or "").lower()),
            "raw_md5": next((str(p[k]).lower() for k in ("rm.raw_md5", "submitted.raw_md5", "pnr.raw_md5")
                             if _MD5.match(str(p.get(k) or "").lower())), None),
            "stream_path": p.get("store.stream_path") or None, "artefact_md5": md5s[:50], "filed": filed}


SIG_MAX = 64 << 10                           # bytes read of one signature
_SIG_EXTS = (".sig", ".minisig", ".asc", ".sigstore.json")


def _signature(client: StoreClient, st: Settings, full: str, body: bytes, sigs: set[str], failed: list[str]
               ) -> tuple[str, str, str | None]:
    """(state, why, signer fingerprint) of the manifest at `full`, read from the store and checked here:

        verified    `<full>.sig` is an SSHSIG (namespace nanosoc-rm) over these bytes by a key the allowed signers
                    file accepts for `signer_identity` (the publish unit's own check, `publish.signing.verify`)
        invalid     a `.sig` is there and does not verify (junk, another key, other bytes, another namespace)
        unverified  a signature is there but this host cannot check it (no allowed signers configured, an
                    unreadable allowed signers file, a signature that could not be downloaded, a format the tool
                    does not check)
        absent      no signature beside the manifest

    Only `verified` makes a manifest trusted: on Artifactory OSS every token is effectively admin, so the store
    itself cannot say who wrote a file."""
    have = [ext for ext in _SIG_EXTS if full + ext in sigs]
    if not have:
        return "absent", "no signature beside the manifest", None
    if ".sig" not in have:
        return "unverified", "a %s signature is not checked by this tool (only an SSHSIG .sig)" % have[0], None
    if not st.allowed_signers:
        return "unverified", ("no allowed signers to verify against ([stores.artifactory] allowed_signers, or the "
                              "signing key's .pub)"), None
    from nanosoc_rm.publish import signing
    try:
        allowed = signing.load_allowed(st.allowed_signers, st.signer_identity)
    except signing.SignatureError as e:
        return "unverified", str(e), None
    try:
        text = client.get_bytes("%s/%s" % (quote(st.release_repo, safe=""), quote(full + ".sig")), SIG_MAX)
    except StoreError as e:
        failed.append(str(e))
        return "unverified", "the signature could not be read: %s" % e.cause, None
    try:
        fpr = signing.verify(body, text.decode("ascii"), allowed, principal=st.signer_identity,
                             ssh_keygen=st.ssh_keygen)
    except Exception as e:                                        # noqa: BLE001 - any parse or check failure
        why = str(e) if isinstance(e, signing.SignatureError) else "not an SSH signature (%s)" % type(e).__name__
        return "invalid", why[:200], None
    return "verified", "signed by %s" % fpr, fpr


def _manifests(client: StoreClient, st: Settings, probes: list, items: list[dict] | None, hosts: dict | None
               ) -> tuple[dict, list[dict]]:
    try:
        client.get_json("api/storage/%s" % quote(st.release_repo, safe=""))
    except StoreError as e:
        why = ("release repository %s does not exist" % st.release_repo if e.status == 404 else str(e))
        _probe(probes, "storage.release_repo", "NOT-RUN" if e.status in (400, 404) else "FAIL", why)
        return {"verdict": "NOT-RUN" if e.status in (400, 404) else "INFRA", "reason": why}, []
    _probe(probes, "storage.release_repo", "PASS", "%s exists" % st.release_repo)
    if items is None:
        return {"verdict": "NOT-RUN", "reason": "AQL is not available, so the release repository was not listed"}, []
    rows = [a for a in items if a["repo"] == st.release_repo and a["role"] in ("manifest", "signature")]
    sigs = {"/".join(x for x in (a["path"], a["name"]) if x) for a in rows if a["role"] == "signature"}
    shared = set(st.shared_logins)
    out, failed = [], []
    for a in rows:
        if a["role"] != "manifest":
            continue
        full = "/".join(x for x in (a["path"], a["name"]) if x)
        try:
            body = client.get_bytes("%s/%s" % (quote(st.release_repo, safe=""), quote(full)), MANIFEST_MAX)
        except StoreError as e:
            failed.append(str(e))
            continue
        m = parse_manifest(full, a["name"], body, hosts)
        state, why, fpr = _signature(client, st, full, body, sigs, failed)
        m.update(md5=a["md5"], created=a["created"], created_by=a["created_by"], signature=state,
                 signature_detail=why, signer=fpr)
        if state != "verified":
            m["problems"].append("signature %s: %s" % (state, why) if state != "absent" else "signature absent")
        if a["created_by"] and a["created_by"] in shared:
            m["problems"].append("uploaded by the shared login %s" % a["created_by"])
        m["trusted"] = state == "verified" and a["created_by"] not in shared
        out.append(m)
    if not rows:
        _probe(probes, "download.manifest", "NOT-RUN", "no manifest in %s yet" % st.release_repo)
    elif failed:
        _probe(probes, "download.manifest", "FAIL", "%d manifest file(s) unreadable: %s" % (len(failed), failed[0]))
    else:
        _probe(probes, "download.manifest", "PASS", "%d manifest(s) read, %d verified"
               % (len(out), sum(1 for m in out if m["signature"] == "verified")))
    return ({"verdict": "PASS" if not failed else "INFRA",
             "reason": None if not failed else "%d manifest file(s) could not be read" % len(failed)}, out)


def build_index(client: StoreClient, st: Settings, *, hosts: dict | None = None,
                now: dt.datetime | None = None) -> dict:
    """Read the store and return the `store_index` fact (store.schema.json#/$defs/store_index)."""
    started = clock.iso(now)
    probes: list[dict] = []
    fact: dict[str, Any] = {"store": STORE, "release_repo": st.release_repo, "started_at": started,
                            "finished_at": started, "server": {"version": None, "license": None},
                            "verdict": "PASS", "reason": None, "probes": probes, "sections": {},
                            "artefacts": [], "foundry": [], "builds": [], "manifests": [],
                            "counts": {"repos": [], "files": 0, "bytes": 0, "with_properties": 0, "with_commit": 0,
                                       "with_repo": 0},
                            "token": {"expires": st.token_expires}}
    try:
        client.get("api/system/ping")
        _probe(probes, "system.ping", "PASS", "the API answers")
    except StoreError as e:
        _probe(probes, "system.ping", "FAIL", str(e))
        fact.update(verdict="INFRA", reason=("the store refused the token: %s" % e) if e.cause == "auth" else
                    "the store did not answer: %s" % e,
                    sections={k: {"verdict": "NOT-RUN", "reason": "the store did not answer"}
                              for k in ("items", "builds", "manifests")})
        fact["finished_at"] = clock.iso()
        return fact
    try:
        v = client.get_json("api/system/version")
        fact["server"] = {"version": v.get("version"), "license": v.get("license")}
        _probe(probes, "system.version", "PASS", "%s %s" % (v.get("version"), v.get("license")))
    except StoreError as e:
        _probe(probes, "system.version", "NOT-RUN" if _absent(e) else "FAIL", str(e))
        if e.cause == "auth":
            fact.update(verdict="INFRA", reason="the store refused the token: %s" % e,
                        sections={k: {"verdict": "NOT-RUN", "reason": "the token was refused"}
                                  for k in ("items", "builds", "manifests")})
            fact["finished_at"] = clock.iso()
            return fact
    items = None
    try:
        res = client.aql(_aql_items(st))
        rows = res["results"]
        total = (res.get("range") or {}).get("total")
        _probe(probes, "aql.items", "PASS", "%d file(s)%s" % (len(rows), "" if total in (None, len(rows))
                                                              else " of %s (the answer was cut)" % total))
        got = classify(rows, st, hosts)
        items = got["artefacts"]
        fact.update(artefacts=got["artefacts"], foundry=got["foundry"], counts=got["counts"])
        fact["sections"]["items"] = ({"verdict": "PASS", "reason": None} if total in (None, len(rows)) else
                                     {"verdict": "INFRA", "reason": "AQL returned %d of %s files" % (len(rows), total)})
        _probe(probes, "aql.properties", "PASS" if got["counts"]["with_properties"] else "NOT-RUN",
               "%d file(s) carry properties" % got["counts"]["with_properties"] if got["counts"]["with_properties"]
               else "no file carries a property, so no join key can be read")
    except StoreError as e:
        absent = _absent(e)
        _probe(probes, "aql.items", "NOT-RUN" if absent else "FAIL",
               ("AQL is not available on this server: %s" % e) if absent else str(e))
        fact["sections"]["items"] = {"verdict": "NOT-RUN" if absent else "INFRA",
                                     "reason": ("AQL is not available on this server" if absent else str(e))}
    fact["sections"]["builds"], fact["builds"] = _builds(client, probes, hosts)
    fact["sections"]["manifests"], fact["manifests"] = _manifests(client, st, probes, items, hosts)
    secs = [s["verdict"] for s in fact["sections"].values()]
    if "INFRA" in secs:
        fact["verdict"], fact["reason"] = "INFRA", "; ".join(
            "%s: %s" % (k, s["reason"]) for k, s in fact["sections"].items() if s["verdict"] == "INFRA")
    elif fact["sections"]["items"]["verdict"] != "PASS":
        fact["verdict"], fact["reason"] = "UNVERIFIED", "the file index could not be read: %s" % (
            fact["sections"]["items"]["reason"])
    fact["finished_at"] = clock.iso()
    return fact


# ---------------------------------------------------------------------------------------------------------
# Collateral cache (so a page can offer a download without calling the store)
# ---------------------------------------------------------------------------------------------------------
def _confidential(a: dict) -> bool:
    c = (a.get("confidentiality") or "").lower()
    return bool(c) and c not in ("public", "internal")


def cache_collateral(client: StoreClient, fact: dict, cache_dir: Path) -> None:
    """Download each document (role doc, plus manifests) at most CACHE_MAX into `cache_dir/<sha1>`, verified
    against the store's sha1; record on each row `cached` (sha1) or `cache_reason`. Never a confidential file."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for a in fact.get("artefacts") or []:
        if a["role"] not in ("doc", "manifest"):
            continue
        a["cached"], a["cache_reason"] = None, None
        sha1 = (a.get("sha1") or "").lower()
        if _confidential(a):
            a["cache_reason"] = "withheld: confidentiality %s" % a["confidentiality"]
            continue
        if not re.match(r"^[0-9a-f]{40}\Z", sha1):
            a["cache_reason"] = "no sha1 from the store to verify a download"
            continue
        if a["size"] > CACHE_MAX or total + a["size"] > CACHE_TOTAL:
            a["cache_reason"] = "larger than the %d MiB the tool caches" % (CACHE_MAX >> 20)
            continue
        dest = cache_dir / sha1
        if not dest.is_file():
            full = "/".join(x for x in (a["repo"], a["path"], a["name"]) if x)
            try:
                body = client.get_bytes(quote(full), CACHE_MAX)
            except StoreError as e:
                a["cache_reason"] = "download failed: %s" % e.cause
                continue
            if hashlib.sha1(body).hexdigest() != sha1:
                a["cache_reason"] = "the downloaded bytes do not match the store's sha1"
                continue
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.replace(dest)
        a["cached"] = sha1
        total += a["size"]


def index(config: Any, *, store: bool = False, client: StoreClient | None = None, cache: bool = True) -> dict:
    """Build the index from the configured store and return the observation; with `store`, put it."""
    from nanosoc_rm import db
    from nanosoc_rm.store.client import settings
    st = settings(config)
    own = client is None
    client = client or StoreClient.from_config(config)
    try:
        fact = build_index(client, st, hosts=config.get("hosts") or {})
        if cache and fact["verdict"] != "INFRA":
            cache_collateral(client, fact, config.data_dir / CACHE_DIR)
    finally:
        if own:
            client.close()
    calls = ["GET api/system/ping", "GET api/system/version", "POST %s items.find(type=file)" % AQL_PATH,
             "GET api/build", "GET api/storage/%s" % st.release_repo]
    doc = {"schema": 1, "kind": "store_index", "subject": subject(), "observed_at": fact["started_at"],
           "sources": [{"kind": "artifactory_api", "command": c} for c in calls], "fact": fact}
    if store:
        db.put(doc, config=config)
    return doc
