"""The web plug-in seam: optional modules that add pages to the GUI without editing app.py (Q6 page shell).

A plug-in is a module listed in PLUGINS that defines:

    router     a FastAPI APIRouter with its own routes (GET pages; any POST is CSRF-checked by the app's guard),
               or a function that returns one: with no arguments, or with one (the app, so it can register its
               POST routes only in a local deployment, `app.state.rm.local_routes`, as section 13a requires)
    NAV_LABEL  optional: the top-navigation label (default: the PLUGINS label); None adds no navigation entry (a
               module that keeps routes but no page of its own, as the retired /freshness)
    NAV_PATH   optional: where the navigation entry links (default: the router's prefix); the entry is marked
               current on the prefix and every path under it

It renders its pages inside the app's shell with `render_page(request, template, title, environment=..., **ctx)`:
the same layout, sign-in, CSRF token, data age and fact hooks (`fact(obs, label)`) as every other page.

The helpers every plug-in needs live here, so a new plug-in imports them instead of copying one:

    template_env(here, ...)   its Jinja environment: its own templates/, then the app's
    renderer(env, local=)     its `_page(request, template, title, status=200, **ctx)`
    config_of(request)        the config the guard read for this request
    login_of(request)         the signed-in login ("" when none)
    is_local(request)         True in a local deployment (local POST routes registered, section 13a)
    job_card(env, request, ...) one job's card (the app's _job.html), polling while the job runs
    public_text(config, text) an error message as a page may show it: the service's own data and registry
                              directories named, never printed as paths

Every route a plug-in adds sits behind the same guard (sign-in, roles, CSRF on POST) because the guard is ASGI
middleware in front of the whole router. A plug-in that is not installed is skipped; one that is installed but
fails to import is an error, never hidden.
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Callable

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import HTMLResponse

__all__ = ["PLUGINS", "load", "render_page", "local_routes", "template_env", "renderer", "config_of", "login_of",
           "is_local", "job_card", "public_text", "WEB_TEMPLATES"]

WEB_TEMPLATES = Path(__file__).resolve().parent / "templates"

# (module, navigation label)
PLUGINS = (("nanosoc_rm.quality.web", "Quality"), ("nanosoc_rm.matrix.web", "Matrix"),
           ("nanosoc_rm.bumps.web", "Bumps"),
           ("nanosoc_rm.versioning.web", "Versions"),
           ("nanosoc_rm.releases.web", "Releases"),
           ("nanosoc_rm.store.web", "Store"),                           # section 22
           ("nanosoc_rm.publish.web", "Publish"),
           ("nanosoc_rm.freshness.web", "Freshness"),                   # section 24; page retired: redirects + POST
           ("nanosoc_rm.catalogue.web", "Catalogue"),
           ("nanosoc_rm.capabilities.web", "Capabilities"))             # no navigation entry (NAV_LABEL None)


def local_routes(app: Any) -> bool:
    """True when `app` registers local-deployment POST routes (section 13a); False for no app (a test building
    a router alone) and for a GitHub + CI deployment."""
    return bool(getattr(getattr(getattr(app, "state", None), "rm", None), "local_routes", False))


def load(app: Any, plugins=PLUGINS) -> list[tuple[str, str, tuple[str, ...]]]:
    """Mount every installed plug-in's router on `app`. Returns their navigation entries."""
    nav = []
    for modname, label in plugins:
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            parts = modname.split(".")
            if e.name in {".".join(parts[:i]) for i in range(1, len(parts) + 1)}:
                continue                                 # not built (or not installed) here
            raise
        router = getattr(mod, "router", None)
        if router is None:
            raise RuntimeError("%s has no `router`" % modname)
        if not hasattr(router, "routes") and callable(router):
            takes_app = bool(inspect.signature(router).parameters)
            router = router(app) if takes_app else router()   # a factory, as nanosoc_rm.quality.web defines it
        app.include_router(router)
        if getattr(mod, "NAV_LABEL", label) is None:
            continue                                     # routes only, no navigation entry
        prefix = getattr(router, "prefix", "") or "/"
        path = getattr(mod, "NAV_PATH", None) or prefix
        nav.append((getattr(mod, "NAV_LABEL", label), path, (prefix,)))
    return nav


