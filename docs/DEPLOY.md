# Deploying the GUI on one host

A single-user or small-team install: local sign-in (CR-16), local jobs (CR-17), served on loopback and reached
through an SSH tunnel. What the pages do: `docs/GUI.md`. Placeholders: `<host>` is the server, `<port>` a free
loopback port, `<login>` your user name.

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
without them the tier result says the tool is not on this host. Private repos stay unreadable: every fetch is
anonymous.

## Regenerator (Wave D, interfaces.md section 20)

Optional. To class generator moves by regeneration on this host:

1. Add `[regen] read_only_roots = ["<shared IP library root>"]` to the private config (any path under it is
   refused); `work_dir` defaults to `<data_dir>/regen`, `nice` to 15.
2. Add a `regen` entry for the configuration to the registry's `tiers.yaml` (example:
   `schemas/examples/eligibility.tiers.example.yaml`; `PYTHON` must name an interpreter with the generator's
   packages, else the render is INFRA "tool not on this host").
3. `nanosoc-rm walk <config>`, then `nanosoc-rm regen pending --run` (or queue a `regen-class` job). About 60 s
   per render; renders are cached by digest.
