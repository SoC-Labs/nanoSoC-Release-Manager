"""Q6: the quality pages, a FastAPI APIRouter mounted at /quality by the web app (behind its sign-in guard).

    GET /quality/                               overview: every target with its latest stored numbers and trend
                                                (?scope=mine, the default when any listed target is
                                                in my configurations, keeps those repos; ?scope=all)
    GET /quality/{target}                       summary of one target at a ref (analyses it when not stored)
    GET /quality/{target}/density               Q1 language table and sortable file table
    GET /quality/{target}/comments              Q2 findings with suggested edits     .../comments.diff  the patch
                                                (the decoration rule, banner and
                                                boilerplate findings, is excluded unless ?kind=decoration (or
                                                one of its kinds); ?banners=1 adds the rule to the patch; rows
                                                in review order)
    GET /quality/{target}/readme                Q3 mismatches and generated layout   .../readme.diff    the patch
    GET /quality/{target}/docs                  Q4 files ranked least concise first
    GET /quality/{target}/trend                 Q5 stored results per commit

    POST /quality/{target}/analyse              queue the analysis (local deployments; form ref, next)

Query: `ref` (branch, tag or sha; default the default branch, or the work tree for a `[quality] paths` target).
A page shows the stored result for the commit the ref names when there is one. It never analyses inside the
request and a GET never queues work: with nothing stored it says "not
analysed yet" and offers the Analyse button, a POST that queues `nanosoc-rm quality all TARGET [--ref REF] --store`
in the local job worker (job kind `quality`, one per target and ref at a time); while that job waits or runs the
page answers 202 "analysing, refresh" with the job's live card. Every page shows the same rows as
its CLI command (`nanosoc-rm quality ...`) and links each result to `/facts/<subject>` (A3).
"""
from __future__ import annotations

import threading
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import Environment
from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import quality as q
from nanosoc_rm.web import plugins
from nanosoc_rm.web.plugins import WEB_TEMPLATES, config_of, local_routes, login_of, template_env

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "sparkline", "render_page", "selected_findings",
           "comments_view", "my_components", "overview_model"]

PREFIX = "/quality"
NAV_LABEL = "Code and doc quality"
NAV_PATH = PREFIX + "/"
HERE = Path(__file__).resolve().parent
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _env() -> Environment:
    return template_env(HERE, app_globals=True, sparkline=sparkline, qurl=_qurl,
                        pct=lambda x: "%.1f%%" % (100 * (x or 0)))


def _base_template() -> str:
    """The GUI's base template when it has one (base.html, else layout.html), else this package's own."""
    for name in ("base.html", "layout.html"):
        if (WEB_TEMPLATES / name).is_file():
            return name
    return "quality_base.html"


def render_page(template: str, *, title: str, identity: Any = None, problems: list[str] | None = None,
                **ctx: Any) -> str:
    """The body first (its facts recorded for the data age, A3), then the GUI's base around it."""
    from nanosoc_rm import clock
    try:
        from nanosoc_rm.web.page import Page, human_age
    except ImportError:                                       # pragma: no cover
        Page, human_age = None, None
    page = Page() if Page else None
    fact = page.fact if page else (lambda obs, label=None, **k: Markup(label or obs["subject"]))
    body = ENV.get_template(template).render(page=page, fact=fact, identity=identity, **ctx)
    return ENV.get_template(_base_template()).render(
        body=Markup(body), title=title, identity=identity, problems=problems or [],
        age_seconds=page.age_seconds() if page else None, oldest=page.oldest() if page else None,
        human_age=human_age or (lambda s: ""), now=clock.iso(page.now if page else None))


