# Concepts and words

The one place that defines the words the Release Manager uses on its pages, on the command line and in the other
docs. Other docs link here instead of repeating these definitions. Contract-level detail (the exact rules the code
applies) is in [contracts/README.md](contracts/README.md) and [contracts/interfaces.md](contracts/interfaces.md);
where this page and a contract disagree, the contract is right and this page has a bug.

Marked **(coming)**: agreed but not built yet. Everything else here is on master (Wave G merged 2026-10-16).

## Things the tool knows about

| Word | Meaning |
|---|---|
| **Repository** (repo) | One git repository, on GitHub or GitLab, or both. |
| **Component** | A repository the registry's catalogue lists, with an id (`tidelink`, `nanosoc-ethernet-chiplet`). Each component has one **canonical host**: the copy that counts when the GitHub and GitLab copies disagree. |
| **Group** | What kind of component it is: project, soc, subsystem, component, toolkit, flow, generator, arch, lib, infra or legacy. A `?` beside a group means it was guessed and not yet confirmed (Catalogue page, **Needs your call**). |
| **Project** | A group: a top-level thing the team builds or ships, such as a chip, a chiplet or a forkable example project. Projects are the rows at the top of Home. |
| **SoC** | A group: a system-on-chip tree with its own submodules (for example the multicore SoC). A project usually wraps one SoC. |
| **Subsystem** | A group: a reusable block bigger than one IP, with its own submodules (for example the Ethernet subsystem). |
| **Type** and **role** | For projects, SoCs and subsystems: the type is chip, chiplet, soc or subsystem; the role is implementation, forkable (a template people fork) or example. |
| **Arch tech** | The CPU, debug, DMA and fabric units the architecture tree integrates. Other peripheral or interface IP is a plain component. |
| **Registry** | A separate git repository that holds the tool's facts a person decides: the catalogue (`catalogue.yaml`), the registered configurations (`eligibility.yaml`), errata, defects, waivers and per-configuration test commands (`tiers.yaml`). The tool reads it and never writes it: every change is a patch you apply by merge request. |
| **Catalogue** | The registry's list of components. The Catalogue page also shows the **new-repo queue**: repositories the nightly org scan found that the catalogue does not list yet. |
| **Mirror** | The tool's own read-only copy (heads and tags) of each catalogued repository, refreshed by `mirror sync`. Every page reads the mirrors and the stored facts, never the live forge during a page load. |

## Lines, configurations and pins

| Word | Meaning |
|---|---|
| **Line** | One branch or one tag of one repository. Always written with its repo, `tidelink:main`, never a bare `main`: two repos can both have a `main`. |
| **Configuration** | A line that is registered in `eligibility.yaml`, with an id such as `eth-main` (follows the branch `main` of the Ethernet chiplet) or `eth-rc4` (the tag `v1.0.0`). Only configurations are checked, scanned for freshness and shown with a verdict. A project with no configuration reads **Not measured yet**. |
| **Branch vs tag** | A branch moves when someone pushes; a configuration that follows a branch is re-judged at its newest commit. A tag names one commit for ever; a configuration on a tag is judged at that commit. |
| **Submodule pin** (pin) | The commit a parent repository records for one submodule path (its gitlink), written `path @ sha`, for example `tidelink @ 1107151`. The pin is what a recursive clone checks out, whatever branch `.gitmodules` names. |
| **Tracks B** | `.gitmodules` may name `branch = B` for a submodule. That only matters to `git submodule update --remote`; the pin is still one commit. Shown as **Tracks** in the pin panel. |
| **Tree** and **walk** | A configuration's whole submodule tree, every pin at every depth. A **walk** resolves it from the configuration's own `.gitmodules` and gitlinks and stores it; pages show the last stored walk. |
| **Direct pin** | A pin one level below the configuration's root. A configuration's freshness is judged on its direct pins. |
| **Pinned more than once** | One component pinned at two or more paths of the same tree (code `B7`, "split" in older text). The rows read **PINNED ×N** and the pin panel lists **Also pinned at**. Not to be confused with the branch verdict PINNED below. |
| **Hosts differ** | The GitHub and GitLab copies of one repository disagree. Pages say **HOSTS DIFFER** and how far one is ahead (`github ahead 23`). |
| **Frozen** | A configuration whose commit never changes (`frozen: true`), usually because it was taped out. Its old pins are old on purpose. |
| **Silicon** | A frozen configuration that became a chip (`silicon:` names the chip and revision). Its commits must stay reachable for ever; the Silicon page lists any that are not protected by a tag. |

## Freshness: how far behind is a pin?

