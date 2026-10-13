"""The web plug-in seam: optional modules that add pages to the GUI without editing app.py (Q6 page shell).

A plug-in is a module listed in PLUGINS that defines:

    router     a FastAPI APIRouter with its own routes (GET pages; any POST is CSRF-checked by the app's guard),
               or a function that returns one: with no arguments, or with one (the app, so it can register its
               POST routes only in a local deployment, `app.state.rm.local_routes`, as section 13a requires)
    NAV_LABEL  optional: the top-navigation label (default: the PLUGINS label)
    NAV_PATH   optional: where the navigation entry links (default: the router's prefix); the entry is marked
               current on the prefix and every path under it

It renders its pages inside the app's shell with `render_page(request, template, title, environment=..., **ctx)`:
the same layout, sign-in, CSRF token, data age and fact hooks (`fact(obs, label)`) as every other page.

Every route a plug-in adds sits behind the same guard (sign-in, roles, CSRF on POST) because the guard is ASGI
middleware in front of the whole router. A plug-in that is not installed is skipped; one that is installed but
fails to import is an error, never hidden.
"""
from __future__ import annotations

import importlib
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse

__all__ = ["PLUGINS", "load", "render_page"]

# (module, navigation label)
PLUGINS = (("nanosoc_rm.quality.web", "Quality"),
           ("nanosoc_rm.versioning.web", "Versions"))                   # CR-VER (Wave B versioning)


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
            import inspect
            takes_app = bool(inspect.signature(router).parameters)
            router = router(app) if takes_app else router()   # a factory, as nanosoc_rm.quality.web defines it
        app.include_router(router)
        prefix = getattr(router, "prefix", "") or "/"
        path = getattr(mod, "NAV_PATH", None) or prefix
        nav.append((getattr(mod, "NAV_LABEL", label), path, (prefix,)))
    return nav


def render_page(request: Request, template: str, title: str, *, status: int = 200, environment: Any = None,
                problems: list[str] | None = None, **ctx: Any) -> HTMLResponse:
    """A plug-in page in the app's shell (layout, navigation, CSRF token, data age, fact hooks)."""
    return request.app.state.rm.page(request, template, title, status=status, problems=problems,
                                     environment=environment, **ctx)
