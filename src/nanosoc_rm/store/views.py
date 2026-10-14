"""What the pages show from the stored store index (interfaces.md section 22). Stored facts only: nothing here
calls the store, walks, fetches or queues; a GET reads these functions and nothing else.

    latest(config) -> doc | None            the newest store_index observation
    shown(config) -> (doc | None, latest)   the index whose rows pages show: the newest that was not INFRA
    taped_out(config, cid) -> dict          Configuration > Silicon "Taped out" panel
    timeline(config, cid) -> [event]        Configuration > History: tags, release drafts, uploads, tapeouts
    component_versions(config, comp)        Component > Versions: per tag its commit link and the chips it went into
    release_rows(config) -> {(cfg, ver): r} Releases page: Store and Published columns
    collateral(config) -> [row]             documents with a download link when the tool holds a verified copy
    findings(config) -> [finding]           store_mismatch, release_not_archived, tapeout_unregistered (inbox)
    search(config, q) -> [hit]              by artefact path, name, run tag, commit or md5 prefix
    commit_link(config, repo, sha)          the web URL of a commit on its host, or None (never a guess)

Joins: GDS by raw md5 (manifest `gds.raw_md5`, the stream's raw-md5 property, the release lock's `outputs.gds.md5`),
never by the stored `.zst` checksum. A store project belongs to a configuration only through `[stores.artifactory]
projects` (a person's statement: component -> store project) or a manifest that names the configuration; nothing
is inferred from a name.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from nanosoc_rm import clock, db
from nanosoc_rm.config import ConfigError

__all__ = ["trusted", "latest", "shown", "taped_out", "timeline", "component_versions", "release_rows", "collateral",
           "findings", "search", "commit_link", "projects", "NO_IDENTITY", "cached_file"]

NO_IDENTITY = "no git identity recorded"
_HEX = re.compile(r"^[0-9a-f]{4,40}$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
SHIPPED = ("yes", "true", "shipped")


def latest(config: Any) -> dict | None:
    try:
        docs = db.latest("store_index", "store_index:", config=config)
    except Exception:                                             # noqa: BLE001 - no store yet: nothing stored
        return None
    return max(docs, key=lambda d: clock.parse(d["observed_at"])) if docs else None


def shown(config: Any) -> tuple[dict | None, dict | None]:
    """(the index pages take rows from, the newest index). An INFRA run (the store did not answer) keeps the
    previous good rows on the page, with the failure shown beside them."""
    new = latest(config)
    if new is None or new["fact"]["verdict"] != "INFRA":
        return new, new
    for d in reversed(db.history(new["subject"], config=config)):
        if d["fact"]["verdict"] != "INFRA":
            return d, new
    return None, new


def _settings(config: Any) -> dict:
    return ((config.get("stores") or {}).get("artifactory") or {}) if config is not None else {}


def projects(config: Any) -> dict[str, str]:
    """`[stores.artifactory] projects`: catalogue component -> store project folder (a person's statement)."""
    return {str(k): str(v) for k, v in (_settings(config).get("projects") or {}).items()}


def _projects_of_comp(config: Any, comp: str | None) -> list[str]:
    p = projects(config).get(comp or "")
    return [p] if p else []


def _projects_of_cfg(config: Any, cid: str) -> list[str]:
    rec = next((_rec(d) for d in _cfg_records(config) if d["fact"]["id"] == cid), {})
    return _projects_of_comp(config, rec.get("component"))


def commit_link(config: Any, repo: dict | None, sha: str | None) -> str | None:
    """`<hosts.<host>.web>/<path>/tree/<sha>` (GitLab: `/-/tree/`), only for a full https host and a known host
    key; None otherwise."""
    if not repo or not sha or not _SHA.match(sha) or not repo.get("path"):
        return None
    try:
        web = config.host(repo["host"])["web"].rstrip("/")
    except (ConfigError, KeyError, AttributeError, TypeError):
        return None
    if not web.startswith("https://"):
        return None
    return "%s/%s/%s/%s" % (web, repo["path"].strip("/"), "-/tree" if repo["host"] == "gitlab" else "tree", sha)


def _component_repo(config: Any, comp: str | None) -> dict | None:
    from nanosoc_rm.web import views as wv
    if not comp:
        return None
    try:
        rec = next((d for d in wv.components(config) if d["fact"]["id"] == comp), None)
    except Exception:                                             # noqa: BLE001 - registry unreadable
        return None
    canon = ((rec or {}).get("fact", {}).get("record") or {}).get("canonical") or {}
    return {"host": canon["host"], "path": canon["path"]} if canon.get("host") and canon.get("path") else None


def _cfg_records(config: Any) -> list[dict]:
    from nanosoc_rm.web import views as wv
    try:
        return wv.configurations(config)
    except Exception:                                             # noqa: BLE001 - registry unreadable
        return []


def _rec(d: dict) -> dict:
    return (d.get("fact") or {}).get("record") or {}


def _silicon_cfgs(config: Any) -> list[dict]:
    return [d for d in _cfg_records(config) if isinstance(_rec(d).get("silicon"), dict)]


def _lock_gds(config: Any, cid: str) -> list[dict]:
    """[{version, md5, bytes}] from the release drafts' locks of `cid` (files on the service's own disk)."""
    from nanosoc_rm import contracts
    from nanosoc_rm.releases.core import is_safe_id, releases_dir
    out = []
    if not is_safe_id(cid):
        return out
    root = releases_dir(config) / cid
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        f = d / "release.lock.yaml"
        if not d.is_dir() or not f.is_file() or f.is_symlink():
            continue
        try:
            lock = contracts.load_yaml(f.read_text())
            g = ((lock or {}).get("outputs") or {}).get("gds") or {}
        except Exception:                                         # noqa: BLE001 - an unreadable lock: no hash
            continue
        if isinstance(g, dict) and g.get("md5"):
            out.append({"version": d.name, "md5": str(g["md5"]).lower(), "bytes": g.get("bytes")})
    return out


def trusted(m: dict) -> bool:
    """A manifest is trusted only when the index verified its signature and no shared login uploaded it."""
    return m.get("trusted") is True


def _distrust(m: dict) -> str:
    """Why a manifest is not trusted, in one line."""
    why = ["signature %s (%s)" % (m.get("signature") or "unknown", m.get("signature_detail") or "not checked")]
    why += [p for p in m.get("problems") or [] if p.startswith("uploaded by the shared login")]
    return "; ".join(why)


def _untrusted(verdict: tuple[str, str, str], m: dict) -> tuple[str, str, str]:
    """A green verdict on an untrusted manifest is UNVERIFIED (amber): a store token holder could have written it."""
    if trusted(m) or verdict[1] != "c-pass":
        return verdict
    return ("UNVERIFIED", "c-warn", "%s, but the manifest is not trusted: %s" % (verdict[2], _distrust(m)))


def _streams_by_raw(fact: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in fact.get("artefacts") or []:
        if a["role"] == "stream" and a.get("raw_md5"):
            out.setdefault(a["raw_md5"], []).append(a)
    return out


def _full(a: dict) -> str:
    return "/".join(x for x in (a.get("repo"), a.get("path"), a.get("name")) if x)


def _manifests_for(config: Any, fact: dict, cid: str, kind: str | None = None) -> list[dict]:
    rec = next((_rec(d) for d in _cfg_records(config) if d["fact"]["id"] == cid), {})
    projects = set(_projects_of_cfg(config, cid))
    commit = str(rec.get("commit") or "")
    out = []
    for m in fact.get("manifests") or []:
        if kind and m.get("kind") != kind:
            continue
        if m.get("configuration") == cid or (m.get("project") and m.get("project") in projects and
                                              not m.get("configuration")) or (
                commit and m.get("commit") and commit.startswith(m["commit"]) and not m.get("configuration")):
            out.append(m)
    return out


# ---------------------------------------------------------------------------------------------------------
# Configuration > Silicon: "Taped out"
# ---------------------------------------------------------------------------------------------------------
def taped_out(config: Any, cid: str) -> dict:
    doc, new = shown(config)
    rec = next((_rec(d) for d in _cfg_records(config) if d["fact"]["id"] == cid), {})
    out: dict[str, Any] = {"doc": doc, "latest": new, "silicon": rec.get("silicon") if isinstance(rec.get("silicon"),
                                                                                               dict) else None,
                           "rows": [], "streams": [], "projects": _projects_of_cfg(config, cid),
                           "locks": _lock_gds(config, cid), "configured": bool(_settings(config)),
                           "drafts": registry_tapeout_drafts(config, cid)}
    if doc is None:
        return out
    fact = doc["fact"]
    by_raw = _streams_by_raw(fact)
    comp_repo = _component_repo(config, rec.get("component"))
    for m in _manifests_for(config, fact, cid, "tapeout"):
        raw = m.get("gds_raw_md5")
        stored = by_raw.get(raw or "", [])
        at = [s for s in stored if m.get("gds_store_path") in (None, _full(s))] if m.get("gds_store_path") else stored
        if not raw:
            store = ("NO MD5", "c-warn", "the manifest records no raw GDS md5")
        elif at:
            store = ("IN STORE", "c-pass", "raw md5 %s at %s" % (raw, _full(at[0])))
        elif stored:
            store = ("MISMATCH", "c-fail", "the stream with raw md5 %s is at %s, not %s" % (
                raw, _full(stored[0]), m.get("gds_store_path")))
        else:
            store = ("NOT IN STORE", "c-fail", "no stream carries raw md5 %s" % raw)
        locks = out["locks"]
        if not locks:
            lock = ("NO LOCK", "c-notrun", "no release draft of %s records a GDS md5" % cid)
        elif raw and any(lk["md5"] == raw for lk in locks):
            lock = ("MATCH", "c-pass", "release lock %s" % ", ".join(lk["version"] for lk in locks if lk["md5"] == raw))
        else:
            lock = ("MISMATCH", "c-fail", "lock md5 %s, manifest %s" % (", ".join(sorted({lk["md5"] for lk in locks})),
                                                                       raw or "none"))
        store, lock = _untrusted(store, m), _untrusted(lock, m)
        repo = m.get("repo") or comp_repo
        out["rows"].append({"manifest": m, "store": store, "lock": lock, "trusted": trusted(m),
                            "link": commit_link(config, repo, m.get("commit")),
                            "repo_from": "manifest" if m.get("repo") else ("registry" if comp_repo else None)})
    # Streams of this configuration's store projects (`[stores.artifactory] projects`), newest first
    projects = set(out["projects"])
    manifest_raw = {r["manifest"].get("gds_raw_md5") for r in out["rows"] if r["trusted"]}
    for a in fact.get("artefacts") or []:
        if a["role"] == "stream" and a.get("project") in projects:
            out["streams"].append({"a": a, "registered": a.get("raw_md5") in manifest_raw,
                                   "identity": _identity(config, a)})
    out["streams"].sort(key=lambda r: r["a"].get("created") or "", reverse=True)
    return out


_DRAFT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")          # the publish page's tapeout id rule


def registry_tapeout_drafts(config: Any, cid: str) -> list[dict]:
    """CR-WE-int: the registry's unpublished tapeout drafts (`<registry>/tapeouts/<id>.tapeout.yaml`, the publish
    unit's input) whose record names configuration `cid`. A local file read, never the store. A draft that does not
    read or does not meet `tapeout-record#/$defs/tapeout_draft` is listed with the reason, never dropped."""
    from nanosoc_rm import contracts
    reg = (config.get("registry") or {}).get("path") if isinstance(config, dict) else None
    folder = Path(reg) / "tapeouts" if reg else None
    if folder is None or not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("*.tapeout.yaml")):
        tid = f.name[:-len(".tapeout.yaml")]
        if not _DRAFT_ID.match(tid):
            continue
        try:
            doc = contracts.load_yaml(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append({"id": tid, "problem": "does not read (%s)" % str(e)[:120]})
            continue
        rec = doc.get("record") if isinstance(doc, dict) else None
        if not isinstance(rec, dict) or rec.get("configuration") != cid:
            continue
        problems = contracts.validate("tapeout-record", doc, "/$defs/tapeout_draft")
        gds = rec.get("gds") if isinstance(rec.get("gds"), dict) else {}
        out.append({"id": tid, "draft_status": doc.get("status"), "status": rec.get("status"),
                    "tapeout_status": rec.get("tapeout_status"),
                    "raw_md5": gds.get("raw_md5"),
                    "problem": ("does not meet its contract: %s" % "; ".join(problems[:2])) if problems else None})
    return out


def _identity(config: Any, a: dict) -> dict:
    """What the store says about a file's source: commit and repository, with where each came from. Honest gaps:
    no commit -> NO_IDENTITY; a commit without a repository -> the commit, "repository not recorded"."""
    if not a.get("commit"):
        return {"text": NO_IDENTITY, "link": None, "commit": None}
    repo = a.get("git_repo")
    text = "%s (property %s)" % (a["commit"][:12], a.get("commit_key"))
    if repo:
        return {"text": text, "link": commit_link(config, repo, a["commit"]), "commit": a["commit"]}
    comp = next((c for c, p in projects(config).items() if p == a.get("project")), None)
    crepo = _component_repo(config, comp)
    if crepo and len(a["commit"]) == 40:
        return {"text": text + "; repository from [stores.artifactory] projects, not recorded in the store",
                "link": commit_link(config, crepo, a["commit"]), "commit": a["commit"]}
    return {"text": text + "; repository not recorded", "link": None, "commit": a["commit"]}


# ---------------------------------------------------------------------------------------------------------
# Configuration > History: one timeline
# ---------------------------------------------------------------------------------------------------------
def _tag_rows(config: Any, comp: str | None) -> tuple[dict | None, list[dict]]:
    if not comp:
        return None, []
    try:
        docs = db.latest("tag_check", "tag_check:%s" % comp, config=config)
    except Exception:                                             # noqa: BLE001
        return None, []
    doc = next((d for d in docs if d["fact"].get("target") == comp), None)
    return doc, (doc["fact"].get("rows") or []) if doc else []


def _tag_commit(row: dict, canonical: str | None) -> str | None:
    hosts = row.get("hosts") or {}
    if canonical and hosts.get(canonical):
        return hosts[canonical]
    return next((v for v in hosts.values() if v), None)


def timeline(config: Any, cid: str, limit: int = 60) -> dict:
    rec = next((_rec(d) for d in _cfg_records(config) if d["fact"]["id"] == cid), {})
    comp = rec.get("component")
    events: list[dict] = []
    tdoc, rows = _tag_rows(config, comp)
    canon = (tdoc or {}).get("fact", {}).get("canonical") if tdoc else None
    repo = _component_repo(config, comp)
    for r in rows:
        if r.get("kind") in ("release", "silicon", "candidate") and r.get("created_at"):
            sha = _tag_commit(r, canon)
            events.append({"at": r["created_at"], "what": "tag", "label": r["tag"], "doc": tdoc,
                           "detail": "%s tag on %s" % (r["kind"], (sha or "?")[:12]),
                           "link": commit_link(config, repo, sha)})
    try:
        for d in db.latest("release_draft", "release_draft:%s:" % cid, config=config):
            f = d["fact"]
            events.append({"at": f.get("generated_at") or d["observed_at"], "what": "release draft",
                           "label": "%s %s" % (cid, f.get("version")), "doc": d,
                           "detail": "%s draft, %s" % (f.get("kind"), f.get("verdict")),
                           "href": "/releases/%s/%s" % (urllib.parse.quote(cid, safe=""),
                                                        urllib.parse.quote(f.get("version") or "", safe=""))})
    except Exception:                                             # noqa: BLE001 - releases not built here
        pass
    doc, new = shown(config)
    projects = set(_projects_of_cfg(config, cid))
    if doc is not None:
        fact = doc["fact"]
        for m in _manifests_for(config, fact, cid):
            events.append({"at": m.get("created") or doc["observed_at"], "what": m.get("kind") or "manifest",
                           "label": m.get("version") or m.get("name"), "doc": doc,
                           "detail": "%s manifest %s%s" % (m.get("kind"), m["path"],
                                                           (", foundry %s" % m["foundry"]) if m.get("foundry") else ""),
                           "link": commit_link(config, m.get("repo") or repo, m.get("commit"))})
        for a in fact.get("artefacts") or []:
            if a.get("project") in projects and a["role"] in ("stream", "source", "doc") and a.get("created"):
                events.append({"at": a["created"], "what": "upload", "label": a["name"], "doc": doc,
                               "detail": "%s %s by %s" % (a["role"], _full(a), a.get("created_by") or "?"),
                               "href": "/store/?q=%s" % urllib.parse.quote(a["name"], safe="")})
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"events": events[:limit], "more": max(0, len(events) - limit), "doc": doc, "latest": new,
            "projects": sorted(projects), "tags_read": tdoc is not None, "configured": bool(_settings(config))}


# ---------------------------------------------------------------------------------------------------------
# Component > Versions: per tag, its commit and the chips it went into
# ---------------------------------------------------------------------------------------------------------
def _chips_of(config: Any, comp: str, sha: str, fact: dict | None) -> list[str]:
    """Silicon configurations whose stored tree pins `comp` at `sha` (or whose root is that commit), plus tapeout
    manifests that pin it."""
    out = set()
    for d in _silicon_cfgs(config):
        cid, rec = d["fact"]["id"], _rec(d)
        s = rec["silicon"]
        label = "%s (%s %s)" % (cid, s.get("chip", ""), s.get("revision", ""))
        if rec.get("component") == comp and str(rec.get("commit") or "") == sha:
            out.add(label)
            continue
        try:
            pins = db.latest("pin_state", "pin_state:%s:" % cid, config=config)
        except Exception:                                         # noqa: BLE001
            pins = []
        if any((p["fact"].get("child") or {}).get("component") == comp and
               (p["fact"].get("child") or {}).get("commit") == sha for p in pins):
            out.add(label)
    for m in (fact or {}).get("manifests") or []:
        if m.get("kind") == "tapeout" and ((m.get("pin_components") or {}).get(comp) == sha or
                                           (comp == m.get("component") and m.get("commit") == sha)):
            out.add("%s (%s)" % (m.get("configuration") or m.get("name"),
                                 "tapeout manifest" if trusted(m) else "unverified tapeout manifest"))
    return sorted(out)


def component_versions(config: Any, comp: str) -> dict:
    tdoc, rows = _tag_rows(config, comp)
    canon = (tdoc or {}).get("fact", {}).get("canonical") if tdoc else None
    repo = _component_repo(config, comp)
    doc, _ = shown(config)
    fact = doc["fact"] if doc else None
    out = []
    for r in rows:
        if r.get("kind") not in ("release", "silicon", "candidate"):
            continue
        sha = _tag_commit(r, canon)
        out.append({"tag": r["tag"], "kind": r["kind"], "created_at": r.get("created_at"), "commit": sha,
                    "link": commit_link(config, repo, sha), "chips": _chips_of(config, comp, sha, fact) if sha else []})
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"rows": out, "doc": tdoc, "store": doc, "repo": repo}


