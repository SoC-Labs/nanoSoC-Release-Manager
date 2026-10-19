# Admin guide: install and operate the service

For whoever runs the service on one host: install, sign-in, HTTPS for a team, backups, upgrades, and the optional
parts (remote tier executor, private repos, artefact store, regenerator). People who only use the pages start at
[getting-started.md](getting-started.md); the pages are described in [user-guide.md](user-guide.md#10-the-pages).
Placeholders: `<host>` is the server, `<port>` a free port, `<login>` a user name.

The install is local sign-in (CR-16) and a local job worker (CR-17), served on loopback behind an SSH tunnel, or
over HTTPS to the team (CR-20). A deployment with GitHub sign-in and a CI project is in the contracts
([interfaces.md](contracts/interfaces.md) sections 10, 13, 14) and is not installed anywhere yet.

Contents: [Try it from a checkout](#try-it-from-a-checkout-10-minutes) ·
[Install](#install-about-15-minutes) · [Open it](#open-it) · [Team access over HTTPS](#team-access-over-https-cr-20) ·
[Operate](#operate) · [Remote tier executor](#remote-tier-executor-optional-cr-wg-executor-interfacesmd-section-27) ·
[Private repos](#private-repos-optional-off-by-default-cr-pr1) ·
[Artefact store](#artefact-store-index-optional-interfacesmd-section-22) ·
[Publishing](#publishing-to-the-release-store-optional-cr-art-publish) ·
[Regenerator](#regenerator-optional-interfacesmd-section-20) · [Troubleshooting](#troubleshooting)

## Try it from a checkout (10 minutes)

To look at the pages before installing a service, from a clone of this repository:

1. `scripts/dev-env.sh && export PATH=$PWD/.venv/bin:$PATH`
2. `nanosoc-rm user add <login> --generate` prints a config stanza and the password; it writes nothing.
3. Write the config from install step 5 below (any `data_dir` you own) and point `NANOSOC_RM_CONFIG` at it.
4. `nanosoc-rm serve --host 127.0.0.1 --port 8000` (add `--no-schedule` to skip the 15-minute poll, which syncs
   every mirror and walks every configuration).
5. From your laptop: `ssh -N -L 8000:127.0.0.1:8000 <host>`, open http://127.0.0.1:8000/ and sign in.

## Layout on the host

```
~/nanosoc-rm/app          the tool (a git clone)
~/nanosoc-rm/venv         Python 3.11 virtual environment with the package installed
~/nanosoc-rm/registry     a clone of the registry
~/nanosoc-rm/data         rm.sqlite, jobs.sqlite, sessions.sqlite, bumps.sqlite, gui.sqlite, session.secret,
                          mirrors, job logs (store files the tool creates are mode 0600)
~/.config/nanosoc-rm/config.toml        private config (0600)
~/.config/nanosoc-rm/initial-password   the generated password (0600)
~/.config/systemd/user/nanosoc-rm.service
```

## Install (about 15 minutes)

1. Get the code from GitHub: `git clone https://github.com/SoC-Labs/nanoSoC-Release-Manager ~/nanosoc-rm/app`.
   On a host without network access to GitHub, ship a bundle from a machine that has the repo:
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
5. Write `~/.config/nanosoc-rm/config.toml` (chmod 600). Every key is in
   [interfaces.md](contracts/interfaces.md) section 17.1; this is the minimum:
   ```toml
   [service]
   data_dir = "/path/to/nanosoc-rm/data"          # ~/nanosoc-rm/data, written out in full
   [registry]
   path = "/path/to/nanosoc-rm/registry"          # the registry clone from step 3
   [hosts.github]
   web = "https://github.com"
   api = "https://api.github.com"
   ssh = "git@github.com:"
   org = "SoC-Labs"
   [hosts.gitlab]
   web = "https://<gitlab-host>"
   api = "https://<gitlab-host>/api/v4"
   ssh = "git@<gitlab-host>:"
   group = "soclabs"
   [auth]
   mode = "local"
   [[auth.users]]                                 # the stanza from step 4
   login = "<login>"
   hash = "scrypt$..."
   ```
   Leave out `[ci]`, so **Run** checks on this host. No `[roles]` is needed: a local user is a `member`. Optional:
   `[quality] paths = { <name> = "<checkout>" }`.
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

**What protects a loopback install.** Loopback keeps the service off the network, but every user with a login on
the host can reach the port. The password and the session cookie are the boundary:
- **Sign out** revokes the session, so a copy of the cookie stops working. Changing a user's hash in the config
  ends that user's sessions on their next request.
- After 5 wrong passwords for one login in 5 minutes, that login is refused for the rest of the 5 minutes, even
  with the right password. Anyone who can reach the port can trigger this for a login they name. A restart clears
  it (the counter is in memory). Use generated passwords (`--generate`): the limit is there for chosen ones.

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
| Reinstall | `VIRTUAL_ENV=~/nanosoc-rm/venv uv pip install --reinstall-package nanosoc-rm ~/nanosoc-rm/app` (the package version stays `0.1.0.dev0` between releases, so a plain install would skip it; `nanosoc-rm --version` therefore does not tell two installs apart: compare `git -C ~/nanosoc-rm/app rev-parse HEAD`) |
| Update the registry | `git -C ~/nanosoc-rm/registry pull` (the next poll reads it) |
| Add a user | `nanosoc-rm user add <login> --generate`, append the stanza to the config, restart |
| Backup | `systemctl --user stop nanosoc-rm; b=~/nanosoc-rm-backup/$(date +%F); (umask 077; mkdir -p $b && cp -p ~/nanosoc-rm/data/{rm,jobs,sessions,bumps,gui}.sqlite ~/nanosoc-rm/data/session.secret $b/ && cp -rp ~/.config/nanosoc-rm $b/config); systemctl --user start nanosoc-rm`. All five stores: `gui.sqlite` (cards, reviews, notifications) and `bumps.sqlite` (proposals, marks, ledger) cannot be rebuilt from the mirrors. Mirrors can be re-synced |
| Store modes | the tool creates each store 0600 and a new data dir 0700. Stores created before 2026-10-16 keep their mode: `chmod 600 ~/nanosoc-rm/data/*.sqlite*; chmod 700 ~/nanosoc-rm/data` once |

### Upgrades

Back up first (the Backup row above). An update may migrate the stores on its first start: `rm.sqlite` is at
schema version 3 and `jobs.sqlite` at 2 today; a migration runs once, under a write lock, while pages keep
reading. Never run an older install of the CLI against a data directory a newer one has opened: it does not
damage the store, but it stores every run again and shows stale check times. Changes that need an operator are
under **breaking** in [CHANGELOG.md](../CHANGELOG.md).

### Store growth and retention (keep-on-change, CR-1015-retention)

A poll whose result equals the scope's current snapshot stores no new rows: the snapshot records the check
(`confirmed_at`, `confirmations`) instead, and pages read the last check as before (interfaces.md section 12). A
store at the live cadence grows by about 1 MB a day when nothing changes, and by about 7 MB a day with one
configuration's walk changing every hour (`scripts/measure_store_growth.py`).

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

## Matrix, bumps, versions and releases (built; their automation stays off)

The service also serves Matrix (`/matrix/`) and Releases (`/releases/`) in the header, and Bump proposals (`/bumps/`) and
Versions (`/versions/`) under **More**. Every long action on them (a matrix cell, a bump scan, a version refresh or
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

## Per-user state (`gui.sqlite`)

The service serves the projects home at `/`, Notifications behind the bell (the full cards at `/inbox`), the
configuration and component workspaces and the command bar ([user-guide.md](user-guide.md#10-the-pages)). Notifications, alert-group
clears, the review queue and the Home timeline keep per-user state (read and snoozed items, review decisions, the
patch basket, display choices) in `<data_dir>/gui.sqlite`, created mode 0600. It is not a fact store
and cannot be rebuilt from the mirrors: back it up with the other `.sqlite` files. Its POST routes exist only in a
local deployment; a GitHub+CI deployment still serves exactly the two Run routes as writes.

A quick check after an update, signed in: `/`, `/notifications`, `/configurations/<configuration>/whatif` (pick a component and a
target, **Run what-if**, wait for Now vs After), `/components/<component>/branches` and
`/components/<component>/review` (Accept one finding, then download `patch.diff`) all answer 200.

## What this host can and cannot run

T0 (resolve, pins, URLs, vendor scan) needs only git. T1 and T2 tiers call EDA tools (Verilator, VCS); on a host
without them the tier result says the tool is not on this host. The remote tier executor (below) runs them on the
EDA host instead; each repository declares its tests in `.nanosoc/capabilities.yaml`, and the registry's
`tiers.yaml` keeps only per-host paths and licence data (`hosts`). By default every fetch is anonymous and private
repos stay unreadable (one `mirror_failed` alert group, "private repos cannot be mirrored anonymously").

## Remote tier executor (optional; CR-WG-executor, [interfaces.md section 27](contracts/27-executor.md))

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
   whole `nanosoc-rm` tier jobs (step 8); `jobs:test-bump` (or `jobs:compat`, `jobs:matrix-cell`) grants one job
   kind only. A job's tools are checked on the agent host (a missing tool reads INFRA, so UNVERIFIED, never PASS).

On the EDA host, as a DEDICATED user (no ssh keys, nothing else in its home; never a person's account):

4. Check the sandbox: `command -v bwrap && bwrap --ro-bind / / --unshare-all /bin/true && echo usable`. Without a
   usable bubblewrap the agent falls back to a scrubbed environment only (residual risk below). The example
   config sets `mode = "bwrap"`, so the agent refuses to start instead of falling back silently; choose
   `fallback` only on purpose.
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
ends INFRA after `max_attempts` claims; a job ends INFRA (also one left running with no lease by a service crash).
Never put `[executor] host` in the service config: a service config's host is ignored, and `nanosoc-rm test-bump
--host` on the service host is refused, so tier commands never run here.

Bump-check readers (the repositories' CI, [interfaces.md section 26.7](contracts/26-capabilities.md)): for each repository whose CI asks
`GET /api/v1/bump-check`, `nanosoc-rm executor token new <repo>-ci --out <repo>-ci.token --reader`, paste the
printed `[[api.readers]]` entry into the private config, and put the token into that repository's CI secret
`NANOSOC_RM_TOKEN` (and the service's HTTPS URL into variable `NANOSOC_RM_URL`). A reader token opens that one
route and nothing else; revoke with `revoked = true`. The route must be reachable from where the CI runs.

What the sandbox gives ([interfaces.md section 27.4](contracts/27-executor.md)):
- **bwrap** (preferred): an empty root with only `/usr` and `/etc` read-only, the tool trees read-only, the mirror
  read-only and the task's work dir writable; the agent user's home (its `~/.ssh`, `~/.config`, the token) is not
  there; new user/pid/ipc namespaces; network shared for licence servers unless `network = false`. In a tier job,
  each tier command also runs in its own pid namespace (a nested bwrap), so it cannot reach the job's `nanosoc-rm`
  process and forge its result.
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

## Artefact store index (optional; [interfaces.md section 22](contracts/22-store-index.md))

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

## Publishing to the release store (optional; CR-ART-publish)

Off until configured: the Publish page then says why. Set up the store index first (the section above): it
holds the URL and the token. Publishing adds a signing key and the list of people who may publish.

1. Token: the same `[stores.artifactory] token_file` as the index (one year, revocable, minted once by the store
   admin; `umask 077; cat > ~/.config/nanosoc-rm/artifactory.token`, paste, Ctrl-D). Bearer header only: never Basic
   auth against `/access/api`, where each failure locks the admin out for seconds.
2. Signing key (RSA: this host's OpenSSH 8.0 has no `ssh-keygen -Y`, so the tool signs RSA keys itself):
   `ssh-keygen -t rsa -b 4096 -N '' -C nanosoc-rm -f ~/.config/nanosoc-rm/release-signing.key`. Keep a copy of the
   `.pub` with the people who verify; `ssh-keygen -Y verify` (OpenSSH 8.1+) checks a `.sig` with an allowed_signers
   line `nanosoc-rm namespaces="nanosoc-rm" ssh-rsa AAAA...`.
3. Add to the `[stores.artifactory]` table of the index:
   ```toml
   signing_key = "~/.config/nanosoc-rm/release-signing.key"
   publishers = ["<login>"]          # who may press Publish
   # shared_logins (index) never publishes, even if listed in publishers (a config error)
   ```
4. Restart. `/publish/` lists drafts; Preview, then Publish. From the shell: `nanosoc-rm publish-release C/V --dry-run`.

What it writes, only on a confirmed Publish: new files under `asic-release/<project>/<version>/` (the record, its
`.sig`, the draft's documents), a build record `rm-release-<project>` or `rm-tapeout-<project>`, and a status
promotion. It refuses when any of those paths already holds other bytes. Ledger: `rm.sqlite` (`release_publication`,
`actions`) and `<data_dir>/publish/`.

## Regenerator (optional; [interfaces.md section 20](contracts/20-regen.md))

Optional. To class generator moves by regeneration on this host:

1. Add `[regen] read_only_roots = ["<shared IP library root>"]` to the private config (any path under it is
   refused); `work_dir` defaults to `<data_dir>/regen`, `nice` to 15.
2. Add a `regen` entry for the configuration to the registry's `tiers.yaml` (example:
   `schemas/examples/eligibility.tiers.example.yaml`; `PYTHON` must name an interpreter with the generator's
   packages, else the render is INFRA "tool not on this host").
3. `nanosoc-rm walk <config>`, then `nanosoc-rm regen pending --run` (or queue a `regen-class` job). About 60 s
   per render; renders are cached by digest.

## Troubleshooting

| You see | Cause | What to do |
|---|---|---|
| a page answers 500 | a bug | `journalctl --user -u nanosoc-rm -n 100`; the 500 page says a POST may have been partly applied |
| a sign-in refused with the right password | 5 failed sign-ins of that login within 5 minutes | wait 5 minutes, or restart the service |
| `serve` exits 3 at start | a config error, unreadable TLS files, or a non-loopback `--host` without TLS | the message names it; `--help` lists the flags |
| tier jobs stay "waiting for an executor" | no agent is claiming work | [Remote tier executor](#remote-tier-executor-optional-cr-wg-executor-interfacesmd-section-27); `nanosoc-rm executor list` |
| "private repos cannot be mirrored anonymously" | no `private_read` for that host | [Private repos](#private-repos-optional-off-by-default-cr-pr1) |
| `rm.sqlite` grows fast | many scopes change every poll | `nanosoc-rm store-maint compact --dry-run` shows which kinds |