def sparkline(values: list[float], width: int = 120, height: int = 24, invert: bool = False) -> Markup:
    """An inline SVG polyline of `values` (oldest first); a single point is drawn as a dot."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return Markup('<span class="note">no history</span>')
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 2 + (width - 4) * (i / (n - 1) if n > 1 else 0.5)
        y = 2 + (height - 4) * (1 - (v - lo) / span if not invert else (v - lo) / span)
        pts.append("%.1f,%.1f" % (x, y))
    last = pts[-1].split(",")
    title = " → ".join(("%.3f" % v).rstrip("0").rstrip(".") for v in vals[-6:])
    return Markup('<svg class="spark" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="%s">'
                  '<title>%s</title><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="%s"/>'
                  '<circle cx="%s" cy="%s" r="2.2" fill="currentColor"/></svg>'
                  % (width, height, width, height, title, title, " ".join(pts), last[0], last[1]))


def _qurl(target: str, page: str = "", **params: Any) -> str:
    path = "%s/%s%s" % (PREFIX, urllib.parse.quote(target, safe=""), ("/" + page) if page else "")
    params = {k: v for k, v in params.items() if v not in (None, "", False)}
    return path + ("?" + urllib.parse.urlencode(params) if params else "")


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _key(fact_or_source) -> str:
    if isinstance(fact_or_source, dict):
        f = fact_or_source
        return "%s:%s%s" % (f["target"], f["commit"] or "worktree", "+worktree" if f["worktree"] and f["commit"] else "")
    s = fact_or_source
    return "%s:%s%s" % (s.label, s.commit or "worktree", "+worktree" if s.worktree and s.commit else "")


class NotAnalysed(Exception):
    """No stored result and no quality job queued or running for this target and ref. A GET never queues one
    (link prefetchers and previews would start analyses); the page offers the POST
    Analyse button instead."""


class Pending(Exception):
    """No stored result: a quality job was queued (or one already runs) for this target and ref."""

    def __init__(self, job: dict):
        super().__init__("analysis job %d" % job["id"])
        self.job = job


def _waiting(config, target: str, ref: str | None) -> dict | None:
    """The queued or running quality job for (target, ref), or None."""
    from nanosoc_rm import jobs
    params = {"target": target, "ref": ref}
    for j in jobs.jobs(config, limit=200, kind="quality", states=("queued", "running")):
        if j["params"] == params:
            return j
    return None


def _queue(config, target: str, ref: str | None, login: str) -> dict:
    """The queued or running quality job for (target, ref), or a new one (never analyse in a request).
    Called by the POST Analyse route only: a GET never queues work."""
    from nanosoc_rm import jobs
    with _lock("queue:%s:%s" % (target, ref)):
        return _waiting(config, target, ref) or jobs.submit(config, "quality", {"target": target, "ref": ref},
                                                            login or "web")


def _result(config, kind: str, target: str, ref: str | None, analyse: bool = False, login: str = "") -> tuple[dict, Any]:
    """(observation, source) for one kind at one ref when it is stored (a work-tree result only while the disk is
    as it was). Otherwise Pending with the queued or running quality job, or NotAnalysed when there is none: the
    page offers the POST Analyse button (a GET never queues). `analyse` and `login` are kept for callers; ignored."""
    from nanosoc_rm import db
    source = q.open_source(target, ref=ref, config=config)
    obs_kind = q.KINDS[kind]
    subject = "%s:%s" % (obs_kind, _key(source))
    hit = [d for d in db.latest(obs_kind, subject, config=config) if d["subject"] == subject]
    # a work-tree result is reused only while the disk is as it was (same files, sizes, mtimes)
    if hit and (not source.worktree or hit[-1]["fact"].get("worktree_state") == source.state):
        return hit[-1], source
    job = _waiting(config, target, ref)
    if job is not None:
        raise Pending(job)
    raise NotAnalysed(target)


def _targets(config, by_kind: dict[str, dict[str, list[dict]]] | None = None) -> list[dict]:
    """Configured targets plus any target with stored results (a --path run stored from the CLI). `by_kind`:
    {kind: q.stored_by_target(kind)} when the caller has already read it."""
    rows = {t["target"]: dict(t) for t in q.targets(config)}
    for kind in q.KINDS:
        if by_kind is not None:
            stored_targets = list(by_kind[kind])
        else:
            stored_targets = [d["fact"]["target"] for d in q.stored(kind, config=config)]
        for target in stored_targets:
            rows.setdefault(target, {"target": target, "origin": "stored", "where": ""})
    return [rows[k] for k in sorted(rows)]


def my_components(config) -> set[str]:
    """The repos in my configurations: each registered configuration's own component and every component
    its last walked tree pins (stored pin_state facts). Empty when no registry or tree is stored."""
    out: set[str] = set()
    try:
        from nanosoc_rm.web import views
        for rec in views.configurations(config):
            comp = (rec["fact"].get("record") or {}).get("component")
            if comp:
                out.add(comp)
    except Exception:                                         # noqa: BLE001 - no registry here: nothing is mine
        pass
    try:
        from nanosoc_rm import db
        for d in db.latest("pin_state", "pin_state:", config=config):
            comp = (d["fact"].get("child") or {}).get("component")
            if comp:
                out.add(comp)
    except Exception:                                         # noqa: BLE001
        pass
    return out


def overview_model(config, scope: str | None = None) -> dict:
    """The overview's rows and its filter: `scope` "mine" or "all"; None picks "mine" when any listed
    target is a repo in a configuration, else "all" (a `[quality] paths` name is not a component id, so it never
    counts as mine and must not be hidden by default)."""
    mine = my_components(config)
    rows = _overview_rows(config)
    for r in rows:
        r["mine"] = r["target"] in mine
    if scope not in ("mine", "all"):
        scope = "mine" if any(r["mine"] for r in rows) else "all"   # never default to an empty table
    shown = [r for r in rows if r["mine"]] if scope == "mine" else rows
    return {"rows": shown, "scope": scope, "n_all": len(rows), "n_mine": sum(1 for r in rows if r["mine"])}


def _overview_rows(config) -> list[dict]:
    by_kind = {kind: q.stored_by_target(kind, config=config) for kind in q.KINDS}   # one read per kind, not one per target
    out = []
    for t in _targets(config, by_kind):
        row = dict(t)
        for kind in q.KINDS:
            hist = by_kind[kind].get(t["target"], [])
            row[kind] = hist[-1] if hist else None
            row[kind + "_trend"] = [p["value"] for p in q.trend(kind, t["target"], docs=hist)] if hist else []
        out.append(row)
    return out


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    """In the GUI's shell (navigation, sign-out form, CSRF token) when mounted by the web app; else stand-alone."""
    if getattr(getattr(request.app, "state", None), "rm", None) is not None:
        return plugins.render_page(request, template, title, status=status, environment=ENV, **ctx)
    html = render_page(template, title=title, identity=getattr(request.state, "identity", None), **ctx)
    return HTMLResponse(html, status_code=status)


