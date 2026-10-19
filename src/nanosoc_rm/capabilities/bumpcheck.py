"""`GET /api/v1/bump-check`: may this submodule-pointer move land? (interfaces.md section 26.7)

The question a repository's CI and its pre-push hook ask (decision 3: hooks warn, CI blocks, the bump gate is the
authority). The answer is the bump gate's own evidence (`gate.evidence`): the newest finished test-bump of EXACTLY
this move, in a registered configuration of the asking repository, from exactly the asking commit.

    GET /api/v1/bump-check?component=<id>&path=<tree path>&to=<40 hex>&root=<40 hex>[&from=<40 hex>]
    200 {"verdict": "PASS"|"FAIL"|"WARN"|"UNVERIFIED"|null, "url": <job page>|null, "command": <the test-bump to
         run>|null, "configurations": [<ids>], "configuration": <the one whose evidence answers>|null,
         "note": <why no evidence counts>|null}
    400 a malformed parameter; 401 no session and no reader token; 404 nothing under [api] (bearer only)

`root` is the asking repository's commit BEFORE the move (a CI range's base). A test-bump counts only when its
`root_commit` is that commit, its `to` is `to` and its tree paths include `path`, so a PASS for another base, or
for a neighbouring move, never turns a CI run green. Every configuration whose `component` is `component` is
tried. A new red (FAIL) in ANY one answers FAIL, naming that configuration, even when another passes (D14,
David 2026-10-19). Otherwise a PASS in any one answers PASS. Otherwise a WARN (every red gate was already red before the bump) answers
WARN with that note, and WARN lets the move land as PASS does (`gate.lands`). Read-only: it reads the job records and the registry, never runs or queues
anything, never fetches.

Who may ask: a signed-in member (the session route), or a holder of a READER token: `[[api.readers]]` entries
(`name`, `token_sha256`, optional `revoked`), the same `rmx_` token format as the executor's
(`nanosoc-rm executor token new NAME --reader`). A reader token opens this one route and nothing else; an
executor's token does not open it.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Callable

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from nanosoc_rm.config import Config, ConfigError

__all__ = ["PATH", "answer", "serve", "readers"]

PATH = "/api/v1/bump-check"
_SHA = re.compile(r"^[0-9a-f]{40}\Z")
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")
_TREE = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_.@+/-]{1,512}\Z")
_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


class BadRequest(ValueError):
    pass


def readers(config: Config) -> list[dict]:
    return list(((config.get("api") or {}).get("readers")) or [])


def _params(q: dict) -> dict:
    out = {}
    for key, rx, need in (("component", _ID, True), ("path", _TREE, True), ("to", _SHA, True),
                          ("root", _SHA, True), ("from", _SHA, False)):
        v = q.get(key)
        if v is None or v == "":
            if need:
                raise BadRequest("%s is required" % key)
            continue
        v = str(v).strip().strip("/") if key == "path" else str(v).strip().lower() if key != "component" else str(v)
        if not rx.match(v):
            raise BadRequest("%s=%r is not valid" % (key, v[:80]))
        out[key] = v
    return out


def answer(config: Config, query: dict) -> dict:
    """The bump-check answer (module docstring) for one query. BadRequest on a malformed parameter."""
    from nanosoc_rm.capabilities import gate
    from nanosoc_rm.ci import configs
    p = _params(query)
    confs = [c["id"] for c in configs.configurations(config) if c.get("component") == p["component"]]
    out = {"verdict": None, "url": None, "command": None, "configurations": confs, "configuration": None,
           "note": None}
    if not confs:
        out["note"] = ("no registered configuration of %s: register one (or test the move in the configuration "
                       "that pins this repository)" % p["component"])
        return out
    out["command"] = "nanosoc-rm test-bump %s %s %s" % (confs[0], p["path"], p["to"])
    evs = [(cid, gate.evidence(config, cid, [p["path"]], p["to"], p["root"])) for cid in confs]
    broke = gate.new_red(config, confs, [p["path"]], p["to"], p["root"], known=dict(evs))
    if broke is not None:
        # D14: a new red in ANY configuration holds the move, even when another configuration passes
        cid, ev = broke
        others = ["%s %s" % (c, e.get("verdict") or "untested") for c, e in evs if c != cid]
        return dict(out, verdict=ev["verdict"], url=ev.get("href"), configuration=cid,
                    command="nanosoc-rm test-bump %s %s %s" % (cid, p["path"], p["to"]),
                    note="%s: job %s is %s (a new red holds the move in any configuration, D14%s)%s"
                    % (cid, ev.get("job"), ev["verdict"], "; " + ", ".join(others) if others else "",
                       ": " + ev["note"] if ev.get("note") else ""))
    notes, best, landing = [], None, None
    for cid, ev in evs:
        if ev.get("verdict") == "PASS":
            return dict(out, verdict="PASS", url=ev.get("href"), command=None, note=None, configuration=cid)
        if gate.lands(ev) and landing is None:
            landing = (cid, ev)
        if ev.get("verdict") is not None and best is None:
            best = (cid, ev)
        if ev.get("note"):
            notes.append("%s: %s" % (cid, ev["note"]))
    if landing is not None:
        cid, ev = landing
        return dict(out, verdict=ev["verdict"], url=ev.get("href"), command=None, configuration=cid,
                    note="%s: job %s: %s" % (cid, ev.get("job"), ev.get("note") or gate.ALREADY_RED))
    if best is not None:
        cid, ev = best
        out.update(verdict=ev["verdict"], url=ev.get("href"), configuration=cid,
                   command="nanosoc-rm test-bump %s %s %s" % (cid, p["path"], p["to"]))
        notes.insert(0, "%s: job %s is %s%s" % (cid, ev.get("job"), ev["verdict"],
                                               ": " + ev["note"] if ev.get("note") else ""))
    out["note"] = "; ".join(notes) or ("no test-bump of %s to %s from %s has finished"
                                       % (p["path"], p["to"][:12], p["root"][:12]))
    return out


def _json(payload: dict, status: int = 200) -> Response:
    return JSONResponse(payload, status, headers=_HEADERS)


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def bearer(scope: dict) -> bool:
    """Does the request carry a bearer token (then it is answered here, never through a session)?"""
    return (_header(scope, b"authorization") or "").lower().startswith("bearer ")


async def serve(scope: dict, receive: Callable, send: Callable, load: Callable[[], Config]) -> None:
    """The bearer-token door (the Guard hands a request for PATH with `Authorization: Bearer` here, before its
    session check): a configured, unrevoked reader token, GET only, this one path."""
    resp = await _answer(scope, load)
    await resp(scope, receive, send)


async def _answer(scope: dict, load: Callable[[], Config]) -> Response:
    from nanosoc_rm.executor import tokens
    try:
        config = load()
    except ConfigError:
        return _json({"error": "the service config cannot be read"}, 503)
    if scope.get("path") != PATH:
        return _json({"error": "not found"}, 404)
    if scope.get("method") not in ("GET", "HEAD"):
        return Response(status_code=405, headers={**_HEADERS, "allow": "GET"})
    who = tokens.identify_in(readers(config), _header(scope, b"authorization"))
    if who is None:
        return _json({"error": "reader token missing, unknown or revoked"}, 401)
    query = dict(urllib.parse.parse_qsl((scope.get("query_string") or b"").decode("latin-1")))
    try:
        return _json(await run_in_threadpool(answer, config, query))
    except BadRequest as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:                                   # noqa: BLE001 - never a bare 500 to a CI job
        return _json({"error": "cannot answer: %s" % type(e).__name__}, 503)
