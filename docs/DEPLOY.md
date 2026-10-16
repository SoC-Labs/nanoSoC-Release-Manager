# Deploying the GUI on one host

A single-user or small-team install: local sign-in (CR-16), local jobs (CR-17), served on loopback and reached
through an SSH tunnel, or (CR-20) served over HTTPS to the team (Team access over HTTPS, below). What the pages
do: `docs/GUI.md`. Placeholders: `<host>` is the server, `<port>` a free port, `<login>` your user name.

## Layout on the host

```
~/nanosoc-rm/app          the tool (a git clone)
~/nanosoc-rm/venv         Python 3.11 virtual environment with the package installed
~/nanosoc-rm/registry     a clone of the registry
~/nanosoc-rm/data         rm.sqlite, jobs.sqlite, sessions.sqlite, session.secret, mirrors, job logs
~/.config/nanosoc-rm/config.toml        private config (0600)
~/.config/nanosoc-rm/initial-password   the generated password (0600)
~/.config/systemd/user/nanosoc-rm.service
```

## Install (about 15 minutes)

1. Get the code. From GitHub once published: `git clone https://github.com/SoC-Labs/nanoSoC-Release-Manager ~/nanosoc-rm/app`.
   Without network access to it, ship a bundle from a machine that has the repo:
   ```
   git bundle create rm.bundle master && scp rm.bundle <host>:
   ssh <host> 'git clone -b master rm.bundle ~/nanosoc-rm/app'
   ```
2. Make the environment (needs Python 3.11; `uv` is optional, `python3.11 -m venv` works too):
   ```
   uv venv --python python3.11 ~/nanosoc-rm/venv
   VIRTUAL_ENV=~/nanosoc-rm/venv uv pip install ~/nanosoc-rm/app
   ```
3. Clone the registry to `~/nanosoc-rm/registry` (a bundle works the same way as step 1).
4. Make a user. The stanza goes to the config; the password goes to a 0600 file and nowhere else:
   ```
   umask 077; mkdir -p ~/.config/nanosoc-rm
   ~/nanosoc-rm/venv/bin/nanosoc-rm user add <login> --generate > stanza.toml 2> pw.txt
   sed -n 's/^# password for .* (shown once): //p' pw.txt > ~/.config/nanosoc-rm/initial-password; rm pw.txt
   ```
5. Write `~/.config/nanosoc-rm/config.toml` (chmod 600): `[service] data_dir`, `[registry] path`, `[hosts.github]`,
   `[hosts.gitlab]`, `[auth] mode = "local"` and the stanza from step 4. The full template is in `docs/GUI.md`
   step 3. Leave out `[ci]` so Run checks on this host. Optional `[quality] paths = { <name> = "<checkout>" }`.
6. Install the service (`systemctl --user`; run `loginctl enable-linger` once, or ask an admin, so it keeps
   running after you log out):
   ```ini
   # ~/.config/systemd/user/nanosoc-rm.service
   [Unit]
   Description=nanoSoC Release Manager (local GUI)
   After=network-online.target

   [Service]
   Environment=NANOSOC_RM_CONFIG=%h/.config/nanosoc-rm/config.toml
   ExecStart=%h/nanosoc-rm/venv/bin/nanosoc-rm serve --host 127.0.0.1 --port <port>
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```
   ```
   systemctl --user daemon-reload && systemctl --user enable --now nanosoc-rm
   ```
7. Load data once, so the first pages are not empty (each command is also a button in the GUI):
   ```
   export NANOSOC_RM_CONFIG=~/.config/nanosoc-rm/config.toml PATH=~/nanosoc-rm/venv/bin:$PATH
   nanosoc-rm mirror sync --component <root component>    # then walk; sync each component the walk names
   nanosoc-rm walk <configuration>
   nanosoc-rm quality all <component> --store              # a cold quality page otherwise takes about a minute
   ```

## Open it

```
ssh -N -L <port>:127.0.0.1:<port> <host>
```
Then open http://127.0.0.1:<port>/ and sign in as `<login>` with the password in
`~/.config/nanosoc-rm/initial-password` on the host.