def _error(request: Request, e: Exception) -> HTMLResponse:
    status = 404 if isinstance(e, q.UsageError) else 503
    return _page(request, "quality_message.html", "Quality", status=status, message=str(e))


def _pending(request: Request, target: str, ref: str | None, job: dict) -> HTMLResponse:
    """202: the analysis runs as a job; the page shows its live card and asks for a refresh."""
    config = config_of(request)
    html = ""
    if getattr(getattr(request.app, "state", None), "rm", None) is not None:
        html = plugins.job_card(ENV, request, config, job)
    return _page(request, "quality_pending.html", "Quality: %s" % target, status=202, target=target, ref=ref,
                 job=job, job_html=Markup(html), refresh_url=str(request.url.remove_query_params("analyse")))


def _not_analysed(request: Request, target: str, ref: str | None) -> HTMLResponse:
    """200: nothing stored and nothing queued; a local deployment offers the Analyse button (a POST)."""
    return _page(request, "quality_unanalysed.html", "Quality: %s" % target, target=target, ref=ref,
                 here=str(request.url.path))


def router(app: Any = None) -> APIRouter:
    local = local_routes(app)
    r = APIRouter(prefix=PREFIX)

    async def post_analyse(request: Request, target: str) -> Response:
        """Queue the quality job for (target, ref) and go back to the page it was asked from (303)."""
        from starlette.concurrency import run_in_threadpool
        form = await request.form()
        ref = str(form.get("ref") or "").strip() or None
        back = str(form.get("next") or "")
        if not back.startswith(PREFIX + "/") or "//" in back or "\\" in back:
            back = _qurl(target, ref=ref) if ref else _qurl(target)
        config = config_of(request)
        try:
            await run_in_threadpool(q.open_source, target, ref=ref, config=config)
            await run_in_threadpool(_queue, config, target, ref, login_of(request))
        except (q.UsageError, q.QualityError, q.SourceError) as e:
            return _error(request, e)
        return RedirectResponse(back, 303)

    def overview(request: Request) -> HTMLResponse:
        config = config_of(request)
        return _page(request, "quality_overview.html", "Code and doc quality",
                     **overview_model(config, request.query_params.get("scope")))

    def summary(request: Request, target: str) -> Response:
        config = config_of(request)
        ref = request.query_params.get("ref") or None
        try:
            res = {k: _result(config, k, target, ref)[0] for k in q.KINDS}
        except Pending as p:
            return _pending(request, target, ref, p.job)
        except NotAnalysed:
            return _not_analysed(request, target, ref)
        except (q.UsageError, q.QualityError, q.SourceError) as e:
            return _error(request, e)
        trends = {k: q.trend(k, target, config=config) for k in q.KINDS}
        return _page(request, "quality_summary.html", "Quality: %s" % target, target=target, ref=ref, res=res,
                     trends=trends)

    def kind_page(kind: str, template: str, title: str):
        def handler(request: Request, target: str) -> Response:
            config = config_of(request)
            ref = request.query_params.get("ref") or None
            try:
                obs, _ = _result(config, kind, target, ref)
            except Pending as p:
                return _pending(request, target, ref, p.job)
            except NotAnalysed:
                return _not_analysed(request, target, ref)
            except (q.UsageError, q.QualityError, q.SourceError) as e:
                return _error(request, e)
            sel = request.query_params.get("kind") or ""
            extra = comments_view(config, target, obs, sel) if kind == "comments" else {}
            return _page(request, template, "%s: %s" % (title, target), target=target, ref=ref, obs=obs, f=obs["fact"],
                         sel=sel, **extra)
        return handler

    def comments_diff(request: Request, target: str) -> Response:
        config = config_of(request)
        ref = request.query_params.get("ref") or None
        sel = request.query_params.get("kind") or ""
        try:
            obs, source = _result(config, "comments", target, ref)
        except Pending as p:                                              # not stored, or the disk moved on
            return PlainTextResponse("Analysing %s (job %d): download again when it has finished.\n"
                                     % (target, p.job["id"]), 202)
        except NotAnalysed:
            return PlainTextResponse("%s is not analysed at this ref: open %s and press Analyse, then download "
                                     "again.\n" % (target, _qurl(target, ref=ref) if ref else _qurl(target)), 404)
        except (q.UsageError, q.QualityError, q.SourceError) as e:
            return PlainTextResponse(str(e), 404 if isinstance(e, q.UsageError) else 503)
        banners = request.query_params.get("banners") == "1"
        findings = selected_findings(obs["fact"]["findings"], sel, banners)
        text, _ = q.comment_patch(source, {"fact": {"findings": findings}})
        if not text.strip():
            return PlainTextResponse("Nothing to patch: no finding%s of %s has a mechanical edit.\n"
                                     % ((" of kind %s" % sel) if sel else "", target), 404)
        return _diff_response(text, "%s-comments%s%s.diff" % (target, ("-" + sel) if sel else "",
                                                             "-with-banners" if banners and not sel else ""))

    def readme_diff(request: Request, target: str) -> Response:
        config = config_of(request)
        ref = request.query_params.get("ref") or None
        from nanosoc_rm.quality.readmes import layout_patch
        try:
            source = q.open_source(target, ref=ref, config=config)
        except (q.UsageError, q.QualityError, q.SourceError) as e:
            return PlainTextResponse(str(e), 404 if isinstance(e, q.UsageError) else 503)
        text = layout_patch(source)
        if not text.strip():
            why = "has no README.md" if source.text("README.md") is None else \
                "README.md already has this Repository layout"
            return PlainTextResponse("Nothing to patch: %s %s.\n" % (target, why), 404)
        return _diff_response(text, "%s-readme-layout.diff" % target)

    def trend_page(request: Request, target: str) -> HTMLResponse:
        config = config_of(request)
        by_key: dict[str, dict] = {}
        for k in q.KINDS:
            for d in q.stored(k, target, config=config):
                row = by_key.setdefault(d["subject"].split(":", 1)[1], {"first": d["observed_at"]})
                row[k] = d
                row["first"] = min(row["first"], d["observed_at"])
        table = sorted(by_key.values(), key=lambda r: r["first"])
        return _page(request, "quality_trend.html", "Quality trend: %s" % target, target=target, table=table,
                     trends={k: q.trend(k, target, config=config) for k in q.KINDS})

    get = ["GET"]
    r.add_api_route("/", overview, methods=get, include_in_schema=False)
    r.add_api_route("/{target}/density", kind_page("density", "quality_density.html", "Comment density"),
                    methods=get, include_in_schema=False)
    r.add_api_route("/{target}/comments", kind_page("comments", "quality_comments.html", "Comment findings"),
                    methods=get, include_in_schema=False)
    r.add_api_route("/{target}/comments.diff", comments_diff, methods=get, include_in_schema=False)
    r.add_api_route("/{target}/readme", kind_page("readme", "quality_readme.html", "README check"),
                    methods=get, include_in_schema=False)
    r.add_api_route("/{target}/readme.diff", readme_diff, methods=get, include_in_schema=False)
    r.add_api_route("/{target}/docs", kind_page("docs", "quality_docs.html", "Doc conciseness"),
                    methods=get, include_in_schema=False)
    r.add_api_route("/{target}/trend", trend_page, methods=get, include_in_schema=False)
    r.add_api_route("/{target}", summary, methods=get, include_in_schema=False)
    if local:
        r.add_api_route("/{target}/analyse", post_analyse, methods=["POST"], include_in_schema=False)
    return r