# ---------------------------------------------------------------------------------------------------------
# Releases page
# ---------------------------------------------------------------------------------------------------------
def release_rows(config: Any) -> dict:
    """{(configuration, version): {published, manifest, store}} for every manifest of the shown index, and the
    index doc under key None."""
    doc, new = shown(config)
    out: dict[Any, Any] = {None: {"doc": doc, "latest": new}}
    if doc is None:
        return out
    fact = doc["fact"]
    by_raw = _streams_by_raw(fact)
    for m in fact.get("manifests") or []:
        key = (m.get("configuration"), m.get("version"))
        raw = m.get("gds_raw_md5")
        if raw is None:
            store = ("-", "c-notrun", "no GDS in this manifest")
        elif by_raw.get(raw):
            store = ("MATCH", "c-pass", "raw md5 %s in the store" % raw)
        else:
            store = ("NOT IN STORE", "c-fail", "no stream carries raw md5 %s" % raw)
        out[key] = {"published": m.get("created"), "manifest": m, "store": _untrusted(store, m),
                    "trusted": trusted(m)}
    return out


def collateral(config: Any) -> dict:
    doc, new = shown(config)
    rows = []
    if doc is not None:
        for a in doc["fact"].get("artefacts") or []:
            if a["role"] == "doc":
                rows.append({"a": a, "href": "/store/file/%s" % a["cached"] if a.get("cached") else None,
                             "why": a.get("cache_reason") or ("not downloaded yet" if not a.get("cached") else "")})
    rows.sort(key=lambda r: (r["a"].get("project") or "", r["a"].get("kind") or "", r["a"]["name"]))
    return {"rows": rows, "doc": doc, "latest": new}


