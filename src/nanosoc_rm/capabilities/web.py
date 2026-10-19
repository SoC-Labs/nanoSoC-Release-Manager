"""The Capabilities pages and "Test this bump", mounted at /capabilities through the web plug-in seam.

    GET  /capabilities/                                   every configuration: how many trees declare capabilities
    GET  /capabilities/{configuration}                    per tree: its capabilities at the pinned commit, the hosts
                                                          that can run each, the last test-bump result
    GET  /capabilities/{configuration}/bump?path=[&to=]   the Test this bump form for one pin, its earlier tests
         ?job=N                                           one test-bump job: Now vs After per capability
    POST /capabilities/{configuration}/test-bump          queue a test-bump job (path, to); 303 to its page

Every GET reads stored facts, mirrors, the registry and job records only: it never walks, fetches, runs or queues.
The POST is CSRF-checked by the app's guard like every other POST, exists only in a local deployment
(`app.state.rm.local_routes`, section 13a) and only QUEUES: the job is an executor kind, so the service host never
runs it. No navigation entry (the rail is long enough): Home's detail panel, the configuration
pages and the submodule tree link here.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

__all__ = ["router", "PREFIX", "NAV_LABEL", "POSTS", "bump_href"]

PREFIX = "/capabilities"
NAV_LABEL = None
POSTS = frozenset({("POST", PREFIX + "/{configuration}/test-bump")})
HERE = Path(__file__).resolve().parent


def _env() -> Environment:
    env = Environment(loader=ChoiceLoader([FileSystemLoader(str(HERE / "templates"))]),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    return env


ENV = _env()


def bump_href(configuration: str, path: str, to: str | None = None) -> str:
    q = {"path": path}
    if to:
        q["to"] = to
    return "%s/%s/bump?%s" % (PREFIX, urllib.parse.quote(configuration, safe=""), urllib.parse.urlencode(q))


def _config(request: Request):
    cfg = getattr(request.state, "config", None)
    if cfg is None:
        from nanosoc_rm.config import load_config
        cfg = load_config()
    return cfg


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    from nanosoc_rm.web import plugins
    return plugins.render_page(request, template, title, status=status, environment=ENV, **ctx)


def _local(request: Request) -> bool:
    return bool(getattr(getattr(request.app.state, "rm", None), "local_routes", False))


def _message(request: Request, message: str, status: int) -> HTMLResponse:
    return _page(request, "caps_message.html", "Capabilities", status=status, message=message)


def router(app: Any = None) -> APIRouter:
    from nanosoc_rm.web.plugins import local_routes
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        from nanosoc_rm import jobs
        from nanosoc_rm.capabilities.view import tree_view
        from nanosoc_rm.ci import configs
        config = _config(request)
        try:
            confs = configs.configurations(config)
            hosts = sorted(configs._registry(config).sites())
        except Exception as e:                                   # noqa: BLE001 - shown, never a bare 500
            return _message(request, "the registry does not load: %s" % str(e)[:300], 503)
        rows = []
        for c in confs:
            try:
                rows.append({"id": c["id"], "view": tree_view(config, c["id"]), "note": None})
            except Exception as e:                               # noqa: BLE001 - one configuration never hides the rest
                rows.append({"id": c["id"], "view": None, "note": str(e)[:200]})
        try:
            waiting = len(jobs.executor_queue(config))
        except Exception:                                        # noqa: BLE001
            waiting = "?"
        return _page(request, "caps_index.html", "Capabilities", rows=rows, hosts=hosts, waiting=waiting)

    def tree(request: Request, configuration: str) -> HTMLResponse:
        from nanosoc_rm.analysis._inputs import CannotAnswer
        from nanosoc_rm.capabilities.view import tree_view
        from nanosoc_rm.ci import configs
        config = _config(request)
        try:
            configs.configuration(configuration, config)
        except configs.UnknownConfiguration as e:
            return _message(request, str(e), 404)
        except Exception as e:                                   # noqa: BLE001
            return _message(request, str(e)[:300], 503)
        try:
            v = tree_view(config, configuration)
        except CannotAnswer as e:
            return _message(request, str(e), 503)
        return _page(request, "caps_tree.html", "Capabilities of %s" % configuration, v=v)

    def bump(request: Request, configuration: str) -> HTMLResponse:
        from nanosoc_rm import jobs
        from nanosoc_rm.ci import configs
        config = _config(request)
        q = request.query_params
        try:
            configs.configuration(configuration, config)
        except configs.UnknownConfiguration as e:
            return _message(request, str(e), 404)
        except Exception as e:                                   # noqa: BLE001
            return _message(request, str(e)[:300], 503)
        job = None
        jid = jobs.parse_id(q.get("job"))
        if q.get("job") is not None:
            job = jobs.get(config, jid) if jid is not None else None
            if job is None or job["kind"] != "test-bump" or job["params"].get("configuration") != configuration:
                return _message(request, "no test-bump job %s for %s" % (q.get("job")[:20], configuration), 404)
        path = (q.get("path") or (job["params"]["path"] if job else "") or "").strip("/")
        to = q.get("to") or (job["params"]["to"] if job else None)
        pin, history = None, []
        if path:
            pin = _pin(config, configuration, path)
            history = [j for j in jobs.jobs(config, limit=50, kind="test-bump", configuration=configuration)
                       if j["params"].get("path") == path and (job is None or j["id"] != job["id"])][:10]
        if pin is not None and not to:
            to = pin.get("default_branch")
        self_href = "%s/%s/bump?job=%d" % (PREFIX, urllib.parse.quote(configuration, safe=""), job["id"]) if job else ""
        return _page(request, "caps_bump.html", "Test a bump in %s" % configuration, configuration=configuration,
                     job=job, bump_path=path, to=to, pin=pin, history=history, local=_local(request),
                     queued=bool(q.get("queued")), self_href=self_href)

    async def post_test_bump(request: Request, configuration: str) -> Response:
        from starlette.concurrency import run_in_threadpool
        from nanosoc_rm import jobs
        if not _local(request):
            return PlainTextResponse("404 Not Found: this service has no local job queue", 404)
        form = await request.form()
        path = str(form.get("path") or "").strip().strip("/")
        to = str(form.get("to") or "").strip()
        config = _config(request)
        ident = getattr(request.state, "identity", None)
        login = getattr(ident, "login", None) or "web"
        try:
            from nanosoc_rm.ci import configs
            configs.configuration(configuration, config)
            job = await run_in_threadpool(lambda: jobs.submit(
                config, "test-bump", {"configuration": configuration, "path": path, "to": to}, by=login))
        except jobs.JobUsage as e:
            return PlainTextResponse("400 Bad Request: %s" % e, 400)
        except Exception as e:                                   # noqa: BLE001
            from nanosoc_rm.ci.configs import UnknownConfiguration
            if isinstance(e, UnknownConfiguration):
                return PlainTextResponse("404 Not Found: %s" % e, 404)
            return PlainTextResponse("503 Service Unavailable: %s" % str(e)[:300], 503)
        return RedirectResponse("%s/%s/bump?job=%d&queued=1" % (PREFIX, urllib.parse.quote(configuration, safe=""),
                                                                job["id"]), 303)

    get = ["GET"]
    r.add_api_route("/", index, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}", tree, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/bump", bump, methods=get, include_in_schema=False)
    if local:
        r.add_api_route("/{configuration}/test-bump", post_test_bump, methods=["POST"], include_in_schema=False)
    return r


def _pin(config, configuration: str, path: str) -> dict | None:
    """The stored pin at `path` (component, commit, its repo's default branch), or None."""
    from nanosoc_rm.analysis import _inputs as I
    try:
        docs = I.stored_tree(configuration, config)
    except Exception:                                            # noqa: BLE001 - no stored walk: no pin facts
        return None
    d = next((d for d in docs if path in d["fact"]["tree_paths"]), None)
    if d is None:
        return None
    f = d["fact"]
    default = next((h.get("default_branch") for h in (f.get("hosts") or {}).values() if h.get("pinned")), None)
    return {"component": f["child"].get("component"), "commit": f["child"].get("commit"), "default_branch": default}
