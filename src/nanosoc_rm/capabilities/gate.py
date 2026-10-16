"""The bump gate (David 2026-10-16, decision 3): no bump pull request is proposed without a passing test-bump.

    evidence(config, configuration, tree_paths, to, root_commit) -> dict     the newest test-bump of exactly this move
    blocker(ev, configuration, path, to) -> str | None                        the sentence a proposal carries, or None

A test-bump counts only when it tested EXACTLY the move: the same configuration, the same root commit as the walk
the proposal comes from, the same moved commit, and every tree path the proposal moves. Its functional verdict must
be PASS: FAIL (a gate went green to red), WARN (a gate that was already red), UNVERIFIED (nothing covered the
move, or something did not run) all hold the proposal. Hooks only warn; this gate, the CI bump-check template and
the bump engine are where a move is blocked. The gate reads job records only: it never runs anything.
"""
from __future__ import annotations

__all__ = ["evidence", "blocker", "KIND"]

KIND = "test-bump"


def evidence(config, configuration: str, tree_paths: list[str], to: str, root_commit: str | None) -> dict:
    """{verdict, job, href, finished_at, note}: the newest finished test-bump job of exactly this move, or
    verdict None with a note saying why none counts."""
    from nanosoc_rm import jobs
    out = {"verdict": None, "job": None, "href": None, "finished_at": None, "note": None}
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
        at = j.get("finished_at")
        return dict(out, verdict=f.get("verdict") or j.get("verdict"), job=j["id"],
                    href="/capabilities/%s/bump?job=%d" % (configuration, j["id"]),
                    finished_at=at if at and at.endswith("Z") else None, note=f.get("reason"))
    if stale:
        out["note"] = ("%d test-bump%s of this move ran on another root commit than the walked %s: not used"
                       % (stale, "" if stale == 1 else "s", (root_commit or "?")[:12]))
    return out


def blocker(ev: dict, configuration: str, path: str, to: str) -> str | None:
    """The proposal's blocker sentence, or None when a passing test-bump of exactly this move exists."""
    if ev.get("verdict") == "PASS":
        return None
    cmd = "nanosoc-rm test-bump %s %s %s" % (configuration, path, to[:12])
    if ev.get("verdict") is None:
        return ("bump gate: no test-bump of exactly this move has finished%s; run `%s` (or Test this bump on the "
                "pin row) and it must PASS" % ("; " + ev["note"] if ev.get("note") else "", cmd))
    return ("bump gate: the newest test-bump of this move (job %s) is %s%s; a bump is proposed only after one "
            "PASSes (`%s`)" % (ev.get("job"), ev["verdict"], ": " + ev["note"] if ev.get("note") else "", cmd))
