"""`nanosoc-rm executor ...` (interfaces.md section 27.7).

On the EDA host (agent config, $NANOSOC_RM_AGENT_CONFIG or --agent-config):
    executor run --once | --loop     claim and run tasks (exit 0 ran or nothing queued; 2 service unreachable)
    executor check                   the sandbox this host will use, the exposure check, the token file; no network

On the service host (service config, $NANOSOC_RM_CONFIG):
    executor token new NAME --out FILE [--labels a,b]   a new token written to FILE (0600); prints the config entry
    executor enqueue TASK.json       queue a task (exit 3 when it breaks the contract)
    executor list [--state S]        tasks, newest first (expires stale leases first)
    executor show ID                 one task with its result
    executor cancel ID               cancel a queued task / ask a running one to stop
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, json_option, subgroup, table

__all__ = ["register"]


def _agent_option(f):
    return click.option("--agent-config", "agent_config", type=click.Path(dir_okay=False), default=None,
                        help="The agent's TOML (default: $NANOSOC_RM_AGENT_CONFIG).")(f)


def register(main: click.Group) -> None:
    grp = subgroup(main, "executor", help="The remote tier executor: the pull agent and its queue (section 27).")

    @grp.command("run")
    @click.option("--once", "once", is_flag=True, help="Claim at most one task, run it, exit.")
    @click.option("--loop", "loop", is_flag=True, help="Claim and run until SIGTERM or SIGINT.")
    @_agent_option
    def run_cmd(once: bool, loop: bool, agent_config: str | None) -> int:
        """Claim tasks from the release manager and run them sandboxed (on the EDA host)."""
        from nanosoc_rm.executor import agent, sandbox
        from nanosoc_rm.executor.agentconfig import load_agent_config, read_token
        from nanosoc_rm.executor.client import ApiError, ExecutorClient
        if once == loop:
            raise click.UsageError("give exactly one of --once and --loop")
        cfg = load_agent_config(agent_config)
        client = ExecutorClient(cfg, read_token(cfg))
        mode = sandbox.mode_for(cfg)
        try:
            if once:
                res = agent.run_once(cfg, client, mode=mode)
                click.echo("nothing queued" if res is None else "job %d: %s" % (res["job_id"], res.get("verdict"))
                           if "job_id" in res else "task %d: %s" % (res["task_id"], res["verdict"]))
                return EXIT_OK
            stopping = {"now": False}

            def on_signal(signum, frame):  # noqa: ARG001 - signal handler signature
                stopping["now"] = True
            signal.signal(signal.SIGTERM, on_signal)
            signal.signal(signal.SIGINT, on_signal)
            n = agent.run_loop(cfg, client, mode=mode, stop=lambda: stopping["now"])
            click.echo("stopped after %d task(s)" % n)
            return EXIT_OK
        except ApiError as e:
            click.echo("nanosoc-rm executor: %s" % e, err=True)
            return EXIT_USAGE if e.status in (401, 403, 404) else EXIT_UNVERIFIED

    @grp.command("check")
    @_agent_option
    @json_option
    def check_cmd(agent_config: str | None, as_json: bool) -> int:
        """What this host would run with: sandbox mode, exposure check, token file and mirror root (no network)."""
        from nanosoc_rm.config import ConfigError
        from nanosoc_rm.executor import sandbox
        from nanosoc_rm.executor.agentconfig import load_agent_config, read_token
        cfg = load_agent_config(agent_config)
        problems = []
        try:
            mode = sandbox.mode_for(cfg)
        except ConfigError as e:
            mode = "none"
            problems.append(str(e))
        try:
            read_token(cfg)
        except ConfigError as e:
            problems.append(str(e))
        problems += sandbox.exposure(cfg, Path(cfg.scratch_dir) / "task-0")
        if not Path(cfg.mirror_root).is_dir():
            problems.append("mirror_root %s is not a directory" % cfg.mirror_root)
        doc = {"agent": cfg.name, "sandbox": mode, "network": cfg.network, "labels": cfg.labels,
               "ro_binds": [str(b) for b in cfg.ro_binds], "problems": problems}
        if as_json:
            click.echo(json.dumps(doc, separators=(",", ":")))
        else:
            click.echo("agent %s: sandbox %s%s, labels %s" % (cfg.name, mode, "" if cfg.network else " (no network)",
                                                             ", ".join(cfg.labels) or "none"))
            if mode == "fallback":
                click.echo("  WARNING: no usable bubblewrap: the command runs as this user with a scrubbed environment "
                           "only (docs/DEPLOY.md, residual risk)")
            for p in problems:
                click.echo("  problem: %s" % p)
        return EXIT_FINDING if problems else EXIT_OK

    tok = subgroup(grp, "token", help="Executor tokens (service host).")

    @tok.command("new")
    @click.argument("name")
    @click.option("--out", "out", required=True, type=click.Path(dir_okay=False),
                  help="Write the token here (created 0600; must not exist). It is never printed.")
    @click.option("--labels", default="", help="Comma-separated labels to grant (vcs,xcelium,verilator,...).")
    @click.option("--reader", is_flag=True, help="A bump-check READER token instead (a CI job's NANOSOC_RM_TOKEN): "
                                                  "prints an [[api.readers]] entry; it opens GET /api/v1/bump-check "
                                                  "only.")
    def token_new_cmd(name: str, out: str, labels: str, reader: bool) -> int:
        """A new executor token for agent NAME: the token goes to --out, the [[executor.agents]] entry to stdout
        (with --reader: a bump-check reader token and its [[api.readers]] entry)."""
        from nanosoc_rm.executor import tokens
        import re
        if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,63}\Z", name):
            raise click.UsageError("NAME must be lower case letters, digits, _ . -")
        labs = [x.strip() for x in labels.split(",") if x.strip()]
        if reader and labs:
            raise click.UsageError("--labels is for an executor agent; a reader token has none")
        value = tokens.new_token()
        try:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise click.UsageError("%s exists; a token file is never overwritten" % out) from None
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        if reader:
            click.echo("# add to the service config; put the token in %s into the repository's CI secret "
                       "NANOSOC_RM_TOKEN:" % out)
            click.echo("[[api.readers]]")
            click.echo("name = %s" % json.dumps(name))
            click.echo("token_sha256 = %s" % json.dumps(tokens.digest(value)))
            return EXIT_OK
        click.echo("# add to the service config, then copy %s to the agent host (0600):" % out)
        click.echo("[[executor.agents]]")
        click.echo("name = %s" % json.dumps(name))
        click.echo("token_sha256 = %s" % json.dumps(tokens.digest(value)))
        click.echo("labels = %s" % json.dumps(labs))
        return EXIT_OK

    @grp.command("enqueue")
    @click.argument("task_file", type=click.Path(exists=True, dir_okay=False))
    @json_option
    def enqueue_cmd(task_file: str, as_json: bool) -> int:
        """Queue the task in TASK_FILE (executor.schema.json#/$defs/task)."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.executor import queue
        config = load_config()
        try:
            doc = json.loads(Path(task_file).read_text())
        except ValueError as e:
            raise click.UsageError("%s is not JSON: %s" % (task_file, e)) from None
        try:
            tid = queue.enqueue(config, doc)
        except queue.TaskRefused as e:
            for p in e.problems:
                click.echo("refused: %s" % p, err=True)
            return EXIT_USAGE
        click.echo(json.dumps({"task_id": tid}) if as_json else "queued task %d" % tid)
        return EXIT_OK

    def _row(t: dict) -> dict:
        s = t["task"].get("subject") or {}
        return {"id": t["id"], "state": t["state"], "verdict": t["verdict"], "agent": t["agent"],
                "component": s.get("component"), "capability": s.get("capability"),
                "tier": t["task"]["run"]["tier"], "commit": t["task"]["commit"][:12], "created": t["created_at"]}

    @grp.command("list")
    @click.option("--state", type=click.Choice(["queued", "claimed", "done", "lost", "cancelled"]), default=None)
    @click.option("--limit", type=int, default=50)
    @json_option
    def list_cmd(state: str | None, limit: int, as_json: bool) -> int:
        """Executor tasks, newest first."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.executor import queue
        config = load_config()
        queue.expire(config)
        rows = queue.tasks(config, state, limit)
        if as_json:
            for t in rows:
                click.echo(json.dumps(t, separators=(",", ":"), sort_keys=True))
        else:
            click.echo(table([_row(t) for t in rows], ("id", "state", "verdict", "agent", "component", "capability",
                                                       "tier", "commit", "created")))
        return EXIT_OK

    @grp.command("show")
    @click.argument("task_id", type=int)
    @json_option
    def show_cmd(task_id: int, as_json: bool) -> int:
        """One task, with its result and log path."""
        from nanosoc_rm.config import load_config
        from nanosoc_rm.executor import queue
        t = queue.get(load_config(), task_id)
        if t is None:
            click.echo("no executor task %d" % task_id, err=True)
            return EXIT_UNVERIFIED
        if as_json:
            click.echo(json.dumps(t, separators=(",", ":"), sort_keys=True))
            return EXIT_OK
        click.echo(table([_row(t)], ("id", "state", "verdict", "agent", "component", "capability", "tier", "commit",
                                     "created")))
        r = t.get("result") or {}
        if t.get("note"):
            click.echo("note: %s" % t["note"])
        if r:
            click.echo("sandbox %s, %s s, exit %s%s" % (r["sandbox"], r["seconds"], r["exit_code"],
                                                         ", killed" if r["killed"] else ""))
            if r.get("reason"):
                click.echo("reason: %s" % r["reason"])
            click.echo(table(r["rows"], ("status", "gate", "detail")))
        if t.get("log"):
            click.echo("log: %s" % t["log"])
        return EXIT_OK

    @grp.command("cancel")
    @click.argument("task_id", type=int)
    def cancel_cmd(task_id: int) -> int:
        """Cancel a queued task, or ask the agent running it to stop."""
        import getpass
        from nanosoc_rm.config import load_config
        from nanosoc_rm.executor import queue
        try:
            t = queue.cancel(load_config(), task_id, getpass.getuser())
        except queue.NotFound as e:
            click.echo(str(e), err=True)
            return EXIT_UNVERIFIED
        click.echo("task %d: %s%s" % (task_id, t["state"], " (stop asked)" if t["cancel_requested"] else ""))
        return EXIT_OK