def cached_file(config: Any, sha1: str) -> tuple[Path, dict] | None:
    """The cached copy of a document the shown index lists, or None (an unknown sha1 is never served)."""
    from nanosoc_rm.store.index import CACHE_DIR
    if not re.match(r"^[0-9a-f]{40}$", sha1 or ""):
        return None
    doc, _ = shown(config)
    if doc is None:
        return None
    a = next((x for x in doc["fact"].get("artefacts") or [] if x.get("cached") == sha1 and x["role"] in ("doc",
                                                                                                         "manifest")),
             None)
    p = config.data_dir / CACHE_DIR / sha1
    if a is None or not p.is_file() or p.is_symlink():
        return None
    return p, a


# ---------------------------------------------------------------------------------------------------------
# Findings (inbox cards, `store findings`)
# ---------------------------------------------------------------------------------------------------------
def findings(config: Any) -> list[dict]:
    doc, _ = shown(config)
    if doc is None:
        return []
    fact = doc["fact"]
    by_raw = _streams_by_raw(fact)
    out: list[dict] = []
    silicon = {d["fact"]["id"]: _rec(d) for d in _silicon_cfgs(config)}
    # store_mismatch: a manifest whose GDS is not what the store holds, or not what the release lock says
    for m in fact.get("manifests") or []:
        raw, path = m.get("gds_raw_md5"), m.get("gds_store_path")
        why = []
        if raw:
            at = [s for s in by_raw.get(raw, [])]
            if path and not any(_full(s) == path for s in at):
                held = next((a for a in fact.get("artefacts") or [] if _full(a) == path), None)
                why.append("the manifest names %s with raw md5 %s; the store %s" % (
                    path, raw, ("holds raw md5 %s there" % held.get("raw_md5")) if held else "holds no file there"))
            elif not at:
                why.append("no stream in the store carries raw md5 %s" % raw)
        for lk in _lock_gds(config, m.get("configuration") or "") if m.get("configuration") else []:
            if raw and lk["md5"] != raw and lk["version"] == m.get("version"):
                why.append("release lock %s says GDS md5 %s, the manifest %s" % (lk["version"], lk["md5"], raw))
        if not trusted(m):
            why.append("the manifest is not trusted: %s" % _distrust(m))
        for a in m.get("artefacts") or []:
            held = next((x for x in fact.get("artefacts") or [] if _full(x) == a.get("path")), None)
            if held and a.get("stored_md5") and held.get("md5") and a["stored_md5"] != held["md5"]:
                why.append("%s: manifest says stored md5 %s, the store %s" % (a["path"], a["stored_md5"], held["md5"]))
            elif held and a.get("md5") and held.get("raw_md5") and a["md5"] != held["raw_md5"]:
                why.append("%s: manifest says raw md5 %s, the store %s" % (a["path"], a["md5"], held["raw_md5"]))
        if why:
            out.append({"kind": "store_mismatch", "about": m["path"], "doc": doc,
                        "title": "The store does not hold what manifest %s names." % m["name"], "evidence": why})
    # release_not_archived: a silicon configuration with no trusted manifest in the release repository. Only when
    # the manifests were read (a NOT-RUN or INFRA section knows nothing about what the repository holds).
    for cid, rec in sorted(silicon.items()):
        if fact["sections"].get("manifests", {}).get("verdict") != "PASS":
            break
        if not [m for m in _manifests_for(config, fact, cid) if trusted(m)]:
            s = rec["silicon"]
            out.append({"kind": "release_not_archived", "about": cid, "doc": doc,
                        "title": "%s (%s %s) has no verified manifest in %s." % (cid, s.get("chip"), s.get("revision"),
                                                                        fact["release_repo"]),
                        "evidence": ["silicon configuration at %s, tag %s" % (str(rec.get("commit") or "?")[:12],
                                                                              ((rec.get("ref") or {}).get("tag")
                                                                               if isinstance(rec.get("ref"), dict)
                                                                               else None) or "-"),
                                     "the store's release repository %s holds %d manifest(s), none for it with a "
                                     "verified signature" % (fact["release_repo"], len(fact.get("manifests") or []))]})
    # tapeout_unregistered: a stream the store marks shipped that no registered tapeout manifest covers
    registered_raw = {m.get("gds_raw_md5") for m in fact.get("manifests") or []
                      if m.get("kind") == "tapeout" and m.get("configuration") in silicon and trusted(m)}
    groups: dict[str, list[dict]] = {}
    for a in fact.get("artefacts") or []:
        shipped = (a.get("ships") or "").lower() in SHIPPED
        if a["role"] == "stream" and shipped and a.get("raw_md5") not in registered_raw:
            groups.setdefault(a.get("project") or "?", []).append(a)
    for m in fact.get("manifests") or []:
        if m.get("kind") == "tapeout" and m.get("configuration") not in silicon:
            groups.setdefault(m.get("project") or m.get("configuration") or "?", []).append(
                {"name": m["name"], "repo": fact["release_repo"], "path": m["path"], "raw_md5": m.get("gds_raw_md5"),
                 "commit": m.get("commit"), "tag": m.get("tag")})
    for project, rows in sorted(groups.items()):
        out.append({"kind": "tapeout_unregistered", "about": project, "doc": doc,
                    "title": "Store project %s holds %d shipped stream(s) no registered silicon configuration covers."
                             % (project, len(rows)),
                    "evidence": ["%s: raw md5 %s, %s" % (r["name"], r.get("raw_md5") or "?",
                                                         ("tag %s, commit %s" % (r.get("tag") or "-",
                                                                                 (r.get("commit") or "")[:12]))
                                                         if r.get("commit") else NO_IDENTITY) for r in rows]})
    return out


