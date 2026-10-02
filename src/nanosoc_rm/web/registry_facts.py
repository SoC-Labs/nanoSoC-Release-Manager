"""Registry records as facts (interfaces.md section 2 "Facts"; observation kind `registry_record`; A3, H1).

Every configuration, component, defect, erratum and import source the web shows is a `registry_record`
observation, so a registry fact carries an age and a command like every other fact:

    subject      registry_record:<file>#<pointer>
    source       git -C <registry> show <commit>:<file>        (the checkout's path is written <registry>)
    observed_at  when the service read the registry at <commit>
    snapshot     one per file, scope registry_record:<file>

`refresh(config)` reads the checkout at `registry.path` when its HEAD differs from the commit of the stored
records. The registry is loaded with the contracted validator first (`nanosoc_rm.registry.load_registry`):
a registry with any problem is not loaded, and the service keeps serving the last good records and shows
the problems (interfaces.md section 2 "Errors").

What is stored is exactly what was validated. The loader reads the checkout's files and reports the commit it
read them at (`Registry.commit`); a checkout with uncommitted changes or untracked files has no such commit,
and is not loaded either. The records are then read with `git show <Registry.commit>:<file>`, the command the
fact cites, and compared with the loaded Registry's records (`configurations()`, `components()`, `defects()`,
`errata()`, `imports()`): any difference means the two reads saw different trees, and nothing is stored.
Files that disappeared get an empty snapshot, so a deleted record leaves the current view.

`refresh` never raises: what went wrong is returned as problem strings, which pages show as a banner.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import run_git

__all__ = ["KIND", "REGISTRY_PLACEHOLDER", "records_from_files", "refresh", "current", "stored_commit"]

KIND = "registry_record"
REGISTRY_PLACEHOLDER = "<registry>"      # the checkout's private path is not printed in a fact's command
_GIT_TIMEOUT_S = 60
_lock = threading.Lock()


def _obs(file: str, pointer: str, record_kind: str, rid: str, commit: str, record: dict, at: str,
         snap_id: str) -> dict:
    return {"schema": 1, "kind": KIND, "subject": "%s:%s#%s" % (KIND, file, pointer), "observed_at": at,
            "sources": [{"kind": "registry", "command": "git -C %s show %s:%s" % (REGISTRY_PLACEHOLDER, commit, file)}],
            "snapshot": {"id": snap_id, "scope": "%s:%s" % (KIND, file)},
            "fact": {"record_kind": record_kind, "id": rid, "file": file, "pointer": pointer, "commit": commit,
                     "record": record}}


def _snap_id(file: str, commit: str, at: str) -> str:
    return "%s:%s@%s/%s" % (KIND, file, at, commit[:12])


def records_from_files(files: dict[str, Any], commit: str, at: str) -> dict[str, list[dict]]:
    """file -> registry_record observations, from parsed registry files (README section 1 layout).
    Pure. Files outside the layout are ignored."""
    out: dict[str, list[dict]] = {}
    for file, doc in sorted(files.items()):
        if not isinstance(doc, dict):
            continue
        sid = _snap_id(file, commit, at)
        rows: list[dict] = []
        if file == "eligibility.yaml":
            rows = [_obs(file, "/configurations/%d" % i, "configuration", c.get("id", "?"), commit, c, at, sid)
                    for i, c in enumerate(doc.get("configurations") or []) if isinstance(c, dict)]
        elif file == "catalogue.yaml":
            rows = [_obs(file, "/components/%d" % i, "component", c.get("id", "?"), commit, c, at, sid)
                    for i, c in enumerate(doc.get("components") or []) if isinstance(c, dict)]
        elif file == "imports.yaml":
            for i, s in enumerate(doc.get("sources") or []):
                d = (s or {}).get("doc") or {}
                rows.append(_obs(file, "/sources/%d" % i, "import_source",
                                 "%s:%s" % (d.get("component", "?"), d.get("path", "?")), commit, s, at, sid))
        elif file.startswith("defects/") and file.endswith((".yaml", ".yml")):
            rows = [_obs(file, "", "defect", doc.get("id", "?"), commit, doc, at, sid)]
        elif file.startswith("errata/") and file.endswith((".yaml", ".yml")) and file.count("/") == 2:
            rows = [_obs(file, "", "erratum", doc.get("id", "?"), commit, doc, at, sid)]
        else:
            continue
        out[file] = rows
    return out


def stored_commit(config: Config | None = None) -> str | None:
    """The registry commit of the current stored records (None if none are stored)."""
    commits = {d["fact"]["commit"] for d in db.latest(KIND, config=config)}
    return commits.pop() if len(commits) == 1 else (None if not commits else "mixed")


def _head(path: str) -> str:
    r = run_git("rev-parse", "--verify", "HEAD^{commit}", cwd=path, timeout=_GIT_TIMEOUT_S)
    if not r.ok:
        raise RuntimeError("the registry checkout is not readable (git rev-parse exit %s)" % r.returncode)
    return r.stdout.strip()


def _read_files(path: str, commit: str) -> dict[str, Any]:
    ls = run_git("ls-tree", "-r", "--name-only", commit, cwd=path, timeout=_GIT_TIMEOUT_S).check()
    files = {}
    for name in ls.stdout.splitlines():
        if not (name in ("eligibility.yaml", "catalogue.yaml", "imports.yaml")
                or name.startswith(("defects/", "errata/"))):
            continue
        shown = run_git("show", "%s:%s" % (commit, name), cwd=path, timeout=_GIT_TIMEOUT_S).check()
        files[name] = contracts.load_yaml(shown.stdout)
    return files


def _load(path: str) -> tuple[Any, list[str]]:
    """(the contracted loader's Registry, []) when the checkout loads, else (None, its problems). A missing
    loader is a problem too: the web never trusts registry files nothing has validated."""
    try:
        from nanosoc_rm.registry import RegistryError, load_registry
    except ImportError as e:
        return None, ["the registry loader (nanosoc_rm.registry) is not installed: %s" % e]
    from pathlib import Path
    try:
        return load_registry(Path(path)), []
    except RegistryError as e:
        probs = getattr(e, "problems", None) or [e]
        return None, ["registry problem: %s" % _problem_text(p) for p in probs]


def _differences(reg: Any, by_file: dict[str, list[dict]]) -> list[str]:
    """Where the records read at the commit differ from the records the loader validated ([] = none)."""
    got: dict[str, list] = {}
    for rows in by_file.values():
        for d in rows:
            got.setdefault(d["fact"]["record_kind"], []).append(d["fact"]["record"])
    imports = reg.imports()
    want = {"configuration": reg.configurations(), "component": reg.components(), "defect": reg.defects(),
            "erratum": reg.errata(), "import_source": list((imports or {}).get("sources") or [])}

    def key(r: Any) -> str:
        return json.dumps(r, sort_keys=True, default=str)
    out = []
    for kind, records in sorted(want.items()):
        a, b = got.get(kind, []), list(records or [])
        if kind in ("defect", "erratum"):                      # one file each: the order is not the files'
            a, b = sorted(a, key=key), sorted(b, key=key)
        if a != b:
            out.append("%s records (%d read at the commit, %d validated)" % (kind, len(a), len(b)))
    return out


def _problem_text(p: Any) -> str:
    if all(hasattr(p, k) for k in ("file", "pointer", "message")):
        return "%s:%s: %s [%s]" % (p.file, p.pointer, p.message, getattr(p, "rule", ""))
    return str(p)


def refresh(config: Config) -> list[str]:
    """Bring the stored registry records up to the checkout's HEAD. Returns the problems (empty = the
    stored records are the checkout's current, validated content)."""
    path = (config.get("registry") or {}).get("path")
    if not path:
        return ["no registry checkout configured ([registry] path): showing the last stored records"]
    with _lock:
        try:
            head = _head(path)
        except Exception as e:                                   # noqa: BLE001 - a banner, never a 500
            return ["%s: showing the last stored records" % e]
        if stored_commit(config) == head:
            return []
        reg, problems = _load(path)
        if problems:
            return problems + ["the registry at %s was not loaded: showing the last good records" % head[:12]]
        commit = getattr(reg, "commit", None)
        if not commit:
            return ["the registry checkout has changes that are not committed (or untracked files), so no "
                    "`git show <commit>:<file>` reproduces what was validated: not loaded, showing the last "
                    "good records"]
        if stored_commit(config) == commit:
            return []
        try:
            at = clock.iso()
            by_file = records_from_files(_read_files(path, commit), commit, at)
            diff = _differences(reg, by_file)
            if diff:
                return ["the registry at %s was not loaded: what was validated differs from what %s holds: %s"
                        % (commit[:12], commit[:12], "; ".join(diff))]
            stored_scopes = {d["snapshot"]["scope"] for d in db.latest(KIND, config=config)}
            for file, docs in by_file.items():
                db.put_snapshot("%s:%s" % (KIND, file), docs, config=config)
            for scope in sorted(stored_scopes - {"%s:%s" % (KIND, f) for f in by_file}):
                db.put_snapshot(scope, [], config=config)            # the file is gone: so are its records
        except db.ObservationRefused as e:
            return ["registry records refused by the store: %s" % "; ".join(e.problems[:3])]
        except Exception as e:                                   # noqa: BLE001
            return ["reading the registry at %s failed: %s" % (commit[:12], e)]
    return []


def current(config: Config, record_kind: str | None = None) -> list[dict]:
    """The stored registry_record observations (after `refresh`), optionally of one record kind."""
    docs = db.latest(KIND, config=config)
    return [d for d in docs if record_kind is None or d["fact"]["record_kind"] == record_kind]
