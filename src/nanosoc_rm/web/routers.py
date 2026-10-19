"""How the app's routes are registered: its own route modules, then the workspace routers, then the plug-ins.

    CORE             the app's own route modules (web/routes/), each listing its routes in ROUTES
    Route            one row of ROUTES: method, path, handler, and whether it exists only in a local deployment
    mount_core(app)  register every CORE route; a local-only route only when `app.state.rm.local_routes`
    ROUTERS          the workspace modules that own the main pages (inbox, configuration workspace, component
                     workspace, projects home, notifications), in mount order
    WAVE_C_POSTS     the exact set of POST routes the workspace modules may add (spec section 8), for the census
    WAVE_F_POSTS     the same for the home and notification modules
    mount(app)       import each workspace module and include its router; called BEFORE mount_core, so
                     `/configurations/{id}/tree` is matched before the catch-all `/configurations/{id}`
    optional(name)   the module `nanosoc_rm.web.<name>` when it is built here, else None (the app then serves its
                     older page, and the shell renders an empty block)
    route_table(app) every (method, path) the app serves, INCLUDING routes of included routers (FastAPI keeps an
                     included router as one entry with no path or methods; a census over `app.routes` alone sees
                     none of a plug-in's routes)

A module in ROUTERS defines `router`: an APIRouter, or a factory taking the app. A factory registers its POST
routes only when `app.state.rm.local_routes` is true (section 13a), so a GitHub + CI deployment keeps exactly the
two Run routes (D5). A module that is not built is skipped; one that is built but fails to import is an error,
never hidden (the rule of web/plugins.py). A CORE module is never optional: a missing one is an import error.
"""
from __future__ import annotations

import importlib
import inspect
from types import ModuleType
from typing import Any, Callable, NamedTuple

__all__ = ["CORE", "Route", "mount_core", "ROUTERS", "WAVE_C_POSTS", "WAVE_F_POSTS", "DELEGATES", "mount", "optional", "route_table", "writes"]

CORE = ("nanosoc_rm.web.routes.pages", "nanosoc_rm.web.routes.actions", "nanosoc_rm.web.routes.api",
        "nanosoc_rm.web.routes.signin")


class Route(NamedTuple):
    method: str
    path: str
    endpoint: Callable
    local_only: bool = False          # section 13a: registered only in a local deployment


def mount_core(app: Any, modules: tuple[str, ...] = CORE) -> None:
    """Register the CORE routes on the app itself (not as included routers), every GET before any POST, in
    module order: the order the app has always had, so a same-path GET still answers before its POST."""
    local = bool(getattr(app.state.rm, "local_routes", False))
    table = [r for m in modules for r in importlib.import_module(m).ROUTES if local or not r.local_only]
    for r in sorted(table, key=lambda r: r.method != "GET"):
        app.add_api_route(r.path, r.endpoint, methods=[r.method], include_in_schema=False)


ROUTERS = ("nanosoc_rm.web.inbox", "nanosoc_rm.web.cfgspace", "nanosoc_rm.web.compspace", "nanosoc_rm.web.home",
           "nanosoc_rm.web.notifications")

# The app's older pages hand over to the workspace module that owns them (spec 1.7): app handler -> (module
# under nanosoc_rm.web, function). The function takes the request (and the path
# parameter) and returns a Response, or None to let the older page answer.
DELEGATES = {
    "dashboard": ("home", "dashboard"),             # GET / (the projects home)
    "alerts": ("inbox", "alerts"),                  # GET /alerts
    "configuration": ("cfgspace", "configuration"),  # GET /configurations/{id}
    "compat": ("cfgspace", "compat"),               # GET /compat (?job= and ?configuration= 303 to the workspace)
    "component": ("compspace", "component"),        # GET /components/{id}
    "branches": ("compspace", "branches"),          # GET /branches (?component= 303 to the workspace)
}

# Spec section 8: every workspace POST, all local-only and CSRF-checked; none accepts a result.
WAVE_C_POSTS = frozenset({
    ("POST", "/inbox/done"),
    ("POST", "/inbox/snooze"),
    ("POST", "/inbox/undo"),
    ("POST", "/alerts/clear-group"),
    ("POST", "/components/{id}/review/decide"),
    ("POST", "/components/{id}/review/basket"),
})


# The home and notification POSTs, all local-only and CSRF-checked; each writes gui.sqlite (one login's view
# state) and nothing else. The catalogue page's POST is a plug-in route (nanosoc_rm.catalogue.web).
WAVE_F_POSTS = frozenset({
    ("POST", "/home/timeline"),
    ("POST", "/notifications/read"),
    ("POST", "/notifications/read-all"),
    ("POST", "/notifications/prefs"),
})


def _import(modname: str) -> ModuleType | None:
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as e:
        parts = modname.split(".")
        if e.name in {".".join(parts[:i]) for i in range(1, len(parts) + 1)}:
            return None                                  # not built here
        raise


def optional(name: str) -> ModuleType | None:
    """`nanosoc_rm.web.<name>` when it exists, else None."""
    return _import("nanosoc_rm.web." + name)


def mount(app: Any, modules: tuple[str, ...] = ROUTERS) -> list[str]:
    """Include every built module's router. Returns the names mounted."""
    done = []
    for modname in modules:
        mod = _import(modname)
        if mod is None:
            continue
        router = getattr(mod, "router", None)
        if router is None:
            raise RuntimeError("%s has no `router`" % modname)
        if not hasattr(router, "routes") and callable(router):
            router = router(app) if inspect.signature(router).parameters else router()
        app.include_router(router)
        done.append(modname)
    return done


def _walk(routes: Any, prefix: str = "") -> list[tuple[str, frozenset[str], Any]]:
    out = []
    for r in routes:
        sub = getattr(r, "original_router", None)
        if sub is not None:                              # an included router: its routes carry their own prefix
            ctx = getattr(r, "include_context", None)
            out += _walk(sub.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        path = getattr(r, "path", None)
        if path is None:
            continue
        out.append((prefix + path, frozenset(getattr(r, "methods", None) or ()), r))
    return out


def route_table(app: Any) -> list[tuple[str, frozenset[str], Any]]:
    """(path, methods, route) for every route the app serves, included routers flattened."""
    return _walk(app.routes)


def writes(app: Any) -> set[tuple[str, str]]:
    """Every (method, path) the app serves with a method other than GET, HEAD or OPTIONS."""
    return {(m, p) for p, methods, _ in route_table(app) for m in methods if m not in ("GET", "HEAD", "OPTIONS")}