Shown on Home and on the Matrix's Freshness tab. Read-only: it compares the stored walk with the mirrors.

| State | Meaning |
|---|---|
| **Current** | The pin is at the head of its upstream default branch. |
| **Behind** | 1 to 9 commits behind, and moved within the last 90 days. |
| **Outdated** | 10 or more commits behind, or behind and not moved for 90 days. |
| **Unpushed** | No host holds the pinned commit: someone committed a pin to a commit they never pushed. |
| **Not measured** | The commits behind could not be counted; the details say why (most often the repository is private and the tool has no read access to it). |
| **Current except N not measured** | A configuration whose measured direct pins are all current, but N could not be measured. It is never shown as plain Current. |
| **Frozen** | A frozen configuration. It shows what it would be if it were live, and is never counted as outdated. |

A configuration shows its worst direct pin; a pin row shows the worst state at or beneath it. "N d ago" is the time
since anyone last moved that pin: **Fresh** under 30 days, **Aging** 30 to 89, **Stale** 90 or more. Each state is
a word beside a shape, so colour is never the only signal.

Freshness and the check verdict below are two different questions: a configuration can be **Outdated** (its pins
lag) and **PASS** (what it pins works), or **Current** and **FAIL**. **(coming)** pages name both axes, for
example "Freshness: Outdated · Check: FAIL".

## Checks and verdicts: does it work?

| Word | Meaning |
|---|---|
| **Gate** | One named check, such as `t0.pins.fetchable` (every pin can be fetched without credentials). |
| **Negative control** | Every gate also runs on input built to fail, in the same run. A gate whose control did not fail is UNVERIFIED, never PASS: a check that cannot fail proves nothing. |
| **Tier** | A level of testing, cheapest first. **T0** resolve and hygiene (pins, URLs, vendor content; git only, under 2 minutes); **T1** lint and unit tests; **T2** smoke simulation; **T3** regression; **T4** FPGA build; **T5** on hardware; **T6** ASIC flow. The tool runs T0 itself and T1/T2 from the command a configuration declares. |
| **T0** | The resolve tier. Its gates: the hosts answer; every pin is fetchable and reachable from a branch or tag; no public repo pins a submodule by ssh URL; manifests validate; an old release upgrades by clone; no Arm source (red) and no foundry data (amber) leaks into a public tree. |
| **Capability** | A test a repository declares it can run (lint, elab, sim, cdc, formal, synth, fpga, ...), in `.nanosoc/capabilities.yaml` at its root: the command, the tools and licences it needs, its time budget, which submodules affect it, and how its result is judged. The tool reads the file at the commit under test, so the declaration moves with the code. The registry's `tiers.yaml` keeps only per-host paths and licence data (`hosts`). Page: `/capabilities/<configuration>`. |
| **Executor** | Where tier runs execute. The tool's own host has no EDA tools, so T1 and later run on a lab EDA server through a sandboxed, unprivileged pull agent (bubblewrap) that claims the job from the service and posts the result back; GitLab CI may take over later. Until an agent is installed, tier jobs wait ("waiting for an executor"). |
| **Hygiene vs functional** | T0 is hygiene (is the tree well formed and publishable?); the tiers are functional (does it build and simulate?). A test-bump reports both side by side, so a long-standing hygiene red does not hide the functional result. |

Every check ends in one verdict. Worst first:

| Verdict | Meaning | Exit code |
|---|---|---|
| **FAIL** | A real problem, with the evidence. | 1 |
| **UNVERIFIED** | Could not be checked: a control did not fire, a private repo could not be read, nothing was checked at all, or a person forced a tier past a T0 FAIL. Never counts as passing. | 2 |
| **INFRA** | The host could not run the check (for example "tool not on this host": the EDA tool is missing). Not the design's fault. | 2 |
| **WAIVED** | A frozen configuration's known, approved failure (see waiver). Amber: the raw verdict is still FAIL or UNVERIFIED. | 0 |
| **WARN** | Checked and passed, with listed warnings (for example foundry data findings at their default severity). Amber, not blocking. | 0 |
| **PASS** | Checked, and the control fired. | 0 |

Other words you will see beside verdicts: **NOT-RUN** (a gate that may legitimately skip, with the reason),
**NOT CHECKED** (never run for this configuration), **FLAKY** (a matrix cell that failed once and passed on the
re-run; two FLAKY results in 14 days quarantine the test). Exit code 3 always means a usage or config error.

