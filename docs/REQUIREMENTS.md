# nanoSoC Release Manager: Requirements

**Version 1.0** · frozen 2026-09-30 · approved by David Mapstone in two sign-off rounds.

- Source proposal: `proposal-rev2.md` (the § numbers below point there).
- Sign-off record: `signoff-answers/` (round 1: 89 items; round 2: 8 items).
- Change control: after this freeze, any change is a numbered change request (CR-n) naming the IDs it affects, agreed the same way (PR2).
- **Must** items are required for the phase shown; **Should** items are expected unless a CR drops them.

The decision log and the Phase 0 actions are kept in the private registry (`DECISIONS.md`), because they describe
the lab's current security posture.

## PR · How we agree and build

### PR1 · Must · Process

Agree the requirements before any implementation starts. This page is round 1; round 2 shows only the items you marked Disagree or Change, rewritten.

- **Why:** Rework after building costs far more than a second review round.
- **Done when:** Every item is Agreed or Dropped, and the list is committed as REQUIREMENTS.md v1.0 in the tool's repo.
- **Source:** —

### PR2 · Must · Process

After the freeze, any change goes through a numbered change request (CR-n) that names the requirement IDs it affects and is agreed the same way.

- **Why:** Keeps the spec and the build in step as the lab's needs change.
- **Done when:** A CR log exists next to REQUIREMENTS.md.
- **Source:** —

### PR3 · Must · Process

Write the contracts before the code: the data formats (registry, component manifest, lock file, result bundle), the interfaces between modules, and one acceptance test per Must requirement. You approve them before building.

- **Why:** Fixed contracts let several agents build modules in parallel without colliding.
- **Done when:** Contracts and acceptance tests are merged and approved (gate 2).
- **Source:** §3.5, §4.4

### PR4 · Must · Process

Build in vertical slices, one phase at a time, v1 first. Each capability ships as a command-line tool first and a web view second.

- **Why:** The pin walker and silicon-protection list are useful in week one, before any web page exists.
- **Done when:** Each phase ends with a working, demonstrable slice.
- **Source:** §9

### PR5 · Must · Process

Run each phase with four agent roles: builders that each own one module on their own branch; independent reviewers that try to break each module; a coverage checker that confirms every Must requirement in the phase has a passing test; and one integrator that merges and runs end to end on the real repos.

- **Why:** Parallel builders are fast; independent reviewers and a coverage check catch what builders miss.
- **Done when:** You accept each phase from a live demo (gate 3).
- **Source:** —

### PR6 · Must · Process

An acceptance test counts only after it has been shown to fail on a deliberately broken input.

- **Why:** A check that cannot fail proves nothing; this lab has been caught by false greens before.
- **Done when:** Each test records its negative control and that it fired.
- **Source:** §4.5

### PR7 · Must · Process

Every commit and pull request names the requirement IDs it implements, and each release of the tool lists the requirements it delivers.

- **Why:** Lets you check coverage and trace any behaviour back to what you agreed.
- **Done when:** A traceability table (requirement → tests → commits) is generated for each release.
- **Source:** —

### PR8 · Must · Process

While v1 is built, agents stay read-only on all shared repos. Any write to a shared repo (tags, branch protection, settings) is listed for you to approve or run yourself.

- **Why:** Shared repos hold taped-out lineages that other people and sessions depend on.
- **Done when:** No agent credential in v1 has write scope on a shared repo.
- **Source:** §8.2

### PR9 · Should · Process

Reuse existing lab tooling before building anything new: the toolkit result format (verdicts.tsv), the fpgahub stack, the vendor-guard scanner, Artifactory, compute's pin checker and the design team's walk.py prototype.

- **Why:** Less new code to write and maintain, and formats people already use.
- **Done when:** The design lists each reused component and where it is used.
- **Source:** §8.3

## A · Scope and principles

### A1 · Must · v1

Track every nanoSoC-related repo on GitHub (SoC-Labs) and GitLab (soclabs) as a catalogue of components, each with exactly one canonical host.

