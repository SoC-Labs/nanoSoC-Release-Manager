# The web GUI on one host (local mode)

The same pages as the service VM will serve, run from a login server with no GitHub OAuth app and no CI
project: local sign-in (CR-16) and a local job worker (CR-17). As a service: `docs/DEPLOY.md`. Contracts: `docs/contracts/interfaces.md`
sections 13, 13a, 14 and 25 (the front pages).

First visit: [getting-started.md](getting-started.md). What the states and verdicts on these pages mean:
[concepts.md](concepts.md) (the "How is this judged?" box on Home and the Matrix summarises the freshness part).

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
| Shell | every page: a slim header (Projects, Matrix, Releases, Silicon, Catalogue; every other page under **More**; search, `/` focuses it, `GET /search` finds configurations, components and commits; the bell with the unread count), one data age, light and dark themes. Workspace and tool pages also have the left rail (notifications count, each configuration with its deciding gate and a text chip, flagged components, alert lines, Tools) and breadcrumbs starting at Projects. Projects, Matrix, Catalogue and Notifications are full width |
| Projects (`/`, the home since CR-WF-gui) | one toolbar line: freshness pills (Outdated, Behind, Current, Frozen, Not measured; Unpushed, and Current except not measured, when any) that filter, **Filters** (Type: Project / SoC / Subsystem; Builds a: Chip / Chiplet; Role: Implementation / Forkable template / Example; **No forkable project**: SoCs no forkable project wraps), **Timeline** (off by default; your choice is saved for your login), **How is this judged?**. Below it the tree: project, then its configurations, then their pins in the order SoC, custom peripherals, subsystems and components, arch tech, generator, libraries, toolkits and flows. Each row: name, state word beside a shape (never colour alone), "N d ago" (since the pin last moved); a parent shows its worst child; a configuration is judged on its direct pins, and is Current only when every direct pin was measured and is current: when some could not be measured and none is worse it says **Current except N not measured** (amber, a half-filled dot; David 2026-10-15); a frozen silicon configuration says Frozen and "not counted". Rows are links (open, close, select; the page state is in the URL). Projects with no measured configuration fold into **Not measured yet**. The right panel shows the selected pin (behind, upstream head, last bump with author, pinned sha, branches, other configurations where the same pin lags), configuration (verdict, lagging pins, root, the scan, **Scan <id> now**) or project (type, role, what it wraps). Timeline on: a bar per row from its last bump to today on one axis, the today line and a dashed line at each frozen configuration's tapeout. Under the tree, closed: the configuration board (last compat or CI T0 verdict, red pins, errata, host drift, **Run**) |
| Notifications (`/notifications`, the bell) | the inbox's items, one line each with one action; unread first; **Mark read** (an item comes back when its facts change), **Mark all read** (only what the page showed), **All** shows read items too; **Notify me about**: tick kinds (red checks, silicon, branches, hosts, mirrors, quality, bumps, new repos) and projects, saved for your login. The full cards with evidence and **Snooze** are at `/inbox` |
| Configuration workspace | `/configurations/{id}` tabs: Overview (gates with the deciding rows first, each vendor gate with a one-line explanation of what it judges (CR-6: Arm source fails; foundry data and site inventory, and (CR-19) release/bundle codes, are amber unless the catalogue marks a rule blocking; a vendor gate red only because trees were not fetched reads "{n} trees not scanned (not fetched)", never "Arm source in"), pins, waivers), Submodule tree (`?show=uncovered`, a node aside), What-if (pick a component and a target, **Run what-if** as a job, then Now vs After per gate), Matrix, Silicon (uncovered rows first, errata), History (jobs and runs); **Walk now** |
| Compat | move pins to any tag or branch in the mirror, choose T1/T2, run; live log; per-gate and per-pin table. `/compat?job=N` and `?configuration=` open the workspace's What-if tab |
| Component workspace | `/components/{id}` tabs: Overview, Branches (every copy by default; the delete list to copy comes first and follows the ticks; the tool never deletes; **Re-read mirror**), Quality, Versions, Review (one comment finding at a time: **Accept**, **Edit**, **Skip**, **Not a finding**; accepted edits go to a per-login patch basket, downloaded as `patch.diff`) |
| Branches | `/branches` across components; `?component=` opens that component's Branches tab |
| Hosts | canonical vs copy relation; **Compare now** (a POST: a link can never start one) |
| Contains, Silicon | containment of a commit; silicon-protection rows |
| Registry | components, defects, errata |
| Mirrors | fetch status and age; how each repo was read (public, private with credentials, unreadable and why: `private_read`, CR-PR1, docs/DEPLOY.md); **Sync** one component or all |
| Alerts | open alerts grouped by cause (messages redacted); **Clear all** of a group after fixing it (refused with 409 if the group changed since the page was drawn) and **Clear** one; each clear is logged with your login |
| Matrix | `/matrix/` opens on **Freshness**: one row per repository (tabs Projects, Toolkits, Flows, Components, Generators, Arch tech, Libraries, Boards, Legacy, External; sort Most behind or Oldest bump), one column per scanned configuration, each cell the state word, commits behind and days since bump; each column head the tree's verdict (Current except N not measured when a direct pin could not be measured); a repository with a live cell nobody could measure is never called current and stays under Lagging only; a frozen column is greyed and not counted; a cell opens one detail line linking to that pin on Projects. The **Compatibility** tab (`?tab=compat`) is the D3 matrix: per configuration: a row per component, columns pinned / latest tag in range / latest tag / default-branch head; each cell's verdict (text and colour), age and stale flag; **Run cell**, **Run row**, **Run column**, **Run all** queue local jobs (a T0 run each, about 3 minutes); a cell page with every attempt, the D4 provenance and the comparison with the same night's pinned run; the D8 quarantine list. The nightly matrix is disabled until the Phase 2 gate |
| Quality | comment density, comment findings with a patch, README check with a layout patch, doc conciseness, trend; a page with no stored result says "not analysed yet" and offers **Analyse** (a POST; opening a page never queues work), then "analysing, refresh" while the job runs (about a minute on a large repository). The overview opens on **Repos in my configurations** when any listed target is one, otherwise on **All targets** (chip row: that, or **All targets**). Banners and header boilerplate are one **decoration** rule: hidden from the table, the queue and the patch until you include it. Each finding has a severity per kind (stale TODO High, dead code Medium, unknown name Check, restating comment Low); rows with a suggested edit come first, and the preview shows the real deleted lines |
| Versions | per component: current tag, Unreleased entries by class, change class and next version, changelog lint, manifest and API.md, tag check; **Refresh** (a job); per component the signals, findings, manifest patch and **Draft release** (a job writing a draft folder for a person; nothing is pushed) |
| Bumps | shadow-mode bump proposals: queue with filters, one page per proposal (changelog, defects, matrix cell or Run, patch download, **Mark done** / **Decline**), the shadow ledger; **Scan now** queues `nanosoc-rm bumps --store`. Nothing is pushed; the schedule is DISABLED until the Phase 4 gate |
| Catalogue | `/catalogue/`: **New repos** seen by the anonymous org scan that the catalogue does not hold (the scheduler lists the org and the group every night; **Scan now** queues the same read-only, anonymous scan as a job and the line under the heading shows its state and log; local deployments only), each with one-click Project (with Builds a and Role), Component, Toolkit, Arch tech, **More…** (any other group) or **Not nanoSoC** (ignored); every row starts on **Decide later** and the suggested group is only a hint, so an untouched Save writes nothing; a choice is tied to its repo, not its row, so a scan landing meanwhile cannot move it; **Needs your call**: the guessed groups with their question and a group dropdown (or **Confirm** the guess, or **Decide later**); every group with its repos (? = guessed); the ignored list; **Save to catalogue** shows, under **What Save writes**, the catalogue.yaml patch and whether the patched catalogue would load; **Download the patch** saves it as `catalogue.patch`. Nothing is written to the registry: apply the patch there by merge request |
| Freshness (retired) | `/freshness/` is retired (David 2026-10-15): it duplicated Projects and the Matrix's Freshness tab. Old links redirect to Projects; one naming a configuration (`/freshness/?configuration=eth-main` or `/freshness/eth-main`) opens that configuration's row there. Scanning stays: **Scan <id> now** in a configuration's detail panel on Projects, the scheduler after every poll, and `nanosoc-rm freshness scan` / `freshness show` on the command line. There is no navigation entry. Interfaces section 24.4 |
| Publish | `/publish/`: what this tool published (the ledger), drafts ready to publish, the registry's tapeout drafts; per release or tapeout **Preview** (a job: builds the signed record and reads the store) then **Publish** (a job: exactly the previewed sha256, tick the box). Only for logins in `[stores.artifactory] publishers`, never a shared login; disabled with the reason when no token or signing key is set (CR-ART-gui-publish) |
| Releases | release drafts (lock, errata document, notes, checklist, gate ledger), **Draft** and **Verify** as jobs, download a draft as a zip, the errata document; forks of the example projects with **List forks now**. Input files (archive, GDS, netlist) are picked from `<data_dir>/release-inputs/`. With a store index: **Published** and **Store** columns per draft and the store's documents with download links; publishing is on the Publish page |
| Store | `/store/`: what the artefact store holds as the last index read it: feature probes, counts per repository, GDS streams (raw md5, run tag, build, git identity or "no git identity recorded"), release and tapeout manifests, documents (download when a verified copy is held), source archives, build records, foundry returns; `?q=` finds by path, name, run tag, commit or md5; **Refresh the index** queues a job (about a minute). Also: Configuration > Silicon **Taped out** panel, History **Timeline**, Versions **Tags, commits and chips**, inbox cards `store_mismatch`, `release_not_archived`, `tapeout_unregistered`. Set-up: docs/DEPLOY.md |
| Capabilities | `/capabilities/{configuration}` (no navigation entry: Home's detail panel, the pin aside and `/capabilities/` link to it): per tree of the stored walk, its `.nanosoc/capabilities.yaml` at the pinned commit, each capability (kind, tier, gate, runtime, command, tools), the registry hosts that can run it and why not the others, and its last test-bump result. **Test this bump** on a pin (Home's detail panel, the pin aside, `/capabilities/{id}/bump?path=`): queues one job for an executor (never this host) that runs the right capabilities before and after the move; its page shows Now vs After per capability, the T0 hygiene and the functional verdict (Wave G, interfaces.md section 26) |

**Repository, line, submodule pin.** A configuration is a line (a branch or tag) of its repository and reads
"branch main @ 8e6d043" or "tag v1.0.0 @ ab73e40" with its id beside it; a submodule pin reads "path @ sha";
one repository pinned at two commits in a tree says PINNED ×2 on both rows. Each kind has its own icon (book =
repository, fork = branch, tag = tag, pushpin = submodule pin); the legend is under "How is this judged?". A branch
of a pinned repo is always written `repo:branch`. Codes such as ON_DEFAULT or T0 print as words; hover for the code.

Every fact links to `src`: the commands that found it. Every page shows its data age (the oldest fact on it).

## Jobs

Run, Sync, Walk, a quality analysis, a release draft or verify and a fork listing queue a job; one runs at a time. A job is the CLI command shown on its card, run as a child
process: cancel stops it and everything it started. A run without pin overrides is stored (the dashboard shows
it); a what-if with overrides is shown but never stored. If the service stops while a job runs, the job becomes
INFRA with the reason when the service starts again.

Times on the eth configurations: walk about 15 s; compat (T0) about 3 minutes; mirror sync of every component
several minutes.