| Word | Meaning |
|---|---|
| **Waiver** | A registry entry that accepts one known failure of a **frozen** configuration: one gate, one tree path, one result, with a reason, an approver and a date. It turns that item from FAIL into WAIVED so a new failure on the same tree still shows red. A waiver that covered nothing raises a `waiver_unused` alert so it can be retired. |
| **What-if** | A check of a configuration with one or more pins moved (`compat --pin`, or the What-if tab). Shown, never stored. |
| **Matrix** | For each component of a configuration: the check with that component at its pinned commit, the latest tag in range, the latest tag and its default-branch head. |
| **Test this bump** | `nanosoc-rm test-bump CONFIG PATH TO` or the button on a pin: move a component to a tag, branch or commit everywhere it is pinned, run the same capabilities before and after at the same root commit on an executor, and show each as same, better, worse, same-red or new-red. Green to red is FAIL; already red and still red is WARN. A bump proposal, and a repository's CI, need a PASS of exactly that move. |

## Pin states on a host

Each pin is also judged on each host (GitHub, GitLab). The pages show these as chips:

The chip shows the words; the code is its tooltip and what the CLI and JSON print.

| Chip (code) | Plain meaning | Colour |
|---|---|---|
| tagged (TAGGED) | a release tag points at the commit | green |
| on default branch (ON_DEFAULT) | on the default branch (shows how many commits it lags) | green, amber as it lags |
| kept by a tag (PROTECTED_BY_TAG) | not on the default branch, but a tag keeps it reachable | grey-green |
| on a side branch only (BRANCH_ONLY) | only some other branch keeps it; deleting that branch loses it | amber; red in a frozen tree |
| history rewritten, in a pull request only, on no branch or tag (REWRITTEN, PR_ONLY, ORPHAN), MISSING | not reachable from any branch or tag of that host, or absent | red |
| repo unreadable (UNREADABLE) | the repository cannot be read with the tool's access | red |
| local URL, no gitlink (LOCAL_URL, NO_GITLINK) | the `.gitmodules` entry is broken (a local path, or no gitlink at that path) | red |

**SSH PIN** beside a state means `.gitmodules` uses an ssh URL, which nobody without a key can clone.

## Branches

`nanosoc-rm branches` and a component's Branches tab give each branch one verdict:

| Verdict | Meaning | Safe to delete? |
|---|---|---|
| MERGED | its tip is on the default branch | yes, unless a tag or pin holds it |
| STALE | unmerged, no pin, no tag, no commit for 90 days | yes |
| PINNED | a registered configuration or a registry citation needs a commit only this branch holds | **no** |
| PROTECTED | a tag sits on its tip | **no** (the tag keeps the commits) |
| ACTIVE | unmerged and recent | no |

The tool only prints the `git push --delete` lines; a person runs them.

## Versions and releases

| Word | Meaning |
|---|---|
| **Change class** | Every changelog entry is one of breaking, feature, fix or none. The class, not the number, decides the next version and which gates a release needs. |
| **Classed changelog** | `CHANGELOG.md` at a repository root with entries under `### breaking`, `### feature`, `### fix` or `### none`; `nanosoc-rm changelog lint` checks it ([versioning.md](versioning.md)). |
| **Bump** | Moving a pin to a newer commit or tag. Bump proposals are shown in **shadow mode**: what a bot would propose, for a person to apply. |
| **Release draft** | A folder the tool writes for a person: release lock, errata document, notes, a checklist of the tag commands to run and a gate ledger to sign. Nothing is tagged or pushed. |
| **Publish** | The one write the tool can make outside its host, and only as a person's confirmed step: a signed release or tapeout record to the artefact store. |
| **Erratum** | A recorded defect of a released or taped-out tree, with its workaround and status. |

## How the project is run

| Word | Meaning |
|---|---|
| **Requirement id** | `B3`, `D2`, `H1`: one numbered requirement in [REQUIREMENTS.md](REQUIREMENTS.md) (frozen v1.0, 2026-09-30). Pages are moving these out of the visible text into tooltips. |
| **CR** | A change request: a numbered, agreed change to a requirement or a contract, one row in [CHANGES.md](CHANGES.md). |
| **Phase gate** | Each later phase (2 matrix, 3 versions, 4 bump bot, 5 releases) may be built and used by hand, but its automation (nightly runs, bot writes) stays off until its entry gate is met ([phase-gates.yaml](phase-gates.yaml); `nanosoc-rm phase-gates`). |
| **Wave** | One batch of parallel build work, merged together (Wave A to Wave G). Used in the project record, not on the pages. |
| **Hooks** | Git hooks that look at a submodule pointer change (`scripts/hooks/pre-push` in the eth chiplet and TideLink, on branch `rm/capabilities`, not merged there yet): they warn on a developer's machine; the same check blocks in CI (asking `GET /api/v1/bump-check`) and in the tool's bump gate. |
