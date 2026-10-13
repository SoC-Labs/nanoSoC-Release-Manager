"""The matrix pages (D3, D8, D9, B7), mounted at /matrix through the web plug-in seam (nanosoc_rm.web.plugins).

    GET  /matrix/                                     configurations, each with its matrix link; the nightly status
    GET  /matrix/{configuration}                      the grid: cell verdict (text and colour), age, Run buttons
         ?component=C (repeatable) &columns=a,b       the same filters as the CLI
    GET  /matrix/{configuration}/cell?component=&column=   one cell: every run of it, attempts, D4 provenance,
                                                      the D9 comparison naming its baseline run, the latest job
    POST /matrix/{configuration}/run                  queue cells as local jobs: scope = cell | row | column | all

Every page is the shared layout (`plugins.render_page`): sign-in, CSRF, data age, fact links. Nothing runs inside a
request: Run queues jobs (nanosoc_rm.jobs) that the service's worker runs one at a time. The POST route answers
404 unless this app was built with local routes (local sign-in or no CI project), like the other local actions;
without them the page shows the CLI command instead of buttons.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "CELL_CLASS"]

PREFIX = "/matrix"
NAV_LABEL = "Matrix"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
# README section 6 colours, with FLAKY (D8) amber like WARN: always shown beside its text label.
CELL_CLASS = {"PASS": "state-green", "WARN": "state-amber", "WAIVED": "state-amber", "FLAKY": "state-amber",
              "FAIL": "state-red", "UNVERIFIED": "state-red", "INFRA": "state-amber", "NOT-RUN": "state-grey"}
COLUMN_LABEL = {"pinned": "pinned", "in-range": "latest tag in range", "latest-tag": "latest tag",
                "head": "default-branch head"}


def _env() -> Environment:
    env = Environment(loader=ChoiceLoader([FileSystemLoader(str(HERE / "templates"))]),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.globals.update(CELL_CLASS=CELL_CLASS, COLUMN_LABEL=COLUMN_LABEL, murl=_murl, quote=urllib.parse.quote)
    return env


def _murl(configuration: str, page: str = "", **params: Any) -> str:
    path = "%s/%s%s" % (PREFIX, urllib.parse.quote(configuration, safe=""), ("/" + page) if page else "")
    items = []
    for k, v in params.items():
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if x not in (None, "", False):
                items.append((k, x))
    return path + ("?" + urllib.parse.urlencode(items) if items else "")


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
    rm = getattr(request.app.state, "rm", None)
    return bool(getattr(rm, "local_routes", False))


def _error(request: Request, e: Exception) -> HTMLResponse:
    from nanosoc_rm.analysis._inputs import UsageProblem
    status = 404 if isinstance(e, (UsageProblem, LookupError)) else 503
    return _page(request, "matrix_message.html", "Matrix", status=status, message=str(e))


def _filters(request: Request) -> tuple[list[str], str | None]:
    comps = [c for c in request.query_params.getlist("component") if c]
    return comps, request.query_params.get("columns") or None


def router() -> APIRouter:
    from nanosoc_rm import matrix as M
    from nanosoc_rm.matrix import cells as K
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        from nanosoc_rm.analysis import _inputs as I
        config = _config(request)
        try:
            confs = I.registry(config).configurations()
        except Exception as e:                                   # noqa: BLE001 - shown, never a bare 500
            return _error(request, e)
        return _page(request, "matrix_index.html", "Compatibility matrix", confs=confs, nightly=M.NIGHTLY)

    def grid(request: Request, configuration: str) -> HTMLResponse:
        config = _config(request)
        comps, cols = _filters(request)
        try:
            view = M.matrix(configuration, columns=M.parse_columns(cols), components=comps or None, config=config)
        except M.MatrixUsage as e:
            return _page(request, "matrix_message.html", "Matrix", status=400, message=str(e))
        except Exception as e:                                   # noqa: BLE001
            return _error(request, e)
        docs = {d["subject"]: d for d in K.cells(configuration, config)}
        from nanosoc_rm.analysis import _inputs as I
        all_components = sorted({d["fact"]["child"]["component"] for d in I.stored_tree(configuration, config)
                                 if d["fact"]["child"].get("component")})
        queued = request.query_params.get("queued")
        return _page(request, "matrix.html", "Matrix %s" % configuration, v=view["fact"], view=view, docs=docs,
                     all_components=all_components, components=comps, columns_q=cols, queued=queued,
                     local=_local(request),
                     cli="nanosoc-rm matrix %s%s%s" % (configuration, "".join(" --component %s" % c for c in comps),
                                                       " --columns %s" % cols if cols else ""))

    def cell(request: Request, configuration: str) -> HTMLResponse:
        from nanosoc_rm import jobs
        config = _config(request)
        q = request.query_params
        component, column = q.get("component") or None, q.get("column") or ""
        if column not in M.COLUMNS:
            return _page(request, "matrix_message.html", "Matrix", status=404, message="no column %r" % column)
        try:
            view = M.matrix(configuration, columns=[column] if column != "pinned" else ["pinned"],
                            components=[component] if component else None, config=config)
        except Exception as e:                                   # noqa: BLE001
            return _error(request, e)
        f = view["fact"]
        cv = f["pinned"] if column == "pinned" else f["rows"][0]["cells"][column]
        row = None if column == "pinned" else f["rows"][0]
        runs = [d for d in K.cells(configuration, config)
                if d["fact"]["component"] == component and d["fact"]["column"] == column]
        stored = K.cells(configuration, config)
        q_active = set(K.active_quarantine(K.quarantine(configuration, config)))
        latest = runs[-1] if runs else None
        comparison = K.compare(latest["fact"], K.baseline_for(latest["fact"], stored), q_active) \
            if latest and column != "pinned" else None
        job = jobs.get(config, cv["job"]) if cv.get("job") else None
        return _page(request, "matrix_cell.html", "Matrix cell %s %s/%s" % (configuration, component or "tree",
                                                                             column),
                     configuration=configuration, component=component, column=column, cv=cv, row=row,
                     runs=list(reversed(runs)), latest=latest, comparison=comparison, job=job,
                     local=_local(request), q_active=q_active)

    async def run(request: Request, configuration: str) -> Response:
        from starlette.concurrency import run_in_threadpool
        if not _local(request):
            return PlainTextResponse("404 Not Found: this service has no local job worker; run `nanosoc-rm matrix "
                                     "%s --run` on a login server" % configuration, 404)
        form = await request.form()
        scope = str(form.get("scope") or "")
        comp = str(form.get("component") or "") or None
        col = str(form.get("column") or "") or None
        back_comps = [c for c in form.getlist("filter_component") if c]
        back_cols = str(form.get("filter_columns") or "") or None
        config = _config(request)
        ident = getattr(request.state, "identity", None)
        login = getattr(ident, "login", None) or "web"
        try:
            if scope == "cell":
                if not col:
                    raise M.MatrixUsage("a cell needs a column")
                kw = {"cells": [(comp if col != "pinned" else None, col)],
                      "components": [comp] if comp and col != "pinned" else None, "columns": [col]}
            elif scope == "row":
                if not comp:
                    raise M.MatrixUsage("a row needs a component")
                kw = {"components": [comp], "columns": M.parse_columns(back_cols)}
            elif scope == "column":
                if col not in M.COLUMNS:
                    raise M.MatrixUsage("a column needs one of %s" % ", ".join(M.COLUMNS))
                kw = {"columns": ["pinned", col] if col != "pinned" else ["pinned"],
                      "components": back_comps or None}
            elif scope == "all":
                kw = {"columns": M.parse_columns(back_cols), "components": back_comps or None}
            else:
                raise M.MatrixUsage("scope must be cell, row, column or all")
            queued = await run_in_threadpool(lambda: M.queue(configuration, by=login, config=config, **kw))
        except M.MatrixUsage as e:
            return PlainTextResponse("400 Bad Request: %s" % e, 400)
        except Exception as e:                                   # noqa: BLE001
            from nanosoc_rm.analysis._inputs import UsageProblem
            if isinstance(e, UsageProblem):
                return PlainTextResponse("404 Not Found: %s" % e, 404)
            return PlainTextResponse("503 Service Unavailable: %s" % str(e)[:300], 503)
        return RedirectResponse(_murl(configuration, component=back_comps, columns=back_cols,
                                      queued=len(queued)), 303)

    get, post = ["GET"], ["POST"]
    r.add_api_route("/", index, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}", grid, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/cell", cell, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/run", run, methods=post, include_in_schema=False)
    return r


ENV = _env()