- **Why:** One view of the whole estate, including the ~100 repos still on GitLab.
- **Done when:** All ~157 repos appear, each classified; any repo without a class is flagged.
- **Source:** §1, §2.1

### A2 · Must · v1 *(round 2)*

v1 never pushes, tags, merges or changes settings in any repo. Its only credential beyond read access is a trigger token that can start jobs in the CI project and nothing else.

- **Why:** The Run button needs a trigger token; nothing else in v1 needs write access.
- **Done when:** The service holds read-only repo credentials plus one trigger token scoped to nanosoc-compat-ci; every Run is logged with who pressed it.
- **Source:** A2 (amended for A4)

### A3 · Must · v1

Every fact the tool shows carries where it came from and when it was observed.

- **Why:** Stale data read as current has misled this project several times.
- **Done when:** Each page shows the data age; each fact links to the command or API call that produced it.
- **Source:** §2.1, §10.1

### A4 · Must · v1 *(round 2)*

The tool never runs tests itself. It starts CI jobs, on a schedule and when a member presses Run in the web interface, and then fetches their results.

- **Why:** Your note: you should be able to start jobs manually from the tool.
- **Done when:** Pressing Run on a configuration starts its T0 job in the CI project, and the result appears in the tool.
- **Source:** A4 (round 1 note)

### A5 · Must · v1

The tool is private to SoC Labs members.

- **Why:** It shows private repos, licensed IP paths and silicon details.
- **Done when:** Every page requires a SoC-Labs member login.
- **Source:** §1, §8.2

### A6 · Must · All

The tool never writes to users' forks and never withdraws a silicon release.

- **Why:** Forks belong to their users; silicon cannot change.
- **Done when:** No code path writes to a fork or yanks a silicon release.
- **Source:** §1

### A7 · Must · All

Capabilities beyond v1 (test matrix, versioning, bump bot, releases, forks) each start only when their entry gate is met.

- **Why:** Stops later phases being built on foundations that aren't ready.
- **Done when:** Each phase's entry gate is written down and checked before it starts.
- **Source:** §9

## B · Repos, pins and dependencies

### B1 · Must · v1

Classify each component as SoC reference, example project, generator, arch_tech, IP tech, toolkit, infrastructure or legacy. Legacy repos are listed but never tested.

- **Why:** Each class follows different versioning and release rules.
- **Done when:** Every catalogue entry has one class.
- **Source:** §2.2

### B2 · Must · v1

For each registered SoC configuration (a SoC repo at a branch or tag, including silicon references like rc4), resolve the full nested submodule tree from that commit's own .gitmodules and pinned commits.

- **Why:** Pins nest several levels deep; rc4 has 44 pins across 5 levels.
- **Done when:** rc4's tree of 44 pins is reproduced exactly.
- **Source:** §2.3

### B3 · Must · v1

Give every pin a state on each host: tagged, on the default branch (and how far behind), branch-only, protected by a tag, pull-request-only, orphaned, missing, rewritten, SSH-only, or a local path. Never trust the .gitmodules branch= field.

- **Why:** These states are how pins silently break.
- **Done when:** The states match a hand check on rc4 and multicore.
- **Source:** §2.4

### B4 · Must · v1

Answer "which configurations contain commit X?" on each host, counting only commits reachable from branches and tags, and only after proving both commits exist.

- **Why:** A missing commit otherwise reads as "not contained".
- **Done when:** Querying 036d381 lists the configurations that contain the cpu_ss_hsel fix, per host.
- **Source:** §1, §2.5

### B5 · Must · v1

Show a silicon-protection list: every commit in a silicon or frozen tree that its component's default branch cannot reach, and whether a protective tag covers it.

- **Why:** Deleting one unprotected branch can lose taped-out source.
- **Done when:** The list for rc4 shows the 11 commits and their coverage.
- **Source:** §1, §10.2

### B6 · Must · v1

Compare a component's copies on the two hosts and show same, ahead by n, or diverged; diverged shows red.

- **Why:** The GitLab and GitHub copies are independent repos, not mirrors.
- **Done when:** TideLink shows GitHub ahead; AHB-QSPI and ASIC-Flow show diverged.
- **Source:** §2.5

