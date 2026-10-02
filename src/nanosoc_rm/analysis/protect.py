"""B5: the silicon-protection list (interfaces.md section 7; GATE2 Q9: both hosts).

    silicon_protection(configuration=None) -> [silicon_protection observation]; one snapshot per configuration

For every pin of a `frozen` or `silicon` configuration, read from its latest stored walk (never re-walked),
each pin is judged on two hosts:

- the host the pinned URL names (`pinned: true`): what a recursive clone uses;
- the component's canonical host (`canonical: true`): where C10's protective tags belong.

One row when they are the same host. A row exists when that host's default branch cannot reach the commit:
the host state is not ON_DEFAULT, the default branch is not among the branches containing it, and it is not
TAGGED on the default branch (a lag). `covering_tags` are the tags ON THAT HOST that contain the commit (the
walker's per-host `tags`, a `--contains` census), and `covered` = at least one: coverage is judged by
containment, never by tag name, so `keep/eth-rc4/tidelink-phy` covers component tidelink-gpio-phy.

The same (component, commit, host) reached through several pins is one row with every tree path.

A pin whose protection cannot be judged gives no row and is named in the result's `unanswered`: no host state
(LOCAL_URL, NO_GITLINK, an uncatalogued URL), no child commit, or no state for the host its URL names. The CLI
then exits 2 when no row is uncovered (could not answer), never 0: a silicon pin nobody judged is not "covered".
A frozen configuration never walked is named there too when every configuration is asked; CannotAnswer when it
is the only one asked, or when no frozen configuration has a stored tree.

A configuration that is neither frozen nor silicon gives `[]`, not an error, and stores nothing (its scope can
never hold a row).
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.analysis._inputs import Answer, CannotAnswer
from nanosoc_rm.config import Config

__all__ = ["silicon_protection", "is_protected_configuration", "reached_by_default"]


def is_protected_configuration(configuration: dict) -> bool:
    """B5 judges frozen and silicon configurations (a silicon one is always frozen, eligibility schema)."""
    return bool(configuration.get("frozen")) or "silicon" in configuration


def reached_by_default(state: dict) -> bool:
    """Does this host's default branch reach the pinned commit? (one pin-state host state)"""
    if state["state"] == "ON_DEFAULT":
        return True
    if state["default_branch"] and state["default_branch"] in state["branches"]:
        return True
    return state["state"] == "TAGGED" and state["lag"] is not None


def _unjudged(f: dict) -> str | None:
    """Why this pin's protection cannot be judged from its stored host states, or None when it can."""
    if f["state"] is not None:
        return "its pin state is %s (no host state)" % f["state"]
    if not f["child"]["commit"]:
        return "it has no child commit"
    if not f["child"]["component"]:
        return "its URL %s names no catalogued component" % f["url"]
    if not any(st["pinned"] for st in f["hosts"].values()):
        return "the walk recorded no state for the host its URL names"
    return None


def _rows(configuration: str, tree: list[dict], unanswered: list[str]) -> list[dict]:
    acc: dict[tuple[str, str, str], dict] = {}
    for doc in tree:
        f = doc["fact"]
        component, commit = f["child"]["component"], f["child"]["commit"]
        why = _unjudged(f)
        if why:
            unanswered.append("%s pin %s (%s): protection not judged, %s" % (
                configuration, " ".join(f["tree_paths"]), (commit or "no commit")[:12], why))
            continue
        for host, st in sorted(f["hosts"].items()):
            if not (st["pinned"] or st["role"] == "canonical") or reached_by_default(st):
                continue
            row = acc.get((component, commit, host))
            if row is None:
                covered = bool(st["tags"]) and st["state"] in ("TAGGED", "PROTECTED_BY_TAG")
                row = acc[(component, commit, host)] = {
                    "fact": {"configuration": configuration, "component": component, "repo": st["repo"],
                             "commit": commit, "pinned": False, "canonical": st["role"] == "canonical",
                             "state": st["state"], "tree_paths": [], "default_branch": st["default_branch"],
                             "branches": list(st["branches"]), "covering_tags": list(st["tags"]) if covered else [],
                             "covered": covered},
                    "times": [], "sources": []}
            row["fact"]["pinned"] = row["fact"]["pinned"] or st["pinned"]
            row["fact"]["tree_paths"] += [p for p in f["tree_paths"] if p not in row["fact"]["tree_paths"]]
            row["times"] += [st["observed_at"], doc["observed_at"]]
            for s in st["sources"] + [{"kind": "derived", "command": "host state %s of stored %s (snapshot %s)"
                                       % (host, doc["subject"], doc["snapshot"]["id"])}]:
                if s not in row["sources"]:
                    row["sources"].append(s)
    out = []
    for (component, commit, host), row in acc.items():
        out.append({"schema": 1, "kind": "silicon_protection",
                    "subject": "silicon_protection:%s:%s:%s:%s" % (configuration, component, commit, host),
                    "observed_at": inp.oldest(*row["times"]), "sources": row["sources"], "fact": row["fact"]})
    return out


def silicon_protection(configuration: str | None = None, *, config: Config | str | Path | None = None,
                       store: bool = True) -> Answer:
    """silicon_protection observations of one configuration, or of every registered one (None). Each
    configuration's rows are stored as one snapshot of scope `silicon_protection:<configuration>`.
    `.unanswered` names the pins (and, for None, the frozen configurations never walked) that were not judged.
    UsageProblem for an unknown configuration; CannotAnswer when no frozen configuration asked was walked."""
    cfg = inp.config_of(config)
    reg = inp.registry(cfg)
    targets = [reg.configuration(configuration)] if configuration is not None else reg.configurations()
    protected = [c for c in targets if is_protected_configuration(c)]
    trees, unanswered = {}, []
    for c in protected:
        try:
            trees[c["id"]] = inp.stored_tree(c["id"], cfg)
        except CannotAnswer as e:
            unanswered.append(str(e))
    if unanswered and not trees:
        raise CannotAnswer("; ".join(unanswered))
    out = Answer([], unanswered)
    for c in targets:
        if c["id"] not in trees:
            # Never walked (named in unanswered), or neither frozen nor silicon: [] and nothing stored. Such a
            # scope never holds a fact, and an empty snapshot's id is `<scope>@<second>` (db.put_snapshot), so
            # two runs in one second would collide over nothing.
            continue
        docs = _rows(c["id"], trees[c["id"]], out.unanswered)
        scope = "silicon_protection:%s" % c["id"]
        snap = inp.new_snapshot(scope)
        for d in docs:
            d["snapshot"] = dict(snap)
        if store:
            inp.store_snapshot(scope, docs, cfg)
        out += docs
    return out
