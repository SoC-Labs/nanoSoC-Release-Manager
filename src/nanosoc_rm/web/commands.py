"""`nanosoc-rm serve` (interfaces.md section 0.5): the one long-running process.

It serves the web app (sections 13, 14) and runs the schedules by calling `nanosoc_rm.schedule.tick(now)` every
30 s (section 12a). The config is `$NANOSOC_RM_CONFIG`, read per request and per
tick. Bind to localhost behind the lab's reverse proxy; the app sets its session cookie `Secure` when the
request arrives over https.
"""
from __future__ import annotations

import threading

import click

from nanosoc_rm.cliutil import EXIT_OK

TICK_S = 30


def _ticker(stop: threading.Event) -> None:
    from nanosoc_rm import clock
    from nanosoc_rm.schedule import tick
    while not stop.is_set():
        try:
            tick(clock.now())
        except Exception as e:                       # noqa: BLE001 - a failed tick is reported, the next one runs
            click.echo("nanosoc-rm serve: tick failed: %s" % e, err=True)
        stop.wait(TICK_S)


def register(main: click.Group) -> None:
    @main.command("serve")
    @click.option("--host", default="127.0.0.1", show_default=True, help="Address to bind.")
    @click.option("--port", default=8000, show_default=True, type=int, help="Port to bind.")
    @click.option("--no-schedule", is_flag=True, help="Serve pages only; do not run schedule.tick().")
    def serve_cmd(host: str, port: int, no_schedule: bool) -> int:
        """Serve the web pages and JSON API, and run the schedules (poll, nightly T0, result fetch)."""
        import uvicorn

        from nanosoc_rm.config import load_config
        from nanosoc_rm.web import create_app

        load_config()                                # a config error is exit 3 before anything starts
        stop = threading.Event()
        if not no_schedule:
            threading.Thread(target=_ticker, args=(stop,), name="rm-schedule", daemon=True).start()
        try:
            uvicorn.run(create_app(None), host=host, port=port, proxy_headers=True, log_level="info")
        finally:
            stop.set()
        return EXIT_OK
