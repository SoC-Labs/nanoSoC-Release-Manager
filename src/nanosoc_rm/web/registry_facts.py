"""Registry records as facts for the web (interfaces.md section 2 "Facts"; observation kind `registry_record`; A3, H1).

The facts themselves are produced by the registry module (`nanosoc_rm.registry.store_records`, the one producer):

    subject      registry_record:<file>#<pointer>
    source       git -C <registry checkout> show <commit>:<file>
    observed_at  when the service read the registry at <commit>
    snapshot     one per file, scope registry_record:<file>

`refresh(config)` reads the checkout at `registry.path` when its HEAD differs from the commit of the stored
records. The registry is loaded with the contracted validator (`nanosoc_rm.registry.load_registry`): a registry
with any problem is not loaded, and the service keeps serving the last good records and shows the problems
(interfaces.md section 2 "Errors"). A checkout with uncommitted changes or untracked files has no commit
(`Registry.commit` is None: no `git show` would reproduce what was read), and is not loaded either. What is
stored is exactly what the loader validated; a file that disappeared gets an empty snapshot (store_records).

`refresh` never raises: what went wrong is returned as problem strings, which pages show as a banner.
"""
from __future__ import annotations

import threading
from pathlib import Path

from nanosoc_rm import db
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import run_git
from nanosoc_rm.registry import RegistryError, load_registry, store_records

__all__ = ["KIND", "refresh", "current", "stored_commit"]

KIND = "registry_record"
_GIT_TIMEOUT_S = 60
_lock = threading.Lock()


def stored_commit(config: Config | None = None) -> str | None:
    """The registry commit of the current stored records (None if none are stored)."""
    commits = {d["fact"]["commit"] for d in db.latest(KIND, config=config)}
    return commits.pop() if len(commits) == 1 else (None if not commits else "mixed")


def _head(path: str) -> str:
    r = run_git("rev-parse", "--verify", "HEAD^{commit}", cwd=path, timeout=_GIT_TIMEOUT_S)
    if not r.ok:
        raise RuntimeError("the registry checkout is not readable (git rev-parse exit %s)" % r.returncode)
    return r.stdout.strip()


def _problem_text(p) -> str:
    if all(hasattr(p, k) for k in ("file", "pointer", "message")):
        return "%s:%s: %s [%s]" % (p.file, p.pointer, p.message, getattr(p, "rule", ""))
    return str(p)


def _fallback(config: Config, last: str = "good") -> str:
    """What the pages show instead: the last good records, or, when none were ever stored, say so (a banner
    must not promise records that do not exist)."""
    try:
        have = bool(db.latest(KIND, config=config))
    except Exception:                                            # noqa: BLE001 - a banner, never a 500
        have = True
    return ("showing the last %s records" % last if have
            else "no registry has ever loaded here, so there are no records to show")


def refresh(config: Config) -> list[str]:
    """Bring the stored registry records up to the checkout's HEAD. Returns the problems (empty = the
    stored records are the checkout's current, validated content)."""
    path = (config.get("registry") or {}).get("path")
    if not path:
        return ["no registry checkout configured ([registry] path): %s" % _fallback(config, "stored")]
    with _lock:
        try:
            head = _head(path)
        except Exception as e:                                   # noqa: BLE001 - a banner, never a 500
            return ["%s: %s" % (e, _fallback(config, "stored"))]
        if stored_commit(config) == head:
            return []
        try:
            reg = load_registry(Path(path))
        except RegistryError as e:
            return (["registry problem: %s" % _problem_text(p) for p in (e.problems or [e])]
                    + ["the registry at %s was not loaded: %s" % (head[:12], _fallback(config))])
        except Exception as e:                                   # noqa: BLE001
            return ["reading the registry at %s failed: %s" % (head[:12], e)]
        if not reg.commit:
            return ["the registry checkout has changes that are not committed (or untracked files), so no "
                    "`git show <commit>:<file>` reproduces what was validated: not loaded, %s" % _fallback(config)]
        if stored_commit(config) == reg.commit:
            return []
        try:
            store_records(reg, config=config)
        except db.ObservationRefused as e:
            return ["registry records refused by the store: %s" % "; ".join(e.problems[:3])]
        except Exception as e:                                   # noqa: BLE001
            return ["reading the registry at %s failed: %s" % (reg.commit[:12], e)]
    return []


def current(config: Config, record_kind: str | None = None) -> list[dict]:
    """The stored registry_record observations (after `refresh`), optionally of one record kind."""
    docs = db.latest(KIND, config=config)
    return [d for d in docs if record_kind is None or d["fact"]["record_kind"] == record_kind]