If your SSH config multiplexes connections (`ControlMaster`), the forward is added to the master connection and
`ssh -N` returns at once; the tunnel stays up until `ssh -O cancel -L <port>:127.0.0.1:<port> <host>`.

## Team access over HTTPS (CR-20)

To let a team open the pages without SSH tunnels, `serve` binds a non-loopback address and terminates TLS itself.
In local auth mode it refuses (exit 3) a non-loopback `--host` without `--i-know-this-is-exposed`, and refuses one
without `--tls-cert` and `--tls-key` (both readable) unless `--allow-plain-http` is given. With TLS on, every
cookie the service sets is `Secure`.

1. Make a self-signed certificate on the host (valid two years; browsers warn once, then remember the exception):
   ```
   umask 077; mkdir -p ~/.config/nanosoc-rm/tls
   openssl req -x509 -newkey rsa:3072 -nodes -days 730 \
       -keyout ~/.config/nanosoc-rm/tls/rm.key -out ~/.config/nanosoc-rm/tls/rm.crt \
       -subj "/CN=<host>" -addext "subjectAltName=DNS:<host>"
   ```
   Give each team member the certificate's fingerprint so they can check the browser warning:
   `openssl x509 -in ~/.config/nanosoc-rm/tls/rm.crt -noout -fingerprint -sha256`.
2. Change `ExecStart` in `~/.config/systemd/user/nanosoc-rm.service` (install step 6), then
   `systemctl --user daemon-reload && systemctl --user restart nanosoc-rm`:
   ```ini
   ExecStart=%h/nanosoc-rm/venv/bin/nanosoc-rm serve --host 0.0.0.0 --port <port> --tls-cert %h/.config/nanosoc-rm/tls/rm.crt --tls-key %h/.config/nanosoc-rm/tls/rm.key --i-know-this-is-exposed
   ```
3. Ask an admin to open the port in the host firewall:
   ```
   sudo firewall-cmd --permanent --add-port=<port>/tcp && sudo firewall-cmd --reload
   ```
4. Add the team's login (a shared user, or better one per person), append the stanza to the config, restart:
   ```
   ~/nanosoc-rm/venv/bin/nanosoc-rm user add <team-login> --generate
   ```
5. Open https://<host>:<port>/ and sign in.

**A shared login hides who did what.** Every action (Run, Sync, Walk, cancel, ...) is recorded under the
signed-in login, so with one shared team user the actions log shows the team user, not the person. Use one
`user add` per person when you need to know who did something.

## Operate

| Task | Command (on the host) |
|---|---|
| Status | `systemctl --user status nanosoc-rm` |
| Logs | `journalctl --user -u nanosoc-rm -f` (job logs: `~/nanosoc-rm/data/jobs/`) |
| Restart | `systemctl --user restart nanosoc-rm` (also clears sign-in lockouts) |
| Update (GitHub) | `git -C ~/nanosoc-rm/app pull`, reinstall, restart |
| Update (bundle) | `git -C ~/nanosoc-rm/app pull <new bundle> master`, reinstall, restart |
| Reinstall | `VIRTUAL_ENV=~/nanosoc-rm/venv uv pip install --reinstall-package nanosoc-rm ~/nanosoc-rm/app` (the version string does not change, so a plain install would skip it) |
| Update the registry | `git -C ~/nanosoc-rm/registry pull` (the next poll reads it) |
| Add a user | `nanosoc-rm user add <login> --generate`, append the stanza to the config, restart |
| Backup | stop the service, copy `~/nanosoc-rm/data/*.sqlite` (rm, jobs, sessions, bumps, gui) and `~/.config/nanosoc-rm/`; mirrors can be re-synced |

### Store schema upgrade (CR-ROB-store)

The first start after this update migrates `rm.sqlite` to schema version 2 (a new `current_snapshots` table, backfilled
from the stored snapshots; measured 0.26 s for 100,000 snapshots) and stamps `jobs.sqlite` at version 1. It runs once,
under a write lock; page reads continue meanwhile. Take the usual backup first. Do not run an older install of the CLI
against the same data directory afterwards: it would stamp the store back to version 1, and every later connection
of the new code would repeat the (harmless, but writing) backfill until the old install is gone.

