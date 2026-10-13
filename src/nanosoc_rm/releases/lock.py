"""The release lock (G3; release-lock.schema.json): every nested pin with its host, state and protection.

    lock_pins(walk_docs) -> (pins, unjudged)          one lock pin per walked pin; unjudged = why a pin has no row
    protection(fact) -> {by, tags, hosts}              how the pin's commit stays reachable (B5, G2)
    repo_tags(config, component, repo) -> {tag: commit}  a repo's tags in its component mirror, peeled
    public_command(cmd, config) -> str                 a source command with the service's mirror path hidden

Protection is judged on the pinned host (what a recursive clone of the release uses) and on the component's
canonical host (where C10's keep tags belong; GATE2 Q9: both hosts), from the walker's per-host states:
release-tag (a tag other than keep/* contains the commit), keep-tag (a keep/* tag contains it), default-branch
(the default branch reaches it, no tag does), none. A host that does not hold the commit is `not-held`; one
that refused the read (UNREADABLE, e.g. a private repo read anonymously) is `unreadable`: not judged. The pin's
`by` is the pinned host's answer; the canonical host's gaps are reported separately (draft check
`lock.pins-canonical`), as B5's decision-R1 rows are.
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm.analysis.protect import reached_by_default
from nanosoc_rm.config import Config
from nanosoc_rm.mirror.layout import mirror_path, ref_prefix, run_in

__all__ = ["lock_pins", "protection", "host_protection", "repo_tags", "public_command", "PROTECTED_BY"]

PROTECTED_BY = ("release-tag", "keep-tag", "default-branch")
_NOT_HELD = ("MISSING",)


def host_protection(st: dict) -> str:
    """One host's answer for one pinned commit (a pin-state host state)."""
    if st["state"] in _NOT_HELD:
        return "not-held"
    if st["state"] == "UNREADABLE":
        return "unreadable"                      # the host refused the read: not judged, never "none"
    tags = st.get("tags") or []
    if any(not t.startswith("keep/") for t in tags):
        return "release-tag"
    if tags:
        return "keep-tag"
    if reached_by_default(st):
        return "default-branch"
    return "none"


def protection(fact: dict) -> dict | None:
    """{by, tags, hosts} for one pin_state fact, or None when its pinned host has no state."""
    hosts = {}
    pinned = None
    for name, st in sorted((fact.get("hosts") or {}).items()):
        if st.get("pinned") or st.get("role") == "canonical":
            hosts[name] = host_protection(st)
        if st.get("pinned"):
            pinned = st
    if pinned is None:
        return None
    by = hosts[pinned["repo"]["host"]]
    return {"by": "none" if by == "not-held" else by, "tags": sorted(pinned.get("tags") or []), "hosts": hosts}


def lock_pins(walk_docs: list[dict]) -> tuple[list[dict], list[str]]:
    """Lock pins (release-lock $defs/pin) from a walk, ordered by tree path, and the pins that cannot be one (no
    state, no catalogued component, no commit), each with its reason. The writer never emits a pin without a
    state (G3)."""
    pins, unjudged = [], []
    for d in sorted(walk_docs, key=lambda d: d["fact"]["tree_paths"][0]):
        f = d["fact"]
        child, where = f["child"], f["tree_paths"][0]
        pinned = next((st for st in (f.get("hosts") or {}).values() if st.get("pinned")), None)
        state = f["state"] or (pinned["state"] if pinned else None)
        if state is None:
            unjudged.append("%s: no state on the host its URL names" % where)
            continue
        if not child.get("component") or not child.get("repo") or not child.get("commit"):
            unjudged.append("%s: %s" % (where, "its URL %s names no catalogued component" % f["url"]
                                        if not child.get("component") else "no child commit"))
            continue
        pin = {"parent": {"repo": dict(f["parent"]["repo"]), "commit": f["parent"]["commit"]}, "path": f["path"],
               "tree_paths": list(f["tree_paths"]), "depth": f["depth"], "component": child["component"],
               "repo": dict(child["repo"]), "commit": child["commit"], "state": state}
        if pinned is not None and pinned.get("lag") is not None:
            pin["lag"] = pinned["lag"]
        pin.update(url_kind=f["url_kind"], tags=sorted((pinned or {}).get("tags") or []), effective=f["effective"])
        prot = protection(f)
        if prot is not None:
            pin["protection"] = prot
        pins.append(pin)
    return pins, unjudged


def repo_tags(config: Config, component: str, repo: dict) -> dict[str, str]:
    """{tag name: commit it peels to} for one repo, from its component mirror (empty without a mirror)."""
    mirror = mirror_path(config, component)
    if not (Path(mirror) / "HEAD").is_file():
        return {}
    prefix = ref_prefix(repo["host"], repo["path"]) + "/tags/"
    r = run_in(mirror, "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)", prefix.rstrip("/"))
    out = {}
    for line in r.stdout.splitlines() if r.ok else []:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(prefix):
            out[parts[0][len(prefix):]] = parts[2] if len(parts) > 2 and parts[2] else parts[1]
    return out


def public_command(cmd: str, config: Config) -> str:
    """A source command as a release file may carry it: the service's own mirror directory becomes `<mirrors>`
    (a lock is meant to be shared; the host's filesystem layout is not)."""
    return cmd.replace(str(config.mirrors_dir), "<mirrors>")