### B7 · Should · v1

Flag a component pinned at more than one commit in the same tree, and once CI captures the build's file lists, mark which pin the build actually uses.

- **Why:** rc4 carries two generators; only one produced its RTL.
- **Done when:** rc4 shows its 4 split components.
- **Source:** §2.3

### B8 · Must · v1

Detect when a default branch's history is rewritten (a force-push) and raise an alert.

- **Why:** The ASIC-Toolkit re-root left 3 pinned commits unreachable.
- **Done when:** A simulated rewrite on a test repo raises the alert.
- **Source:** §2.5

### B9 · Should · v1

Recognise the same repo under different URLs (redirects, moved groups) and flag any pinned URL that matches no known component.

- **Why:** The same repo is pinned under several URLs today.
- **Done when:** All current URL variants map to one component each.
- **Source:** §2.3

## C · Versions and change classes

### C1 · Must · Phase 3

Use Semantic Versioning for every component. Below 1.0.0, a breaking change bumps the middle number and a feature or fix bumps the last.

- **Why:** One scheme across generator, IP, toolkits and SoCs.
- **Done when:** The tag check enforces the scheme on every release.
- **Source:** §3.1

### C2 · Must · Phase 3

Every changelog entry records a change class (breaking, feature, fix or none), and every gate and auto-merge rule uses the class, not the version number.

- **Why:** Below 1.0 the number alone cannot tell a feature from a fix.
- **Done when:** A changelog lint rejects entries without a class.
- **Source:** §3.1

### C3 · Must · Phase 3

A change's class is the most severe of three signals: what the author declared, the diff of the public API, and the diff of every registered consumer's regenerated output (ports, instance names, memory and IRQ maps, and which module sits behind each address window).

- **Why:** On real history, commit messages alone classed 2 of 9 changes correctly.
- **Done when:** Replaying 354faa9, f8cf164 and the PL230→DMA-250 swap gives the classes agreed in review.
- **Source:** §3.1

### C4 · Must · Phase 3

Classify changes with the per-class decision table in §3.2 (generator, arch_tech, IP tech, toolkit, SoC reference, example project).

- **Why:** Makes "what counts as breaking" explicit for each kind of repo.
- **Done when:** The table lives in the spec, and each row has a worked example.
- **Source:** §3.2

### C5 · Must · Phase 3

A new check that fails by default, or a warning turned into a failure, is a breaking change.

- **Why:** Such a change stops builds that used to pass. (You chose fail-by-default for the HSEL lint, so the next generator release is breaking.)
- **Done when:** The rule appears in the decision table, with the HSEL lint as its example.
- **Source:** §3.1 rule 5

### C6 · Must · Phase 3

A repo that passes a submodule through to its users (arch_tech passes on nanosoc_gen and sys_desc) is classed at least as severely as the change it passes on.

- **Why:** Users of arch_tech call nanosoc_gen directly.
- **Done when:** Each manifest declares what it passes through (reexports).
- **Source:** §3.1 rule 6

### C7 · Must · Phase 3

A change that alters the set of files a SoC builds from, in any variant (simulation, FPGA or ASIC), is marked build-affecting and needs the regression tier before release or merge.

- **Why:** File-list changes have shipped the wrong RTL before.
- **Done when:** The flag is computed from file-list diffs, never set by hand.
- **Source:** §3.1 rule 9

### C8 · Must · Phase 3

Moving a repo to another host is a fix while the old host still serves the same commits; freezing or removing the old host is breaking.

- **Why:** Existing clones keep working only while the old URL does.
- **Done when:** An upgrade-path clone test proves it for each host move.
- **Source:** §2.5, §3.4

### C9 · Should · Phase 3

A repo reaches 1.0.0 only when it has an API.md, deterministic output, golden and consumer test sets, a classed changelog and at least one SoC release using it.

- **Why:** 1.0 promises a stable interface; these prove it can keep that promise.
- **Done when:** The release check refuses a 1.0.0 tag when any item is missing.
- **Source:** §3.1 rule 10

