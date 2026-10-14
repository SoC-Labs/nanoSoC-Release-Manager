# The web GUI on one host (local mode)

The same pages as the service VM will serve, run from a login server with no GitHub OAuth app and no CI
project: local sign-in (CR-16) and a local job worker (CR-17). As a service: `docs/DEPLOY.md`. Contracts: `docs/contracts/interfaces.md`
sections 13, 13a and 14.

## Set up (about 10 minutes)

1. Install: `scripts/dev-env.sh`, then `export PATH=$PWD/.venv/bin:$PATH`.
2. Make a user (prints a config stanza; writes nothing):
   ```
   nanosoc-rm user add <login> --generate        # or without --generate to type a password
   ```
3. Write the private config (outside this repo), named by `NANOSOC_RM_CONFIG`:
   ```toml
   [service]
   data_dir = "/path/to/rm-data"          # mirrors, rm.sqlite, jobs.sqlite, job logs, session.secret
   [registry]
   path = "/path/to/nanosoc-registry"     # a registry checkout
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
   [[auth.users]]                         # the stanza from step 2
   login = "<login>"
   hash = "scrypt$..."
   ```
   No `[ci]` section: Run then checks on this host. No `[roles]` needed: a local user is a `member`.
4. Serve on loopback (it refuses any other address in local mode):
   ```
   nanosoc-rm serve --host 127.0.0.1 --port 8000
   ```
   Add `--no-schedule` to skip the 15-minute poll (it syncs every mirror and walks every configuration).
5. From your laptop: `ssh -N -L 8000:127.0.0.1:8000 <host>`, then open http://127.0.0.1:8000/ and sign in.

**What protects it.** Loopback keeps the service off the network, but every user with a login on the host can
reach 127.0.0.1:8000. The password and the session cookie are the boundary:
- Sign out with **Sign out**: it revokes the session, so a copy of the cookie stops working. Changing a user's
  hash in the config ends that user's sessions on their next request.
- After 5 wrong passwords for one login in 5 minutes, that login is refused for the rest of the 5 minutes, even
  with the right password. Anyone who can reach the port can trigger this for a login they name. Restarting the
  service clears it (the counter is in memory). Use a generated password (`--generate`): the limit is there for
  chosen ones.

## Pages

| Page | What it is for |
|---|---|
| Shell | every page: the left rail (inbox count, each configuration with its deciding gate and a text chip, flagged components, alert lines, Tools), the command bar (`/` focuses it; `GET /search` finds configurations, components and commits), breadcrumbs, one data age, light and dark themes |
| Inbox (`/`) | ranked cards built from stored facts, one cause and one action each (a failing configuration names the gate that decides it); **Mark done** (comes back when the facts change), **Snooze** 1, 7 or 30 days, **Undo**; the configuration board (last compat or CI T0 verdict, red pins, errata, host drift, **Run**) under it |
| Configuration workspace | `/configurations/{id}` tabs: Overview (gates with the deciding rows first, each vendor gate with a one-line explanation of what it judges (CR-6: Arm source, including release names and Arm product codes, fails; foundry data and site inventory are amber unless the catalogue marks a rule blocking; a vendor gate red only because trees were not fetched reads "{n} trees not scanned (not fetched)", never "Arm source in"), pins, waivers), Submodule tree (`?show=uncovered`, a node aside), What-if (pick a component and a target, **Run what-if** as a job, then Now vs After per gate), Matrix, Silicon (uncovered rows first, errata), History (jobs and runs); **Walk now** |
| Compat | move pins to any tag or branch in the mirror, choose T1/T2, run; live log; per-gate and per-pin table. `/compat?job=N` and `?configuration=` open the workspace's What-if tab |
| Component workspace | `/components/{id}` tabs: Overview, Branches (every copy by default; the delete list to copy comes first and follows the ticks; the tool never deletes; **Re-read mirror**), Quality, Versions, Review (one comment finding at a time: **Accept**, **Edit**, **Skip**, **Not a finding**; accepted edits go to a per-login patch basket, downloaded as `patch.diff`) |
| Branches | `/branches` across components; `?component=` opens that component's Branches tab |
| Hosts | canonical vs copy relation; **Compare now** (a POST: a link can never start one) |
| Contains, Silicon | containment of a commit; silicon-protection rows |
| Registry | components, defects, errata |
| Mirrors | fetch status and age; **Sync** one component or all |
| Alerts | open alerts grouped by cause (messages redacted); **Clear all** of a group after fixing it (refused with 409 if the group changed since the page was drawn) and **Clear** one; each clear is logged with your login |
| Matrix | per configuration: a row per component, columns pinned / latest tag in range / latest tag / default-branch head; each cell's verdict (text and colour), age and stale flag; **Run cell**, **Run row**, **Run column**, **Run all** queue local jobs (a T0 run each, about 3 minutes); a cell page with every attempt, the D4 provenance and the comparison with the same night's pinned run; the D8 quarantine list. The nightly matrix is disabled until the Phase 2 gate |
| Quality | comment density, comment findings with a patch, README check with a layout patch, doc conciseness, trend; a page with no stored result queues a quality job and says "analysing, refresh" (about a minute on a large repository). The overview opens on **Repos in my configurations** when any listed target is one, otherwise on **All targets** (chip row: that, or **All targets**). Banners and header boilerplate are one **decoration** rule: hidden from the table, the queue and the patch until you include it. Each finding has a severity per kind (stale TODO High, dead code Medium, unknown name Check, restating comment Low); rows with a suggested edit come first, and the preview shows the real deleted lines |
| Versions | per component: current tag, Unreleased entries by class, change class and next version, changelog lint, manifest and API.md, tag check; **Refresh** (a job); per component the signals, findings, manifest patch and **Draft release** (a job writing a draft folder for a person; nothing is pushed) |
| Bumps | shadow-mode bump proposals: queue with filters, one page per proposal (changelog, defects, matrix cell or Run, patch download, **Mark done** / **Decline**), the shadow ledger; **Scan now** queues `nanosoc-rm bumps --store`. Nothing is pushed; the schedule is DISABLED until the Phase 4 gate |
| Releases | release drafts (lock, errata document, notes, checklist, gate ledger), **Draft** and **Verify** as jobs, download a draft as a zip, the errata document; forks of the example projects with **List forks now**. Input files (archive, GDS, netlist) are picked from `<data_dir>/release-inputs/`. Nothing is published |

Every fact links to `src`: the commands that found it. Every page shows its data age (the oldest fact on it).

## Jobs

Run, Sync, Walk, a quality analysis, a release draft or verify and a fork listing queue a job; one runs at a time. A job is the CLI command shown on its card, run as a child
process: cancel stops it and everything it started. A run without pin overrides is stored (the dashboard shows
it); a what-if with overrides is shown but never stored. If the service stops while a job runs, the job becomes
INFRA with the reason when the service starts again.

Times on the eth configurations: walk about 15 s; compat (T0) about 3 minutes; mirror sync of every component
several minutes.
