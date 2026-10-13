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
| Backup | stop the service, copy `~/nanosoc-rm/data/*.sqlite` and `~/.config/nanosoc-rm/`; mirrors can be re-synced |

## What this host can and cannot run

T0 (resolve, pins, URLs, vendor scan) needs only git. T1 and T2 tiers call EDA tools (Verilator, VCS); on a host
without them the tier result says the tool is not on this host. Private repos stay unreadable: every fetch is
anonymous.