def selected_findings(findings: list[dict], sel: str = "", banners: bool = False) -> list[dict]:
    """One kind when `sel` names it, the whole decoration rule when `sel` is "decoration";
    otherwise every finding outside the rule (banner and boilerplate are one rule, not N findings), plus the
    rule's findings when `banners` asks for the rule in the patch."""
    from nanosoc_rm.quality.findings import BULK_RULE, is_bulk
    if sel == BULK_RULE:
        return [x for x in findings if is_bulk(x)]
    if sel:
        return [x for x in findings if x["kind"] == sel]
    return [x for x in findings if not is_bulk(x) or banners]


def comments_view(config, target: str, obs: dict, sel: str = "", limit: int = 500) -> dict:
    """The comments page model: rows in review order (web.review.rank severity, then path), each with its
    suggested edit as removed and added line TEXT read from the analysed commit's local blob
    (quality.review_text: never fetched in a request); "line N" when the blob is not on this host."""
    from nanosoc_rm.quality import review_text as rt
    from nanosoc_rm.quality.findings import BULK_KINDS, BULK_RULE, bulk_rules, is_bulk
    f = obs["fact"]
    shown = rt.rank(selected_findings(f["findings"], sel))
    n_banner = sum(1 for x in f["findings"] if is_bulk(x))
    rule = (f.get("bulk_rules") or bulk_rules(f["findings"]) or [{}])[0]
    try:
        source = rt.local_source(config, target, f)
    except Exception:                                         # noqa: BLE001 - no local copy: line numbers only
        source = None
    if source is not None:
        try:
            source.read_many(sorted({x["path"] for x in shown[:limit] if x.get("edit")}))
        except Exception:                                     # noqa: BLE001
            source = None
    rows, unread = [], 0
    for x in shown[:limit]:
        diff, read = rt.edit_lines_for(source, x)
        if diff and not read:
            unread += 1
        rows.append({"f": x, "severity": rt.severity(x), "diff": diff})
    counts = [(k, n) for k, n in f.get("counts", {}).items() if k not in BULK_KINDS]
    return {"rows": rows, "n_shown": len(shown), "n_edits": sum(1 for x in shown if x.get("edit")),
            "n_banner": n_banner, "worth": f["total"] - n_banner, "kind_counts": counts, "unread": unread,
            "limit": limit, "bulk_rule": BULK_RULE, "bulk_by_kind": rule.get("by_kind") or {},
            "bulk_files": rule.get("files") or 0}


def _diff_response(text: str, name: str) -> Response:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return Response(text, media_type="text/x-diff",
                    headers={"content-disposition": 'attachment; filename="%s"' % safe})


ENV = _env()