### C10 · Must · Phase 3

Tag names: releases vX.Y.Z (annotated, only on the canonical host, never moved or deleted); candidates vX.Y.Z-rc.N; backport branches release/X.Y; silicon silicon/<chip>-rNpM; protective keep/<config>/<component>. Existing tags are kept as they are.

- **Why:** Predictable names let tools and people find the right commit.
- **Done when:** A tag check rejects names outside the scheme, except the grandfathered tags.
- **Source:** §3.3

### C11 · Must · Phase 3

Each repo carries a manifest, nanosoc-component.yaml, giving its ID, class, version, canonical URL, what it provides, what it requires (with version ranges), what it passes through, its build variants and its test entry points.

- **Why:** The tool reads dependencies and tests from the manifest, not from guesses.
- **Done when:** The manifest schema is validated in T0.
- **Source:** §3.5

### C12 · Must · Phase 3

Each repo declares its public interface in API.md.

- **Why:** Breaking is defined against a declared interface.
- **Done when:** API.md exists in the generator, arch_tech, both toolkits and 3 IP repos before their 1.0.
- **Source:** §3.1 rule 1

## D · Testing and CI

### D1 · Must · All

Use these test tiers: T0 resolve (under 2 min, no licence); T1 static and unit (~10 min); T2 smoke simulation (5–15 min per SoC, 1 VCS seat); T3 regression (1–3 h); T4 FPGA build; T5 on hardware (manual); T6 ASIC flow (~6.5 h, manual).

- **Why:** Cheap checks run often; expensive ones run only where they add information.
- **Done when:** Each tier has a documented command per SoC and a runtime budget.
- **Source:** §4.2

### D2 · Must · v1

Run T0 nightly on every registered configuration. T0 checks that every pin can be fetched without credentials and is reachable from a branch or tag on its canonical host; that no public component uses SSH URLs; that manifests are valid; that an upgrade-path clone works; that no Arm IP has leaked; and that every commit cited in a defect record exists.

- **Why:** Catches broken or unprotected pins before anyone clones.
- **Done when:** A nightly T0 report exists for every configuration, with a negative control.
- **Source:** §4.2

### D3 · Must · Phase 2

Keep a compatibility matrix: one row per SoC configuration and, per component, columns for the pinned version, the latest tag in range, the latest tag and main. Nightly runs cover the pinned column plus components that changed that day; main runs weekly.

- **Why:** Shows when a generator or toolkit change breaks a SoC, without testing everything every night.
- **Done when:** The matrix view shows each cell's result and links to its evidence.
- **Source:** §4.1

### D4 · Must · Phase 2

Every run records its exact inputs: all commits (captured after the test), tool versions, a hash of the IP files used, host load and the test-harness version. A run whose captured commits differ from the ones requested is UNVERIFIED.

- **Why:** Setup scripts can silently move submodules to other commits.
- **Done when:** A run with a deliberately moved submodule is marked UNVERIFIED.
- **Source:** §4.4

### D5 · Must · v1

The tool fetches results from the CI system and checks the project, commit and settings they came from. It never accepts results pushed to it.

- **Why:** A job cannot forge its own verdict.
- **Done when:** A result with a mismatched commit is rejected.
- **Source:** §4.4

### D6 · Must · v1

Every gate runs a negative control in the same run. If the control does not fire, the gate is UNVERIFIED, not PASS.

- **Why:** Proves the gate can actually fail.
- **Done when:** Each gate's report shows its control and whether it fired.
- **Source:** §4.5

### D7 · Must · Phase 2

Infrastructure failures (licence denied, out of memory, lost runner, overloaded host) are recorded as INFRA and never counted as a test failure.

- **Why:** The shared server runs at load 85–106; overload must not read as a bug.
- **Done when:** A run killed for memory shows INFRA.
- **Source:** §4.5

### D8 · Must · Phase 2

Re-run a failing test once, alone. Red again is FAIL; green is FLAKY, which blocks auto-merge. Two FLAKY results in 14 days quarantine the test, with an owner and a 30-day expiry.