# ---------------------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------------------
def search(config: Any, q: str, limit: int = 8) -> list[dict]:
    """Rows of the shown index: a path, name or run-tag substring (3+ characters), or a hex prefix (4 to 40) of a
    commit, a build revision, a raw md5 or a stored md5."""
    q = (q or "").strip()
    if len(q) < 3:
        return []
    doc, _ = shown(config)
    if doc is None:
        return []
    ql = q.lower()
    hexq = ql if _HEX.match(ql) else None
    fact = doc["fact"]
    out: list[dict] = []

    def href(text: str) -> str:
        return "/store/?q=%s" % urllib.parse.quote(text, safe="")

    for a in fact.get("artefacts") or []:
        full = _full(a)
        hit = ql in full.lower() or (a.get("run_tag") and ql in a["run_tag"].lower())
        if not hit and hexq:
            hit = any((v or "").startswith(hexq) for v in (a.get("commit"), a.get("raw_md5"), a.get("md5"),
                                                            a.get("subject_md5")))
        if hit:
            out.append({"kind": a["role"], "label": a["name"], "sub": "%s · %s" % (
                full, ("commit %s" % a["commit"][:12]) if a.get("commit") else NO_IDENTITY), "href": href(full),
                "tone": "c-neutral", "path": full})
    for b in fact.get("builds") or []:
        revs = [v.get("revision") or "" for v in b.get("vcs") or []]
        if ql in ("%s/%s" % (b["name"], b["number"])).lower() or (hexq and any(r.startswith(hexq) for r in revs)):
            out.append({"kind": "build", "label": "%s #%s" % (b["name"], b["number"]),
                        "sub": "build-info · vcs %s%s" % (", ".join(r[:12] for r in revs if r) or "none",
                                                          " (dirty tree)" if b.get("dirty") else ""),
                        "href": href(b["number"]), "tone": "c-neutral", "path": "%s/%s" % (b["name"], b["number"])})
    for m in fact.get("manifests") or []:
        if ql in m["path"].lower() or (hexq and (m.get("commit") or "").startswith(hexq)) or (
                hexq and (m.get("gds_raw_md5") or "").startswith(hexq)):
            out.append({"kind": m.get("kind") or "manifest", "label": m["name"],
                        "sub": "%s manifest · %s" % (m.get("kind"), m["path"]), "href": href(m["name"]),
                        "tone": "c-ink", "path": m["path"]})
    return out[:limit]
