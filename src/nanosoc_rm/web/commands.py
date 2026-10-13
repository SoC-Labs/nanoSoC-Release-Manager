"""`nanosoc-rm serve` and `nanosoc-rm user add` (interfaces.md sections 0.5, 13a, 14; CR-16, CR-17).

`serve` is the one long-running process. It serves the web app (sections 13, 14), runs the schedules by calling
`nanosoc_rm.schedule.tick(now)` every 30 s (section 12a), and runs the local job worker (CR-17, `nanosoc_rm.jobs`):
Run, Sync and Walk on the pages queue jobs it runs one at a time. The config is `$NANOSOC_RM_CONFIG`, read per
request and per tick. Bind to localhost: behind the lab's reverse proxy (GitHub sign-in) or, in local auth mode
(CR-16), reached through an SSH tunnel. In local mode `serve` refuses a non-loopback address unless
`--i-know-this-is-exposed` is given. The app sets its session cookie `Secure` when the request arrives over https.

`user add LOGIN` (CR-16) prompts for a password (or generates one with `--generate`) and prints the
`[[auth.users]]` stanza for the private config. It writes no file.
"""
from __future__ import annotations

import ipaddress
import secrets
import threading

import click

from nanosoc_rm.cliutil import EXIT_OK, EXIT_USAGE, subgroup

TICK_S = 30
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and localhost. Anything else (0.0.0.0, a LAN address, a name) is exposed."""
    if host.strip("[]").lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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
    @click.option("--no-worker", is_flag=True, help="Do not run local jobs (Run, Sync and Walk stay queued).")
    @click.option("--i-know-this-is-exposed", "exposed", is_flag=True,
                  help="Local auth mode only: allow a non-loopback --host. Without it, serve refuses to bind one.")
    def serve_cmd(host: str, port: int, no_schedule: bool, no_worker: bool, exposed: bool) -> int:
        """Serve the web pages and JSON API, run the schedules (poll, nightly T0, result fetch) and the local job
        worker (CR-17). Exit 3 for a config error or a refused --host."""
        import uvicorn

        from nanosoc_rm.config import load_config
        from nanosoc_rm.web import create_app

        config = load_config()                       # a config error is exit 3 before anything starts
        if config.auth_mode == "local" and not is_loopback(host) and not exposed:
            click.echo("nanosoc-rm serve: [auth] mode = \"local\" serves on loopback only (passwords travel over "
                       "plain http). Use --host 127.0.0.1 and an SSH tunnel, or pass --i-know-this-is-exposed.",
                       err=True)
            return EXIT_USAGE
        stop = threading.Event()
        if not no_schedule:
            threading.Thread(target=_ticker, args=(stop,), name="rm-schedule", daemon=True).start()
        worker = None
        if not no_worker:
            from nanosoc_rm.jobs import Worker
            worker = Worker(None).start()
        click.echo("nanosoc-rm serve: http://%s:%d/ (auth: %s%s)" % (
            host if ":" not in host else "[%s]" % host, port, config.auth_mode,
            ", local jobs" if worker is not None else ""), err=True)
        app = create_app(None)
        if worker is not None:
            # uvicorn runs the app's shutdown handlers on SIGTERM and SIGINT, then re-raises the signal, so code after
            # uvicorn.run() may never run: stop the worker (and the job it runs) in the shutdown handler.
            app.router.on_shutdown.append(worker.stop)
        try:
            uvicorn.run(app, host=host, port=port, proxy_headers=True, log_level="info")
        finally:
            stop.set()
            if worker is not None:
                worker.stop()
        return EXIT_OK

    user = subgroup(main, "user", help="Local sign-in users (CR-16): print config stanzas; never writes a file.")

    @user.command("add")
    @click.argument("login")
    @click.option("--generate", is_flag=True, help="Generate a random password and print it once.")
    @click.option("--role", "roles", multiple=True, help="A role for this user (default: member). Repeatable.")
    def user_add(login: str, generate: bool, roles: tuple[str, ...]) -> int:
        """Print the [[auth.users]] stanza for LOGIN with a scrypt hash of its password. Paste it into the
        private service config and set `[auth] mode = "local"`. Prompts for the password unless --generate."""
        from nanosoc_rm.auth import hash_password
        from nanosoc_rm.config import _LOGIN
        if not _LOGIN.match(login):
            click.echo("nanosoc-rm: %r is not a login (letters, digits, _ . -; at most 64)" % login, err=True)
            return EXIT_USAGE
        bad = [r for r in roles if not _LOGIN.match(r)]
        if bad:                                  # printed into TOML: a quote would add config keys
            click.echo("nanosoc-rm: %r is not a role name (letters, digits, _ . -; at most 64)" % bad[0], err=True)
            return EXIT_USAGE
        if generate:
            password = secrets.token_urlsafe(15)
        else:
            password = click.prompt("Password for %s" % login, hide_input=True, confirmation_prompt=True)
            if len(password) < 8:
                click.echo("nanosoc-rm: use at least 8 characters", err=True)
                return EXIT_USAGE
        lines = ["[[auth.users]]", 'login = "%s"' % login, 'hash = "%s"' % hash_password(password)]
        if roles:
            lines.append("roles = [%s]" % ", ".join('"%s"' % r for r in roles))
        click.echo("\n".join(lines))
        if generate:
            click.echo("# password for %s (shown once): %s" % (login, password), err=True)
        click.echo("# add the stanza above to the private config, with [auth] mode = \"local\"", err=True)
        return EXIT_OK