- **Why:** Separates real failures from noise without hiding either.
- **Done when:** The quarantine list is visible and expires on its own.
- **Source:** §4.6

### D9 · Must · Phase 2

Compare results test by test against the same night's pinned run, never against history.

- **Why:** Almost every historical run is red, so history is no baseline.
- **Done when:** Each comparison names its baseline run.
- **Source:** §4.5

### D10 · Must · v1

Run CI jobs on a dedicated, hardened runner: a service account, protected branches only, and a fresh home and temp directory per job. Not a shared or untagged lab runner, and not a shared login server. Budget in CPU-hours, not licence seats.

- **Why:** CPU is the scarce resource; licences are mostly idle.
- **Done when:** The runner's settings are recorded and checked.
- **Source:** §4.3

### D11 · Must · v1

Scan every result bundle and release file for vendor content before upload.

- **Why:** Logs from licensed-IP runs are not proven clean.
- **Done when:** A bundle containing a planted Arm banner is refused.
- **Source:** §4.4, §4.7

### D12 · Should · Phase 2

Store result bundles in Artifactory (falling back to disk). Keep logs for 90 days and release evidence forever.

- **Why:** Evidence must survive after scratch folders are cleaned.
- **Done when:** A release's evidence can be fetched a year later.
- **Source:** §4.7

## E · Automatic pointer bumps

### E1 · Must · Phase 4

When a component gets a new tag, open a bump pull request in each user whose declared range allows it, deepest first. Each PR regenerates generated RTL and lists the changelog, change class, defects fixed and introduced, and the matrix results.

- **Why:** Moves pointers forward with no manual work.
- **Done when:** A tagged fix produces correct bump PRs in the right order.
- **Source:** §5.1

### E2 · Must · Phase 4

Start the bot in shadow mode for at least 4 weeks or 20 bumps, whichever is later: it records what it would do and a person merges. Auto-merge is switched on per change class only after zero disagreements.

- **Why:** Proves the bot's judgement before trusting it.
- **Done when:** The bump-queue view shows the agreement rate.
- **Source:** §5.1

### E3 · Must · Phase 4

Auto-merge only when all of these hold: T0–T2 pass on the exact commits; controls fired; no test went from green to red; the class is fix or feature; the source is tagged; the target is an eligible, non-silicon default branch; nothing has diverged or split; no workaround retires; it is not part of a train; and no defect detector newly fires. A build-affecting change also needs T3 first.

- **Why:** Every condition closes a way a bad bump could slip through.
- **Done when:** Each condition has a test that blocks a merge when broken.
- **Source:** §5.2

### E4 · Must · Phase 4 *(amended per X1 a, confirmed 2026-09-30)*

The bot may create release-candidate tags, and the final tag of a tool's fix or feature release once T3 passes on the candidate. Breaking tool releases and every SoC release are tagged by a person.

- **Why:** A final tag is permanent; a candidate is not.
- **Done when:** The bot's credential cannot create vX.Y.Z tags.
- **Source:** §5.1

### E5 · Must · Phase 4

