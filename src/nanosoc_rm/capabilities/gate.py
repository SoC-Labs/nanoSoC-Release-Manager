"""The bump gate: no bump pull request is proposed without a test-bump of exactly the move that lets it land.

    evidence(config, configuration, tree_paths, to, root_commit) -> dict     the newest test-bump of exactly this move
    lands(ev) -> bool                                                         does that evidence let the move land?
    blocker(ev, configuration, path, to) -> str | None                        the sentence a proposal carries, or None
    already_red(ev) -> str | None                                             the note a landing WARN carries
    siblings(config, configuration) -> [ids]                                  every configuration of its component
    new_red(config, configurations, tree_paths, to, root_commit) -> (id, ev) | None   the first FAIL among them

A test-bump counts only when it tested EXACTLY the move: the same configuration, the same root commit as the walk
the proposal comes from, the same moved commit, and every tree path the proposal moves. Its functional verdict
decides:

- PASS lands.
- WARN lands with the note "already red before the bump": every red block gate was red before the move and is
  still red after it, so the move makes nothing worse. Holding it would block every bump of a tree with a known
  red until that red is fixed, which is not the bump's job.
- FAIL (a gate went green to red) holds.
- UNVERIFIED holds: nothing covered the move, something did not run, or a red After has no green or red Now to
  compare with (an unknown baseline is never "already red").
- A PASS or WARN document whose job exited non-zero holds as UNVERIFIED: never greener than the exit code.

D14 (David 2026-10-19): a NEW red for the same move in ANY registered configuration of the component holds the
bump, even when another configuration passes. A new red is FAIL (a block gate went green before to red after).
`new_red` finds it; the bump engine and `GET /api/v1/bump-check` hold on it and name the configuration that broke.
WARN, UNVERIFIED and no evidence in another configuration are not new reds: they do not change the answer.

Hooks only warn; this gate, the CI bump-check template and the bump engine are where a move is blocked. The gate
reads job records only: it never runs anything.
"""
from __future__ import annotations

__all__ = ["evidence", "lands", "blocker", "already_red", "siblings", "new_red", "broke_in", "KIND", "LANDS",
           "NEW_RED"]

KIND = "test-bump"
LANDS = ("PASS", "WARN")
ALREADY_RED = "already red before the bump"
NEW_RED = "FAIL"                                         # D14: green before, red after


def evidence(config, configuration: str, tree_paths: list[str], to: str, root_commit: str | None) -> dict:
    """{verdict, job, href, finished_at, note, configuration}: the newest finished test-bump job of exactly this
    move, or verdict None with a note saying why none counts."""
    from nanosoc_rm import jobs
    out = {"verdict": None, "job": None, "href": None, "finished_at": None, "note": None,
           "configuration": configuration}
    try:
        records = jobs.jobs(config, limit=500, kind=KIND, configuration=configuration, states=("done",))
    except Exception:                                    # noqa: BLE001 - no job store yet: no evidence
        records = []
    stale = 0
    want = set(tree_paths)
    for j in records:                                    # newest first
        f = ((j.get("result") or {}).get("fact")) or {}
        if not f or (f.get("to") or {}).get("commit") != to or f.get("plan_only"):
            continue
        if not want <= set(f.get("tree_paths") or []):
            continue
        if root_commit is None or f.get("root_commit") != root_commit:
            stale += 1
            continue
        return dict(out, job=j["id"], href="/capabilities/%s/bump?job=%d" % (configuration, j["id"]),
                    finished_at=_utc(j.get("finished_at")), **_judged(f, j))
    if stale:
        out["note"] = ("%d test-bump%s of this move ran on another root commit than the walked %s: not used"
                       % (stale, "" if stale == 1 else "s", (root_commit or "?")[:12]))
    return out


def _utc(at: str | None) -> str | None:
    return at if at and at.endswith("Z") else None


