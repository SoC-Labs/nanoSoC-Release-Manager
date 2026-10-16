"""The remote tier executor (interfaces.md section 27; CR-WG-executor, David 2026-10-16 decision 1).

Tier runs need EDA tools the service host does not have. A pull agent on the EDA host claims queued tasks from
the release manager, runs each one sandboxed and unprivileged, and posts the result back. The service never
connects to the agent and never runs a tier command itself.

Server side (in the service process):
    task        the task and result contract, checked on both sides (task.check_task, task.check_result)
    queue       `<data_dir>/executor.sqlite`: enqueue, claim, heartbeat, log, complete, lease expiry
    tokens      executor tokens: generated once, stored as sha256 in `[[executor.agents]]`, revocable
    api         the POST-only executor API at /executor/v1/ (bearer token only; no session, no page)

Agent side (on the EDA host; `nanosoc-rm executor run --once|--loop`):
    agentconfig the agent's own TOML ($NANOSOC_RM_AGENT_CONFIG), not the service config
    client      the HTTP client: only the four executor API paths of the configured server
    checkout    lays the commit under test (and its pins) out from a read-only mirror into scratch
    sandbox     runs the command under bubblewrap, or the documented fallback, with limits
    agent       claim -> checkout -> run -> verdict -> upload log and result
"""
