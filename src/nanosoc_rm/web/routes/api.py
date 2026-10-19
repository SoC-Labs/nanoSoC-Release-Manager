"""The JSON API (section 13): GET only, NDJSON, every line an observation. The two Run POSTs are in actions.py."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nanosoc_rm import db
from nanosoc_rm.capabilities import bumpcheck
from nanosoc_rm.web import views
from nanosoc_rm.web.routers import Route
from nanosoc_rm.web.routes.common import ctx, ndjson, redacted_alert, registry_problems

__all__ = ["ROUTES"]


def api_configurations(request: Request) -> Response:
    config, _ = ctx(request)
    registry_problems(config)
    return ndjson(views.configurations(config))


def api_pins(request: Request, id: str) -> Response:
    config, _ = ctx(request)
    return ndjson(views.pins(config, id))


def api_hosts(request: Request, id: str) -> Response:
    config, _ = ctx(request)
    return ndjson(views.host_compares(config, id))


def api_contains(request: Request, sha: str) -> Response:
    config, _ = ctx(request)
    rows, problems, _ = views.containment(config, sha, request.query_params.get("component") or None)
    if problems and not rows:
        return JSONResponse({"error": problems[0]}, 422 if "not a" in problems[0] else 503)
    return ndjson(rows)


def api_silicon(request: Request) -> Response:
    config, _ = ctx(request)
    return ndjson(views.silicon_protection(config, request.query_params.get("configuration") or None))


def api_runs(request: Request) -> Response:
    config, _ = ctx(request)
    return ndjson(views.runs(config, request.query_params.get("configuration") or None))


def api_errata(request: Request, configuration: str) -> Response:
    config, _ = ctx(request)
    registry_problems(config)
    return ndjson(views.errata(config, configuration))


def api_mirrors(request: Request) -> Response:
    config, _ = ctx(request)
    return ndjson(views.mirrors(config))


def api_inventory(request: Request) -> Response:
    config, _ = ctx(request)
    flag = (request.query_params.get("unclassified") or "").lower() in ("1", "true", "yes")
    return ndjson(views.inventory(config, unclassified=flag))


def api_runner(request: Request) -> Response:
    config, _ = ctx(request)
    return ndjson(views.runner_settings(config))


def api_alerts(request: Request) -> Response:
    config, _ = ctx(request)
    return ndjson([redacted_alert(a, config) for a in db.alerts(open_only=True, config=config)])


def api_bump_check(request: Request) -> Response:
    """Section 26.7, for a signed-in member: the same answer a CI job's reader token gets (web/app.py routes the
    token request before the session check)."""
    config, _ = ctx(request)
    try:
        return JSONResponse(bumpcheck.answer(config, dict(request.query_params)), headers={"cache-control": "no-store"})
    except bumpcheck.BadRequest as e:
        return JSONResponse({"error": str(e)}, 400)


ROUTES = tuple(Route("GET", path, fn) for path, fn in (
    ("/api/v1/configurations", api_configurations), ("/api/v1/configurations/{id}/pins", api_pins),
    ("/api/v1/components/{id}/hosts", api_hosts), ("/api/v1/contains/{sha}", api_contains),
    ("/api/v1/silicon-protection", api_silicon), ("/api/v1/runs", api_runs),
    ("/api/v1/errata/{configuration}", api_errata), ("/api/v1/mirrors", api_mirrors),
    ("/api/v1/inventory", api_inventory), ("/api/v1/runner", api_runner), ("/api/v1/alerts", api_alerts),
    (bumpcheck.PATH, api_bump_check)))
