# 27. The remote tier executor (CR-WG-executor; D1, D4, A2, A6, PR6)

Section 27 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Tier runs need the EDA tools that only the EDA login host has (VCS, Xcelium, Verilator, Genus); the service host has
none. A **pull agent** on the EDA host claims work from the service,
runs it **sandboxed and unprivileged**, and posts the result back; GitLab CI may take its place later. The service
never connects to the agent, never runs a tier command, and never hands the agent a credential.

Package `nanosoc_rm.executor`. Two queues, one transport:

| Queue | What one entry is | Producer | Where it is kept |
|---|---|---|---|
| capability **task** (27.2, 27.3) | one command of one tree at one commit, self-contained (mirror ids, commits, pins, command, budget, verdict rule) | `executor.queue.enqueue` (the capabilities unit's planner, `nanosoc-rm executor enqueue`) | `<data_dir>/executor.sqlite` |
| tier **job** (27.6) | a whole `nanosoc-rm` command of an executor job kind (`test-bump`, `compat --tier`, `matrix cell --tier`; section 26) | the local job queue (`jobs.submit`) | `<data_dir>/jobs.sqlite` (section 13a), plus a lease row in `executor.sqlite` |

## 27.1 Who owns what (the agreement with the capabilities unit, section 26)

- Section 26 (capabilities) owns WHAT runs: the manifests, the plan, the job kinds, the Now/After comparison and the
  bump gate. It marks a job kind as an executor kind; `jobs.claim_for_executor(config, executor, accepts=None)` and
  `jobs.finish_for_executor(config, job_id, executor, rc, stdout, log)` are its seam.
- This section owns HOW it reaches the EDA host and runs there: the executor token, the API, leases and expiry, the
  checkout from the read-only mirror, the sandbox, limits, and the verdict of a capability task.
- `executor.jobbridge` is the only service-side caller of the two job functions. When the job queue lacks them the
  API hands out tasks only (`jobbridge.available()`).
- Config: section 26 reads `[executor] host` and `local_fallback`; this section reads `lease_seconds`,
  `max_attempts` and `[[executor.agents]]`. The keys share the `[executor]` table and the merge must keep both sets in
  `config._EXECUTOR`. A site's host paths and licence values stay in the registry `tiers.yaml` `hosts` (decision 2);
  the agent's own `[sandbox] env`, `path` and `ro_binds` are what that host actually binds.

## 27.2 The task and result contract (`schemas/executor.schema.json`, `executor.task`)

- **task** (`$defs/task`): `mirror` and each pin's `mirror` are catalogue entry ids (`<mirror_root>/<id>.git` on the
  agent, the section 3 layout); pins are laid out inside the root; every one of `labels` must be granted to the
  claiming agent ([history](../archive/interfaces-history.md#section-27-the-remote-tier-executor)).
  `check_task` adds what a schema cannot say: no command or control sources a `set_env.sh`; `env` sets none of
  `TREE HOME PATH TMPDIR USER LOGNAME SSH_AUTH_SOCK GIT_SSH_COMMAND LD_PRELOAD LD_LIBRARY_PATH` nor any
  `NANOSOC_RM_*`; no tree path twice; no path with a `..` part. Both sides call it: the service before it queues, the
  agent before it runs (a task that should never have been queued is still not run: INFRA).
- **claim** (`$defs/claim`, `type: task`) and **result** (`$defs/result`). `check_result(doc, nonce, task_id)`: every row carries the task's nonce; PASS only with a PASS row and no
  FAIL/UNVERIFIED/INFRA row; finished_at not before started_at; the poster is the agent holding the lease.
- **Verdict of one task** (`agent.judge`): `tier.<tier>` PASS iff the exit code is `verdict.exit` (default 0); a
  run killed (time limit, output limit, cancel, lost lease) is FAIL; each `forbid` string found in the output makes
  `tier.<tier>.forbid` FAIL (exit 0 alone is not trusted; the evaluation's `WRAPPER: SKIPPED`); a declared
  `verdicts` file (removed before the run) adds its rows (`tierrun.parse_verdicts`; missing, a link out of the tree or not a regular file = UNVERIFIED,
  never read: the agent reads it unsandboxed); then the
  `control` runs in the same tree and `tier.<tier>.negctl` is PASS iff it exited non-zero (the gate can fail); a
  control that did not fire turns a PASS into UNVERIFIED. Roll-up FAIL > UNVERIFIED > INFRA > WARN > PASS. Anything
  that stops the command from running (commit not in the mirror, budget over the agent's `max_budget_s`, a bind that
  exposes a protected path, a `ro_binds` tool tree missing on this host, a cancel or lost lease seen at the heartbeat
  just before the run, a refused task) is INFRA with one `tier.<tier>` INFRA row, never FAIL.

## 27.3 The task queue (`executor.queue`, `<data_dir>/executor.sqlite`, 0600)

States `queued -> claimed -> done`; `claimed -> queued` when the lease runs out (up to `max_attempts` claims, default
2), then `lost` (verdict INFRA, "the executor stopped reporting"); `queued -> cancelled`; a claimed task asked to stop
(`cancel`) sees `cancel: true` at its next heartbeat and ends `done` with the agent's result. `claim` takes the oldest
queued task whose labels the agent carries, under `BEGIN IMMEDIATE` (two agents never hold one task). Only the
agent holding the current lease may `heartbeat`, `store_log` (kept at `<data_dir>/executor-logs/<id>.log`, 0600,
at most 4 MiB) or `complete`; anyone else gets `LeaseLost` (409). Expiry runs inside `claim`, `heartbeat` and
`executor list`: no thread, no timer. A row never shows its lease.

## 27.4 The agent (`nanosoc-rm executor run --once|--loop`, on the EDA host)

Config: its own TOML (`$NANOSOC_RM_AGENT_CONFIG`, `executor.agentconfig`), never the service config:
`[agent] name server token_file ca_file? mirror_root scratch_dir labels poll_seconds heartbeat_seconds max_budget_s
max_log_bytes max_upload_bytes max_file_bytes keep_work rm_config? site_host?` and `[sandbox] mode auto|bwrap|fallback,
network, ro_binds, path, env`. `server` is https (http only for 127.0.0.1/localhost) with no user or token in it;
the token file must be 0600 and the agent user's.

One task: (1) `check_task`; budget; (2) `checkout.lay_out`: a fresh repo per checkout whose
`objects/info/alternates` points at the read-only mirror (nothing copied, fetched or written to the mirror); a
missing commit is INFRA ("sync the agent's mirror"); (3) one heartbeat (the checkout may have been long), then
the run in the sandbox below, heartbeating every `heartbeat_seconds`; (4) `judge`; (5) upload the log (head and tail, at most `max_upload_bytes`), then the result;
a 409 drops the result (another claim owns the task). The work dir `<scratch_dir>/task-<id>` (the sandbox's one
writable bind) and the logs dir `<scratch_dir>/task-<id>.logs` (outside every bind, so a command can neither swap a
log for a link the agent would follow nor empty it to hide a `forbid` marker) are removed unless `keep_work`. `run_loop` retries an unreachable service after `poll_seconds` and stops on 401/403/404 (a revoked
agent stops).

**Sandbox** (`executor.sandbox`). Environment built from nothing: `PATH=<[sandbox] path>:/usr/bin:/bin`, HOME and
TMPDIR empty dirs in the work dir, `LANG/LC_ALL=C.UTF-8`, `TREE`, `NANOSOC_RM_TIER`, `NANOSOC_RM_NONCE`,
`NANOSOC_RM_VERDICTS` (when declared), git with no prompt, no system config and ssh with no identity or agent, then
the site env, then the task's env (`${TREE}` and earlier entries expand). Own session and process group, stdin
closed, umask 077, `RLIMIT_CORE` 0, `RLIMIT_FSIZE` `max_file_bytes`; output past `max_log_bytes` kills the run;
the whole group is killed at `budget_s x 1.5`, on cancel and on a lost lease.
- **bwrap** (used when `bwrap --unshare-all` works; `mode = "bwrap"` refuses to start without it): user, pid, ipc, uts
  and cgroup namespaces, the network shared unless `network = false`, `--die-with-parent`, `--new-session`, and an
  EMPTY root holding only `/usr` and `/etc` (read-only), `/proc`, `/dev`, a tmpfs `/tmp`, `ro_binds` (read-only),
  the mirror root (read-only, for the alternates) and the task's work dir (the only writable bind). The agent
  user's home is not mounted: `~/.ssh`, `~/.config` and the executor token do not exist inside.
- **fallback** (no usable bwrap; `mode = "fallback"` or `auto`): the same environment and limits, no filesystem
  isolation. **Residual risk:** the command runs as the agent user and can read what that user can read, the
  executor token file included; with the token it could claim tasks and post results for this agent (it can never
  reach any other route, any store, git host or CI). Mitigation: run the agent as a dedicated user with no ssh keys
  and nothing else in its home, revoke the token on any doubt. `executor check` warns when it would fall back.
- **Exposure check** (`sandbox.exposure`, both modes): no bind (work dir, mirror root, `ro_binds`, a job's binds) may
  contain the token file, the agent user's home, `~/.ssh`, `~/.config` or `~/.gnupg`. A task that would is INFRA,
  not run.

## 27.5 The executor API (`executor.api`; `[executor]` and `[[executor.agents]]` in the service config)

The web app's Guard sends every request under `/executor` to `executor.api.serve` BEFORE its session check, and
the app routes nothing under it: a session never reaches this API and an executor token never reaches any other
route. No `[executor]` table: every path is 404. Any method but POST: 405.

| POST | Body | Answer |
|---|---|---|
| `/executor/v1/claim` | `{labels?}` | 200 `$defs/claim` (`type: task`) or `$defs/job_claim` (`type: job`); 204 nothing to run |
| `/executor/v1/tasks/<id>/heartbeat` | `{lease}` | 200 `{cancel, lease_seconds}` |
| `/executor/v1/tasks/<id>/log` | raw bytes, header `X-Executor-Lease` | 200 `{stored}` |
| `/executor/v1/tasks/<id>/result` | `{lease, result}` | 200 `{state, verdict}` |
| `/executor/v1/jobs/<id>/heartbeat` | `{lease}` | 200 `{cancel, lease_seconds}` |
| `/executor/v1/jobs/<id>/result` | `{lease, exit_code, stdout, log}` | 200 `{state, verdict}` |

401 token missing, unknown or revoked; 404 unknown path or id; 409 not this agent's lease; 413 body over 2 MiB (JSON)
or 4 MiB + 64 KiB (log); 422 a body or result that breaks the contract (the task stays claimed; the agent may retry).
Answers carry `cache-control: no-store`, never a cookie, never the token or the body. A claim's labels are the
intersection of what the agent asks with what its `[[executor.agents]] labels` grant.

**Tokens** (`executor.tokens`): `rmx_` + 64 hex (256 bits). `nanosoc-rm executor token new NAME --out FILE
[--labels a,b]` writes the token to a new 0600 file (never overwrites, never prints it) and prints the
`[[executor.agents]]` entry with its `token_sha256`. The service keeps only the digest and compares constant-time
against every entry. **Revoke:** `revoked = true` on the entry, or delete it; the config is read per request, so the
next call is 401 without a restart. Config checks: `name` a label and never `local` (the service's own worker in
section 26's job queue), `token_sha256` 64 hex and unique, `lease_seconds >= 10`, `max_attempts >= 1`.

**Client** (`executor.client.ExecutorClient`): its one POST call site goes through `check_path`, which admits only
the six paths above; bearer header only, no redirects, `trust_env=False`, TLS verified (`ca_file` or the system
store). WRITE_CALLS effect `executor` (section 17.5).

## 27.6 Tier jobs over the API (`executor.jobbridge`)

An agent whose `[[executor.agents]]` entry grants `jobs` and whose own config names `rm_config` (a nanosoc-rm config
on the EDA host: its own data dir and mirror, anonymous read only, **no credentials**) asks for jobs too. A claim
hands out a capability task first, else `jobs.claim_for_executor(config, agent)`; the bridge records a lease
(`job_leases` in executor.sqlite). The agent runs `<python> -c CLI_BOOT <argv>` (the job queue's own `cli_args`) in
the same sandbox with `NANOSOC_RM_CONFIG=<rm_config>` and `NANOSOC_RM_EXECUTOR_HOST=<site_host>`; read-only binds:
the Python prefix, the nanosoc-rm install, the rm_config directory and its registry checkout; writable: its data dir
(all under the exposure check). stdout (the JSON documents) and the log go back with the exit code, and
`jobs.finish_for_executor` reads them as the local worker would. A job whose agent stops heartbeating is finished
with no exit code (INFRA) by `jobbridge.expire`; so is a running executor job with NO lease row older than one lease
(the claim's two transactions were cut by a service crash). Cancel is the job's
`cancel_by`. Grants: `jobs` claims every tier job kind; `jobs:<kind>` (`jobs:test-bump`, `jobs:compat`,
`jobs:matrix-cell`) only that kind. The tools a job needs are checked on the agent host (`host_check`: a missing
tool is INFRA, so the job reads UNVERIFIED, never PASS). In a bwrap job the agent sets
`NANOSOC_RM_TIER_PIDNS=<bwrap>`: every tier command of the job then runs in its own pid namespace under a nested
bwrap (`tierrun.pidns_wrap`), so it cannot reach the launching `nanosoc-rm`'s stdout through `/proc/<pid>/fd/1` and
forge the job's documents; a namespace that cannot be made is INFRA, never a bare run.

Paired runs: a capability task may carry `subject.side` (`now`|`after`) and `subject.pair`, so a planner can queue
both sides of one bump as tasks and compare the two results (section 26 compares inside one test-bump job today).

## 27.7 CLI

`nanosoc-rm executor run --once|--loop [--agent-config P]` (exit 0 ran or nothing queued; 3 token refused; 2 service
unreachable with --once), `executor check [--agent-config P] [--json]` (sandbox mode, exposure, token file, mirror
root; no network; exit 1 on a problem), `executor token new NAME --out FILE [--labels | --reader]` (`--reader`: a
bump-check reader token and its `[[api.readers]]` entry, section 26.7), `executor enqueue
TASK.json [--json]` (exit 3 when it breaks the contract), `executor list [--state S] [--limit N] [--json]`,
`executor show ID [--json]`, `executor cancel ID`. The install (a systemd --user unit, never started by the tool)
is in DEPLOY.md, "Remote tier executor".
