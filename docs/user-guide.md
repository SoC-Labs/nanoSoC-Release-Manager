# User guide

Task by task: each section answers one question, first in the web pages, then on the command line. Words are
defined in [concepts.md](concepts.md); every command and option is in [cli.md](cli.md). First visit:
[getting-started.md](getting-started.md).

The tool only reads repositories. It never pushes, tags or deletes; where a change is due it prints the command or
writes a patch for a person.

Marked **(coming)**: agreed but not built yet. Everything else is on master.

## Contents

1. [Set up the command line](#1-set-up-the-command-line)
2. [Is my SoC compatible?](#2-is-my-soc-compatible)
3. [Should I bump this submodule?](#3-should-i-bump-this-submodule)
4. [Can I delete this branch?](#4-can-i-delete-this-branch)
5. [What is red, and why?](#5-what-is-red-and-why)
6. [How do I release?](#6-how-do-i-release)
7. [Tidy comments and READMEs](#7-tidy-comments-and-readmes)
8. [Classify a new repository](#8-classify-a-new-repository)
9. [Troubleshooting](#9-troubleshooting)
10. [The pages](#10-the-pages)

## 1. Set up the command line

The web pages need nothing on your machine. For the command line (about 5 minutes):

```
git clone https://github.com/SoC-Labs/nanoSoC-Release-Manager nanosoc-rm && cd nanosoc-rm
scripts/dev-env.sh                      # Python 3.11 venv in .venv with the nanosoc-rm command
export PATH=$PWD/.venv/bin:$PATH
```

You also need:

- **A config file** (TOML, outside this repo), named by `NANOSOC_RM_CONFIG`. Minimal anonymous example (every key:
  [contracts/interfaces.md](contracts/interfaces.md) section 17.1):
  ```toml
  [service]
  data_dir = "/path/to/rm-data"                    # mirrors and the local store live here
  [registry]
  path = "/path/to/nanosoc-registry"               # a checkout of the registry repo
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
  ```
- **A registry checkout**. Check it with `nanosoc-rm registry validate`.
- **Mirrors** of the repos you ask about: `nanosoc-rm mirror sync --component COMPONENT` (heads and tags only,
  anonymous).

Every command exits 0 answered (PASS), 1 finding or FAIL, 2 could not answer (UNVERIFIED, INFRA, missing data),
3 usage or config error, and takes `--json` (one JSON document per line).

## 2. Is my SoC compatible?

**On the pages.** Open the configuration (Home, select it, **Open the configuration**). **Overview** lists the
gates with the deciding rows first; **Submodule tree** shows every pin; **History** lists earlier runs. **Run**
on the board checks it now (a T0 check took about 35 seconds on eth-main once started; one job runs at a time, so it may queue first).

**On the command line.**

```
nanosoc-rm walk eth-main                      # resolve and store the whole submodule tree
nanosoc-rm pins eth-main                      # one line per pin with its state on each host
nanosoc-rm compat eth-rc4                     # walk, then the T0 gates locally; one line per pin, the run verdict
nanosoc-rm compat eth-main --json             # one compat_result document
nanosoc-rm compat eth-main --tier t1          # then the SoC's lint (T1) in a sandbox checkout
nanosoc-rm compat eth-main --tier t2          # or its smoke simulation (T2)
nanosoc-rm tiers eth-main                     # which tier commands the configuration declares, with budgets
```

`compat` resolves the configuration's tree from its own `.gitmodules` and gitlinks and runs the T0 gates with the
same code CI uses. Every gate runs its negative control in the same run; a gate whose control does not fire is
UNVERIFIED, never PASS.

| Gate | Checks |
|---|---|
| `t0.infra.probe` | both hosts answer anonymously |
| `t0.pins.fetchable` | every pin can be fetched without credentials |
| `t0.pins.reachable` | every pin is on a branch or tag of its canonical repo |
| `t0.urls.no-ssh-public` | no public parent pins a submodule by ssh URL |
| `t0.manifest.valid` | component manifests validate (when present) |
| `t0.clone.upgrade-path` | an anonymous clone of the previous release upgrades to this one |
| `t0.vendor.no-arm-ip` | no Arm source outside the catalogue's allowlist (red) |
| `t0.vendor.no-foundry-data` | foundry data, site inventory and release codes (amber, unless the catalogue marks a rule blocking) |
| `t0.runner.hardened` | NOT-RUN locally (only meaningful in CI) |

Verdicts and exit codes: [concepts.md](concepts.md#checks-and-verdicts-does-it-work). A component pinned at two
commits in one tree is printed as a split.

**Tiers.** `--tier t1|t2` runs the command the registry's `tiers.yaml` gives for that configuration, in a fresh
sandbox checkout of the resolved tree, with a clean environment (`set_env.sh` is never sourced), a time budget
(killed at 1.5 times the budget) and one VCS seat. A tier runs only when T0 is not FAIL; `--force-tier` runs it
anyway and marks the result UNVERIFIED for release. A command that regenerates `build_soc` needs
`NANOSOC_GEN_OK=1`. `--keep-work DIR` keeps the sandbox; `--store` records a run without overrides.

The tool's host has no EDA tools, so a tier job (`compat --tier`, a matrix tier cell, a test-bump) queued from the
pages waits for an **executor**: a sandboxed, unprivileged pull agent on a lab EDA server (DEPLOY.md, "Remote tier
executor"). Each repository declares its own tests in `.nanosoc/capabilities.yaml`, read at the commit under test
(`nanosoc-rm capabilities validate FILE` while you write one; `capabilities show CONFIG` lists what each tree can
run and where). Run on your own EDA login, `compat --tier` still runs the tier where you are.

**Worked examples (real data, 2026-10-11).**

- `compat eth-rc4` (143 s): fetchable, reachable and no-ssh are WAIVED (rc4's approved waivers for its frozen ssh
  pins and the private ASIC toolkit). Vendor is FAIL on one tree, nanosoc-multicore-system: one Arm banner in a
  DMA-250 config file that the DMA-230 allow entry does not cover. 12 trees are WARN under the allowlist.
- `compat eth-main --tier t2 --force-tier`: the D2D decode smoke benches PASS in 29 s; the run stays FAIL because
  eth-main pins TideLink by ssh.

## 3. Should I bump this submodule?

**How far behind is it?** Home shows each pin's freshness; the Matrix's **Freshness** tab shows one row per
repository and one column per configuration. On the command line:

```
nanosoc-rm freshness show                      # one line per tree: verdict, lagging pins, days since the newest bump
nanosoc-rm freshness show eth-main --tree      # every pin, depth first, with the worst state beneath it
nanosoc-rm freshness scan --store              # re-measure every configuration (the scheduler does this after each poll)
```

**Would the bump break it?** On the pages: the configuration's **What-if** tab. Pick a component and a target,
**Run what-if**, then read **Now vs After** per gate. On the command line, override the gitlink at a tree path
(full 40-hex sha; repeatable; never stored):

```
nanosoc-rm compat eth-main --pin nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=<sha>
nanosoc-rm contains <sha>                      # which configurations already contain that commit
```

**Across versions.** The Matrix's **Compatibility** tab (or `nanosoc-rm matrix`) tries each component at its
pinned commit, the latest tag in range, the latest tag and its default-branch head:

```
nanosoc-rm matrix eth-rc4 --component tidelink --component nanosoc_gen     # the grid from stored results
nanosoc-rm matrix eth-rc4 --component tidelink --run --wait                # run its cells as local jobs
nanosoc-rm matrix cell eth-rc4 --column head --component nanosoc_gen       # one cell in the foreground
nanosoc-rm matrix quarantine eth-rc4                                       # tests FLAKY twice in 14 days
```

A FAIL cell re-runs once (FLAKY when green the second time), host failures are INFRA, and a run that read other
commits than it asked for is UNVERIFIED. `--tiers t1` (or `matrix cell ... --tier t1`) adds a lint cell that runs
the configuration's t1 command after T0; a pin the command moved in its checkout makes the cell UNVERIFIED. The
nightly matrix waits for its phase gate.

**What would the bot propose?** Bump proposals run in shadow mode: the tool says what it would do; a person does
it.

```
nanosoc-rm walk eth-main && nanosoc-rm bumps eth-main --store   # releases newer than each pin, deepest first
nanosoc-rm bumps show <id>                                       # changelog, class, defects, matrix cell, blockers
nanosoc-rm bumps patch <id> --out <id>.diff                      # then, in the consumer: git apply --index <id>.diff
nanosoc-rm bumps mark <id> done --by <login>                     # a person applied it
```

Frozen and silicon configurations are never proposed.

**How big is the change?** A change's class (breaking, feature, fix, none) comes from the component's classed
changelog, its API.md and, for generated RTL, a sandbox regeneration at both commits:

```
nanosoc-rm regen class multicore-master --component nanosoc_gen --from <sha> --to <sha> --store
nanosoc-rm regen pending --run           # measure every move a stored version plan or bump proposal needs
```

A configuration opts in with a `regen` entry in the registry's `tiers.yaml`. The tree is checked out anonymously in
a sandbox with the component moved and the command runs in a clean environment; the vendor-library variables are
never set, so the vendor renderers are skipped and listed as not covered.

**Test this bump.** `nanosoc-rm test-bump eth-main tidelink v1.0` (TO: a tag, a branch, a full sha or a 7+ hex
prefix; `--plan` shows what would run, `--queue` queues it for an executor), or the **Test this bump** button on a
pin: one paired run, before and after at the same root commit and the same tests, chosen from the capability
manifests (the moved tree's own tests, or the ones its parent composes, plus every consumer test whose
`affected_by` names the moved path), reported as hygiene (T0) and functional (capabilities) side by side. Exit 0
PASS or WARN (already red, still red), 1 FAIL (green to red), 2 UNVERIFIED, 3 usage. A bump proposal reads `would:
hold` until a PASS or a WARN of exactly that move exists; with a WARN it notes "already red before
the bump" and names the gates.

A repository's CI asks the same question: `GET /api/v1/bump-check?component=&path=&to=&root=` answers from the
same evidence, with a reader token (`nanosoc-rm executor token new NAME --out F --reader`). The eth chiplet and
TideLink carry the CI job and a warn-only pre-push hook on their `rm/capabilities` branches (not merged there yet).

## 4. Can I delete this branch?

**On the pages.** The component's **Branches** tab: every branch on both hosts, the delete list first. Tick
branches to adjust it; copy the list; run it yourself. **Re-read mirror** refreshes it.

**On the command line.**

```
nanosoc-rm mirror sync --component tidelink    # refresh both hosts' copies
nanosoc-rm branches tidelink --all             # one row per branch
nanosoc-rm branches tidelink --all --delete-list
```

Verdicts (MERGED, STALE, PINNED, PROTECTED, ACTIVE): [concepts.md](concepts.md#branches). `--delete-list` prints
`git push <remote> --delete <branch>` lines for MERGED and STALE branches only; nothing pinned, tagged or cited is
listed. Exit 0 nothing to delete, 1 something to delete, 2 no mirror or a repo could not be judged.

Example (2026-10-11): `branches tidelink --all` found 29 branches, 9 deletable (8 merged, 1 stale), 5 protected by
archive tags.

## 5. What is red, and why?

**On the pages.** The bell opens **Notifications**: one line per thing that needs a person, unread first, with one
action each. **Notify me about** chooses kinds and projects. The full cards with evidence and **Snooze** are at
`/inbox`; open alerts, grouped by cause, at **More > Alerts**. Every fact has a **src** link to the exact command
that found it.

| Question | Page | Command |
|---|---|---|
| Which pins are red on which host? | configuration > Submodule tree | `nanosoc-rm pins CONFIG [--state S] [--host H]` |
| Is taped-out silicon still protected? | More > Silicon protection | `nanosoc-rm silicon-protection [CONFIG] [--uncovered]` |
| Do the GitHub and GitLab copies agree? | More > Host copies | `nanosoc-rm hosts [COMPONENT]` |
| Which configurations contain a commit? | More > Contains commit | `nanosoc-rm contains SHA` |
| Which repos exist but are not catalogued? | **Catalogue** | `nanosoc-rm inventory --unclassified` |
| What alerts are open? | More > Alerts | `nanosoc-rm alerts` |
| Are the mirrors fresh? | More > Mirrors | `nanosoc-rm mirror status` |
| Which errata apply? | More > Components (Defects and errata) | `nanosoc-rm registry records` |

A frozen configuration's known failures read WAIVED (amber), with the raw verdict and every waiver's reason and
approver beside it; a new failure on the same tree still reads red.

## 6. How do I release?

Everything here writes local drafts for a person. Nothing is tagged, pushed or opened by the tool; each phase gate
still governs the automation.

**Versions.** Each component keeps a classed `CHANGELOG.md` ([versioning.md](versioning.md)):

```
nanosoc-rm changelog lint nanosoc_gen          # every entry carries breaking/feature/fix/none
nanosoc-rm version next nanosoc_gen            # next version from the class
nanosoc-rm version tags tidelink               # tag names, grandfathered tags, moved release tags
nanosoc-rm manifest init tidelink --patch m.diff
nanosoc-rm version draft nanosoc_gen           # a tool release draft folder
```

Pages: **More > Versions**.

**SoC releases.** A release draft holds the release lock (every pin with its host, state and protection), the
errata document with its revision, release notes, a CHECKLIST.md of the tag commands for a person, GATES.md (the
gate ledger to sign) and a generated-RTL md5 manifest.

```
nanosoc-rm release draft eth-rc4 1.0.0 --archive build_soc.tar.gz --gds chip.gds --netlist chip.pnr.v
nanosoc-rm release verify eth-rc4/1.0.0 --against <dir holding build_soc/>   # re-check against the mirrors now
nanosoc-rm release list
nanosoc-rm forks --store                                   # public forks of the example projects
```

Pages: **Releases**. Exit 0 PASS/WARN/WAIVED, 1 FAIL, 2 UNVERIFIED (gate evidence or a person step still open).

**Publish.** The one write the tool can make outside its host, and only as a person's confirmed step: a signed
record to the release store, never over an existing file, read back and verified.

```
nanosoc-rm publish-release eth-rc4/1.0.0 --dry-run           # the record, its sha256, every target path
nanosoc-rm publish-release eth-rc4/1.0.0 --confirm <sha256>  # publish exactly what the preview showed
nanosoc-rm register-tapeout <tapeout id> --dry-run
```

The **Publish** page does the same for a login listed as a publisher; a shared login never publishes. What the
store holds: **More > Store**, or `nanosoc-rm store index|search|findings`.

## 7. Tidy comments and READMEs

Read-only reports on any catalogued repo at any ref, or a local checkout; every fix is a patch you review and
apply with `git apply`. No config is needed with `--path`.

```
nanosoc-rm quality density  --path ~/work/my-repo                     # comment ratio per language, directory, file
nanosoc-rm quality comments --path ~/work/my-repo --patch fix.diff    # findings plus mechanical edits as a patch
nanosoc-rm quality readme   --path ~/work/my-repo --patch layout.diff # stale paths, make targets, submodules, flows
nanosoc-rm quality docs     --path ~/work/my-repo                     # conciseness score per Markdown file and section
nanosoc-rm quality all tidelink --ref main --store                    # a mirrored component, stored for trends
```

On the pages: the component's **Quality** and **Review** tabs. Review shows one finding at a time (**Accept**,
**Edit**, **Skip**, **Not a finding**); accepted edits go to your patch basket, downloaded as `patch.diff`.
`docs --rewrite` sends sections to the Anthropic API only when an API key is configured.

## 8. Classify a new repository

The nightly org scan lists repositories the catalogue does not hold. On the **Catalogue** page, choose a group
for each (Project, Component, Toolkit, Arch tech, **More…**, or **Not nanoSoC**), then **Save to catalogue**: it
shows the `catalogue.yaml` patch and whether the patched catalogue would load. Apply the patch in the registry by
merge request; the tool never writes the registry.

```
nanosoc-rm catalogue org-scan --store       # repos not in the catalogue
nanosoc-rm catalogue show --unconfirmed     # entries with a guessed field, and the open question
nanosoc-rm catalogue show --gaps            # SoCs that no forkable project wraps
nanosoc-rm catalogue preview choices.json --out cat.patch
```

To have a project measured, register a configuration for it: a merge request adding it to the registry's
`eligibility.yaml`.

## 9. Troubleshooting

| You see | Cause | What to do |
|---|---|---|
| INFRA "tool not on this host", or a job "waiting for an executor" | the tier needs an EDA tool the tool's host lacks; tier jobs run on an executor | ask the operator whether the EDA-server agent is installed and running (DEPLOY.md, "Remote tier executor") |
| Not measured, or UNREADABLE, on a private repo | the tool reads anonymously unless the operator configured read access for that host | ask the tool owner; the operator step is in [DEPLOY.md](DEPLOY.md), "Private repos" |
| A T0 FAIL that never goes away | a long-standing hygiene problem, often an ssh URL in `.gitmodules` | fix the URL in the repo (use https), or, for a frozen tree, a waiver in the registry |
| UNVERIFIED for a pin you moved in a what-if, "not fetched" | the moved commit was never fetched or scanned (an ssh or private URL) | fix the URL, or test the bump on an executor; a FAIL found elsewhere still reads FAIL |
| "oldest fact 3 days" on a fresh-looking page | the header shows when the page was last updated and the age of its oldest fact | follow the oldest fact's link in the header to see what it is and how it was found |
| Sign-in refused with the right password | 5 wrong passwords for that login within 5 minutes lock it for the rest of the 5 minutes | wait 5 minutes, or ask the operator to restart the service |
| A Quality page says "not analysed yet" | nothing stored for that ref, and opening a page never starts an analysis | press **Analyse** |
| A page says "analysing, refresh" | an analysis job is queued or running | refresh in about a minute |
| Exit code 3 | usage or config error: unknown configuration, bad `--pin`, missing `NANOSOC_RM_CONFIG` | read the message; `nanosoc-rm <command> --help` |

## 10. The pages

The header holds **Projects** (Home), **Matrix**, **Releases** and **Catalogue**; every other page is under
**More**, in three groups named as each page is titled: **Check** (Compatibility check, Silicon protection, Code and
doc quality, Alerts), **Repos** (Components, Branches, Host copies, Mirrors, Contains commit) and **Release**
(Versions, Bump proposals, Store, Publish). `/` focuses the search box (configurations, components, commits); the
bell opens Notifications. The header shows two clocks, "Updated 4 min ago · oldest fact 3 days"; the oldest links to
that fact. Every fact links to **src**, the commands that found it.
States are always a word beside a shape, never colour alone. Buttons that change something exist only in a local
deployment; elsewhere the page shows the CLI command instead.

| Page | What it is for |
|---|---|
| Projects (`/`) | **What needs me?** first: at most three one-line actions, each with one link (a taped-out configuration with no published release, held bump proposals, private repos the mirrors cannot read, repos that need a catalogue group), hidden when there are none. Then the tree of projects, configurations and pins with a freshness state each ([section 3](#3-should-i-bump-this-submodule)); freshness pills and **Filters** narrow it; **Timeline** draws each row's last bump; the right panel shows the selected row (for a pin: behind, upstream head, last bump, where else it lags, **Test this bump**; for a configuration: **Scan now**). Projects that cannot be measured fold into **Registered, not scanned yet** and **No configuration registered** (whose detail says how to register one). Under the tree, closed: the configuration board with the last check verdict, red pins, errata, host drift and **Run** |
| Notifications (the bell) | one line per item that needs a person, unread first; **Mark read**, **Mark all read** (only what the page showed), **Notify me about** (kinds and projects). The full cards with evidence and **Snooze** are at `/inbox` |
| Configuration (`/configurations/<id>`) | tabs Overview (gates, deciding rows first; pins; waivers), Submodule tree, What-if (Now vs After per gate), Matrix, Silicon, History; **Walk now** |
| Component (`/components/<id>`) | tabs Overview, Branches (the delete list; [section 4](#4-can-i-delete-this-branch)), Quality, Versions, Review (one finding at a time into a per-login `patch.diff`) |
| Matrix (`/matrix/`) | **Freshness**: one row per repository, one column per configuration. **Compatibility** (`?tab=compat`): each component at its pin, the latest tag in range, the latest tag and its default-branch head; **Run cell/row/column/all** queue T0 jobs; the nightly matrix is off until its phase gate |
| Releases, Publish | release drafts with **Draft** and **Verify** jobs (input files, such as the archive, GDS and netlist, are picked from `<data_dir>/release-inputs/` on the host), the errata document, forks of the example projects; Publish previews and then publishes a signed record ([section 6](#6-how-do-i-release)) |
| Catalogue | new repositories from the nightly org scan (**Scan now**), entries with an open question, every group; **Save to catalogue** shows the `catalogue.yaml` patch ([section 8](#8-classify-a-new-repository)) |
| Capabilities (`/capabilities/<id>`) | what each tree of a configuration declares in `.nanosoc/capabilities.yaml`, which hosts can run it, and its last test-bump result |
| More > Check | Compatibility check, Silicon protection (taped-out commits and whether a tag protects each, uncovered first), Code and doc quality (**Analyse**), Alerts (grouped by cause, one group is one notification; **Clear all** of a group, refused if the group changed since the page was drawn, or **Clear** one) |
| More > Repos | Components (with Defects and errata), Branches, Host copies (**Compare now**), Mirrors (**Sync**; how each repo was read), Contains commit |
| More > Release | Versions, Bump proposals (shadow-mode proposals; **Scan now**), Store, Publish |

**Repository, line, submodule pin.** A configuration is a line of its repository and reads "branch main @ 8e6d043"
or "tag v1.0.0 @ ab73e40"; a submodule pin reads "path @ sha"; a repository pinned at two commits in one tree says
PINNED ×2 on both rows. The legend is under **How is this judged?**. Codes such as ON_DEFAULT or T0 print as words;
hover for the code.

**Jobs.** Run, Sync, Walk, an analysis, a draft or verify and a fork listing each queue a job; one runs at a time,
and its card shows the CLI command it runs. **Cancel** stops it and everything it started. A run without moved pins
is stored; a what-if is shown but never stored. Tier jobs wait for an executor. If the service stops while a job
runs, the job reads INFRA with the reason. Times on the eth configurations: walk about 15 s, a T0 check about 35 s
once started, a sync of every mirror several minutes.