### Store growth and retention (keep-on-change, CR-1015-retention)

Before this change `rm.sqlite` grew by about 122 MB a day on the live host. Nearly all of it was set-kind history:
every 15-minute poll stored a complete `pin_state` snapshot of each configuration, a `freshness` snapshot, a
`mirror_status` snapshot of each component and so on, whether or not anything had changed (about 3.7 GB a month).

Now a producer run whose content equals the scope's current snapshot is not stored again: the current snapshot
records the check (`confirmed_at`, `confirmations`, and the run's check times and snapshot id). Content means the
documents with their check times and snapshot ids taken out (interfaces.md section 12). Pages are unchanged: they
read the last check's documents, so a data age still says "checked at" the last poll. A change stores a new
snapshot as before. Measured on a synthetic store at the live cadence (`scripts/measure_store_growth.py`: 57
scopes, 543 set-kind rows per poll, 96 polls): 164.7 MB a day before; 0.8 MB a day with no content changes,
6.8 MB a day with 24 (one configuration's walk changing each hour), 25 MB a day with 96.

The first start after this update migrates `rm.sqlite` to schema version 3 (six columns and two indexes on
`snapshots`; `ADD COLUMN` does not rewrite the table). An older install run against the store afterwards would show
stale check times for unchanged scopes (it does not read confirmations) and store every run again; it does not
damage the store. Roll back by reinstalling the older version only if you accept that.

**Compacting the existing history (one-off, operator).** The store still holds the copies written before this change.
`nanosoc-rm store-maint compact` collapses each run of identical snapshots of a scope into its first snapshot, which
keeps the run's last check: every distinct state stays, with its first and last observed times; what pages show as
current does not change. It runs as one transaction, and a second run removes nothing. On the synthetic store it
took 11 s for one day of polls (166 MB) and, with `VACUUM`, left 8.5 MB.

1. Stop the service: `systemctl --user stop nanosoc-rm`.
2. Back up: `cp ~/nanosoc-rm/data/rm.sqlite ~/nanosoc-rm/rm.sqlite.before-compact` (needs free disk about the
   size of the store; so does step 5).
3. See what it would remove (changes nothing): `nanosoc-rm store-maint compact --dry-run`. The table shows, per
   kind, the snapshots, rows and MB it would remove, and any scope it skips (one whose current snapshot is not its
   last stored one; it is left as it is).
4. Apply: `nanosoc-rm store-maint compact --apply`. It took about a minute per GB on the synthetic store; it holds the
   write lock until it commits, which is why the service is stopped.
5. Return the space to the file system: `sqlite3 ~/nanosoc-rm/data/rm.sqlite 'VACUUM;'` (or
   `python3 -c "import sqlite3; sqlite3.connect('$HOME/nanosoc-rm/data/rm.sqlite').execute('VACUUM')"`).
6. Start the service: `systemctl --user start nanosoc-rm`, open Home, and check that the pages and their data ages
   look as they did. Delete the backup from step 2 once you are satisfied.

Event kinds (alerts, T0 results, quality, releases) and the `runs` and `actions` tables are never compacted: they
cannot be rebuilt. If the store still grows faster than expected, `store-maint compact --dry-run` shows which kinds
change most often.

## Wave B pages (built per CR-15; their automation stays off)

The same service also serves Matrix (`/matrix/`), Bumps (`/bumps/`), Versions (`/versions/`) and Releases
(`/releases/`) in the one navigation. Every long action on them (a matrix cell, a bump scan, a version refresh or
draft, a release draft or verify, a fork listing, a cold quality page) is a job in the local runner, never work
inside a request. They write only on the host's own disk, under the data directory:

| What | Where |
|---|---|
| Matrix cells and quarantine | `rm.sqlite` (observations `matrix_cell`, `matrix_quarantine`) |
| Bump proposals, marks, ledger | `bumps.sqlite` |
| Tool release drafts | `drafts/<component>/<version>/` |
| SoC release drafts | `releases/<configuration>/<version>/` |

Nothing is scheduled for them: the nightly matrix, bump pull requests and published releases wait for their phase
entry gates (`docs/phase-gates.yaml`), and `nanosoc-rm phase-gates` exits 1 if one is switched on. Every page that
would write to a forge (a bump, a tag, a release) gives a person a patch, a folder or a command instead.

A quick check after an update (each from the GUI, or with the CLI on the host):
```
nanosoc-rm matrix <configuration> --component <c1> --component <c2> --component <c3> --run --wait
nanosoc-rm bumps <configuration> --store
nanosoc-rm release draft <configuration> <version>      # a folder under releases/; nothing tagged or pushed
nanosoc-rm version refresh
nanosoc-rm phase-gates --repo ~/nanosoc-rm/app           # exit 0: no later-phase automation enabled
```

## Wave C pages (the merged GUI)

The same service serves the Inbox at `/`, the configuration and component workspaces and the command bar
(`docs/GUI.md`, Pages). The Inbox, alert-group clears and the review queue keep per-user state (done and snoozed
cards, review decisions, the patch basket) in `<data_dir>/gui.sqlite`, created mode 0600. It is not a fact store
and cannot be rebuilt from the mirrors: back it up with the other `.sqlite` files. Its POST routes exist only in a
local deployment; a GitHub+CI deployment still serves exactly the two Run routes as writes.

A quick check after an update, signed in: `/`, `/configurations/<configuration>/whatif` (pick a component and a
target, **Run what-if**, wait for Now vs After), `/components/<component>/branches` and
`/components/<component>/review` (Accept one finding, then download `patch.diff`) all answer 200.

## What this host can and cannot run

T0 (resolve, pins, URLs, vendor scan) needs only git. T1 and T2 tiers call EDA tools (Verilator, VCS); on a host
without them the tier result says the tool is not on this host. The remote tier executor (below) runs them on the
EDA host instead. By default every fetch is anonymous and private
repos stay unreadable (one `mirror_failed` alert group, "private repos cannot be mirrored anonymously").

## Remote tier executor (optional; CR-WG-executor, interfaces.md section 27)

T1 and up need the EDA host (decision 1, 2026-10-16). A pull agent there claims tier work from this service over
HTTPS, runs it sandboxed, and posts the result back. This service never connects to the agent. Nothing in the tool
installs or starts the agent: these steps are for the operator. About 30 minutes.

On the service host:

1. Make a token for the agent. The token goes to a new 0600 file and is never printed; the config entry is:
   ```
   nanosoc-rm executor token new eda-1 --out eda-1.token --labels verilator,vcs,xcelium,genus
   ```
2. Paste the printed `[[executor.agents]]` entry into the private config under an `[executor]` table
   (`lease_seconds = 300`, `max_attempts = 2` are the defaults). The service reads its config per request: no
   restart. The API is served by the same process at `/executor/v1/` (POST only, token only); it needs CR-20 TLS when
   the agent is on another host.
3. Move `eda-1.token` to the agent host (scp, then delete it here). Grant `jobs` too only when the agent will run
   whole `nanosoc-rm` tier jobs (step 8).

On the EDA host, as a DEDICATED user (no ssh keys, nothing else in its home; never a person's account):

4. Check the sandbox: `command -v bwrap && bwrap --ro-bind / / --unshare-all /bin/true && echo usable`. Without a
   usable bubblewrap the agent falls back to a scrubbed environment only (residual risk below).
5. Install the tool in a venv (as in "Install" steps 1 and 2) and put the token at
   `~/.config/nanosoc-rm-executor/token` (`chmod 600`).
6. Give it a read-only mirror: a copy of the service's `<data_dir>/mirrors/` (`<component>.git` bare repos) that the
   agent user can read but not write, refreshed by another account (for example an rsync on a timer). The agent never
   fetches from a git host; a commit missing from the mirror is INFRA ("sync the agent's mirror").
7. Write `~/.config/nanosoc-rm-executor/agent.toml` from `scripts/executor/agent.example.toml` (server URL, mirror
   root, scratch dir, labels, tool trees as `ro_binds`, licence values in `[sandbox.env]`), then check it:
   `NANOSOC_RM_AGENT_CONFIG=... nanosoc-rm executor check` (sandbox mode, exposure, token file; no network).
8. Optional, tier jobs (`test-bump`, `compat --tier`): a nanosoc-rm config on this host (`rm_config`), with its own
   data dir, mirror and registry checkout, anonymous reads only and NO credentials in it, plus `jobs` in `labels`.
9. Try one task by hand: `nanosoc-rm executor run --once`. Then install `scripts/executor/nanosoc-rm-executor.service`
   as a `systemctl --user` unit (`loginctl enable-linger` for the agent user) and enable it.

Operate: `nanosoc-rm executor list|show ID|cancel ID` on the service host. Revoke an agent at once with
`revoked = true` on its entry (or delete the entry). A task whose agent stops heartbeating is queued again, then
ends INFRA after `max_attempts` claims.

What the sandbox gives (interfaces.md section 27.4):
- **bwrap** (preferred): an empty root with only `/usr` and `/etc` read-only, the tool trees read-only, the mirror
  read-only and the task's work dir writable; the agent user's home (its `~/.ssh`, `~/.config`, the token) is not
  there; new user/pid/ipc namespaces; network shared for licence servers unless `network = false`.
- **fallback**: environment built from nothing (no `SSH_AUTH_SOCK`, no token, empty HOME), umask 077, time and
  output limits, its own process group. **Residual risk:** no filesystem isolation. The command can read what the
  agent user can read, including the token file; with it, it could claim tasks and post results as this agent (and
  reach nothing else: the token opens only `/executor/v1/`). Use bwrap; if you cannot, keep the agent user empty and
  revoke the token on any doubt.
- Both: no bind may contain the token file, the home, `~/.ssh`, `~/.config` or `~/.gnupg` (the task is INFRA, not
  run); a time limit of `budget_s x 1.5`, `max_log_bytes` of output, `max_file_bytes` per file written.

## Private repos (optional, off by default; CR-PR1)

To mirror private repos, give a host ONE read-only method. Add this one line under that host's table in
`~/.config/nanosoc-rm/config.toml` and restart the service:

```toml
[hosts.gitlab]
private_read = "ssh"          # the service user's existing ssh key, BatchMode (never prompts)
```

or, for a token with read scope only (GitLab `read_repository`; GitHub fine-grained "Contents: read"), in a
0600 file:

```toml
private_read = { method = "token", token_file = "~/.config/nanosoc-rm/gitlab-read.token" }
```

- It is used only by the mirror sync, only for `git fetch` and `git ls-remote`, and only after an anonymous fetch
  of that repo failed; `gitrun` refuses every other git call with it, and every write as before. The T0 gates and
  `compat` stay anonymous: they judge what anyone can fetch.
- The ssh method uses whatever key the service user has. If that key can also push, the tool still never pushes,
  but a key or deploy key with read access only is the safer choice (`identity_file = "..."` picks one).
- Check it: `nanosoc-rm mirror sync --host gitlab`, then `nanosoc-rm mirror status --summary`. Repos read this way
  show `credentials:ssh` (or `credentials:token`); a key or token the host refuses shows `unreadable:<cause>` and
  raises one alert group, "private repos the read credentials cannot read" (a broken `private_read`, such as a
  missing token file, raises the `private-read-config` group). These open even if you already cleared the old
  anonymous alerts; the old anonymous alerts that were not cleared stay open until you clear their group on
  `/alerts`.
- The credential is never logged, stored or shown: not in commands, observations, alerts or pages.

## Publishing to the release store (optional; CR-ART-publish)

Off until configured: the Publish page then says why. On the tool host the store is
`http://localhost:8082/artifactory` (the TLS proxy is not reachable from elsewhere).

1. Token: the tool's own Artifactory access token (one year, revocable), minted once by the admin. Put it in a 0600
   file, never in the config:
   `umask 077; cat > ~/.config/nanosoc-rm/artifactory.token` (paste, then Ctrl-D). The tool sends it as a Bearer
   header only. Never use Basic auth against `/access/api`: each failure locks the admin out for seconds.
2. Signing key (RSA: this host's OpenSSH 8.0 has no `ssh-keygen -Y`, so the tool signs RSA keys itself):
   `ssh-keygen -t rsa -b 4096 -N '' -C nanosoc-rm -f ~/.config/nanosoc-rm/release-signing.key`. Keep a copy of the
   `.pub` with the people who verify; `ssh-keygen -Y verify` (OpenSSH 8.1+) checks a `.sig` with an allowed_signers
   line `nanosoc-rm namespaces="nanosoc-rm" ssh-rsa AAAA...`.
3. Config:
   ```toml
   [stores.artifactory]
   url = "http://localhost:8082/artifactory"
   token_file = "~/.config/nanosoc-rm/artifactory.token"
   token_expires = "YYYY-MM-DD"
   signing_key = "~/.config/nanosoc-rm/release-signing.key"
   publishers = ["<login>"]          # who may press Publish
   shared_logins = ["<shared login>"] # never publishes, even if listed above (a config error)
   projects = { <component> = "<store project>" }   # optional: the folder under asic-release/
   ```
4. Restart. `/publish/` lists drafts; Preview, then Publish. From the shell: `nanosoc-rm publish-release C/V --dry-run`.

What it writes, only on a confirmed Publish: new files under `asic-release/<project>/<version>/` (the record, its
`.sig`, the draft's documents), a build record `rm-release-<project>` or `rm-tapeout-<project>`, and a status
promotion. It refuses when any of those paths already holds other bytes. Ledger: `rm.sqlite` (`release_publication`,
`actions`) and `<data_dir>/publish/`.

## Regenerator (Wave D, interfaces.md section 20)

Optional. To class generator moves by regeneration on this host:

1. Add `[regen] read_only_roots = ["<shared IP library root>"]` to the private config (any path under it is
   refused); `work_dir` defaults to `<data_dir>/regen`, `nice` to 15.
2. Add a `regen` entry for the configuration to the registry's `tiers.yaml` (example:
   `schemas/examples/eligibility.tiers.example.yaml`; `PYTHON` must name an interpreter with the generator's
   packages, else the render is INFRA "tool not on this host").
3. `nanosoc-rm walk <config>`, then `nanosoc-rm regen pending --run` (or queue a `regen-class` job). About 60 s
   per render; renders are cached by digest.

## Artefact store index (Wave E, interfaces.md section 22)

Optional. The tool reads Artifactory (GET, HEAD and one AQL search; it never writes there except the publish
service's manifests) and shows what it holds on `/store/`, the Silicon and History tabs, Versions and Releases.

1. Put the tool's own access token (one token, revocable, about a year) in a 0600 file. It must be a Bearer
   access token: never the admin login. A Basic login against the store's Access API counts as a failed admin
   login and locks the admin out.
2. Add to the private config and restart the service:
   ```toml
   [stores.artifactory]
   url = "http://localhost:8082/artifactory"         # on the store's own host; no user or token in the URL
   token_file = "~/.config/nanosoc-rm/artifactory.token"
   token_expires = "YYYY-MM-DD"                       # shown on /store/
   # repo = "asic-release"                            # the release repository (default)
   # repos = ["asic-candidate", "asic-record", "asic-release"]   # default: every repository
   allowed_signers = "~/.config/nanosoc-rm/allowed_signers"   # who may sign a manifest (else the signing key's .pub)
   shared_logins = ["<shared store login>"]          # a manifest these uploaded is never trusted
   [stores.artifactory.projects]                      # optional: the store project folder of a component
   <catalogue component> = "<store project>"
   ```
3. Check it: `nanosoc-rm store index --store` (about 10 s; prints each feature probe), then open `/store/`.
   The index also runs once a night (scheduler job `store_index`) and on **Refresh the index**.

A manifest in the release repository counts (green on the pages, silences "no manifest") only when its `.sig`
verifies against `allowed_signers`. Without that key every manifest shows UNVERIFIED and raises a card.

Documents up to 16 MiB that carry no confidentiality marking are kept, verified by sha1, in
`<data_dir>/store-cache/` so the pages can offer them for download; the token never reaches a page.
