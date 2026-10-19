# Command-line reference

Generated from `nanosoc-rm --help` by `scripts/gen_cli_docs.py`; do not edit by hand. After changing a command's
help, run `.venv/bin/python scripts/gen_cli_docs.py` and commit the result (`tests/unit/test_cli_docs.py` fails
until you do).

Every command follows the same exit codes: 0 answered (PASS), 1 finding or FAIL, 2 could not answer (missing data,
unreadable host, UNVERIFIED), 3 usage or config error. Most take `--json` (one JSON document per line). The service
config is named by `$NANOSOC_RM_CONFIG`. Words such as tier, configuration and freshness are defined in
[concepts.md](concepts.md).

## Commands

| Command | What it does |
|---|---|
| [`nanosoc-rm alerts`](#nanosoc-rm-alerts) | Open alerts (dashboard banners): rewrite, unknown_url, token_expiry, runner_drift, tag_moved, mirror_failed, waiver_unused. |
| &nbsp;&nbsp;[`nanosoc-rm alerts clear`](#nanosoc-rm-alerts-clear) | Clear the alert SUBJECT (alert:<kind>:<about>). |
| [`nanosoc-rm branches`](#nanosoc-rm-branches) | Every branch of a repo (a component id, or host:path) from its local mirror: ahead/behind the default branch, merged, tags on the tip, registered pins and registry citations reaching into it, and a... |
| [`nanosoc-rm bumps`](#nanosoc-rm-bumps) | Bump proposals in SHADOW MODE (Phase 4, E1-E7; built per CR-15). |
| &nbsp;&nbsp;[`nanosoc-rm bumps ledger`](#nanosoc-rm-bumps-ledger) | What the bot would have done against what people did, read from the mirrors. (E2) |
| &nbsp;&nbsp;[`nanosoc-rm bumps mark`](#nanosoc-rm-bumps-mark) | Record a person's word: they applied the proposal (done) or will not (declined). |
| &nbsp;&nbsp;[`nanosoc-rm bumps patch`](#nanosoc-rm-bumps-patch) | The patch for one kept proposal (gitlink move only; generated RTL is not regenerated). |
| &nbsp;&nbsp;[`nanosoc-rm bumps queue`](#nanosoc-rm-bumps-queue) | The kept proposals (oldest first), as the /bumps page shows them. |
| &nbsp;&nbsp;[`nanosoc-rm bumps scan`](#nanosoc-rm-bumps-scan) | Scan one configuration (or all) for releases newer than their pins (the default subcommand). |
| &nbsp;&nbsp;[`nanosoc-rm bumps schedule`](#nanosoc-rm-bumps-schedule) | The bump schedule as shipped: disabled, naming the Phase 4 entry gate. |
| &nbsp;&nbsp;[`nanosoc-rm bumps show`](#nanosoc-rm-bumps-show) | One kept proposal: the move, changelog, defects, matrix, blockers. |
| [`nanosoc-rm capabilities`](#nanosoc-rm-capabilities) | What each tree can test (.nanosoc/capabilities.yaml). |
| &nbsp;&nbsp;[`nanosoc-rm capabilities show`](#nanosoc-rm-capabilities-show) | Per tree of CONFIGURATION's latest stored walk: its capabilities file at the pinned commit, each capability, the hosts that can run it, and its last test-bump result. |
| &nbsp;&nbsp;[`nanosoc-rm capabilities validate`](#nanosoc-rm-capabilities-validate) | Check a capabilities file while you write it (exit 1 with every problem). |
| [`nanosoc-rm catalogue`](#nanosoc-rm-catalogue) | Catalogue characterisation: groups and types, new repos, change previews. |
| &nbsp;&nbsp;[`nanosoc-rm catalogue org-scan`](#nanosoc-rm-catalogue-org-scan) | List the org and the group anonymously; report each repo the catalogue does not hold (section 23.2). |
| &nbsp;&nbsp;[`nanosoc-rm catalogue preview`](#nanosoc-rm-catalogue-preview) | The catalogue.yaml patch for a JSON list of choices (section 23.3). |
| &nbsp;&nbsp;[`nanosoc-rm catalogue show`](#nanosoc-rm-catalogue-show) | Every component as the front page groups it (section 23.1). |
| [`nanosoc-rm changelog`](#nanosoc-rm-changelog) | The classed changelog (C2): lint it. |
| &nbsp;&nbsp;[`nanosoc-rm changelog lint`](#nanosoc-rm-changelog-lint) | Lint CHANGELOG.md (format, classes, Unreleased, links, tags; C5 rule). (C2) |
| [`nanosoc-rm compat`](#nanosoc-rm-compat) | Are these pins compatible? Resolve CONFIGURATION's tree (with each --pin override), then run the configuration T0 gates LOCALLY with their negative controls (anonymous; t0.runner.hardened NOT-RUN)... |
| [`nanosoc-rm contains`](#nanosoc-rm-contains) | Which configurations contain commit SHA, per host. (B4) |
| [`nanosoc-rm credentials`](#nanosoc-rm-credentials) | The service's credentials and their scopes (H3). |
| &nbsp;&nbsp;[`nanosoc-rm credentials check`](#nanosoc-rm-credentials-check) | Check every credential's scope against its record and, where the host can say, the host (H3). |
| [`nanosoc-rm detect`](#nanosoc-rm-detect) | Run DETECTOR of DEFECT on built output and print the run for the erratum's exposure.run. (F2) |
| [`nanosoc-rm executor`](#nanosoc-rm-executor) | The remote tier executor: the pull agent and its queue (section 27). |
| &nbsp;&nbsp;[`nanosoc-rm executor cancel`](#nanosoc-rm-executor-cancel) | Cancel a queued task, or ask the agent running it to stop. |
| &nbsp;&nbsp;[`nanosoc-rm executor check`](#nanosoc-rm-executor-check) | What this host would run with: sandbox mode, exposure check, token file and mirror root (no network). |
| &nbsp;&nbsp;[`nanosoc-rm executor enqueue`](#nanosoc-rm-executor-enqueue) | Queue the task in TASK_FILE (executor.schema.json#/$defs/task). |
| &nbsp;&nbsp;[`nanosoc-rm executor list`](#nanosoc-rm-executor-list) | Executor tasks, newest first. |
| &nbsp;&nbsp;[`nanosoc-rm executor run`](#nanosoc-rm-executor-run) | Claim tasks from the release manager and run them sandboxed (on the EDA host). |
| &nbsp;&nbsp;[`nanosoc-rm executor show`](#nanosoc-rm-executor-show) | One task, with its result and log path. |
| &nbsp;&nbsp;[`nanosoc-rm executor token`](#nanosoc-rm-executor-token) | Executor tokens (service host). |
| &nbsp;&nbsp;&nbsp;&nbsp;[`nanosoc-rm executor token new`](#nanosoc-rm-executor-token-new) | A new executor token for agent NAME: the token goes to --out, the [[executor.agents]] entry to stdout (with --reader: a bump-check reader token and its [[api.readers]] entry). |
| [`nanosoc-rm forks`](#nanosoc-rm-forks) | Forks of the example projects (I1): base release, divergence, errata, user-path changes (read-only, anonymous). |
| [`nanosoc-rm freshness`](#nanosoc-rm-freshness) | How far each submodule pin lags its upstream and when it last moved (section 24). |
| &nbsp;&nbsp;[`nanosoc-rm freshness scan`](#nanosoc-rm-freshness-scan) | Measure each registered configuration's stored walk against the mirrors (read-only git). |
| &nbsp;&nbsp;[`nanosoc-rm freshness show`](#nanosoc-rm-freshness-show) | The stored freshness, re-judged on the day: every tree, or one tree's nodes with --tree. |
| [`nanosoc-rm hosts`](#nanosoc-rm-hosts) | Compare each component's copies with its canonical repo (rev-list in one mirror). (B6) |
| [`nanosoc-rm inventory`](#nanosoc-rm-inventory) | Every repo in the GitHub org and the GitLab group (with subgroups), classified by the catalogue. |
| [`nanosoc-rm manifest`](#nanosoc-rm-manifest) | The component manifest nanosoc-component.yaml (C11). |
| &nbsp;&nbsp;[`nanosoc-rm manifest init`](#nanosoc-rm-manifest-init) | Propose nanosoc-component.yaml from the catalogue and the tree, as a patch for a person. (C11) |
| [`nanosoc-rm matrix`](#nanosoc-rm-matrix) | Compatibility matrix (D3): `matrix CONFIG` shows it; `matrix cell\|quarantine\|nightly` (Phase 2, built per CR-15). |
| &nbsp;&nbsp;[`nanosoc-rm matrix cell`](#nanosoc-rm-matrix-cell) | Run one cell now, in the foreground: compat T0 (and a tier) with the component moved, D8 re-run on FAIL. |
| &nbsp;&nbsp;[`nanosoc-rm matrix nightly`](#nanosoc-rm-matrix-nightly) | What the nightly matrix WOULD queue tonight. |
| &nbsp;&nbsp;[`nanosoc-rm matrix quarantine`](#nanosoc-rm-matrix-quarantine) | Tests quarantined after two FLAKY results in 14 days (30-day expiry), active and expired. (D8) |
| &nbsp;&nbsp;[`nanosoc-rm matrix show`](#nanosoc-rm-matrix-show) | One configuration's matrix: a row per component, a cell per column. |
| [`nanosoc-rm mirror`](#nanosoc-rm-mirror) | Local mirrors of every catalogued repo: heads and tags only (H6). |
| &nbsp;&nbsp;[`nanosoc-rm mirror status`](#nanosoc-rm-mirror-status) | The latest mirror_status of every mirrored repo, with its age (now - last_success_at, seconds). |
| &nbsp;&nbsp;[`nanosoc-rm mirror sync`](#nanosoc-rm-mirror-sync) | Fetch heads and tags of every catalogued repo (or one component's) into its mirror. |
| [`nanosoc-rm phase-gates`](#nanosoc-rm-phase-gates) | Every later phase has a checked entry gate before its code, routes or commands appear. (A7) |
| [`nanosoc-rm pins`](#nanosoc-rm-pins) | The pins of CONFIGURATION from its latest stored walk (walked now if it never was). |
| [`nanosoc-rm publish-release`](#nanosoc-rm-publish-release) | Publish a ready local release draft's manifest to the release store (asic-release/<project>/<version>/): documents, release-manifest.json and its signature, a build-info record promoted RELEASED. |
| [`nanosoc-rm quality`](#nanosoc-rm-quality) | Code and documentation quality: comment density, comment problems, README accuracy, doc conciseness (Q1-Q6). |
| &nbsp;&nbsp;[`nanosoc-rm quality all`](#nanosoc-rm-quality-all) | Every quality report for one target (and store them with --store): one line each. |
| &nbsp;&nbsp;[`nanosoc-rm quality comments`](#nanosoc-rm-quality-comments) | Commented-out code, restating comments, banners, boilerplate, stale TODOs, unknown names. (Q2) |
| &nbsp;&nbsp;[`nanosoc-rm quality density`](#nanosoc-rm-quality-density) | Comment ratio per language, directory and file; outliers against per-language thresholds. (Q1) |
| &nbsp;&nbsp;[`nanosoc-rm quality docs`](#nanosoc-rm-quality-docs) | Conciseness score per Markdown file and section; optional Claude rewrite as a patch. (Q4) |
| &nbsp;&nbsp;[`nanosoc-rm quality readme`](#nanosoc-rm-quality-readme) | Every path, make target, script, submodule and flow the READMEs name, against the tree. (Q3) |
| &nbsp;&nbsp;[`nanosoc-rm quality trend`](#nanosoc-rm-quality-trend) | The stored results of one target, oldest first. (Q5) |
| [`nanosoc-rm regen`](#nanosoc-rm-regen) | Regenerate a configuration's generated output in a sandbox; class a move by the diff (C3, C7; section 20). |
| &nbsp;&nbsp;[`nanosoc-rm regen class`](#nanosoc-rm-regen-class) | The change class of moving COMPONENT from --from to --to in CONFIGURATION, by regenerating both. |
| &nbsp;&nbsp;[`nanosoc-rm regen pending`](#nanosoc-rm-regen-pending) | Moves a stored version plan or bump proposal would class by regeneration, not yet measured. |
| &nbsp;&nbsp;[`nanosoc-rm regen run`](#nanosoc-rm-regen-run) | Regenerate CONFIGURATION with COMPONENT at COMMIT (never stored; a render is not a fact). |
| [`nanosoc-rm register-tapeout`](#nanosoc-rm-register-tapeout) | Publish a tapeout record (tapeout-<id>.json and its signature) beside its release; promoted TAPED-OUT once submitted. |
| [`nanosoc-rm registry`](#nanosoc-rm-registry) | The registry: validate it, census its imports, list its facts. |
| &nbsp;&nbsp;[`nanosoc-rm registry imports`](#nanosoc-rm-registry-imports) | Census each imports.yaml source at its cited commit against the ledger. (F5) |
| &nbsp;&nbsp;[`nanosoc-rm registry records`](#nanosoc-rm-registry-records) | Every registry record as a registry_record observation, with its `git show` source (A3). |
| &nbsp;&nbsp;[`nanosoc-rm registry validate`](#nanosoc-rm-registry-validate) | Load PATH (default: [registry] path) and check every file against its schema and the cross-file rules 1 to 15. |
| [`nanosoc-rm release`](#nanosoc-rm-release) | Local release drafts (G2, G3, G7, F4): lock, errata, notes, checklist. |
| &nbsp;&nbsp;[`nanosoc-rm release draft`](#nanosoc-rm-release-draft) | Build a release draft folder of CONFIGURATION as VERSION (not published). |
| &nbsp;&nbsp;[`nanosoc-rm release list`](#nanosoc-rm-release-list) | Every release draft on this host (newest first). |
| &nbsp;&nbsp;[`nanosoc-rm release verify`](#nanosoc-rm-release-verify) | Re-check DRAFT (a folder, or CONFIGURATION/VERSION) against the mirrors and the registry now. |
| [`nanosoc-rm runner`](#nanosoc-rm-runner) | The hardened runner's settings against ci.runner_expected (D10). |
| [`nanosoc-rm scan`](#nanosoc-rm-scan) | Vendor-content scanner (D11): bundles before they are stored. |
| &nbsp;&nbsp;[`nanosoc-rm scan bundle`](#nanosoc-rm-scan-bundle) | Scan a bundle directory: exit 0 clean, 1 finding, 2 unverified. |
| [`nanosoc-rm serve`](#nanosoc-rm-serve) | Serve the web pages and JSON API, run the schedules (poll, nightly T0, result fetch) and the local job worker (CR-17). |
| [`nanosoc-rm silicon-protection`](#nanosoc-rm-silicon-protection) | Commits of frozen or silicon trees that their default branch cannot reach, on the pinned host and the canonical host, and whether a tag there covers each. (B5) |
| [`nanosoc-rm store`](#nanosoc-rm-store) | The artefact store index: what Artifactory holds (read-only). |
| &nbsp;&nbsp;[`nanosoc-rm store findings`](#nanosoc-rm-store-findings) | What the inbox shows from the store: mismatches, releases not archived, unregistered tapeouts. |
| &nbsp;&nbsp;[`nanosoc-rm store index`](#nanosoc-rm-store-index) | Read the store with GET, HEAD and AQL only, probe each feature it relies on, print the index. |
| &nbsp;&nbsp;[`nanosoc-rm store search`](#nanosoc-rm-store-search) | Stored rows matching an artefact path, a name, a run tag, a commit or an md5 prefix. |
| &nbsp;&nbsp;[`nanosoc-rm store show`](#nanosoc-rm-store-show) | The stored index (no store call). |
| [`nanosoc-rm store-maint`](#nanosoc-rm-store-maint) | Store maintenance for the operator. |
| &nbsp;&nbsp;[`nanosoc-rm store-maint compact`](#nanosoc-rm-store-maint-compact) | Collapse each run of identical set-kind snapshots of one scope into its first snapshot, which keeps the run's last check (first and last observed times). |
| [`nanosoc-rm t0`](#nanosoc-rm-t0) | T0, the resolve tier: run (CI only), and the service's trigger and results. |
| &nbsp;&nbsp;[`nanosoc-rm t0 coverage`](#nanosoc-rm-t0-coverage) | Configurations (and @registry) with no accepted scheduled T0 that night (D2). |
| &nbsp;&nbsp;[`nanosoc-rm t0 fetch`](#nanosoc-rm-t0-fetch) | Fetch every pending run from the CI API, verify it (D5) and store the result. |
| &nbsp;&nbsp;[`nanosoc-rm t0 finalise`](#nanosoc-rm-t0-finalise) | Scan a bundle, delete what the scan flags, record the scan in provenance.json (D11). |
| &nbsp;&nbsp;[`nanosoc-rm t0 items`](#nanosoc-rm-t0-items) | The items one gate judged in run NONCE, from its stored bundle. |
| &nbsp;&nbsp;[`nanosoc-rm t0 run`](#nanosoc-rm-t0-run) | Run T0 for the RM_REQUEST pipeline variable and write the bundle (compat-ci job t0 only). |
| &nbsp;&nbsp;[`nanosoc-rm t0 runs`](#nanosoc-rm-t0-runs) | Stored T0 runs, oldest first (latest state of each). |
| &nbsp;&nbsp;[`nanosoc-rm t0 trigger`](#nanosoc-rm-t0-trigger) | Start T0 for CONFIGURATION (or --all) in the CI project. |
| [`nanosoc-rm test-bump`](#nanosoc-rm-test-bump) | Test moving the pin at PATH of CONFIGURATION to TO (a tag, branch or commit of that component), Now against After, with the capabilities the trees declare in .nanosoc/capabilities.yaml. |
| [`nanosoc-rm tiers`](#nanosoc-rm-tiers) | Test tiers with their budgets and, for CONFIGURATION, their commands (D1). |
| [`nanosoc-rm trace`](#nanosoc-rm-trace) | Requirement -> tests -> commits (PR7), from test markers and `Requirements:` trailers. |
| [`nanosoc-rm user`](#nanosoc-rm-user) | Local sign-in users (CR-16): print config stanzas; never writes a file. |
| &nbsp;&nbsp;[`nanosoc-rm user add`](#nanosoc-rm-user-add) | Print the [[auth.users]] stanza for LOGIN with a scrypt hash of its password. |
| [`nanosoc-rm version`](#nanosoc-rm-version) | Versions and change classes (C1-C12), tool releases (G1, G5, G6), F6. |
| &nbsp;&nbsp;[`nanosoc-rm version approve`](#nanosoc-rm-version-approve) | Record a person's approval of one step in the draft's release.json (roles enforced). (G5) |
| &nbsp;&nbsp;[`nanosoc-rm version draft`](#nanosoc-rm-version-draft) | Write a release draft folder (version bump patch, classed changelog, submodules, defects, gates, the G5 approval record) for a person. (G1) |
| &nbsp;&nbsp;[`nanosoc-rm version fixed-in`](#nanosoc-rm-version-fixed-in) | Defect:/Fixes: trailers on the default branch -> fixed_in entries, as a registry patch. (F6) |
| &nbsp;&nbsp;[`nanosoc-rm version next`](#nanosoc-rm-version-next) | The next version from the classed changelog and the measured signals. (C1, C3) |
| &nbsp;&nbsp;[`nanosoc-rm version refresh`](#nanosoc-rm-version-refresh) | Lint, next and tags for every catalogue component (or those named), stored for the /versions pages. |
| &nbsp;&nbsp;[`nanosoc-rm version tags`](#nanosoc-rm-version-tags) | Every tag on every host by kind; names outside the scheme, lightweight or non-canonical release tags, and moved release tags (from the mirror's reflog). (C10) |
| &nbsp;&nbsp;[`nanosoc-rm version yank`](#nanosoc-rm-version-yank) | Propose yanking a tool release (recorded in the registry's versioning.yaml, never bumped to again, tag kept). (G6) |
| [`nanosoc-rm walk`](#nanosoc-rm-walk) | Resolve CONFIGURATION's whole submodule tree from its own .gitmodules and gitlinks, with every pin's state on each host (B2, B3). |

<a id="nanosoc-rm"></a>

## `nanosoc-rm`

```
Usage: nanosoc-rm [OPTIONS] COMMAND [ARGS]...

  nanoSoC Release Manager. Service config: $NANOSOC_RM_CONFIG. Exit codes: 0 answered, 1 finding or
  FAIL, 2 could not answer, 3 usage or config error.

Options:
  --version   Show the version and exit.
  -h, --help  Show this message and exit.

Commands:
  alerts              Open alerts (dashboard banners): rewrite, unknown_url, token_expiry,...
  branches            Every branch of a repo (a component id, or host:path) from its local...
  bumps               Bump proposals in SHADOW MODE (Phase 4, E1-E7; built per CR-15).
  capabilities        What each tree can test (.nanosoc/capabilities.yaml).
  catalogue           Catalogue characterisation: groups and types, new repos, change previews.
  changelog           The classed changelog (C2): lint it.
  compat              Are these pins compatible? Resolve CONFIGURATION's tree (with each --pin...
  contains            B4: which configurations contain commit SHA, per host.
  credentials         The service's credentials and their scopes (H3).
  detect              F2: run DETECTOR of DEFECT on built output and print the run for the...
  executor            The remote tier executor: the pull agent and its queue (section 27).
  forks               Forks of the example projects (I1): base release, divergence, errata,...
  freshness           How far each submodule pin lags its upstream and when it last moved...
  hosts               B6: compare each component's copies with its canonical repo (rev-list in...
  inventory           Every repo in the GitHub org and the GitLab group (with subgroups),...
  manifest            The component manifest nanosoc-component.yaml (C11).
  matrix              Compatibility matrix (D3): `matrix CONFIG` shows it; `matrix...
  mirror              Local mirrors of every catalogued repo: heads and tags only (H6).
  phase-gates         A7: every later phase has a checked entry gate before its code, routes or...
  pins                The pins of CONFIGURATION from its latest stored walk (walked now if it...
  publish-release     Publish a ready local release draft's manifest to the release store...
  quality             Code and documentation quality: comment density, comment problems, README...
  regen               Regenerate a configuration's generated output in a sandbox; class a move...
  register-tapeout    Publish a tapeout record (tapeout-<id>.json and its signature) beside its...
  registry            The registry: validate it, census its imports, list its facts.
  release             Local release drafts (G2, G3, G7, F4): lock, errata, notes, checklist.
  runner              The hardened runner's settings against ci.runner_expected (D10).
  scan                Vendor-content scanner (D11): bundles before they are stored.
  serve               Serve the web pages and JSON API, run the schedules (poll, nightly T0,...
  silicon-protection  B5: commits of frozen or silicon trees that their default branch cannot...
  store               The artefact store index: what Artifactory holds (read-only).
  store-maint         Store maintenance for the operator.
  t0                  T0, the resolve tier: run (CI only), and the service's trigger and results.
  test-bump           Test moving the pin at PATH of CONFIGURATION to TO (a tag, branch or...
  tiers               Test tiers with their budgets and, for CONFIGURATION, their commands (D1).
  trace               Requirement -> tests -> commits (PR7), from test markers and...
  user                Local sign-in users (CR-16): print config stanzas; never writes a file.
  version             Versions and change classes (C1-C12), tool releases (G1, G5, G6), F6.
  walk                Resolve CONFIGURATION's whole submodule tree from its own .gitmodules and...
```

<a id="nanosoc-rm-alerts"></a>

### `nanosoc-rm alerts`

```
Usage: nanosoc-rm alerts [OPTIONS] [COMMAND] [ARGS]...

  Open alerts (dashboard banners): rewrite, unknown_url, token_expiry, runner_drift, tag_moved,
  mirror_failed, waiver_unused. Exit 1 when any listed alert is open.

Options:
  --all       Include cleared alerts.
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.

Commands:
  clear  Clear the alert SUBJECT (alert:<kind>:<about>).
```

<a id="nanosoc-rm-alerts-clear"></a>

#### `nanosoc-rm alerts clear`

```
Usage: nanosoc-rm alerts clear [OPTIONS] SUBJECT

  Clear the alert SUBJECT (alert:<kind>:<about>).

Options:
  --by TEXT   The login clearing it (logged in actions).  [required]
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-branches"></a>

### `nanosoc-rm branches`

```
Usage: nanosoc-rm branches [OPTIONS] TARGET

  Every branch of a repo (a component id, or host:path) from its local mirror: ahead/behind the
  default branch, merged, tags on the tip, registered pins and registry citations reaching into it,
  and a verdict MERGED | PROTECTED | PINNED | STALE | ACTIVE. Exit 0 when nothing is deletable, 1
  when something is, 2 when there is no mirror (use --sync) or a repo could not be judged.

Options:
  --all                       Every repo of the component (canonical and copies), not only the
                              canonical.
  --delete-list               Print only the deletable branches (MERGED or STALE, no pin, no
                              citation, no tag) as `git push origin --delete <branch>` lines for a
                              person to review. Never run by the tool.
  --stale-days INTEGER RANGE  An unmerged, unheld branch whose last commit is older than this is
                              STALE.  [default: (90); x>=0]
  --sync                      Fetch the component's mirror first (mirror sync --component).
  --json                      Print one JSON document per line.
  -h, --help                  Show this message and exit.
```

<a id="nanosoc-rm-bumps"></a>

### `nanosoc-rm bumps`

```
Usage: nanosoc-rm bumps [OPTIONS] COMMAND [ARGS]...

  Bump proposals in SHADOW MODE (Phase 4, E1-E7; built per CR-15). `bumps [CONFIG]` scans now. Never
  pushes, never writes a branch, never opens a pull request: patches and commands for a person.

Options:
  -h, --help  Show this message and exit.

Commands:
  ledger    E2: what the bot would have done against what people did, read from the mirrors.
  mark      Record a person's word: they applied the proposal (done) or will not (declined).
  patch     The patch for one kept proposal (gitlink move only; generated RTL is not regenerated).
  queue     The kept proposals (oldest first), as the /bumps page shows them.
  scan      Scan one configuration (or all) for releases newer than their pins (the default...
  schedule  The bump schedule as shipped: disabled, naming the Phase 4 entry gate.
  show      One kept proposal: the move, changelog, defects, matrix, blockers.
```

<a id="nanosoc-rm-bumps-ledger"></a>

#### `nanosoc-rm bumps ledger`

```
Usage: nanosoc-rm bumps ledger [OPTIONS]

  E2: what the bot would have done against what people did, read from the mirrors.

Options:
  --store     Keep it for the /bumps pages (which never read the mirrors).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-bumps-mark"></a>

#### `nanosoc-rm bumps mark`

```
Usage: nanosoc-rm bumps mark [OPTIONS] PROPOSAL_ID {done|declined}

  Record a person's word: they applied the proposal (done) or will not (declined).

Options:
  --by TEXT    The login of the person who decided.  [required]
  --note TEXT
  --json       Print one JSON document per line.
  -h, --help   Show this message and exit.
```

<a id="nanosoc-rm-bumps-patch"></a>

#### `nanosoc-rm bumps patch`

```
Usage: nanosoc-rm bumps patch [OPTIONS] PROPOSAL_ID

  The patch for one kept proposal (gitlink move only; generated RTL is not regenerated).

Options:
  --out TEXT  Write the patch to this new file (default: stdout).
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-bumps-queue"></a>

#### `nanosoc-rm bumps queue`

```
Usage: nanosoc-rm bumps queue [OPTIONS] [CONFIGURATION]

  The kept proposals (oldest first), as the /bumps page shows them.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-bumps-scan"></a>

#### `nanosoc-rm bumps scan`

```
Usage: nanosoc-rm bumps scan [OPTIONS] [CONFIGURATION]

  Scan one configuration (or all) for releases newer than their pins (the default subcommand).

Options:
  --store     Keep the proposals for the queue page and the shadow ledger.
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-bumps-schedule"></a>

#### `nanosoc-rm bumps schedule`

```
Usage: nanosoc-rm bumps schedule [OPTIONS]

  The bump schedule as shipped: disabled, naming the Phase 4 entry gate.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-bumps-show"></a>

#### `nanosoc-rm bumps show`

```
Usage: nanosoc-rm bumps show [OPTIONS] PROPOSAL_ID

  One kept proposal: the move, changelog, defects, matrix, blockers.

Options:
  --configuration TEXT  That configuration's document of a proposal kept for several (default: the
                        latest computed).
  --json                Print one JSON document per line.
  -h, --help            Show this message and exit.
```

<a id="nanosoc-rm-capabilities"></a>

### `nanosoc-rm capabilities`

```
Usage: nanosoc-rm capabilities [OPTIONS] COMMAND [ARGS]...

  What each tree can test (.nanosoc/capabilities.yaml).

Options:
  -h, --help  Show this message and exit.

Commands:
  show      Per tree of CONFIGURATION's latest stored walk: its capabilities file at the pinned...
  validate  Check a capabilities file while you write it (exit 1 with every problem).
```

<a id="nanosoc-rm-capabilities-show"></a>

#### `nanosoc-rm capabilities show`

```
Usage: nanosoc-rm capabilities show [OPTIONS] CONFIGURATION

  Per tree of CONFIGURATION's latest stored walk: its capabilities file at the pinned commit, each
  capability, the hosts that can run it, and its last test-bump result. Reads only.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-capabilities-validate"></a>

#### `nanosoc-rm capabilities validate`

```
Usage: nanosoc-rm capabilities validate [OPTIONS] FILE

  Check a capabilities file while you write it (exit 1 with every problem). The release manager
  itself only ever reads the file at a commit, from its mirror.

Options:
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-catalogue"></a>

### `nanosoc-rm catalogue`

```
Usage: nanosoc-rm catalogue [OPTIONS] COMMAND [ARGS]...

  Catalogue characterisation: groups and types, new repos, change previews.

Options:
  -h, --help  Show this message and exit.

Commands:
  org-scan  List the org and the group anonymously; report each repo the catalogue does not...
  preview   The catalogue.yaml patch for a JSON list of choices (section 23.3).
  show      Every component as the front page groups it (section 23.1).
```

<a id="nanosoc-rm-catalogue-org-scan"></a>

#### `nanosoc-rm catalogue org-scan`

```
Usage: nanosoc-rm catalogue org-scan [OPTIONS]

  List the org and the group anonymously; report each repo the catalogue does not hold (section
  23.2).

Options:
  --host [github|gitlab]  Scan only this host (repeatable; default every host with an org or group
                          configured).
  --store                 Store one repo_seen snapshot per host listed.
  --json                  Print one JSON document per line.
  -h, --help              Show this message and exit.
```

<a id="nanosoc-rm-catalogue-preview"></a>

#### `nanosoc-rm catalogue preview`

```
Usage: nanosoc-rm catalogue preview [OPTIONS] CHOICES

  The catalogue.yaml patch for a JSON list of choices (section 23.3). Writes nothing to the
  registry.

Options:
  --registry DIRECTORY  The registry checkout (default: [registry] path).
  --out FILE            Also write the patch here.
  --json                Print one JSON document per line.
  -h, --help            Show this message and exit.
```

<a id="nanosoc-rm-catalogue-show"></a>

#### `nanosoc-rm catalogue show`

```
Usage: nanosoc-rm catalogue show [OPTIONS] [PATH]

  Every component as the front page groups it (section 23.1).

Options:
  --group TEXT   Only these groups (repeatable).
  --gaps         Only SoCs that no forkable project wraps.
  --unconfirmed  Only entries with a guessed or derived field.
  --json         Print one JSON document per line.
  -h, --help     Show this message and exit.
```

<a id="nanosoc-rm-changelog"></a>

### `nanosoc-rm changelog`

```
Usage: nanosoc-rm changelog [OPTIONS] COMMAND [ARGS]...

  The classed changelog (C2): lint it.

Options:
  -h, --help  Show this message and exit.

Commands:
  lint  C2: lint CHANGELOG.md (format, classes, Unreleased, links, tags; C5 rule).
```

<a id="nanosoc-rm-changelog-lint"></a>

#### `nanosoc-rm changelog lint`

```
Usage: nanosoc-rm changelog lint [OPTIONS] [TARGET]

  C2: lint CHANGELOG.md (format, classes, Unreleased, links, tags; C5 rule). Exit 1 on an error.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the canonical default branch).
  --path DIRECTORY  Read a local checkout (read-only) instead of the component's mirror.
  --store           Store the result (the /versions pages read stored results).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-compat"></a>

### `nanosoc-rm compat`

```
Usage: nanosoc-rm compat [OPTIONS] CONFIGURATION

  Are these pins compatible? Resolve CONFIGURATION's tree (with each --pin override), then run the
  configuration T0 gates LOCALLY with their negative controls (anonymous; t0.runner.hardened NOT-
  RUN) and roll up with the configuration's T0 waivers. One line per pin and the run verdict. With
  --tier the tier command then runs in a sandbox checkout and joins the run verdict. Exit 0 PASS,
  WAIVED or WARN; 1 FAIL; 2 UNVERIFIED or INFRA; 3 usage (unknown configuration, bad --pin, no pin
  at a tree path, no tier command, a tier that regenerates without NANOSOC_GEN_OK=1).

Options:
  --pin TREE_PATH=SHA  Replace the gitlink at TREE_PATH with SHA (full 40 hex); the tree below it is
                       walked from SHA's own .gitmodules. Repeatable. A what-if: never stored.
  --keep-work PATH     Keep the local run's work directory (anonymous fetches, empty HOME/TMPDIR)
                       here; it must be new or empty. Default: a temporary directory, deleted
                       afterwards.
  --store              Store the walk and the compat_result (real runs only: not with --pin).
  --tier [t1|t2]       After T0, run the registry tiers.yaml command of this tier in a sandbox
                       checkout of the resolved tree (budget enforced, killed at 1.5 x). Skipped
                       when T0 is FAIL.
  --force-tier         Run --tier even when T0 is FAIL: a person's override, recorded; the result is
                       UNVERIFIED for release.
  --json               Print one JSON document per line.
  -h, --help           Show this message and exit.
```

<a id="nanosoc-rm-contains"></a>

### `nanosoc-rm contains`

```
Usage: nanosoc-rm contains [OPTIONS] SHA

  B4: which configurations contain commit SHA, per host. Exit 0 answered (including `no`), 2 if any
  answer is unknown, 3 if SHA resolves to no commit or to several.

Options:
  --component TEXT  Component whose mirror holds the commit (default: search every mirror).
  --config TEXT     Configuration id (repeatable; default: all).
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-credentials"></a>

### `nanosoc-rm credentials`

```
Usage: nanosoc-rm credentials [OPTIONS] COMMAND [ARGS]...

  The service's credentials and their scopes (H3).

Options:
  -h, --help  Show this message and exit.

Commands:
  check  Check every credential's scope against its record and, where the host can say, the...
```

<a id="nanosoc-rm-credentials-check"></a>

#### `nanosoc-rm credentials check`

```
Usage: nanosoc-rm credentials check [OPTIONS]

  Check every credential's scope against its record and, where the host can say, the host (H3). Exit
  0 all ok, 1 any finding, 2 any unverified.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-detect"></a>

### `nanosoc-rm detect`

```
Usage: nanosoc-rm detect [OPTIONS] DEFECT DETECTOR

  F2: run DETECTOR of DEFECT on built output and print the run for the erratum's exposure.run. Exit
  0 clean, 1 fired, 2 could not run. Never run by the service (A4).

Options:
  --input PATH  Built output: a generated-RTL archive or directory, or a netlist file.  [required]
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-executor"></a>

### `nanosoc-rm executor`

```
Usage: nanosoc-rm executor [OPTIONS] COMMAND [ARGS]...

  The remote tier executor: the pull agent and its queue (section 27).

Options:
  -h, --help  Show this message and exit.

Commands:
  cancel   Cancel a queued task, or ask the agent running it to stop.
  check    What this host would run with: sandbox mode, exposure check, token file and mirror...
  enqueue  Queue the task in TASK_FILE (executor.schema.json#/$defs/task).
  list     Executor tasks, newest first.
  run      Claim tasks from the release manager and run them sandboxed (on the EDA host).
  show     One task, with its result and log path.
  token    Executor tokens (service host).
```

<a id="nanosoc-rm-executor-cancel"></a>

#### `nanosoc-rm executor cancel`

```
Usage: nanosoc-rm executor cancel [OPTIONS] TASK_ID

  Cancel a queued task, or ask the agent running it to stop.

Options:
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-executor-check"></a>

#### `nanosoc-rm executor check`

```
Usage: nanosoc-rm executor check [OPTIONS]

  What this host would run with: sandbox mode, exposure check, token file and mirror root (no
  network).

Options:
  --agent-config FILE  The agent's TOML (default: $NANOSOC_RM_AGENT_CONFIG).
  --json               Print one JSON document per line.
  -h, --help           Show this message and exit.
```

<a id="nanosoc-rm-executor-enqueue"></a>

#### `nanosoc-rm executor enqueue`

```
Usage: nanosoc-rm executor enqueue [OPTIONS] TASK_FILE

  Queue the task in TASK_FILE (executor.schema.json#/$defs/task).

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-executor-list"></a>

#### `nanosoc-rm executor list`

```
Usage: nanosoc-rm executor list [OPTIONS]

  Executor tasks, newest first.

Options:
  --state [queued|claimed|done|lost|cancelled]
  --limit INTEGER
  --json                          Print one JSON document per line.
  -h, --help                      Show this message and exit.
```

<a id="nanosoc-rm-executor-run"></a>

#### `nanosoc-rm executor run`

```
Usage: nanosoc-rm executor run [OPTIONS]

  Claim tasks from the release manager and run them sandboxed (on the EDA host).

Options:
  --once               Claim at most one task, run it, exit.
  --loop               Claim and run until SIGTERM or SIGINT.
  --agent-config FILE  The agent's TOML (default: $NANOSOC_RM_AGENT_CONFIG).
  -h, --help           Show this message and exit.
```

<a id="nanosoc-rm-executor-show"></a>

#### `nanosoc-rm executor show`

```
Usage: nanosoc-rm executor show [OPTIONS] TASK_ID

  One task, with its result and log path.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-executor-token"></a>

#### `nanosoc-rm executor token`

```
Usage: nanosoc-rm executor token [OPTIONS] COMMAND [ARGS]...

  Executor tokens (service host).

Options:
  -h, --help  Show this message and exit.

Commands:
  new  A new executor token for agent NAME: the token goes to --out, the [[executor.agents]]...
```

<a id="nanosoc-rm-executor-token-new"></a>

#### `nanosoc-rm executor token new`

```
Usage: nanosoc-rm executor token new [OPTIONS] NAME

  A new executor token for agent NAME: the token goes to --out, the [[executor.agents]] entry to
  stdout (with --reader: a bump-check reader token and its [[api.readers]] entry).

Options:
  --out FILE     Write the token here (created 0600; must not exist). It is never printed.
                 [required]
  --labels TEXT  Comma-separated labels to grant (vcs,xcelium,verilator,...).
  --reader       A bump-check READER token instead (a CI job's NANOSOC_RM_TOKEN): prints an
                 [[api.readers]] entry; it opens GET /api/v1/bump-check only.
  -h, --help     Show this message and exit.
```

<a id="nanosoc-rm-forks"></a>

### `nanosoc-rm forks`

```
Usage: nanosoc-rm forks [OPTIONS] [COMPONENT]

  Forks of the example projects (I1): base release, divergence, errata, user-path changes (read-
  only, anonymous).

Options:
  --store     Store each fork_status (the Forks page shows the stored ones).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-freshness"></a>

### `nanosoc-rm freshness`

```
Usage: nanosoc-rm freshness [OPTIONS] COMMAND [ARGS]...

  How far each submodule pin lags its upstream and when it last moved (section 24).

Options:
  -h, --help  Show this message and exit.

Commands:
  scan  Measure each registered configuration's stored walk against the mirrors (read-only git).
  show  The stored freshness, re-judged on the day: every tree, or one tree's nodes with --tree.
```

<a id="nanosoc-rm-freshness-scan"></a>

#### `nanosoc-rm freshness scan`

```
Usage: nanosoc-rm freshness scan [OPTIONS] [CONFIGURATIONS]...

  Measure each registered configuration's stored walk against the mirrors (read-only git).

Options:
  --store       Keep the result: one snapshot per configuration.
  --today TEXT  Judge as of this UTC date (default: today).
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-freshness-show"></a>

#### `nanosoc-rm freshness show`

```
Usage: nanosoc-rm freshness show [OPTIONS] [CONFIGURATION]

  The stored freshness, re-judged on the day: every tree, or one tree's nodes with --tree.

Options:
  --tree        Every node of CONFIGURATION's tree, depth first.
  --today TEXT  Judge as of this UTC date (default: today).
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-hosts"></a>

### `nanosoc-rm hosts`

```
Usage: nanosoc-rm hosts [OPTIONS] [COMPONENT]

  B6: compare each component's copies with its canonical repo (rev-list in one mirror). Exit 1 if
  any relation is DIVERGED; 2 if any is UNKNOWN and none DIVERGED.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-inventory"></a>

### `nanosoc-rm inventory`

```
Usage: nanosoc-rm inventory [OPTIONS]

  Every repo in the GitHub org and the GitLab group (with subgroups), classified by the catalogue.

Options:
  --unclassified  Only the repos in no catalogue entry (flagged, A1).
  --json          Print one JSON document per line.
  -h, --help      Show this message and exit.
```

<a id="nanosoc-rm-manifest"></a>

### `nanosoc-rm manifest`

```
Usage: nanosoc-rm manifest [OPTIONS] COMMAND [ARGS]...

  The component manifest nanosoc-component.yaml (C11).

Options:
  -h, --help  Show this message and exit.

Commands:
  init  C11: propose nanosoc-component.yaml from the catalogue and the tree, as a patch for a...
```

<a id="nanosoc-rm-manifest-init"></a>

#### `nanosoc-rm manifest init`

```
Usage: nanosoc-rm manifest init [OPTIONS] [TARGET]

  C11: propose nanosoc-component.yaml from the catalogue and the tree, as a patch for a person.

Options:
  --patch TEXT      Write the patch to this NEW file.
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the canonical default branch).
  --path DIRECTORY  Read a local checkout (read-only) instead of the component's mirror.
  --store           Store the result (the /versions pages read stored results).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-matrix"></a>

### `nanosoc-rm matrix`

```
Usage: nanosoc-rm matrix [OPTIONS] COMMAND [ARGS]...

  Compatibility matrix (D3): `matrix CONFIG` shows it; `matrix cell|quarantine|nightly` (Phase 2,
  built per CR-15).

Options:
  -h, --help  Show this message and exit.

Commands:
  cell        Run one cell now, in the foreground: compat T0 (and a tier) with the component...
  nightly     What the nightly matrix WOULD queue tonight.
  quarantine  D8: tests quarantined after two FLAKY results in 14 days (30-day expiry), active...
  show        One configuration's matrix: a row per component, a cell per column.
```

<a id="nanosoc-rm-matrix-cell"></a>

#### `nanosoc-rm matrix cell`

```
Usage: nanosoc-rm matrix cell [OPTIONS] CONFIGURATION

  Run one cell now, in the foreground: compat T0 (and a tier) with the component moved, D8 re-run on
  FAIL.

Options:
  --column [pinned|in-range|latest-tag|head]
                                  [required]
  --component TEXT                The row (not with --column pinned).
  --tier [t1]                     Add the registry's tiers.yaml command of this tier (t1: lint) to
                                  the T0 run.
  --force-tier                    Run the tier past a T0 FAIL (a person's override; the cell stays
                                  FAIL and is UNVERIFIED for release).
  --store                         Store the result (the job always does).
  --json                          Print one JSON document per line.
  -h, --help                      Show this message and exit.
```

<a id="nanosoc-rm-matrix-nightly"></a>

#### `nanosoc-rm matrix nightly`

```
Usage: nanosoc-rm matrix nightly [OPTIONS] CONFIGURATION

  What the nightly matrix WOULD queue tonight. It ships disabled until the Phase 2 entry gate is
  met.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-matrix-quarantine"></a>

#### `nanosoc-rm matrix quarantine`

```
Usage: nanosoc-rm matrix quarantine [OPTIONS] CONFIGURATION

  D8: tests quarantined after two FLAKY results in 14 days (30-day expiry), active and expired.

Options:
  --owner TEXT  GATE=LOGIN: name the owner of an active quarantine.
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-matrix-show"></a>

#### `nanosoc-rm matrix show`

```
Usage: nanosoc-rm matrix show [OPTIONS] CONFIGURATION

  One configuration's matrix: a row per component, a cell per column.

Options:
  --columns TEXT    Comma list of pinned,in-range,latest-tag,head (default all).
  --component TEXT  Restrict to these rows (repeatable).
  --tiers TEXT      Add tier cells beside each T0 cell: t1 (lint). Needs the registry's tiers.yaml
                    t1 command and the tool on this host.
  --run             Queue every runnable cell of this view as a local job.
  --wait            With --run: work the queue here until the cells are done.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-mirror"></a>

### `nanosoc-rm mirror`

```
Usage: nanosoc-rm mirror [OPTIONS] COMMAND [ARGS]...

  Local mirrors of every catalogued repo: heads and tags only (H6).

Options:
  -h, --help  Show this message and exit.

Commands:
  status  The latest mirror_status of every mirrored repo, with its age (now - last_success_at,...
  sync    Fetch heads and tags of every catalogued repo (or one component's) into its mirror.
```

<a id="nanosoc-rm-mirror-status"></a>

#### `nanosoc-rm mirror status`

```
Usage: nanosoc-rm mirror status [OPTIONS]

  The latest mirror_status of every mirrored repo, with its age (now - last_success_at, seconds).

Options:
  --component TEXT  One component or external.
  --summary         Count repos by how they were read and why a read failed.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-mirror-sync"></a>

#### `nanosoc-rm mirror sync`

```
Usage: nanosoc-rm mirror sync [OPTIONS]

  Fetch heads and tags of every catalogued repo (or one component's) into its mirror.

Options:
  --component TEXT        Sync one component or external.
  --host [github|gitlab]  Only the repos on this host.
  --json                  Print one JSON document per line.
  -h, --help              Show this message and exit.
```

<a id="nanosoc-rm-phase-gates"></a>

### `nanosoc-rm phase-gates`

```
Usage: nanosoc-rm phase-gates [OPTIONS]

  A7: every later phase has a checked entry gate before its code, routes or commands appear.

Options:
  --repo TEXT  The tool repo checkout (default: this package's).
  --json       Print one JSON document per line.
  -h, --help   Show this message and exit.
```

<a id="nanosoc-rm-pins"></a>

### `nanosoc-rm pins`

```
Usage: nanosoc-rm pins [OPTIONS] CONFIGURATION

  The pins of CONFIGURATION from its latest stored walk (walked now if it never was). The exit code
  judges every pin, whatever the filters show.

Options:
  --state [TAGGED|ON_DEFAULT|PROTECTED_BY_TAG|BRANCH_ONLY|PR_ONLY|ORPHAN|MISSING|REWRITTEN|LOCAL_URL|UNREADABLE|NO_GITLINK]
                                  Only pins in this state (pin-level, or on --host / any host).
  --host [github|gitlab]          Only pins with a state on this host.
  --json                          Print one JSON document per line.
  -h, --help                      Show this message and exit.
```

<a id="nanosoc-rm-publish-release"></a>

### `nanosoc-rm publish-release`

```
Usage: nanosoc-rm publish-release [OPTIONS] CONFIGURATION/VERSION

  Publish a ready local release draft's manifest to the release store (asic-
  release/<project>/<version>/): documents, release-manifest.json and its signature, a build-info
  record promoted RELEASED.

Options:
  --artefact ROLE=REPO/PATH  A file already in the store this release references (archive, gds,
                             netlist, bitstream, logs, flists, signoff, foundry-return, doc, other);
                             read with GET, joined by raw md5.
  --dry-run                  Build and check; print the preview and its sha256.
  --confirm SHA256           Publish the record whose sha256 the preview printed (refused if it
                             changed).
  --by TEXT                  The GUI login this publish is for (a listed publisher); default
                             cli:<your user name>.
  --json                     Print one JSON document per line.
  -h, --help                 Show this message and exit.
```

<a id="nanosoc-rm-quality"></a>

### `nanosoc-rm quality`

```
Usage: nanosoc-rm quality [OPTIONS] COMMAND [ARGS]...

  Code and documentation quality: comment density, comment problems, README accuracy, doc
  conciseness (Q1-Q6). Read-only; patches only.

Options:
  -h, --help  Show this message and exit.

Commands:
  all       Every quality report for one target (and store them with --store): one line each.
  comments  Q2: commented-out code, restating comments, banners, boilerplate, stale TODOs,...
  density   Q1: comment ratio per language, directory and file; outliers against per-language...
  docs      Q4: conciseness score per Markdown file and section; optional Claude rewrite as a...
  readme    Q3: every path, make target, script, submodule and flow the READMEs name, against...
  trend     Q5: the stored results of one target, oldest first.
```

<a id="nanosoc-rm-quality-all"></a>

#### `nanosoc-rm quality all`

```
Usage: nanosoc-rm quality all [OPTIONS] [TARGET]

  Every quality report for one target (and store them with --store): one line each.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the default branch; with --path: the work tree
                    as it is).
  --path DIRECTORY  Read a local directory (read-only) instead of a mirror.
  --store           Store the result per (target, commit) for trends (Q5).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-quality-comments"></a>

#### `nanosoc-rm quality comments`

```
Usage: nanosoc-rm quality comments [OPTIONS] [TARGET]

  Q2: commented-out code, restating comments, banners, boilerplate, stale TODOs, unknown names.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the default branch; with --path: the work tree
                    as it is).
  --path DIRECTORY  Read a local directory (read-only) instead of a mirror.
  --store           Store the result per (target, commit) for trends (Q5).
  --kind TEXT       Only these finding kinds (repeatable).
  --patch TEXT      Write the mechanical edits as a unified diff.
  --all             List every finding (default: the first 60).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-quality-density"></a>

#### `nanosoc-rm quality density`

```
Usage: nanosoc-rm quality density [OPTIONS] [TARGET]

  Q1: comment ratio per language, directory and file; outliers against per-language thresholds.

Options:
  --json             Print one JSON document per line.
  --ref TEXT         Branch, tag or commit (default: the default branch; with --path: the work tree
                     as it is).
  --path DIRECTORY   Read a local directory (read-only) instead of a mirror.
  --store            Store the result per (target, commit) for trends (Q5).
  --thresholds FILE  A quality.yaml to use instead of the registry's.
  --top INTEGER      Outlier files to list.  [default: 10]
  -h, --help         Show this message and exit.
```

<a id="nanosoc-rm-quality-docs"></a>

#### `nanosoc-rm quality docs`

```
Usage: nanosoc-rm quality docs [OPTIONS] [TARGET]

  Q4: conciseness score per Markdown file and section; optional Claude rewrite as a patch.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the default branch; with --path: the work tree
                    as it is).
  --path DIRECTORY  Read a local directory (read-only) instead of a mirror.
  --store           Store the result per (target, commit) for trends (Q5).
  --rewrite         Ask Claude for shorter section rewrites (needs an API key; sends section text to
                    the Anthropic API).
  --patch TEXT      With --rewrite: write the accepted rewrites as a diff.
  --top INTEGER     Files to list, least concise first.  [default: 15]
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-quality-readme"></a>

#### `nanosoc-rm quality readme`

```
Usage: nanosoc-rm quality readme [OPTIONS] [TARGET]

  Q3: every path, make target, script, submodule and flow the READMEs name, against the tree.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the default branch; with --path: the work tree
                    as it is).
  --path DIRECTORY  Read a local directory (read-only) instead of a mirror.
  --store           Store the result per (target, commit) for trends (Q5).
  --docs            Also check docs/*.md.
  --patch TEXT      Write a patch that puts the generated Repository layout section in README.md.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-quality-trend"></a>

#### `nanosoc-rm quality trend`

```
Usage: nanosoc-rm quality trend [OPTIONS] {density|comments|readme|docs} TARGET

  Q5: the stored results of one target, oldest first.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-regen"></a>

### `nanosoc-rm regen`

```
Usage: nanosoc-rm regen [OPTIONS] COMMAND [ARGS]...

  Regenerate a configuration's generated output in a sandbox; class a move by the diff (C3, C7;
  section 20).

Options:
  -h, --help  Show this message and exit.

Commands:
  class    The change class of moving COMPONENT from --from to --to in CONFIGURATION, by...
  pending  Moves a stored version plan or bump proposal would class by regeneration, not yet...
  run      Regenerate CONFIGURATION with COMPONENT at COMMIT (never stored; a render is not a...
```

<a id="nanosoc-rm-regen-class"></a>

#### `nanosoc-rm regen class`

```
Usage: nanosoc-rm regen class [OPTIONS] CONFIGURATION

  The change class of moving COMPONENT from --from to --to in CONFIGURATION, by regenerating both.

Options:
  --component TEXT  [required]
  --from TEXT       [required]
  --to TEXT         [required]
  --at TEXT         Tree path to move (default: every pin of the component).
  --store           Store the regen_class observation (the classifier reads it).
  --fresh           Ignore the render cache.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-regen-pending"></a>

#### `nanosoc-rm regen pending`

```
Usage: nanosoc-rm regen pending [OPTIONS]

  Moves a stored version plan or bump proposal would class by regeneration, not yet measured.

Options:
  --run       Measure each pending move now (stored).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-regen-run"></a>

#### `nanosoc-rm regen run`

```
Usage: nanosoc-rm regen run [OPTIONS] CONFIGURATION

  Regenerate CONFIGURATION with COMPONENT at COMMIT (never stored; a render is not a fact).

Options:
  --component TEXT  [required]
  --commit TEXT     [required]
  --at TEXT         Tree path to move (default: every pin of the component).
  --out TEXT        Keep the sandbox in this new or empty directory.
  --fresh           Ignore the render cache.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-register-tapeout"></a>

### `nanosoc-rm register-tapeout`

```
Usage: nanosoc-rm register-tapeout [OPTIONS] TAPEOUT_ID

  Publish a tapeout record (tapeout-<id>.json and its signature) beside its release; promoted TAPED-
  OUT once submitted.

Options:
  --draft TEXT      The tapeout draft file (default: <registry>/tapeouts/ID.tapeout.yaml).
  --dry-run         Build and check; print the preview and its sha256.
  --confirm SHA256  Publish the record whose sha256 the preview printed (refused if it changed).
  --by TEXT         The GUI login this publish is for (a listed publisher); default cli:<your user
                    name>.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-registry"></a>

### `nanosoc-rm registry`

```
Usage: nanosoc-rm registry [OPTIONS] COMMAND [ARGS]...

  The registry: validate it, census its imports, list its facts.

Options:
  -h, --help  Show this message and exit.

Commands:
  imports   F5: census each imports.yaml source at its cited commit against the ledger.
  records   Every registry record as a registry_record observation, with its `git show` source...
  validate  Load PATH (default: [registry] path) and check every file against its schema and...
```

<a id="nanosoc-rm-registry-imports"></a>

#### `nanosoc-rm registry imports`

```
Usage: nanosoc-rm registry imports [OPTIONS] [PATH]

  F5: census each imports.yaml source at its cited commit against the ledger. One row per source.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-registry-records"></a>

#### `nanosoc-rm registry records`

```
Usage: nanosoc-rm registry records [OPTIONS] [PATH]

  Every registry record as a registry_record observation, with its `git show` source (A3).

Options:
  --store     Also store them in the service database (one snapshot per file).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-registry-validate"></a>

#### `nanosoc-rm registry validate`

```
Usage: nanosoc-rm registry validate [OPTIONS] [PATH]

  Load PATH (default: [registry] path) and check every file against its schema and the cross-file
  rules 1 to 15. Exit 0 valid, 1 with every problem listed.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-release"></a>

### `nanosoc-rm release`

```
Usage: nanosoc-rm release [OPTIONS] COMMAND [ARGS]...

  Local release drafts (G2, G3, G7, F4): lock, errata, notes, checklist. Never tags or publishes.

Options:
  -h, --help  Show this message and exit.

Commands:
  draft   Build a release draft folder of CONFIGURATION as VERSION (not published).
  list    Every release draft on this host (newest first).
  verify  Re-check DRAFT (a folder, or CONFIGURATION/VERSION) against the mirrors and the...
```

<a id="nanosoc-rm-release-draft"></a>

#### `nanosoc-rm release draft`

```
Usage: nanosoc-rm release draft [OPTIONS] CONFIGURATION VERSION

  Build a release draft folder of CONFIGURATION as VERSION (not published).

Options:
  --kind [fix|feature|tapeout]  G2 gates: fix T0-T3, feature +T4, tapeout +T6 (default: tapeout for
                                silicon, else feature).
  --archive TEXT                Generated-RTL archive (tar of build_soc/) for the G7 manifest.
  --generator TEXT              Tree path of the generator pin (default: the shallowest
                                nanosoc_gen).
  --rtl-committed               The generated RTL is committed (not gitignored).
  --gds TEXT                    Silicon: the GDS that went to the foundry (hashed, never copied).
  --netlist TEXT                Silicon: the P&R netlist (hashed, never copied).
  --artefact TEXT               KIND=PATH (flists, bitstream, logs, source-archive).
  --waiver TEXT                 PATHS=REASON: pins released without a tag (* = all).
  --since TEXT                  Previous release tag for the notes (default: the newest older
                                vX.Y.Z).
  --candidate INTEGER           Also list the vX.Y.Z-rc.N candidate tag command.
  --out TEXT                    Draft folder (default: <data_dir>/releases/CONFIGURATION/VERSION).
  --replace                     Rebuild an existing draft folder.
  --json                        Print one JSON document per line.
  -h, --help                    Show this message and exit.
```

<a id="nanosoc-rm-release-list"></a>

#### `nanosoc-rm release list`

```
Usage: nanosoc-rm release list [OPTIONS] [CONFIGURATION]

  Every release draft on this host (newest first).

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-release-verify"></a>

#### `nanosoc-rm release verify`

```
Usage: nanosoc-rm release verify [OPTIONS] DRAFT

  Re-check DRAFT (a folder, or CONFIGURATION/VERSION) against the mirrors and the registry now.

Options:
  --against TEXT  A directory holding build_soc/: compare it with the archive's manifest file for
                  file (G7).
  --json          Print one JSON document per line.
  -h, --help      Show this message and exit.
```

<a id="nanosoc-rm-runner"></a>

### `nanosoc-rm runner`

```
Usage: nanosoc-rm runner [OPTIONS]

  The hardened runner's settings against ci.runner_expected (D10). Exit 1 drift, 2 unreadable.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-scan"></a>

### `nanosoc-rm scan`

```
Usage: nanosoc-rm scan [OPTIONS] COMMAND [ARGS]...

  Vendor-content scanner (D11): bundles before they are stored.

Options:
  -h, --help  Show this message and exit.

Commands:
  bundle  Scan a bundle directory: exit 0 clean, 1 finding, 2 unverified.
```

<a id="nanosoc-rm-scan-bundle"></a>

#### `nanosoc-rm scan bundle`

```
Usage: nanosoc-rm scan bundle [OPTIONS] DIRECTORY

  Scan a bundle directory: exit 0 clean, 1 finding, 2 unverified.

Options:
  --rules FILE    Private rule file (default: $RM_SCAN_RULES; built-in arm.banner always applies).
  --toolkit FILE  Pinned copy of the toolkit's check-vendor-collateral.sh, lib.sh beside it
                  (default: $RM_SCAN_TOOLKIT; none: this package's rules only).
  --json          Print one JSON document per line.
  -h, --help      Show this message and exit.
```

<a id="nanosoc-rm-serve"></a>

### `nanosoc-rm serve`

```
Usage: nanosoc-rm serve [OPTIONS]

  Serve the web pages and JSON API, run the schedules (poll, nightly T0, result fetch) and the local
  job worker (CR-17). Exit 3 for a config error, unusable TLS files or a refused --host.

Options:
  --host TEXT               Address to bind.  [default: 127.0.0.1]
  --port INTEGER            Port to bind.  [default: 8000]
  --no-schedule             Serve pages only; do not run schedule.tick().
  --no-worker               Do not run local jobs (Run, Sync and Walk stay queued).
  --i-know-this-is-exposed  Local auth mode only: allow a non-loopback --host. Without it, serve
                            refuses to bind one.
  --tls-cert PATH           Serve HTTPS with this PEM certificate (CR-20). Needs --tls-key.
  --tls-key PATH            The private key for --tls-cert (PEM).
  --allow-plain-http        Local auth mode, non-loopback --host: serve without TLS (passwords in
                            clear).
  -h, --help                Show this message and exit.
```

<a id="nanosoc-rm-silicon-protection"></a>

### `nanosoc-rm silicon-protection`

```
Usage: nanosoc-rm silicon-protection [OPTIONS] [CONFIGURATION]

  B5: commits of frozen or silicon trees that their default branch cannot reach, on the pinned host
  and the canonical host, and whether a tag there covers each. Exit 1 if any row is uncovered.

Options:
  --uncovered  Print only rows no tag on their host covers.
  --json       Print one JSON document per line.
  -h, --help   Show this message and exit.
```

<a id="nanosoc-rm-store"></a>

### `nanosoc-rm store`

```
Usage: nanosoc-rm store [OPTIONS] COMMAND [ARGS]...

  The artefact store index: what Artifactory holds (read-only).

Options:
  -h, --help  Show this message and exit.

Commands:
  findings  What the inbox shows from the store: mismatches, releases not archived,...
  index     Read the store with GET, HEAD and AQL only, probe each feature it relies on, print...
  search    Stored rows matching an artefact path, a name, a run tag, a commit or an md5 prefix.
  show      The stored index (no store call).
```

<a id="nanosoc-rm-store-findings"></a>

#### `nanosoc-rm store findings`

```
Usage: nanosoc-rm store findings [OPTIONS]

  What the inbox shows from the store: mismatches, releases not archived, unregistered tapeouts.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-store-index"></a>

#### `nanosoc-rm store index`

```
Usage: nanosoc-rm store index [OPTIONS]

  Read the store with GET, HEAD and AQL only, probe each feature it relies on, print the index.

Options:
  --store     Store the index as the store_index fact.
  --no-cache  Do not download documents for the GUI's download links.
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-store-search"></a>

#### `nanosoc-rm store search`

```
Usage: nanosoc-rm store search [OPTIONS] QUERY

  Stored rows matching an artefact path, a name, a run tag, a commit or an md5 prefix.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-store-show"></a>

#### `nanosoc-rm store show`

```
Usage: nanosoc-rm store show [OPTIONS]

  The stored index (no store call).

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-store-maint"></a>

### `nanosoc-rm store-maint`

```
Usage: nanosoc-rm store-maint [OPTIONS] COMMAND [ARGS]...

  Store maintenance for the operator. Stop the service and back up rm.sqlite first.

Options:
  -h, --help  Show this message and exit.

Commands:
  compact  Collapse each run of identical set-kind snapshots of one scope into its first...
```

<a id="nanosoc-rm-store-maint-compact"></a>

#### `nanosoc-rm store-maint compact`

```
Usage: nanosoc-rm store-maint compact [OPTIONS]

  Collapse each run of identical set-kind snapshots of one scope into its first snapshot, which
  keeps the run's last check (first and last observed times). Safe to rerun: a second run removes
  nothing. Afterwards `VACUUM` returns the space to the file system (DEPLOY.md).

Options:
  --dry-run   Report what --apply would remove; change nothing.
  --apply     Remove the repeated snapshots (one transaction).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-t0"></a>

### `nanosoc-rm t0`

```
Usage: nanosoc-rm t0 [OPTIONS] COMMAND [ARGS]...

  T0, the resolve tier: run (CI only), and the service's trigger and results.

Options:
  -h, --help  Show this message and exit.

Commands:
  coverage  Configurations (and @registry) with no accepted scheduled T0 that night (D2).
  fetch     Fetch every pending run from the CI API, verify it (D5) and store the result.
  finalise  Scan a bundle, delete what the scan flags, record the scan in provenance.json (D11).
  items     The items one gate judged in run NONCE, from its stored bundle.
  run       Run T0 for the RM_REQUEST pipeline variable and write the bundle (compat-ci job t0...
  runs      Stored T0 runs, oldest first (latest state of each).
  trigger   Start T0 for CONFIGURATION (or --all) in the CI project.
```

<a id="nanosoc-rm-t0-coverage"></a>

#### `nanosoc-rm t0 coverage`

```
Usage: nanosoc-rm t0 coverage [OPTIONS]

  Configurations (and @registry) with no accepted scheduled T0 that night (D2). Exit 1 if any.

Options:
  --night TEXT  UTC date YYYY-MM-DD (default: the last night whose trigger time passed).
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-t0-fetch"></a>

#### `nanosoc-rm t0 fetch`

```
Usage: nanosoc-rm t0 fetch [OPTIONS]

  Fetch every pending run from the CI API, verify it (D5) and store the result. Exit 1 if a fetched
  run is FAIL, rejected or refused; 2 if one is UNVERIFIED or INFRA.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-t0-finalise"></a>

#### `nanosoc-rm t0 finalise`

```
Usage: nanosoc-rm t0 finalise [OPTIONS] DIRECTORY

  Scan a bundle, delete what the scan flags, record the scan in provenance.json (D11).

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-t0-items"></a>

#### `nanosoc-rm t0 items`

```
Usage: nanosoc-rm t0 items [OPTIONS] NONCE GATE

  The items one gate judged in run NONCE, from its stored bundle. Exit 1 if any is not `pass`.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-t0-run"></a>

#### `nanosoc-rm t0 run`

```
Usage: nanosoc-rm t0 run [OPTIONS]

  Run T0 for the RM_REQUEST pipeline variable and write the bundle (compat-ci job t0 only). Prints
  the verdict rows the bundle holds once it is final.

Options:
  --out PATH      The bundle directory the job uploads.  [default: rm-bundle]
  --url-map FILE  TESTS ONLY: JSON {URL: local repo}. Refused when CI_JOB_ID is set.
  --json          Print one JSON document per line.
  -h, --help      Show this message and exit.
```

<a id="nanosoc-rm-t0-runs"></a>

#### `nanosoc-rm t0 runs`

```
Usage: nanosoc-rm t0 runs [OPTIONS] [CONFIGURATION]

  Stored T0 runs, oldest first (latest state of each). Exit 1 when the latest final run of a listed
  configuration is red (FAIL, UNVERIFIED or INFRA). The command answered from the store, so a red
  run is a finding (1), never could-not-answer (2): AT-D2 reads `t0 runs` with `rc in (0, 1)`.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-t0-trigger"></a>

#### `nanosoc-rm t0 trigger`

```
Usage: nanosoc-rm t0 trigger [OPTIONS] [CONFIGURATION]

  Start T0 for CONFIGURATION (or --all) in the CI project. Exit 0 started, 2 a trigger failed, 3
  usage (unknown configuration, no --by).

Options:
  --all       Every registered configuration, and @registry.
  --by TEXT   Your login: a run from the shell is manual and is logged (H2).
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-test-bump"></a>

### `nanosoc-rm test-bump`

```
Usage: nanosoc-rm test-bump [OPTIONS] CONFIGURATION PATH TO

  Test moving the pin at PATH of CONFIGURATION to TO (a tag, branch or commit of that component),
  Now against After, with the capabilities the trees declare in .nanosoc/capabilities.yaml. Every
  tree path of that component moves together. T0 runs on both sides and is reported as hygiene. Exit
  0 PASS or WARN; 1 FAIL (a gate went green to red); 2 UNVERIFIED (nothing covers the move, or
  something did not run); 3 usage.

Options:
  --host TEXT  The executor host id whose site data (registry tiers.yaml hosts) builds the
               environment. Default: $NANOSOC_RM_EXECUTOR_HOST, then [executor] host.
  --plan       Show what would run, run nothing.
  --queue      Queue it as a job for an executor instead of running it here (what the GUI's Test
               this bump does).
  --json       Print one JSON document per line.
  -h, --help   Show this message and exit.
```

<a id="nanosoc-rm-tiers"></a>

### `nanosoc-rm tiers`

```
Usage: nanosoc-rm tiers [OPTIONS] [CONFIGURATION]

  Test tiers with their budgets and, for CONFIGURATION, their commands (D1).

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-trace"></a>

### `nanosoc-rm trace`

```
Usage: nanosoc-rm trace [OPTIONS]

  Requirement -> tests -> commits (PR7), from test markers and `Requirements:` trailers.

Options:
  --repo TEXT         The tool repo checkout (default: this package's).
  --requirement TEXT  Show one requirement.
  --json              Print one JSON document per line.
  -h, --help          Show this message and exit.
```

<a id="nanosoc-rm-user"></a>

### `nanosoc-rm user`

```
Usage: nanosoc-rm user [OPTIONS] COMMAND [ARGS]...

  Local sign-in users (CR-16): print config stanzas; never writes a file.

Options:
  -h, --help  Show this message and exit.

Commands:
  add  Print the [[auth.users]] stanza for LOGIN with a scrypt hash of its password.
```

<a id="nanosoc-rm-user-add"></a>

#### `nanosoc-rm user add`

```
Usage: nanosoc-rm user add [OPTIONS] LOGIN

  Print the [[auth.users]] stanza for LOGIN with a scrypt hash of its password. Paste it into the
  private service config and set `[auth] mode = "local"`. Prompts for the password unless
  --generate.

Options:
  --generate   Generate a random password and print it once.
  --role TEXT  A role for this user (default: member). Repeatable.
  -h, --help   Show this message and exit.
```

<a id="nanosoc-rm-version"></a>

### `nanosoc-rm version`

```
Usage: nanosoc-rm version [OPTIONS] COMMAND [ARGS]...

  Versions and change classes (C1-C12), tool releases (G1, G5, G6), F6.

Options:
  -h, --help  Show this message and exit.

Commands:
  approve   G5: record a person's approval of one step in the draft's release.json (roles...
  draft     G1: write a release draft folder (version bump patch, classed changelog,...
  fixed-in  F6: Defect:/Fixes: trailers on the default branch -> fixed_in entries, as a...
  next      C1, C3: the next version from the classed changelog and the measured signals.
  refresh   Lint, next and tags for every catalogue component (or those named), stored for the...
  tags      C10: every tag on every host by kind; names outside the scheme, lightweight or...
  yank      G6: propose yanking a tool release (recorded in the registry's versioning.yaml,...
```

<a id="nanosoc-rm-version-approve"></a>

#### `nanosoc-rm version approve`

```
Usage: nanosoc-rm version approve [OPTIONS] DRAFT_DIR

  G5: record a person's approval of one step in the draft's release.json (roles enforced). The login
  is proven by its local password (local sign-in, `[[auth.users]]`); with GitHub sign-in, approve in
  the GUI (/versions/<component>), where the signed-in login is recorded.

Options:
  --step TEXT       tool-candidate, tool-final or tool-final-breaking.  [required]
  --login TEXT      The approving person's login (roles from the service config).  [required]
  --password-stdin  Read the login's local password from stdin instead of prompting.
  --json            Print one JSON document per line.
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-version-draft"></a>

#### `nanosoc-rm version draft`

```
Usage: nanosoc-rm version draft [OPTIONS] TARGET

  G1: write a release draft folder (version bump patch, classed changelog, submodules, defects,
  gates, the G5 approval record) for a person. Nothing is tagged or pushed.

Options:
  --out DIRECTORY  A new or empty directory (default <data_dir>/drafts/<component>/<version>).
  --date TEXT      Release date YYYY-MM-DD (default today, UTC).
  --json           Print one JSON document per line.
  -h, --help       Show this message and exit.
```

<a id="nanosoc-rm-version-fixed-in"></a>

#### `nanosoc-rm version fixed-in`

```
Usage: nanosoc-rm version fixed-in [OPTIONS] TARGET

  F6: Defect:/Fixes: trailers on the default branch -> fixed_in entries, as a registry patch. Exit 1
  when a defect record needs updating, 2 when a trailer could not be resolved and none needs
  updating.

Options:
  --ref TEXT
  --patch TEXT  Write the registry patch to this NEW file.
  --json        Print one JSON document per line.
  -h, --help    Show this message and exit.
```

<a id="nanosoc-rm-version-next"></a>

#### `nanosoc-rm version next`

```
Usage: nanosoc-rm version next [OPTIONS] [TARGET]

  C1, C3: the next version from the classed changelog and the measured signals. Exit 0 PASS or WARN
  (a partial class: some signal unmeasured), 1 a finding, 2 no class at all.

Options:
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the canonical default branch).
  --path DIRECTORY  Read a local checkout (read-only) instead of the component's mirror.
  --store           Store the result (the /versions pages read stored results).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-version-refresh"></a>

#### `nanosoc-rm version refresh`

```
Usage: nanosoc-rm version refresh [OPTIONS] [COMPONENTS]...

  Lint, next and tags for every catalogue component (or those named), stored for the /versions
  pages.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```

<a id="nanosoc-rm-version-tags"></a>

#### `nanosoc-rm version tags`

```
Usage: nanosoc-rm version tags [OPTIONS] [TARGET]

  C10: every tag on every host by kind; names outside the scheme, lightweight or non-canonical
  release tags, and moved release tags (from the mirror's reflog). Exit 1 on a finding.

Options:
  --alerts          Raise a tag_moved alert for each moved release tag.
  --json            Print one JSON document per line.
  --ref TEXT        Branch, tag or commit (default: the canonical default branch).
  --path DIRECTORY  Read a local checkout (read-only) instead of the component's mirror.
  --store           Store the result (the /versions pages read stored results).
  -h, --help        Show this message and exit.
```

<a id="nanosoc-rm-version-yank"></a>

#### `nanosoc-rm version yank`

```
Usage: nanosoc-rm version yank [OPTIONS] TARGET VERSION_

  G6: propose yanking a tool release (recorded in the registry's versioning.yaml, never bumped to
  again, tag kept). Refused for a silicon release (exit 1).

Options:
  --reason TEXT  [required]
  --by TEXT      Who decided the yank.  [required]
  --patch TEXT   Write the registry patch to this NEW file.
  --json         Print one JSON document per line.
  -h, --help     Show this message and exit.
```

<a id="nanosoc-rm-walk"></a>

### `nanosoc-rm walk`

```
Usage: nanosoc-rm walk [OPTIONS] CONFIGURATION

  Resolve CONFIGURATION's whole submodule tree from its own .gitmodules and gitlinks, with every
  pin's state on each host (B2, B3). Stores the walk.

Options:
  --json      Print one JSON document per line.
  -h, --help  Show this message and exit.
```
