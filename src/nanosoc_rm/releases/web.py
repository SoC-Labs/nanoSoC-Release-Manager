"""The release pages (CR-R1): a FastAPI router mounted at /releases through the plug-in seam (web/plugins.py).

    GET  /releases/                          drafts list, the new-draft form, recent release jobs
    GET  /releases/forks                     the latest fork listing (I1), and a button to list them again
    GET  /releases/{configuration}/{version}               one draft: checks, person steps, train, files, pins
    GET  /releases/{configuration}/{version}/errata        its errata document (F4) in the GUI's shell
    GET  /releases/{configuration}/{version}/download.zip  the whole draft folder
    GET  /releases/{configuration}/{version}/files/{name}  one file of it, as text
    POST /releases/jobs                      queue `release draft` (local deployment only)
    POST /releases/{configuration}/{version}/verify        queue `release verify`
    POST /releases/forks/jobs                queue `forks`

Every page says that nothing is published: the tool never tags, pushes or uploads. Drafting, verifying and
listing forks are long work, so each is a job in the local worker (nanosoc_rm.jobs), never run inside a request.
The POST routes answer 404 in a deployment without local actions (GitHub sign-in with a CI project, section 13a);
their pages then show the CLI command instead. Input files for a draft (archive, GDS, netlist) can be chosen only
from `<data_dir>/release-inputs/`, a folder a person fills on the host: a page never names an arbitrary path.
"""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from nanosoc_rm import jobs
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.releases.core import DRAFT_FILE, PUBLISH_NOTE, SEMVER, is_safe_id, releases_dir

__all__ = ["router", "PREFIX", "NAV_LABEL", "NAV_PATH", "INPUTS", "inputs_dir"]

PREFIX = "/releases"
NAV_LABEL = "Releases"
NAV_PATH = PREFIX + "/"
INPUTS = "release-inputs"
JOB_KINDS = ("release-draft", "release-verify", "forks")
HERE = Path(__file__).resolve().parent
_WEB_TEMPLATES = HERE.parent / "web" / "templates"
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}$")


