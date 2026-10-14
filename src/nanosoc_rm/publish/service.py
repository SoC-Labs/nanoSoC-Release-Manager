"""The publish service (CR-ART-publish; G8, D12): a release manifest or a tapeout record, from draft to ledger.

    preview(config, kind, key, by=...) -> preview          build, validate, diff, check every target path (GET/HEAD
                                                            only); written to <data_dir>/publish/previews/
    publish(config, kind, key, confirm=SHA256, by=...) -> release_publication observation

`kind` is "release" (key `<configuration>/<version>`, a ready local release draft) or "tapeout" (key: the tapeout
id, its draft in the registry's `tapeouts/`). Publishing, in order, stopping at the first failure:

 1. readiness: a store url, a token file, a signing key this host can use, a key to verify with; `by` a listed
    publisher (never a shared login) unless it is the CLI's own `cli:<user>`;
 2. build the record again and refuse unless its sha256 equals `confirm`, the digest of the preview a person saw;
 3. HEAD every target under `asic-release/<project>/<version>/`: a path that holds other bytes refuses the whole
    publish (never overwrite); a path that holds the same bytes is a retry and is not uploaded again;
 4. sign the record (SSHSIG, namespace nanosoc-rm), upload the documents, the record and its `.sig`, each with its
    deploy-time `rm.*` properties and checksum headers;
 5. read back: every path's stored sha256, the record's and signature's bytes, the signature against the allowed
    keys, the record's `rm.manifest_sha256` property;
 6. a build-info record (`rm-release-<project>` / `rm-tapeout-<project>`, number = version / tapeout id) and a
    status-only promotion: RELEASED, or TAPED-OUT for a tapeout at submitted or later;
 7. the ledger: a `release_publication` observation, a copy of the record under <data_dir>/publish/published/,
    and an `actions` row with the login.

Nothing here runs in the web process (client.refuse_writes_here); the GUI queues `nanosoc-rm publish-release` /
`register-tapeout` as a local job.
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from nanosoc_rm import __version__, clock, contracts
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.publish import client as clientmod
from nanosoc_rm.publish import manifest as M
from nanosoc_rm.publish import signing
from nanosoc_rm.publish.core import (BUILD_PREFIX, MANIFEST_NAME, RELEASE_STATUS, TAPEOUT_STATUS, PublishError,
                                     PublishRefused, PublishUsage, can_publish, preview_path, publish_dir,
                                     readiness, settings, tapeout_name, token)

__all__ = ["preview", "publish", "read_preview", "publications", "open_store", "cli_login", "Plan"]


def cli_login() -> str:
    try:
        return "cli:%s" % getpass.getuser()
    except Exception:                                     # noqa: BLE001 - no user name: still say it was the CLI
        return "cli:unknown"


def open_store(config: Config, transport: Any = None) -> clientmod.Store:
    s = settings(config)
    if not s.url:
        raise PublishError("no store: set [stores.artifactory] url")
    try:
        return clientmod.Store(s.url, token(config), release_repo=s.release_repo, transport=transport)
    except clientmod.StoreError as e:
        raise PublishError(str(e)) from None


def _sha(data: bytes) -> dict:
    return {"md5": hashlib.md5(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


class Plan:
    """One record ready to publish: its bytes, the files beside it and where each goes."""

    def __init__(self, config: Config, kind: str, key: str, record: dict, docs: dict[str, bytes]):
        self.config, self.kind, self.key, self.record, self.docs = config, kind, key, record, docs
        self.data = M.canonical(record)
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.project = record["project"]
        self.version = record["version"]
        self.name = MANIFEST_NAME if kind == "release" else tapeout_name(record["tapeout_id"])
        self.folder = "%s/%s/%s" % (settings(config).release_repo, self.project, self.version)
        self.store_path = "%s/%s" % (self.folder, self.name)
        self.build_name = BUILD_PREFIX[kind] + self.project
        self.build_number = self.version if kind == "release" else record["tapeout_id"]
        self.status = RELEASE_STATUS if kind == "release" else (
            TAPEOUT_STATUS if record["status"] in ("submitted", "accepted", "fabricated") else None)

    @property
    def configuration(self) -> str | None:
        return self.record.get("configuration")

    def commit(self) -> str:
        return self.record["commit"] if self.kind == "release" else self.record["source"]["commit"]

    def tag(self) -> str | None:
        if self.kind == "release":
            return self.record["tag"]
        tags = self.record["source"]["tags"]
        return tags[-1] if tags else None

    def repo(self) -> dict:
        return self.record["repo"] if self.kind == "release" else self.record["source"]["repo"]

    def pins(self) -> list[tuple[str, str]]:
        if self.kind == "release":
            return [(p["tree_path"], p["commit"]) for p in self.record["pins"]]
        return sorted(self.record["source"]["pins"].items())

    def props(self, role: str, data: bytes) -> dict[str, str]:
        """Deploy-time properties (the producers' rm.* convention; OSS cannot add them later)."""
        p = {"rm.project": self.project, "rm.repo": self.repo()["path"], "rm.commit": self.commit(), "rm.dirty": "no",
             "rm.pins_md5": M.pins_md5(self.pins()), "rm.role": role, "rm.raw_md5": hashlib.md5(data).hexdigest(),
             "rm.confidentiality": "internal", "rm.release_version": self.version, "rm.manifest_sha256": self.sha256}
        if self.configuration:
            p["rm.config"] = self.configuration
        if self.tag():
            p["rm.tag"] = self.tag()
            p["rm.tag_commit"] = self.commit()
        if self.kind == "tapeout":
            p["rm.tapeout_id"] = self.record["tapeout_id"]
            p["rm.subject_md5"] = self.record["gds"]["raw_md5"]
        return p

    def files(self, sig: bytes | None = None) -> list[tuple[str, str, bytes]]:
        """(store path, role, bytes): the documents first, then the record, then its signature."""
        out = [("%s/%s" % (self.folder, n), "document", b) for n, b in sorted(self.docs.items())]
        out.append((self.store_path, "manifest", self.data))
        if sig is not None:
            out.append((self.store_path + ".sig", "signature", sig))
        return out


def _plan(config: Config, kind: str, key: str, *, artefacts: Sequence[tuple[str, str]] = (),
          draft: str | Path | None = None, store: Any = None) -> Plan:
    if kind == "release":
        configuration, _, version = key.partition("/")
        record, docs = M.release_manifest(config, configuration, version, artefacts=artefacts, store=store)
    elif kind == "tapeout":
        if artefacts:
            raise PublishUsage("--artefact is for a release manifest; a tapeout draft names its own store paths")
        record, docs = M.tapeout_record(config, key, draft=draft, store=store), {}
    else:
        raise PublishUsage("kind must be release or tapeout")
    return Plan(config, kind, key, record, docs)


def _state(store: Any, path: str, data: bytes) -> str:
    """absent | same | differs, from a HEAD (the store answers X-Checksum-Sha256)."""
    code, hdrs = store.head(path)
    if code == 404:
        return "absent"
    if code != 200:
        raise PublishError("HEAD %s: HTTP %d" % (path, code))
    stored = hdrs.get("x-checksum-sha256")
    if not stored:
        st = store.storage(path) or {}
        stored = (st.get("checksums") or {}).get("sha256")
    return "same" if stored == hashlib.sha256(data).hexdigest() else "differs"


def _build_state(store: Any, plan: Plan) -> str:
    bi = store.build(plan.build_name, plan.build_number)
    if bi is None:
        return "absent"
    return "same" if (bi.get("properties") or {}).get("rm.manifest_sha256") == plan.sha256 else "differs"


def _previous(config: Config, plan: Plan) -> dict | None:
    """The last record of this kind and project this tool published (its local copy), for the preview's diff."""
    rows = [r for r in publications(config) if r["fact"]["project"] == plan.project and r["fact"]["kind"] == plan.kind
            and not (r["fact"]["version"] == plan.version and r["fact"]["name"] == plan.name)]
    for r in sorted(rows, key=lambda r: r["fact"]["published_at"], reverse=True):
        p = publish_dir(config) / "published" / r["fact"]["project"] / r["fact"]["version"] / r["fact"]["name"]
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def publications(config: Config) -> list[dict]:
    from nanosoc_rm import db
    try:
        return db.latest("release_publication", config=config)
    except Exception:                                     # noqa: BLE001 - no store yet: nothing published
        return []


# ---------------------------------------------------------------------------------------------------------
def preview(config: Config | str | None, kind: str, key: str, *, by: str | None = None,
            artefacts: Sequence[tuple[str, str]] = (), draft: str | Path | None = None, transport: Any = None) -> dict:
    """Build the record, read the store (GET/HEAD only) and write the preview a person confirms."""
    config = config if isinstance(config, Config) else load_config(config)
    store = open_store(config, transport) if settings(config).url else None
    try:
        plan = _plan(config, kind, key, artefacts=artefacts, draft=draft, store=store)
        problems: list[str] = []
        targets = []
        build = None
        if store is None:
            problems.append("no store configured: the targets were not checked")
        else:
            for path, role, data in plan.files():
                st = _state(store, path, data)
                targets.append({"path": path, "role": role, "state": st, "sha256": hashlib.sha256(data).hexdigest()})
                if st == "differs":
                    problems.append("%s already holds other bytes: the publish would be refused (never overwrite)"
                                    % path)
            build = {"name": plan.build_name, "number": plan.build_number, "state": _build_state(store, plan),
                     "status": plan.status}
            if build["state"] == "differs":
                problems.append("build %s/%s already exists for another record" % (plan.build_name, plan.build_number))
    finally:
        if store is not None:
            store.close()
    problems += readiness(config)
    doc = {"kind": kind, "key": key, "project": plan.project, "version": plan.version, "name": plan.name,
           "store_path": plan.store_path, "sha256": plan.sha256, "record": plan.record,
           "documents": [{"name": n, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
                         for n, b in sorted(plan.docs.items())],
           "artefacts": [list(a) for a in artefacts], "targets": targets, "build": build,
           "diff": M.diff(_previous(config, plan), plan.record), "problems": problems,
           "previewed_at": clock.iso(), "previewed_by": by or cli_login()}
    p = preview_path(config, kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)
    return doc


def read_preview(config: Config, kind: str, key: str) -> dict | None:
    try:
        return json.loads(preview_path(config, kind, key).read_text(encoding="utf-8"))
    except (OSError, ValueError, PublishUsage):
        return None


def _bi_time(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d+0000" % (t.microsecond // 1000)


def _web(config: Config, repo: dict) -> str:
    try:
        return "%s/%s" % (config.host(repo["host"])["web"].rstrip("/"), repo["path"])
    except Exception:                                     # noqa: BLE001 - a host the config does not name
        return "%s:%s" % (repo["host"], repo["path"])


def _build_info(config: Config, plan: Plan, files: list[tuple[str, str, bytes]], by: str) -> dict:
    vcs = [{"url": _web(config, plan.repo()), "revision": plan.commit(), "message": "tag %s" % (plan.tag() or "-")}]
    if plan.kind == "release":
        vcs += [{"url": _web(config, p["repo"]), "revision": p["commit"], "message": "pin %s" % p["tree_path"]}
                for p in plan.record["pins"]]
    else:
        vcs += [{"url": "%s#%s" % (_web(config, plan.repo()), path), "revision": sha, "message": "pin %s" % path}
                for path, sha in plan.pins()]
    arts = []
    for path, role, data in files:
        h = _sha(data)
        arts.append({"name": path.rsplit("/", 1)[1], "type": role, "sha1": h["sha1"], "sha256": h["sha256"],
                     "md5": h["md5"], "path": path.split("/", 1)[1]})
    props = {"rm.manifest_sha256": plan.sha256, "rm.kind": plan.kind, "rm.project": plan.project,
             "rm.version": plan.version, "rm.store_path": plan.store_path}
    if plan.tag():
        props["rm.tag"] = plan.tag()
    if plan.configuration:
        props["rm.configuration"] = plan.configuration
    return {"version": "1.0.1", "name": plan.build_name, "number": plan.build_number, "started": _bi_time(clock.now()),
            "durationMillis": 0, "principal": by, "agent": {"name": "nanosoc-rm", "version": __version__},
            "buildAgent": {"name": "nanosoc-rm", "version": __version__}, "vcs": vcs, "properties": props,
            "modules": [{"id": "%s:%s" % (plan.project, plan.build_number), "artifacts": arts}]}


def publish(config: Config | str | None, kind: str, key: str, *, confirm: str, by: str | None = None,
            artefacts: Sequence[tuple[str, str]] = (), draft: str | Path | None = None, transport: Any = None,
            store_ledger: bool = True) -> dict:
    """Publish one record (module docstring). Returns its release_publication observation."""
    config = config if isinstance(config, Config) else load_config(config)
    if not confirm or len(confirm) != 64:
        raise PublishUsage("--confirm needs the 64-hex sha256 of the previewed record (run with --dry-run first)")
    login = by or cli_login()
    s = settings(config)
    action = "publish_release" if kind == "release" else "register_tapeout"
    from nanosoc_rm import db
    try:
        if not login.startswith("cli:"):
            refused = can_publish(config, login)
            if refused:
                raise PublishRefused("; ".join(refused))
        blocked = readiness(config)
        if blocked:
            raise PublishError("publishing cannot run here: %s" % "; ".join(blocked))
        result = _publish(config, s, kind, key, confirm, login, artefacts, draft, transport)
    except (PublishRefused, PublishError, clientmod.StoreRefused, clientmod.StoreError,
            signing.SignatureError) as e:
        if store_ledger:
            db.log_action(login, action, "%s:%s" % (kind, key), "refused: %s" % str(e)[:300], config=config)
        raise
    if store_ledger:
        db.put(result, config=config)
        f = result["fact"]
        db.log_action(login, action, f["store_path"], "%s sha256 %s" % (f["outcome"], f["manifest_sha256"]),
                      config=config)
    return result


def _publish(config: Config, s: Any, kind: str, key: str, confirm: str, login: str,
             artefacts: Sequence[tuple[str, str]], draft: Any, transport: Any) -> dict:
    started = clock.iso()
    with open_store(config, transport) as store:
        plan = _plan(config, kind, key, artefacts=artefacts, draft=draft, store=store)
        if plan.sha256 != confirm:
            raise PublishRefused("the record's sha256 is now %s, not the confirmed %s: something changed since the "
                                 "preview; preview again" % (plan.sha256, confirm))
        sig = signing.sign(plan.data, s.signing_key, ssh_keygen=s.ssh_keygen)
        sig_bytes = sig.text.encode("ascii")
        files = plan.files(sig_bytes)
        states = {path: _state(store, path, data) for path, _, data in files}
        clash = [p for p, st in states.items() if st == "differs"]
        if clash:
            raise PublishRefused("refused, nothing uploaded: %s already hold(s) other bytes (never overwrite)"
                                 % ", ".join(clash))
        bstate = _build_state(store, plan)
        if bstate == "differs":
            raise PublishRefused("refused, nothing uploaded: build %s/%s already exists for another record"
                                 % (plan.build_name, plan.build_number))
        for path, role, data in files:
            if states[path] == "absent":
                h = _sha(data)
                store.put_file(path, data, props=plan.props(role, data),
                               checksums={"sha256": h["sha256"], "sha1": h["sha1"], "md5": h["md5"]})
        # -- read back ------------------------------------------------------------------------------------------
        notes = []
        for path, role, data in files:
            st = store.storage(path)
            if st is None or (st.get("checksums") or {}).get("sha256") != hashlib.sha256(data).hexdigest():
                raise PublishRefused("read-back: %s is %s in the store" % (
                    path, "missing" if st is None else "different (sha256 %s)" % (st.get("checksums") or {}).get("sha256")))
        got = store.get_bytes(plan.store_path)
        got_sig = store.get_bytes(plan.store_path + ".sig")
        if got != plan.data or got_sig != sig_bytes:
            raise PublishRefused("read-back: the record or its signature came back different")
        allowed = signing.load_allowed(s.allowed_file(), s.signer_identity)
        fpr = signing.verify(got, got_sig.decode("ascii"), allowed, ssh_keygen=s.ssh_keygen)
        props = store.properties(plan.store_path)
        if (props.get("rm.manifest_sha256") or [None])[0] != plan.sha256:
            notes.append("the record's rm.manifest_sha256 property reads back as %r" % (props.get("rm.manifest_sha256"),))
        # -- build-info and status-only promotion ---------------------------------------------------------------
        if bstate == "absent":
            store.put_build(_build_info(config, plan, files, login))
        bi = store.build(plan.build_name, plan.build_number) or {}
        if (bi.get("properties") or {}).get("rm.manifest_sha256") != plan.sha256:
            raise PublishRefused("read-back: build %s/%s does not name this record" % (plan.build_name, plan.build_number))
        build_ok = True
        if plan.status:
            have = [x.get("status") for x in bi.get("statuses") or [] if isinstance(x, dict)]
            if plan.status not in have:
                store.promote(plan.build_name, plan.build_number, {
                    "status": plan.status, "comment": "%s %s (%s) published by nanosoc-rm, record sha256 %s" % (
                        plan.project, plan.version, plan.name, plan.sha256),
                    "ciUser": login, "timestamp": _bi_time(clock.now()), "dryRun": False, "copy": False,
                    "artifacts": False, "dependencies": False, "failFast": True})
                bi = store.build(plan.build_name, plan.build_number) or {}
                have = [x.get("status") for x in bi.get("statuses") or [] if isinstance(x, dict)]
            build_ok = plan.status in have
            if not build_ok:
                notes.append("the build record does not list status %s after the promotion" % plan.status)
    copy = publish_dir(config) / "published" / plan.project / plan.version / plan.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(plan.data)
    copy.with_name(plan.name + ".sig").write_bytes(sig_bytes)
    uploaded = any(st == "absent" for st in states.values())
    fact = {"kind": kind, "project": plan.project, "version": plan.version, "name": plan.name,
            "store_path": plan.store_path, "configuration": plan.configuration,
            "tapeout_id": plan.record.get("tapeout_id"), "tag": plan.tag(), "commit": plan.commit(),
            "status": plan.record.get("status"), "manifest_sha256": plan.sha256,
            "manifest_md5": hashlib.md5(plan.data).hexdigest(),
            "signature": {"file": plan.name + ".sig", "sha256": hashlib.sha256(sig_bytes).hexdigest(),
                          "fingerprint": fpr, "backend": sig.backend},
            "files": [{"path": p, "role": r, "sha256": hashlib.sha256(d).hexdigest(),
                       "md5": hashlib.md5(d).hexdigest(), "bytes": len(d), "uploaded": states[p] == "absent"}
                      for p, r, d in files],
            "build": {"name": plan.build_name, "number": plan.build_number, "status": plan.status},
            "published_by": login, "published_at": clock.iso(),
            "verified": {"checksums": True, "signature": True, "build": build_ok},
            "outcome": "published" if uploaded else "already-published"}
    if notes:
        fact["notes"] = notes
    cmd = "nanosoc-rm %s %s --confirm %s" % ("publish-release" if kind == "release" else "register-tapeout",
                                            key, plan.sha256)
    obs = {"schema": 1, "kind": "release_publication",
           "subject": "release_publication:%s:%s:%s" % (plan.project, plan.version, plan.name),
           "observed_at": started, "sources": [{"kind": "derived", "command": cmd}], "fact": fact}
    problems = contracts.validate_observation(obs)
    if problems:
        raise PublishError("the ledger record does not meet its contract: %s" % "; ".join(problems[:3]))
    return obs
