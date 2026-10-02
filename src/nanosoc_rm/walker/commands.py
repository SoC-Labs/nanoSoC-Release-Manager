"""CLI of the pin walker (interfaces.md section 4):

    nanosoc-rm walk CONFIG [--json]                       walk now; store and print every pin with its host states
    nanosoc-rm pins CONFIG [--state S] [--host H] [--json] the latest stored walk (walks first if there is none)

Both exit 1 if any pin is red on its pinned host, or has pin-level state LOCAL_URL or NO_GITLINK (BRANCH_ONLY is red
in a frozen or silicon configuration); 2 if the walk could not answer, or a pin could not be judged and none is red;
3 for an unknown configuration.
"""
from __future__ import annotations

import click

from nanosoc_rm.cliutil import EXIT_UNVERIFIED, EXIT_USAGE, emit, exit_code, json_option

_STATES = ("TAGGED", "ON_DEFAULT", "PROTECTED_BY_TAG", "BRANCH_ONLY", "PR_ONLY", "ORPHAN", "MISSING", "REWRITTEN",
           "LOCAL_URL", "UNREADABLE", "NO_GITLINK")
_COLUMNS = ("tree_path", "depth", "component", "commit", "url", "pinned", "others", "branch_field")


def _host_text(h: dict) -> str:
    text = "%s:%s" % (h["repo"]["host"], h["state"])
    if h["lag"] is not None:
        text += "(lag %d)" % h["lag"]
    return text


def _row(doc: dict) -> dict:
    f = doc["fact"]
    pinned = next((h for h in f["hosts"].values() if h["pinned"]), None)
    others = [h for h in f["hosts"].values() if not h["pinned"]]
    return {"tree_path": f["tree_paths"][0], "depth": f["depth"], "component": f["child"]["component"] or "?",
            "commit": (f["child"]["commit"] or "-")[:12],
            "url": f["url_kind"] + (" (split)" if f["split"] else ""),
            "pinned": f["state"] or (_host_text(pinned) if pinned else "unjudged"),
            "others": ", ".join(_host_text(h) for h in others), "branch_field": f["branch_field"] or ""}


def _exit(docs: list[dict], frozen: bool) -> int:
    from nanosoc_rm.walker import pin_verdict
    verdicts = [pin_verdict(d["fact"], frozen) for d in docs]
    return exit_code(findings="red" in verdicts, unverified="unknown" in verdicts)


def _frozen(configuration_id: str) -> bool:
    from nanosoc_rm.config import load_config
    from nanosoc_rm.mirror.catalogue import load_catalogue
    cfg = load_catalogue(load_config()).configuration(configuration_id)
    return bool(cfg.get("frozen") or cfg.get("silicon"))


def register(main: click.Group) -> None:
    @main.command("walk")
    @click.argument("configuration")
    @json_option
    def walk_cmd(configuration: str, as_json: bool) -> int:
        """Resolve CONFIGURATION's whole submodule tree from its own .gitmodules and gitlinks, with every pin's
        state on each host (B2, B3). Stores the walk."""
        from nanosoc_rm.walker import UnknownConfiguration, WalkError, walk
        try:
            docs = walk(configuration)
        except UnknownConfiguration:
            click.echo("nanosoc-rm: %s is not a registered configuration" % configuration, err=True)
            return EXIT_USAGE
        except WalkError as e:
            click.echo("nanosoc-rm: walk %s could not answer: %s" % (configuration, e), err=True)
            return EXIT_UNVERIFIED
        emit(docs, as_json, _COLUMNS, _row)
        return _exit(docs, _frozen(configuration))

    @main.command("pins")
    @click.argument("configuration")
    @click.option("--state", "state", type=click.Choice(_STATES), default=None,
                  help="Only pins in this state (pin-level, or on --host / any host).")
    @click.option("--host", "host", type=click.Choice(["github", "gitlab"]), default=None,
                  help="Only pins with a state on this host.")
    @json_option
    def pins_cmd(configuration: str, state: str | None, host: str | None, as_json: bool) -> int:
        """The pins of CONFIGURATION from its latest stored walk (walked now if it never was). The exit code
        judges every pin, whatever the filters show."""
        from nanosoc_rm.walker import UnknownConfiguration, WalkError, stored_pins, walk
        try:
            frozen = _frozen(configuration)
        except KeyError:
            click.echo("nanosoc-rm: %s is not a registered configuration" % configuration, err=True)
            return EXIT_USAGE
        docs = stored_pins(configuration)
        if not docs:
            try:
                docs = walk(configuration)
            except UnknownConfiguration:
                click.echo("nanosoc-rm: %s is not a registered configuration" % configuration, err=True)
                return EXIT_USAGE
            except WalkError as e:
                click.echo("nanosoc-rm: walk %s could not answer: %s" % (configuration, e), err=True)
                return EXIT_UNVERIFIED
        shown = docs
        if host is not None:
            shown = [d for d in shown if host in d["fact"]["hosts"]]
        if state is not None:
            shown = [d for d in shown if d["fact"]["state"] == state or any(
                h["state"] == state for k, h in d["fact"]["hosts"].items() if host is None or k == host)]
        emit(shown, as_json, _COLUMNS, _row)
        return _exit(docs, frozen)
