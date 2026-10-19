"""The app's HTML pages (sections 13 and 13a): every GET a person opens, plus the job card and log the pages
poll. A page owned by a workspace module (routers.DELEGATES) is answered there when that module is built; the
page here is the older one it replaced."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response

from nanosoc_rm import jobs
from nanosoc_rm.web import shell, views
from nanosoc_rm.web.page import Page, env
from nanosoc_rm.web.routers import Route
from nanosoc_rm.web.routes.common import (ctx, delegate, job_fragment, job_html, not_found, page, redacted_alert,
                                          registry_problems)

__all__ = ["ROUTES", "branches_view", "hosts_view"]


def dashboard(request: Request) -> Response:
    answer = delegate("dashboard", request)
    if answer is not None:
        return answer
    config, _ = ctx(request)
    problems = registry_problems(config)
    return page(request, "dashboard.html", "Dashboard", problems=problems, d=views.dashboard(config))


def search_page(request: Request) -> Response:
    """The command bar. An htmx request gets the hits fragment; a plain GET (no JavaScript) the full results
    page. Reads stored facts and registry records only."""
    config, _ = ctx(request)
    q = (request.query_params.get("q") or "").strip()[:80]
    scope = request.query_params.get("scope") or None
    scope = scope if scope == "component" else None
    hits = shell.search(config, q, scope=scope)
    if request.headers.get("hx-request"):
        p = Page()
        return HTMLResponse(env.get_template("_search_hits.html").render(
            hits=hits, q=q, min_query=shell.MIN_QUERY, fact=p.fact, page=p))
    return page(request, "search.html", "Search", hits=hits, q=q, scope=scope, min_query=shell.MIN_QUERY,
                configs=views.configurations(config))


def configuration(request: Request, id: str) -> Response:
    answer = delegate("configuration", request, id)
    if answer is not None:
        return answer
    config, _ = ctx(request)
    problems = registry_problems(config)
    c = views.configuration_page(config, id) if views.is_valid_id(id) else None
    if c is None:
        return not_found(request, "configuration")
    job = jobs.get(config, n) if (n := jobs.parse_id(request.query_params.get("job"))) is not None else None
    return page(request, "configuration.html", "Configuration %s" % id, problems=problems, c=c,
                job_html=job_html(request, job))


def component(request: Request, id: str) -> Response:
    answer = delegate("component", request, id)
    if answer is not None:
        return answer
    config, _ = ctx(request)
    problems = registry_problems(config)
    c = views.component_page(config, id) if views.is_valid_id(id) else None
    if c is None:
        return not_found(request, "component")
    return page(request, "component.html", "Component %s" % id, problems=problems, c=c)


def contains_page(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    sha = request.query_params.get("sha") or ""
    comp = request.query_params.get("component") or None
    rows, problems, computed = ([], [], False) if not sha else views.containment(config, sha, comp)
    return page(request, "contains.html", "Contains commit", problems=problems, sha=sha, component=comp,
                rows=rows, computed=computed)


def silicon(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    rows = views.silicon_protection(config, request.query_params.get("configuration") or None)
    return page(request, "silicon.html", "Silicon protection", rows=rows)


def defects_page(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    problems = registry_problems(config)
    return page(request, "defects.html", "Defects", problems=problems, defects=views.defects(config),
                errata=views.errata(config))


def defect_page(request: Request, id: str) -> HTMLResponse:
    config, _ = ctx(request)
    problems = registry_problems(config)
    d = next((x for x in views.defects(config) if x["fact"]["id"] == id), None)
    if d is None:
        return not_found(request, "defect")
    er = [e for e in views.errata(config) if e["fact"]["record"].get("defect") == id]
    return page(request, "defect.html", id, problems=problems, d=d, errata=er)


def errata_page(request: Request, configuration: str) -> HTMLResponse:
    config, _ = ctx(request)
    problems = registry_problems(config)
    rec = views.configuration_record(config, configuration)
    er = views.errata(config, configuration)
    if rec is None and not er:
        return not_found(request, "configuration")
    return page(request, "errata.html", "Errata of %s" % configuration, problems=problems,
                configuration=configuration, record=rec, errata=er)


def run_page(request: Request, nonce: str) -> HTMLResponse:
    config, _ = ctx(request)
    run = views.run_by_nonce(config, nonce)
    if run is None:
        return not_found(request, "run")
    return page(request, "run.html", "T0 run %s" % nonce[:12], run=run)


def facts_page(request: Request, subject: str) -> HTMLResponse:
    config, _ = ctx(request)
    obs, current = views.fact_by_subject(config, subject)
    if obs is None:
        return not_found(request, "fact")
    return page(request, "fact.html", "Fact", obs=redacted_alert(obs, config), current=current)


def compat_page(request: Request) -> Response:
    answer = delegate("compat", request)
    if answer is not None:
        return answer
    config, _ = ctx(request)
    problems = registry_problems(config)
    q = request.query_params
    job = None
    if (n := jobs.parse_id(q.get("job"))) is not None:
        job = jobs.get(config, n)
        if job is None:
            return not_found(request, "job")
    cid = q.get("configuration") or (job["params"].get("configuration") if job else None)
    configs = views.configurations(config)
    if cid and not views.is_valid_id(cid):
        cid = None
    pins = views.pins(config, cid) if cid else []
    refs: dict[str, list[dict]] = {}
    for p in pins:
        comp = (p["fact"].get("child") or {}).get("component")
        if comp and comp not in refs:
            refs[comp] = views.component_refs(config, comp)
    tiers = {}
    if cid:
        try:
            from nanosoc_rm.ci import configs as cimod
            tiers = cimod.tiers(cid, config)
        except Exception as e:                       # noqa: BLE001 - the form still works without tiers
            problems = problems + ["tiers.yaml could not be read for %s: %s" % (cid, str(e)[:200])]
    return page(request, "compat.html", "Compatibility check", problems=problems, configs=configs, cid=cid,
                pins=pins, refs=refs, tiers=tiers, job=job, job_html=job_html(request, job),
                recent=views.safe_jobs(config, limit=25, kind="compat"),
                results=views.compat_results(config, cid)[:10] if cid else views.compat_results(config))


def branches_page(request: Request) -> Response:
    answer = delegate("branches", request)
    if answer is not None:
        return answer
    q = request.query_params
    return branches_view(request, q.get("component") or "", q.get("all") in ("1", "on", "true"),
                         q.get("verdict") or "")


def branches_view(request: Request, comp: str, all_repos: bool, verdict: str, refresh: bool = False) -> HTMLResponse:
    """The Branches page for `comp`. Only the local POST /branches/refresh passes refresh=True: a GET never
    re-reads a mirror (section 13a)."""
    config, _ = ctx(request)
    problems = registry_problems(config)
    rows, more, computed = (views.branch_rows(config, comp, all_repos=all_repos, refresh=refresh)
                            if comp else ([], [], False))
    counts: dict[str, int] = {}
    for d in rows:
        counts[d["fact"]["verdict"]] = counts.get(d["fact"]["verdict"], 0) + 1
    shown = [d for d in rows if not verdict or d["fact"]["verdict"] == verdict
             or (verdict == "deletable" and d["fact"]["deletable"])]
    mirrored = {d["fact"].get("component") for d in views.mirrors(config)}
    comps = [c for c in views.components(config) if c["fact"]["id"] in mirrored] if not comp else []
    return page(request, "branches.html", "Branches", problems=problems + more, component=comp,
                all_repos=all_repos, rows=shown, total=len(rows), counts=counts, verdict=verdict,
                computed=computed, delete_text=views.delete_text(rows) if rows else "", comps=comps,
                choices=sorted(mirrored - {None}))


def hosts_page(request: Request) -> HTMLResponse:
    return hosts_view(request, request.query_params.get("component") or None)


def hosts_view(request: Request, comp: str | None, problems: list[str] | None = None) -> HTMLResponse:
    config, _ = ctx(request)
    if comp and not views.is_valid_id(comp):
        comp = None
    rows = views.host_compares(config, comp)
    return page(request, "hosts.html", "Host copies", problems=registry_problems(config) + (problems or []),
                rows=rows, component=comp)


def mirrors_page(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    job = jobs.get(config, n) if (n := jobs.parse_id(request.query_params.get("job"))) is not None else None
    rows = views.mirrors(config)
    return page(request, "mirrors.html", "Mirrors", rows=rows, summary=views.mirror_summary(rows),
                job=job, job_html=job_html(request, job),
                recent=views.safe_jobs(config, limit=10, kind="mirror-sync"))


def alerts_page(request: Request) -> Response:
    answer = delegate("alerts", request)
    if answer is not None:
        return answer
    config, _ = ctx(request)
    open_, cleared = views.all_alerts(config)
    return page(request, "alerts.html", "Alerts", open_alerts=open_, cleared=cleared[:50])


def components_page(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    problems = registry_problems(config)
    mirrored = {d["fact"].get("component") for d in views.mirrors(config)}
    return page(request, "components.html", "Components", problems=problems,
                components=views.components(config), mirrored=mirrored)


def errata_index(request: Request) -> HTMLResponse:
    config, _ = ctx(request)
    problems = registry_problems(config)
    return page(request, "errata_all.html", "Errata", problems=problems, errata=views.errata(config))


def _job_or_none(request: Request, id: str) -> dict | None:
    config, _ = ctx(request)
    return jobs.get(config, n) if (n := jobs.parse_id(id)) is not None else None


def job_fragment_route(request: Request, id: str) -> Response:
    job = _job_or_none(request, id)
    if job is None:
        return PlainTextResponse("404 Not Found: no such job", 404)
    return job_fragment(request, job)


def job_log(request: Request, id: str) -> Response:
    job = _job_or_none(request, id)
    if job is None:
        return PlainTextResponse("404 Not Found: no such job", 404)
    config, _ = ctx(request)
    return PlainTextResponse(jobs.log_text(config, job["id"]) or "(no output yet)")


ROUTES = tuple(Route("GET", path, fn) for path, fn in (
    ("/", dashboard), ("/search", search_page), ("/configurations/{id}", configuration),
    ("/components/{id}", component), ("/contains", contains_page), ("/silicon-protection", silicon),
    ("/defects", defects_page), ("/defects/{id}", defect_page), ("/errata/{configuration}", errata_page),
    ("/runs/{nonce}", run_page), ("/facts/{subject:path}", facts_page), ("/compat", compat_page),
    ("/branches", branches_page), ("/hosts", hosts_page), ("/mirrors", mirrors_page), ("/alerts", alerts_page),
    ("/components", components_page), ("/errata", errata_index), ("/jobs/{id}/fragment", job_fragment_route),
    ("/jobs/{id}/log", job_log)))