Never auto-merge into release/* branches, frozen or silicon references, or private GitHub repos.

- **Why:** Private GitHub repos cannot enforce required checks on the Free plan.
- **Done when:** Attempts on each kind of target are refused and sent to a person.
- **Source:** §5.2

### E6 · Must · Phase 4

After a merge, run T3 nightly on the merged branch. On a regression, open a revert PR and mark any candidate cut from it as failed.

- **Why:** Catches what the fast tiers missed.
- **Done when:** An injected regression produces a revert PR.
- **Source:** §5.1

### E7 · Should · Phase 4

Build the bump engine into the tool rather than using Renovate.

- **Why:** Two hosts, nested ordering and RTL regeneration are outside what Renovate does.
- **Done when:** The engine handles a two-host, two-level bump end to end.
- **Source:** §10.2 D3

## F · Defects and errata

### F1 · Must · v1

Keep two kinds of record in the registry: a defect (the root cause in a component: where it came from, its forms, triggers, detectors, fixes, effects and a regression bench pinned by commit), and an erratum (its effect on one SoC release or silicon revision: forms present, exposure, category A/B/C, conditions, workaround, status and document revision).

- **Why:** One defect affects different products differently, and fixes can be partial.
- **Done when:** The cpu_ss_hsel case is recorded as SL-DEF-0001 with errata for eth, compute and QuickStart.
- **Source:** §6.1–6.2

### F2 · Must · v1

Prove exposure by running detectors on each configuration's built output (generated RTL or netlist). Ancestry alone only shows "probably exposed" in amber.

- **Why:** Ancestry is wrong both ways; rc4 carries two generators and gitignored RTL.
- **Done when:** Each erratum shows how its exposure was established.
- **Source:** §6.2

### F3 · Must · v1

Every commit a defect or erratum cites must exist on its canonical host; T0 fails otherwise.

- **Why:** Evidence on unpushed branches disappears.
- **Done when:** A record citing an unpushed commit fails T0.
- **Source:** §6.1

### F4 · Must · Phase 5

Generate ERRATA.md for each SoC release candidate. Each silicon revision has its own errata document with its own revision number.

- **Why:** Users need one document per thing they hold.
- **Done when:** rc4's errata document is generated from the records.
- **Source:** §6.3

### F5 · Must · v1

Import the existing issue registers first: the eth known-issues, open-defects and rc4 registers, and the QuickStart cpu_ss_hsel write-up.

- **Why:** The knowledge exists but is scattered and partly uncommitted.
- **Done when:** Each imported issue is either a record or explicitly dismissed.
- **Source:** §6.3

### F6 · Should · Phase 3

Fill in fixed-in automatically from commit trailers ("Fixes: <sha>", "Defect: SL-DEF-NNNN").

- **Why:** Records stay current without manual edits.
- **Done when:** A commit with a trailer updates its defect record.
- **Source:** §6.2

### F7 · Should · v1

Keep known bugs as issues labelled defect in each repo, and promote one to a defect record when it affects a registered configuration.

- **Why:** Keeps small bugs where developers already work.
- **Done when:** The promotion step is documented and used once.
- **Source:** §6.1

## G · Releases

### G1 · Must · Phase 3 *(amended per X1 a, confirmed 2026-09-30)*

Release a tool like this: a release PR (version, classed changelog, submodules, defects); gates T0, T1 and T2 on at least one SoC that uses it (plus T3 if build-affecting); the bot tags a candidate; the final tag is made by the bot once T3 passes for a fix or feature release, and by a person for a breaking release. Artefacts: source archive with submodules, manifest, changelog, test evidence, and a wheel for nanosoc_gen.

- **Why:** Tools can only be tested through the SoCs that use them.
- **Done when:** One real tool release goes through the flow end to end.
- **Source:** §7.1

### G2 · Must · Phase 5

Release a SoC reference like this: candidate tag; gates T0–T3 for a fix, plus T4 for a feature, plus T6 for a tapeout; clean-clone and upgrade-path tests; every pin tagged or covered by a keep tag; no Category A defect without a decision; a signed gate ledger.

- **Why:** A SoC release is what users build on and fork.
- **Done when:** One real SoC release goes through the flow end to end.
- **Source:** §7.2

### G3 · Must · Phase 5

A SoC release includes a lock file listing every nested pin with its host and state, ERRATA.md, the gate ledger, generated RTL and file lists, bitstreams and logs. A silicon release also records the GDS and netlist hashes and the generator that produced any gitignored RTL.

- **Why:** Anyone can reproduce or audit exactly what was released.
- **Done when:** rc4's lock file lists all 44 pins.
- **Source:** §3.4, §7.2

### G4 · Should · Phase 5

Support trains: one release across several repos, driven by a SoC, gated once with all changes together, approved once, and finalised from the bottom up.

- **Why:** This is how the QuickStart release actually ran.
- **Done when:** The QuickStart mvd release can be expressed as a train.
- **Source:** §7.3

### G5 · Must · Phase 3 *(amended per X1 a, confirmed 2026-09-30)*

Approvals: tool candidate by the bot; tool final (fix or feature) by the bot once T3 passes; tool final (breaking) by an owner and David; SoC release or train by David; a tapeout SoC release or errata revision by David and a second reviewer.

- **Why:** Matches the approver to the risk.
- **Done when:** Roles are set in the service config and enforced.
- **Source:** §7.4

### G6 · Must · Phase 3

A tool release can be yanked: recorded, never bumped to again, tag kept. A silicon release is never yanked.

- **Why:** Tags are permanent; users need to know what to avoid.
- **Done when:** A yanked version never appears in a bump.
- **Source:** §7.1

### G7 · Must · Phase 5 *(added in round 2)*

Archive the exact generated RTL of every SoC release and silicon reference, with an md5 manifest, as a release artefact.

- **Why:** With build_soc/ gitignored (DEC10 b), a release's RTL exists nowhere else once it is regenerated.
- **Done when:** Each release's archive reproduces its build_soc/ file for file.
- **Source:** new (DEC10)

## H · Web interface, hosting and security

### H1 · Must · v1 *(round 2)*

v1 pages: a dashboard (per SoC: T0 result, red pins, open errata, host drift, runner status, data age), repo detail, contains-commit, silicon protection, and defects and errata. The dashboard and each configuration have a Run T0 button. Later phases add the matrix (with Run for any cell), bump queue, releases and trains, and forks.

- **Why:** Adds your manual start to the v1 pages.
- **Done when:** Each v1 page is demonstrated on live data, including a manual Run.
- **Source:** H1 (amended for A4)

### H2 · Must · v1

Sign in with GitHub, limited to SoC-Labs members. Roles come from the service's own configuration, and every write action is logged.

- **Why:** Simple access control using accounts people already have.
- **Done when:** A non-member cannot open any page.
- **Source:** §8.2

### H3 · Must · All *(amended per X1 a, confirmed 2026-09-30)*

Split the credentials: the poller is read-only; the CI trigger is scoped to the CI project; the bump writer exists only on non-silicon repos; the bot's tag credential covers tool repos only; final tags of breaking tool releases and of every SoC release are made by people. Warn before any token expires.

- **Why:** One leaked token cannot do everything.
- **Done when:** Each credential's scope is recorded and checked.
- **Source:** §8.2

### H4 · Should · v1

Use a plain, maintainable stack: Python 3.11, FastAPI, Jinja2, htmx, SQLite, systemd and the git command line. No single-page app, Postgres or Kubernetes.

- **Why:** Same stack as fpgahub; easy for a small lab to maintain.
- **Done when:** The service runs from one systemd unit.
- **Source:** §8.2

### H5 · Should · v1

Host it on a dedicated lab VM (about 2 vCPU, 4 GB RAM, 50 GB disk), separate from the shared login server and the CI runner.

- **Why:** The shared server is overloaded, and the runner runs untrusted jobs.
- **Done when:** The VM exists and runs the service.
- **Source:** §8.2

### H6 · Must · v1

Keep local mirrors of every repo's branches and tags (not pull-request refs), refreshed every 15 minutes. v1 polls; webhooks come later.

- **Why:** Fast, offline-safe answers without hitting API limits.
- **Done when:** The mirrors refresh on schedule and report their age.
- **Source:** §2.5, §8.2

## I · Forked example projects

### I1 · Should · Phase 5

Find forks of the example projects (through the GitHub and GitLab fork APIs, plus forks registered by URL) and show for each: the release it is based on, how many releases behind it is, the errata that affect it, and any changes outside its declared user paths.

- **Why:** Tells users, and you, when a fork needs upgrading.
- **Done when:** The 13 current GitLab forks appear with their status.
- **Source:** §2.6

### I2 · Should · Phase 5

Each example project declares a fork contract (its upstream and the paths users own) and documents the upgrade steps: fetch and merge the release tag, then sync and update the submodules.

- **Why:** Makes upgrading a fork routine.
- **Done when:** accelerator-project carries a fork contract and the upgrade steps.
- **Source:** §2.6