def _judged(fact: dict, job: dict) -> dict:
    """{verdict, note} of one matching job: its document's verdict, unless the exit code says otherwise."""
    verdict, note = fact.get("verdict") or job.get("verdict"), fact.get("reason")
    if verdict in LANDS and job.get("exit_code") != 0:
        # A stale or stray verdict line, or a command that failed after printing it.
        return {"verdict": "UNVERIFIED",
                "note": "its document says %s but the command exited %s" % (verdict, job.get("exit_code"))}
    if verdict == "WARN":
        note = "%s: %s still red after it, so it does not hold the bump" % (
            ALREADY_RED, _red_gates(fact) or "a block gate is")
    return {"verdict": verdict, "note": note}


def _red_gates(fact: dict) -> str:
    keys = [r["key"] for r in fact.get("rows") or []
            if r.get("key") and r.get("gate") == "block" and r.get("change") == "same-red"]
    if not keys:
        return ""
    return "%s %s" % (", ".join(keys[:8]) + (" and %d more" % (len(keys) - 8) if len(keys) > 8 else ""),
                      "is" if len(keys) == 1 else "are")


def siblings(config, configuration: str) -> list[str]:
    """`configuration` first, then every other registered configuration of the same component (D14)."""
    from nanosoc_rm.ci import configs
    try:
        allc = configs.configurations(config)
    except Exception:                                    # noqa: BLE001 - no registry: only the one asked about
        return [configuration]
    comp = next((c.get("component") for c in allc if c.get("id") == configuration), None)
    return [configuration] + [c["id"] for c in allc
                              if comp is not None and c.get("component") == comp and c.get("id") != configuration]


def new_red(config, configurations: list[str], tree_paths: list[str], to: str, root_commit: str | None,
            known: dict | None = None) -> tuple[str, dict] | None:
    """(configuration, evidence) of the first of `configurations` whose newest test-bump of exactly this move is a
    new red (FAIL), else None (D14). `known` maps a configuration to evidence already read."""
    known = known or {}
    for cid in configurations:
        ev = known[cid] if cid in known else evidence(config, cid, tree_paths, to, root_commit)
        if ev.get("verdict") == NEW_RED:
            return cid, dict(ev, configuration=cid)
    return None


def broke_in(ev: dict, configuration: str, decided_by: str, own: dict) -> dict:
    """The evidence a proposal of `decided_by` records when `configuration` (another configuration of the same
    component) has a new red for the move although `own` (decided_by's evidence) would let it land (D14)."""
    return dict(ev, configuration=configuration,
                note="a new red in configuration %s holds the bump (D14) although %s is %s%s" % (
                    configuration, decided_by, own.get("verdict") or "untested",
                    ": " + ev["note"] if ev.get("note") else ""))


def lands(ev: dict) -> bool:
    return ev.get("verdict") in LANDS


def already_red(ev: dict) -> str | None:
    """The note a proposal shows when its evidence lands only because its reds were already red, else None."""
    if ev.get("verdict") == "WARN":
        return "bump gate: %s (job %s): %s" % (ev["verdict"], ev.get("job"), ev.get("note") or ALREADY_RED)
    return None


def blocker(ev: dict, configuration: str, path: str, to: str) -> str | None:
    """The proposal's blocker sentence, or None when a test-bump of exactly this move lets it land."""
    if lands(ev):
        return None
    configuration = ev.get("configuration") or configuration
    cmd = "nanosoc-rm test-bump %s %s %s" % (configuration, path, to[:12])
    if ev.get("verdict") is None:
        return ("bump gate: no test-bump of exactly this move has finished%s; run `%s` (or Test this bump on the "
                "pin row) and it must PASS" % ("; " + ev["note"] if ev.get("note") else "", cmd))
    return ("bump gate: the newest test-bump of this move in %s (job %s) is %s%s; a bump is proposed only after one "
            "PASSes, or is WARN because its reds were already red before the bump (`%s`)"
            % (configuration, ev.get("job"), ev["verdict"], ": " + ev["note"] if ev.get("note") else "", cmd))
