"""The core-router seam (Wave C, CR-W1): the workspace modules that own the app's main pages.

    ROUTERS          the core page modules, in mount order (inbox, configuration workspace, component workspace)
    WAVE_C_POSTS     the exact set of POST routes they may add (spec section 8), for the route census
    mount(app)       import each module and include its router; called by create_app BEFORE the app's own GET
                     routes, so `/configurations/{id}/tree` is matched before the catch-all `/configurations/{id}`
    optional(name)   the module `nanosoc_rm.web.<name>` when it is built here, else None (the app then serves its
                     older page, and the shell renders an empty block)
    route_table(app) every (method, path) the app serves, INCLUDING routes of included routers (FastAPI keeps an
                     included router as one entry with no path or methods; a census over `app.routes` alone sees
                     none of a plug-in's routes)

A module in ROUTERS defines `router`: an APIRouter, or a factory taking the app. A factory registers its POST
routes only when `app.state.rm.local_routes` is true (section 13a), so a GitHub + CI deployment keeps exactly the
two Run routes (D5). A module that is not built is skipped; one that is built but fails to import is an error,
never hidden (the rule of web/plugins.py).
"""
from __future__ import annotations

import importlib
import inspect
from types import ModuleType
from typing import Any

__all__ = ["ROUTERS", "WAVE_C_POSTS", "DELEGATES", "mount", "optional", "route_table", "writes"]

ROUTERS = ("nanosoc_rm.web.inbox", "nanosoc_rm.web.cfgspace", "nanosoc_rm.web.compspace")

# The app's older pages hand over to the workspace module that owns them (spec 1.7), without app.py changing
# again: app handler -> (module under nanosoc_rm.web, function). The function takes the request (and the path
# parameter) and returns a Response, or None to let the older page answer.
DELEGATES = {
    "dashboard": ("inbox", "dashboard"),            # GET /
    "alerts": ("inbox", "alerts"),                  # GET /alerts
    "configuration": ("cfgspace", "configuration"),  # GET /configurations/{id}
    "compat": ("cfgspace", "compat"),               # GET /compat (?job= and ?configuration= 303 to the workspace)
    "component": ("compspace", "component"),        # GET /components/{id}
    "branches": ("compspace", "branches"),          # GET /branches (?component= 303 to the workspace)
}

# Spec section 8: every Wave C POST, all local-only and CSRF-checked; none accepts a result.
WAVE_C_POSTS = frozenset({
    ("POST", "/inbox/done"),
    ("POST", "/inbox/snooze"),
    ("POST", "/inbox/undo"),
    ("POST", "/alerts/clear-group"),
    ("POST", "/components/{id}/review/decide"),
    ("POST", "/components/{id}/review/basket"),
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