def render_page(request: Request, template: str, title: str, *, status: int = 200, environment: Any = None,
                problems: list[str] | None = None, **ctx: Any) -> HTMLResponse:
    """A plug-in page in the app's shell (layout, navigation, CSRF token, data age, fact hooks)."""
    return request.app.state.rm.page(request, template, title, status=status, problems=problems,
                                     environment=environment, **ctx)


def template_env(here: Path, *, app_templates: bool = True, app_globals: bool = False, **globals: Any) -> Environment:
    """A plug-in's Jinja environment: `here/templates` first, then (app_templates) the app's own templates, which
    hold the shared partials (_fp.html, _job.html, _fmatrix.html). app_globals adds the app environment's
    globals (its filters and fact helpers) before the plug-in's own `globals`."""
    loaders = [FileSystemLoader(str(Path(here) / "templates"))]
    if app_templates:
        loaders.append(FileSystemLoader(str(WEB_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)
    if app_globals:
        from nanosoc_rm.web.page import env as web_env
        env.globals.update(web_env.globals)
    env.globals.update(globals)
    return env


def renderer(environment: Environment, *, local: bool = False) -> Callable[..., HTMLResponse]:
    """A plug-in's `_page(request, template, title, status=200, **ctx)`: render_page with its environment, and
    with `local=is_local(request)` in the context when `local` is set (templates that show local-only buttons)."""
    def page(request: Request, template: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        if local:
            return render_page(request, template, title, status=status, environment=environment,
                               local=is_local(request), **ctx)
        return render_page(request, template, title, status=status, environment=environment, **ctx)
    return page


def config_of(request: Request) -> Any:
    """The config the guard read for this request; read afresh when the request did not pass the guard (a
    router used without the app, in a test)."""
    cfg = getattr(request.state, "config", None)
    if cfg is None:
        from nanosoc_rm.config import load_config
        cfg = load_config()
    return cfg


def login_of(request: Request) -> str:
    """The signed-in login, "" when there is none."""
    return str(getattr(getattr(request.state, "identity", None), "login", "") or "")


def is_local(request: Request) -> bool:
    """True when this app registers local-deployment POST routes (section 13a)."""
    return local_routes(request.app)


def job_card(environment: Environment, request: Request, config: Any, job: dict | None) -> Markup:
    """One job's card (the app's _job.html, rendered in the plug-in's `environment`), polling
    /jobs/{id}/fragment while it runs; empty for no job."""
    if job is None:
        return Markup("")
    from nanosoc_rm import jobs
    from nanosoc_rm.web.page import Page
    p = Page()
    ident = getattr(request.state, "identity", None)
    csrf = request.app.state.rm.csrf_for(ident, config) if ident is not None else ""
    return Markup(environment.get_template("_job.html").render(
        job=job, log=jobs.log_text(config, job["id"]), position=jobs.queue_position(config, job["id"]), csrf=csrf,
        fact=p.fact, page=p, stored=None))


def public_text(config: Any, text: Any) -> str:
    """`text` with the service's data directory and registry checkout written as words, so an error page never
    shows the host's filesystem layout (the stored fact and the service log keep the real path)."""
    out = str(text)
    for key, word in ((("service", "data_dir"), "<data dir>"), (("registry", "path"), "<registry>")):
        try:
            path = (config.get(key[0]) or {}).get(key[1])
        except Exception:                                            # noqa: BLE001 - no config: nothing to hide
            path = None
        if path:
            for form in {str(path), str(Path(str(path)).expanduser().resolve())}:
                if len(form) > 1:
                    out = out.replace(form.rstrip("/"), word)
    return out