def _env() -> Environment:
    loaders = [FileSystemLoader(str(HERE / "templates"))]
    if _WEB_TEMPLATES.is_dir():
        loaders.append(FileSystemLoader(str(_WEB_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    from nanosoc_rm.web.page import env as web_env
    env.globals.update(web_env.globals)
    env.globals.update(PUBLISH_NOTE=PUBLISH_NOTE, rurl=_rurl,
                       PROTECTION_CLASS={"release-tag": "state-green", "keep-tag": "state-green",
                                         "default-branch": "state-amber", "none": "state-red",
                                         "not-held": "state-red"})
    return env


def _rurl(configuration: str, version: str, page: str = "") -> str:
    return "%s/%s/%s%s" % (PREFIX, urllib.parse.quote(configuration, safe=""), urllib.parse.quote(version, safe=""),
                           ("/" + page) if page else "")


def _config(request: Request) -> Config:
    cfg = getattr(request.state, "config", None)
    return cfg if cfg is not None else load_config()


def inputs_dir(config: Config) -> Path:
    return config.data_dir / INPUTS


def _inputs(config: Config) -> list[str]:
    d = inputs_dir(config)
    return sorted(p.name for p in d.iterdir() if p.is_file() and _NAME.match(p.name)) if d.is_dir() else []


def _local(request: Request) -> bool:
    return bool(getattr(request.app.state.rm, "local_routes", False))


def _page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    from nanosoc_rm.web import plugins
    return plugins.render_page(request, template, title, status=status, environment=ENV, local=_local(request), **ctx)


def _job_html(request: Request, config: Config, job: dict | None) -> Markup:
    """One job card (the app's own _job.html), polling /jobs/{id}/fragment while it runs."""
    if job is None:
        return Markup("")
    from nanosoc_rm.web.page import Page
    p = Page()
    ident = getattr(request.state, "identity", None)
    csrf = request.app.state.rm.csrf_for(ident, config) if ident is not None else ""
    return Markup(ENV.get_template("_job.html").render(
        job=job, log=jobs.log_text(config, job["id"]), position=jobs.queue_position(config, job["id"]), csrf=csrf,
        fact=p.fact, page=p, stored=None))


def _recent(config: Config, kind: str | None = None, limit: int = 10) -> list[dict]:
    try:
        rows = jobs.jobs(config, limit=200)
    except Exception:                                   # noqa: BLE001 - no job store yet: no jobs
        return []
    return [j for j in rows if j["kind"] in ((kind,) if kind else JOB_KINDS)][:limit]


def _draft_dir(config: Config, configuration: str, version: str) -> Path | None:
    if not is_safe_id(configuration) or not SEMVER.match(version or ""):
        return None
    d = releases_dir(config) / configuration / version
    return d if (d / DRAFT_FILE).is_file() else None


def _read(d: Path) -> tuple[dict | None, str]:
    from nanosoc_rm import contracts
    try:
        doc = json.loads((d / DRAFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, "draft.json does not read: %s" % e
    problems = contracts.validate_observation(doc)
    return (doc, "") if not problems else (None, "draft.json does not meet its contract: %s" % "; ".join(problems[:3]))


def _lock(d: Path) -> dict:
    from nanosoc_rm import contracts
    for name in ("release.lock.yaml", "release.lock.draft.yaml"):
        if (d / name).is_file():
            try:
                return contracts.load_yaml((d / name).read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                return {}
    return {}


async def _submit(request: Request, kind: str, params: dict, back: str) -> Response:
    if not _local(request):
        return PlainTextResponse("404 Not Found: this deployment runs no local jobs; use the CLI", 404)
    config = _config(request)
    ident = getattr(request.state, "identity", None)
    try:
        norm = jobs._params(kind, dict(params))
    except jobs.JobUsage as e:
        return PlainTextResponse("400 Bad Request: %s" % e, 400)
    for j in _recent(config, kind, limit=200):
        if j["state"] in ("queued", "running") and j["params"] == norm:
            return RedirectResponse(back + ("&" if "?" in back else "?") + "job=%d" % j["id"], 303)
    try:
        job = await run_in_threadpool(jobs.submit, config, kind, params, getattr(ident, "login", "") or "")
    except jobs.JobUsage as e:
        return PlainTextResponse("400 Bad Request: %s" % e, 400)
    return RedirectResponse(back + ("&" if "?" in back else "?") + "job=%d" % job["id"], 303)


def router() -> APIRouter:
    r = APIRouter(prefix=PREFIX)

    def index(request: Request) -> HTMLResponse:
        from nanosoc_rm.releases.builder import drafts
        config = _config(request)
        rows = drafts(config)
        q = request.query_params
        job = jobs.get(config, int(q["job"])) if (q.get("job") or "").isdigit() else None
        try:
            from nanosoc_rm.releases.builder import _registry
            configs = [c["id"] for c in _registry(config).configurations()]
            problems = []
        except Exception as e:                          # noqa: BLE001 - the form needs the registry; say why not
            configs, problems = [], ["the registry does not load: %s" % str(e)[:300]]
        return _page(request, "releases_index.html", "Releases", problems=problems, rows=rows, configs=configs,
                     inputs=_inputs(config), inputs_path=INPUTS, job_html=_job_html(request, config, job),
                     recent=_recent(config))

    async def post_draft(request: Request) -> Response:
        form = await request.form()
        config = _config(request)
        names = set(_inputs(config))

        def pick(key: str) -> str | None:
            v = str(form.get(key) or "")
            return str(inputs_dir(config) / v) if v and v in names else None

        params = {"configuration": str(form.get("configuration") or ""), "version": str(form.get("version") or ""),
                  "kind": str(form.get("kind") or "") or None, "archive": pick("archive"), "gds": pick("gds"),
                  "netlist": pick("netlist"),
                  "waivers": [ln.strip() for ln in str(form.get("waivers") or "").splitlines() if ln.strip()],
                  "replace": bool(form.get("replace"))}
        return await _submit(request, "release-draft", params, PREFIX + "/")

    def forks_page(request: Request) -> HTMLResponse:
        config = _config(request)
        q = request.query_params
        job = jobs.get(config, int(q["job"])) if (q.get("job") or "").isdigit() else None
        last = next((j for j in _recent(config, "forks", 50) if j["state"] == "done"), None)
        from nanosoc_rm import db
        try:
            docs = sorted(db.latest("fork_status", config=config), key=lambda d: d["subject"])
        except Exception:                               # noqa: BLE001 - no store yet: nothing listed
            docs = []
        return _page(request, "releases_forks.html", "Forks of the example projects", docs=docs, last=last,
                     job_html=_job_html(request, config, job))

    async def post_forks(request: Request) -> Response:
        form = await request.form()
        return await _submit(request, "forks", {"component": str(form.get("component") or "") or None},
                             PREFIX + "/forks")

    def draft_page(request: Request, configuration: str, version: str) -> HTMLResponse:
        config = _config(request)
        d = _draft_dir(config, configuration, version)
        if d is None:
            return _page(request, "releases_message.html", "Not found", status=404,
                         message="No release draft %s %s here." % (configuration, version))
        doc, problem = _read(d)
        if doc is None:
            return _page(request, "releases_message.html", "Unreadable draft", status=503, message=problem)
        q = request.query_params
        job = jobs.get(config, int(q["job"])) if (q.get("job") or "").isdigit() else None
        verifies = [j for j in _recent(config, "release-verify", 50)
                    if j["params"].get("configuration") == configuration and j["params"].get("version") == version]
        return _page(request, "releases_draft.html", "Release %s %s" % (configuration, version), doc=doc,
                     f=doc["fact"], lock=_lock(d), configuration=configuration, version=version,
                     job_html=_job_html(request, config, job or (verifies[0] if verifies else None)))

    async def post_verify(request: Request, configuration: str, version: str) -> Response:
        config = _config(request)
        if _draft_dir(config, configuration, version) is None:
            return PlainTextResponse("404 Not Found: no such draft", 404)
        return await _submit(request, "release-verify", {"configuration": configuration, "version": version},
                             _rurl(configuration, version))

    def errata_page(request: Request, configuration: str, version: str) -> HTMLResponse:
        config = _config(request)
        d = _draft_dir(config, configuration, version)
        if d is None or not (d / "ERRATA.html").is_file():
            return _page(request, "releases_message.html", "Not found", status=404,
                         message="No errata document for %s %s here." % (configuration, version))
        text = (d / "ERRATA.html").read_text(encoding="utf-8")
        body = text.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
        body = re.sub(r"^\s*<h1>.*?</h1>", "", body, count=1, flags=re.S)
        doc, _ = _read(d)
        return _page(request, "releases_errata.html", "Errata: %s %s" % (configuration, version),
                     errata=Markup(body), configuration=configuration, version=version, doc=doc)

    def download(request: Request, configuration: str, version: str) -> Response:
        config = _config(request)
        d = _draft_dir(config, configuration, version)
        if d is None:
            return PlainTextResponse("404 Not Found: no such draft", 404)
        buf = io.BytesIO()
        top = "%s-%s" % (configuration, version)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(d.iterdir()):
                if p.is_file() and not p.is_symlink():
                    z.write(p, "%s/%s" % (top, p.name))
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"content-disposition": 'attachment; filename="release-draft-%s.zip"' % top})

    def file_view(request: Request, configuration: str, version: str, name: str) -> Response:
        config = _config(request)
        d = _draft_dir(config, configuration, version)
        if d is None or not _NAME.match(name) or not (d / name).is_file() or (d / name).is_symlink():
            return PlainTextResponse("404 Not Found: no such file in the draft", 404)
        p = d / name
        if name.endswith((".gz", ".tgz", ".tar", ".zip")):
            return Response(p.read_bytes(), media_type="application/octet-stream",
                            headers={"content-disposition": 'attachment; filename="%s"' % name})
        return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))

    get = ["GET"]
    r.add_api_route("/", index, methods=get, include_in_schema=False)
    r.add_api_route("/forks", forks_page, methods=get, include_in_schema=False)
    r.add_api_route("/jobs", post_draft, methods=["POST"], include_in_schema=False)
    r.add_api_route("/forks/jobs", post_forks, methods=["POST"], include_in_schema=False)
    r.add_api_route("/{configuration}/{version}", draft_page, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/{version}/errata", errata_page, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/{version}/download.zip", download, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/{version}/files/{name}", file_view, methods=get, include_in_schema=False)
    r.add_api_route("/{configuration}/{version}/verify", post_verify, methods=["POST"], include_in_schema=False)
    return r


ENV = _env()
