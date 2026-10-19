"""Repository, line and submodule pin: one vocabulary and one set of glyphs for every page.

Words, used the same way on Home, the configuration pages, the component workspace, the Matrix and search:
- **Repository**: one git repo (a catalogue component).
- **Line**: a branch or tag of one repository. A configuration is a registered line, so a configuration row
  is named as a line of its repo: "branch main @ 8e6d043", "tag v1.0.0 @ ab73e40", with the configuration id
  beside it.
- **Submodule pin**: a gitlink at path P inside a line's tree, fixed at commit S, written "P @ S". The
  component name is shown only when it differs from the path's last segment.
- **Pinned ×N**: one repository pinned at N different commits in one tree (it was "SPLIT"; "SPLIT" now means
  nothing, and a host copy that differs reads "hosts differ").
- A branch of a pinned repository is always written "<repo>:<branch>", never a bare "main".

    KINDS                                    repo, branch, tag, pin, again: the icon keys (`_ui.html` glyph();
                                             aria-hidden inline SVG, a word always prints beside each)
    LEGEND                                   [(kind, words, example)] for "How is this judged?"
    line(record, commit=None) -> dict        a configuration as a line of its repo
    pin(tree_path, parent_path, sha, component) -> dict      a submodule pin as "path @ sha"
    qualified(repo, branch) -> str           "tidelink:main"
    pinned_again(nodes) -> {component: N}    repositories pinned at N > 1 different commits in one tree
    again_text(n) -> "PINNED ×N"; describe(record, commit) -> one header line with repo, glyph and line
"""
from __future__ import annotations

from typing import Iterable

__all__ = ["KINDS", "LEGEND", "line", "pin", "qualified", "pinned_again", "again_text", "describe"]

# Icons, not font characters: a fork or box-drawing character renders as an empty box in common fonts.
KINDS = ("repo", "branch", "tag", "pin", "again")
LEGEND = (
    ("repo", "repository", "nanosoc-ethernet-chiplet"),
    ("branch", "line on a branch (follows the branch)", "branch main @ 8e6d043"),
    ("tag", "line on a tag (frozen)", "tag v1.0.0 @ ab73e40"),
    ("pin", "submodule pin: a path fixed at one commit", "deps/tidelink-phy @ 5c76e76"),
    ("again", "one repository pinned at more than one commit in a tree", "PINNED ×2"),
)


def _short(sha: str | None) -> str:
    return (sha or "")[:7]


def line(record: dict | None, commit: str | None = None) -> dict:
    """A configuration record ({id, component, ref: {branch|tag}, commit?}) as a line of its repository.
    `commit`: the scanned root commit when the record has none (a branch line). Returns {kind ("branch",
    "tag" or None), ref, sha, glyph, text ("branch main @ 8e6d043"; no " @ sha" when none is known), ref_text
    ("branch main"), repo, cid}."""
    rec = record or {}
    ref = rec.get("ref") if isinstance(rec.get("ref"), dict) else {}
    kind = "tag" if ref.get("tag") else ("branch" if ref.get("branch") else None)
    name = ref.get(kind) if kind else None
    sha = _short(rec.get("commit") or commit)
    ref_text = "%s %s" % (kind, name) if kind else "ref unknown"
    text = ref_text + (" @ %s" % sha if sha else "")
    return {"kind": kind, "ref": name, "sha": sha, "glyph": kind or "branch", "text": text,
            "ref_text": ref_text, "repo": rec.get("component"), "cid": rec.get("id")}


def pin(tree_path: str, parent_path: str | None, sha: str | None, component: str | None) -> dict:
    """A submodule pin: {path (relative to the gitlink holding it), sha, text "path @ sha", alias (the
    component, only when it differs from the path's last segment), glyph}."""
    rel = tree_path
    if parent_path and tree_path.startswith(parent_path + "/"):
        rel = tree_path[len(parent_path) + 1:]
    s = _short(sha)
    alias = component if component and component != tree_path.rsplit("/", 1)[-1] else ""
    return {"path": rel, "sha": s, "text": rel + (" @ %s" % s if s else ""), "alias": alias, "glyph": "pin"}


def qualified(repo: str | None, branch: str | None) -> str:
    """"tidelink:main": a branch of a pinned repository is never written bare."""
    if not branch:
        return "[branch]"
    return "%s:%s" % (repo, branch) if repo else branch


def pinned_again(nodes: Iterable[dict], component_key: str = "component", commit_key: str = "pinned") -> dict:
    """{component: N} for each repository pinned at N > 1 different commits among `nodes`."""
    seen: dict[str, set] = {}
    for n in nodes:
        c = n.get(component_key)
        if c:
            seen.setdefault(c, set()).add(n.get(commit_key))
    return {c: len(s) for c, s in seen.items() if len(s) > 1}


def again_text(n: int) -> str:
    return "PINNED ×%d" % n


def describe(record: dict | None, commit: str | None = None) -> str:
    """One line for a header: "nanosoc-ethernet-chiplet · branch main @ 8e6d043 · follows the branch"."""
    ln = line(record, commit)
    tail = {"branch": "follows the branch", "tag": "frozen at the tag"}.get(ln["kind"] or "", "")
    return "%s · %s%s" % (ln["repo"] or "?", ln["text"], (" · " + tail) if tail else "")
