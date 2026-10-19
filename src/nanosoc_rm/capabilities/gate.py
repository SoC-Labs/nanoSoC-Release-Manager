"""The bump gate: no bump pull request is proposed without a test-bump of exactly the move that lets it land.

    evidence(config, configuration, tree_paths, to, root_commit) -> dict     the newest test-bump of exactly this move
    lands(ev) -> bool                                                         does that evidence let the move land?
    blocker(ev, configuration, path, to) -> str | None                        the sentence a proposal carries, or None
    already_red(ev) -> str | None                                             the note a landing WARN carries

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

Hooks only warn; this gate, the CI bump-check template and the bump engine are where a move is blocked. The gate
reads job records only: it never runs anything.
"""
from __future__ import annotations

__all__ = ["evidence", "lands", "blocker", "already_red", "KIND", "LANDS"]

KIND = "test-bump"
LANDS = ("PASS", "WARN")
ALREADY_RED = "already red before the bump"


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
    cmd = "nanosoc-rm test-bump %s %s %s" % (configuration, path, to[:12])
    if ev.get("verdict") is None:
        return ("bump gate: no test-bump of exactly this move has finished%s; run `%s` (or Test this bump on the "
                "pin row) and it must PASS" % ("; " + ev["note"] if ev.get("note") else "", cmd))
    return ("bump gate: the newest test-bump of this move (job %s) is %s%s; a bump is proposed only after one "
            "PASSes, or is WARN because its reds were already red before the bump (`%s`)"
            % (ev.get("job"), ev["verdict"], ": " + ev["note"] if ev.get("note") else "", cmd))
