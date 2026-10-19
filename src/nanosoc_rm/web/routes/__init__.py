"""The app's own routes (interfaces.md sections 13, 13a and 14), one module per area.

    pages     the HTML pages: every GET a person opens, and the job card and log polled from them
    actions   the POSTs that start or change work: Run T0, local jobs, cancel, refresh, clear an alert
    api       the JSON API (section 13): every line an observation
    signin    sign-in, the GitHub callback and sign-out (section 14)
    common    what they share: the page shell, the request's config and identity, job cards, session cookies

Each route module lists its routes in `ROUTES` (`routers.Route` rows). `routers.mount_core` registers them on the
app, so create_app holds no route of its own and a route's module is where its handler lives.
"""
