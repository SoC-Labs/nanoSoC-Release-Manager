# Gate 2 contracts

The data formats and module interfaces for the nanoSoC release manager, fixed before any
implementation (PR3). Builders code against these; reviewers and the coverage checker test against them
(PR5). After David approves gate 2, a contract changes only through a change request (PR2,
[`../CHANGES.md`](../CHANGES.md)).

- **Formats:** JSON Schema draft 2020-12, in [`schemas/`](../../schemas/).
- **Interfaces:** [`interfaces.md`](interfaces.md): modules, functions, CLI commands, HTTP endpoints, the T0 job,
  and (section 17) every name the acceptance tests rely on.
- **Examples:** [`schemas/examples/`](../../schemas/examples/): one or more valid instances per schema, built
  from real rc4 data where possible, plus deliberately broken ones in `examples/invalid/`.
- **Gate summary for approval:** [`../GATE2.md`](../GATE2.md).

Check everything with:

```
python3 schemas/check_examples.py
```

It exits 0 only if every schema is valid 2020-12, every example validates, and every invalid example
fails for the reason written on its first line (the schemas' own negative control, PR6). The reason is
written as `# expect-error: <validator keyword> at <instance pointer> [~ <message text>]` and matched on the
keyword and path, not on the message wording, so the check gives the same answer on every jsonschema release.
Result on 2026-09-30: **61 ok, 0 failed**, on jsonschema 4.19.1 (Python 3.8) and 4.26.0 (Python 3.11).
Dev dependencies are pinned in [`requirements-dev.txt`](../../requirements-dev.txt).

## 1. Index

| Contract | File (where it lives at run time) | Purpose | Owner module | Requirement IDs | Phase |
|---|---|---|---|---|---|
| [defs](../../schemas/defs.schema.json) | none (shared types) | SHA, host key, repo, component, configuration, defect and erratum ids, timestamps, `source`, `snapshot` (A3), URL kinds and pin states (B3), gate id | all | A1, A3, B3, B4, B9, C1, C2, D6, F3 | v1 |
| [component-catalogue](../../schemas/component-catalogue.schema.json) | registry `catalogue.yaml` | Every repo, grouped into components; class; one canonical host; copies; aliases; known rewrites; third-party repos; repos ignored with a reason | registry | A1, B1, B3, B6, B8, B9 | v1 |
| [eligibility](../../schemas/eligibility.schema.json) | registry `eligibility.yaml` | Registered configurations (branch or tag), frozen and silicon flags, errata id prefix, upgrade path; Phase 4 auto-merge scope | registry | B2, B5, D2, E3, E5, F1, H1 | v1 (auto_merge: 4) |
| [defect](../../schemas/defect.schema.json) | registry `defects/SL-DEF-NNNN.yaml` | Root cause per component: origin, forms, trigger, detectors, fixes, effects, bench pinned by commit, cited commits | registry | F1, F2, F3, F6, F7 | v1 |
| [erratum](../../schemas/erratum.schema.json) | registry `errata/<configuration>/ERR-NNN.yaml` | Effect on one configuration: forms, exposure and how it was proven (with the structured detector run), category, workaround, status, doc revision, cited commits | registry | F1, F2, F3, F4 | v1 |
| [import-ledger](../../schemas/import-ledger.schema.json) | registry `imports.yaml` | Every issue in the existing registers, the rule that finds them (census), and what became of each: record, dismissed (with reason) or pending | registry | F3, F5 | v1 |
| [pin-state](../../schemas/pin-state.schema.json) | walker output (JSONL), DB | One pin of a configuration's tree with its state on each host | walker | A3, B2, B3, B7, B9 | v1 |
| [observation](../../schemas/observation.schema.json) | DB, JSON API, CLI `--json` | Envelope of every fact shown (source, observed_at, snapshot) and the facts: host compare, containment, silicon protection, rewrite alert, mirror status, repo inventory, T0 result, runner settings, credential status, registry record, alert, branch state (CR8), release draft, release verify and fork status (CR-R1) | each producer; db | A1, A2, A3, B4, B5, B6, B8, D1, D2, D5, D6, D7, D10, D11, H1, H3, H6 | v1 |
| [result-bundle](../../schemas/result-bundle.schema.json) | CI artifact `rm-bundle/provenance.json` + `rm-bundle/verdicts.tsv`; pipeline variable `RM_REQUEST` | What a CI job was asked, ran and found; the verdicts.tsv row contract reused from the toolkit, plus a run nonce | T0 runner (writes); ci trigger/fetch/verify | A4, D2, D4, D5, D6, D7, D10, D11, PR9 | v1 (host, ip_hash, cpu_seconds: 2) |
| [component-manifest](../../schemas/component-manifest.schema.json) | `nanosoc-component.yaml` in each repo | ID, class, version, canonical URL, provides, requires, re-exports, variants, tests with controls | registry (reads), T0 (validates) | C1, C6, C7, C11, D1, D6, E1, E3, I2 | 3 (validated by T0 from v1) |
| [matrix](../../schemas/matrix.schema.json) | none ($defs of the observation kinds `matrix_cell`, `matrix_quarantine`, `matrix_view`) | One matrix cell run with its D4 capture, D8 attempts and D9 baseline; a D8 quarantine entry; the derived matrix view (interfaces.md section 18) | matrix | B7, D3, D4, D7, D8, D9 | 2 (built per CR-15) |
| [tool-release](../../schemas/tool-release.schema.json) | draft folder `release.json`; registry `versioning.yaml` (`$defs/versioning_file`) | A tool release draft for a person (G1) with its G5 approval record; the facts of the versioning observation kinds; yanked releases and grandfathered tags | versioning | C1-C12, F6, G1, G5, G6 | 3 (built under CR-15) |
| [release-lock](../../schemas/release-lock.schema.json) | release artefact `release.lock.yaml` (and `release.lock.draft.yaml`, `$defs/draft_lock`, CR-R1) | Every nested pin with host, state and (optional) protection; generated-RTL archive; GDS and netlist hashes; errata and ledger refs | `nanosoc_rm.releases` (`release draft`, interfaces.md section 14b) | A3, B7, C10, G2, G3, G7 | 5 (built under CR-15) |
| [bump-proposal](../../schemas/bump-proposal.schema.json) | `bumps.sqlite`, CLI `nanosoc-rm bumps --json`, `/bumps` pages | Wave B (CR-15, CR-WB-bumps): one shadow-mode bump proposal (pin move, changelog and class, defects, matrix cell, patch, decision with `auto_merge` always false); also the facts of the observation kinds bump_scan and bump_ledger (interfaces.md section 18) | bumps | E1, E2, E3, E5, E7 | Phase 4 (built, not enabled) |

Two process artefacts sit beside the schemas: [`../phase-gates.yaml`](../phase-gates.yaml) (A7, the entry gate of
each later phase; its format is in interfaces.md section 17.6) and [`../CHANGES.md`](../CHANGES.md) (PR2, the CR log).

The registry layout the loader expects:

```
catalogue.yaml
eligibility.yaml
defects/SL-DEF-NNNN.yaml              file name = id
errata/<configuration>/ERR-NNN.yaml   directory = configuration id; the record's id is <errata_prefix>-ERR-NNN
imports.yaml                          F5 import ledger
tiers.yaml                            optional; CR-12 local T1/T2 commands (eligibility#/$defs/tiers_file)
quality.yaml                          optional; CR-14 comment-density thresholds (eligibility#/$defs/quality_file)
versioning.yaml                       optional; CR-VER yanked releases, grandfathered tags (tool-release#/$defs/versioning_file)
```

## 2. Versioning rule

1. Every top-level document carries `schema: <int>`. Each schema file pins it with `const`.
2. Readers accept only the `schema` values they know, and refuse the rest with a clear error. They never guess.
3. **Bump the number** when a change can make a reader misread or reject a document it used to accept:
   a field removed or renamed, a type or meaning changed, a constraint tightened, a new required field.
   Ship a one-shot migration script with the bump.
4. **Keep the number** when adding an optional field or an enum value. Writers and readers are one package,
   so they move together. The tool release that adds the field is class `feature` (C2). A bump is `breaking`.
5. The registry's CI and the compat-ci harness each pin the tool version they validate with, so a schema
   change reaches them only when they move that pin.
6. Schema `$id`s are names (`https://soclabs.org/nanosoc-rm/schemas/...`), not download locations.
7. Until gate 2 is approved every schema is at `schema: 1` and may still change without a bump; the gate-2
   revision (2026-09-30) added required fields without bumping, because no document exists outside this repo.

Formats the tool REUSES keep their owners' rules: `verdicts.tsv` columns 1 to 4 are exactly the toolkit's
`ci/lib.sh` `_ci_record` output, and `ci/collect-results.py` still parses a 5-column row (it ignores column 5).
Two deviations are declared, not hidden:

- **INFRA and WARN.** `collect-results.py`'s `VERDICT_MAP` maps neither, so it parses those rows with status
  `None`. This tool never uses `collect-results.py` to roll up a bundle; its own roll-up (section 6) handles both.
- **Tabs and newlines in a detail.** `_ci_record` does not strip them, which breaks the row. The runner replaces
  each with one space before it writes a row; the row schema refuses a detail that still holds one.

## 3. Rules the loader enforces across files

JSON Schema checks one document. The registry loader (interfaces.md section 2) also refuses:

1. A component id used twice, or a repo that appears in two places (as canonical, copy, alias, external or
   ignored). (A1)
2. A configuration whose component is missing or is not class `soc_ref` or `example`. (B1, B2)
3. A `commit_ref`, `cites` entry, `regression_test`, workaround or detector source naming an unknown
   component; an import item whose `record` names a defect or erratum that does not exist. (F3, F5)
4. An erratum whose configuration or defect does not exist, or whose `forms_present` / `effects_present`
   / `exposure.detector` ids are not in the defect. (F1, F2)
5. A defect whose effects or detectors name a form it does not define. (F1)
6. `status: permanent` on an erratum whose configuration has no `silicon` block. (F1, A6)
7. A file name or directory that disagrees with the id inside it.
8. A duplicate key in any YAML file. YAML is read with timestamps left as strings.
9. **Free-text commits.** A token of 7 to 40 lowercase hex characters containing at least one digit, in any
   free-text field of a defect or erratum (title, trigger, effect texts, conditions, exposure evidence,
   workaround rules), that is not a prefix of a commit in the same record's structured citations (`cites`,
   `origin`, `fixed_in`, `candidate_fix`, detector `source`, `regression_test`, workaround `implementations`,
   `exposure.run.detector_source`). A token directly after `md5` is a file hash and is skipped. This is what
   lets T0 prove every cited commit (F3): the gate reads only structured citations. (F3)
10. `status: fixed` on an erratum of a silicon configuration: silicon is never fixed, only `open`,
    `permanent`, `wont-fix` or `not-affected`. (A6, F1)
11. An erratum whose id does not start with its configuration's `errata_prefix`, or a configuration with
    errata and no `errata_prefix`. (F1, F4)
12. `method: detector` without `exposure.run` is refused by the schema; the loader also refuses a run whose
    `detector_source` differs from the detector's `source` in the defect. (F2)
13. **T0 waivers** (GATE2 Q1). Two waivers of one configuration with the same `gate`, `tree_path` and `result`;
    a waiver whose `gate` is not a declared configuration gate. The schema already refuses a waiver on a
    configuration that is not frozen, and one on `t0.infra.probe`, `t0.runner.hardened`, a `t0.registry.*` gate
    or a `.negctl` row. Whether a waiver's `tree_path` exists is not a loader rule: it needs the walk, so the
    service reports a waiver that matched nothing as a `waiver_unused` alert (section 6). (D2, D6)

14. **Tiers** (CR-12). A `tiers.yaml` naming a configuration that eligibility.yaml does not register. The schema
    already refuses a tier command that sources a set_env.sh. (D1)

Rules 1 to 14 need no network. Whether each cited commit exists is T0's job (gate `t0.registry.refs-resolve`, F3).

Not enforced by any rule in v1, stated so nobody assumes otherwise: a registry change that deletes a silicon
configuration from `eligibility.yaml` would silently drop its silicon-protection rows. Registry `main` is
protected and changed only by merge request, and `eligibility.yaml` needs a code-owner approval: that is the
v1 control (GATE2 Q12, decided 2026-10-01).

## 4. Observations: subjects, snapshots, current value

Keys:

| kind | subject | set or event | snapshot scope |
|---|---|---|---|
| pin_state | `pin_state:<configuration>:<first tree path>` | set | `pin_state:<configuration>` (one walk) |
| host_compare | `host_compare:<component>:<copy host>:<copy path>` | set | `host_compare:<component>` |
| containment | `containment:<component>:<commit>:<configuration>:<host>` | set | `containment:<component>:<commit>` (one query) |
| silicon_protection | `silicon_protection:<configuration>:<component>:<commit>:<host>` | set | `silicon_protection:<configuration>` |
| mirror_status | `mirror_status:<host>:<path>` | set | `mirror_status:<component>` (one sync) |
| repo_inventory | `repo_inventory:<host>:<path>` | set | `repo_inventory:<host>` (one listing) |
| runner_settings | `runner_settings:<runner id>` | set | `runner_settings:<runner id>` |
| credential_status | `credential_status:<name>` | set | `credential_status:all` (one check) |
| registry_record | `registry_record:<file>#<pointer>` | set | `registry_record:<file>` (one registry read) |
| branch_state | `branch_state:<host>:<path>:<branch>` | set | `branch_state:<host>:<path>` (one inventory of one repo; CR8) |
| rewrite_alert | `rewrite_alert:<host>:<path>:<branch>:<old head>` | event | none |
| t0_result | `t0_result:<configuration or @registry>:<nonce>` | event | none |
| compat_result | `compat_result:<configuration>:<nonce>` | event (stored only on `--store`; CR-11) | none |
| release_draft, release_verify | `<kind>:<configuration>:<version>` | event (CR-R1; one per draft, the latest wins) | none |
| fork_status | `fork_status:<component>:<host>:<fork path>` | event (CR-R1; stored on `forks --store`) | none |
| regen_class | `regen_class:<component>:<from>:<to>:<configuration>` | event (interfaces section 20; stored on `regen class --store` and the `regen-class` job) | none |
| store_index | `store_index:artifactory` | event (interfaces section 22; stored on `store index --store` and the `store-index` job; source kind `artifactory_api`) | none |
| alert | `alert:<alert kind>:<about>` | event | none |
| matrix_cell | `matrix_cell:<configuration>:<component or ->:<column>:<nonce>` | event (D3; interfaces.md section 18) | none |
| matrix_quarantine | `matrix_quarantine:<configuration>:<test>` | event (D8; latest per subject is the current entry) | none |
| matrix_view | `matrix_view:<configuration>` | derived, never stored | none |
| quality_density, quality_finding, readme_check, doc_score | `<kind>:<target>:<commit>` (`<commit>+worktree` for a work-tree read; `worktree` outside git) | event (CR-14; one per target and commit, so they form a trend) | none |
| changelog_lint, version_plan, manifest_proposal, defect_trailers | `<kind>:<target>:<commit>` (`+worktree` as quality's) | event (CR-VER; stored on `--store` and by `version refresh`) | none |
| tag_check | `tag_check:<target>` | event (CR-VER) | none |
| tool_release_draft, yank_proposal | `<kind>:<target>:<version>` | event (CR-VER; `tool_release_draft` renamed from the versioning branch's `release_draft`, CR-WB-int) | none |

**Current value (A3).**

- **Set kinds** are produced as a complete set per scope, stamped with `snapshot: {id, scope}`. The current
  value of a scope is its latest *complete* snapshot. A subject absent from it is gone: a pin removed from a
  tree, a branch-only commit merged to main, a repo deleted from the host all disappear from the current view
  instead of staying "current" for ever (contracts review M3). A producer that fails midway writes no
  snapshot, so the previous complete one stays current and its age shows.
- **Event kinds** keep the latest observation per `subject`. Old ones are history.

**What `db.put` enforces** (interfaces.md section 12), beyond the schema:

1. The document validates against its schema.
2. The subject equals the key the table above derives from the fact.
3. The envelope's `observed_at` equals the oldest `observed_at` inside it (for pin_state, the oldest of the
   envelope and every host state), so a page's data age (now minus the oldest `observed_at` it shows) is
   never flattering.
4. A set-kind observation carries a `snapshot`; `put_snapshot(scope, docs)` writes a whole snapshot
   atomically and marks it complete.

## 5. Pin states (B3)

Exactly one state per pin per host. First match wins:

| # | State | Rule | Colour |
|---|---|---|---|
| 1 | LOCAL_URL | the URL is a local path (`/…`, `file://`) | red |
| 2 | NO_GITLINK | .gitmodules names the path, but the tree holds no gitlink there | red |
| 3 | UNREADABLE | the repo itself cannot be read with the poller's credentials | red |
| 4 | TAGGED | a tag other than `keep/*` points exactly at the commit | green |
| 5 | ON_DEFAULT | reachable from the default branch; `lag` = commits the pin lacks | green, amber as lag grows |
| 6 | PROTECTED_BY_TAG | not on the default branch, reachable from at least one tag | grey-green |
| 7 | BRANCH_ONLY | reachable only from non-default branches | amber; red in a silicon or frozen tree |
| 8 | REWRITTEN | not reachable now, but was on the default branch before a rewrite (that host's reflog in the mirror, or `known_rewrites`) | red |
| 9 | PR_ONLY | not reachable from heads or tags; the host has it through a PR/MR ref or its fork network | red |
| 10 | ORPHAN | the host has the object, and no ref of any kind reaches it | red |
| 11 | MISSING | the host does not have the object, after proving the repo is readable and the parent's entry is a gitlink | red |

- **LOCAL_URL and NO_GITLINK** do not depend on a host. They are carried in the pin-level `fact.state`, and
  `hosts` is empty. Every other pin has `fact.state: null` and one entry per host in `hosts`, exactly one of
  them `pinned`.
- **SSH-only** is `url_kind: ssh` on the pin, shown as a badge beside the state.
- **Relative URLs** (`../x.git`) are `url_kind: relative`, resolved against the parent's pinned repo URL as git
  does. None occurs in the rc4 or compute trees today.
- The `.gitmodules` `branch=` value is recorded in `branch_field` (null when absent) and never used.
- **Unknown URL (B9)** is `child.catalogued_as: unknown`, and raises an `unknown_url` alert. A URL naming an `ignored`
  repo is unknown too when a registered tree pins it (K1 option a, 2026-10-10).
- **UNREADABLE** means the host answered 401, 403 or 404 for the repo itself with the poller's token, or the
  repo's latest `mirror_status` is not ok and the mirror holds no refs for it.

"Reachable" always means from a branch or tag **of that host**. The mirror holds both hosts' objects in one
store, so an object can be in the mirror and still absent on one host (TideLink `5e8bdb5` on GitLab, AHB-QSPI
`e908ac8` on GitHub, ASIC-Flow `3824aaa` on GitLab). A commit reachable from no head or tag of that host is
decided by that host's reflog in the mirror, then `known_rewrites`, then that host's API (PR_ONLY, ORPHAN,
MISSING). PR/MR refs are never mirrored.

## 6. Verdicts (D5, D6, D7)

| verdicts.tsv row status | Gate verdict in the service |
|---|---|
| PASS, and row `<gate>.negctl` is PASS (the control fired) | PASS |
| PASS, and the `.negctl` row is FAIL or missing | UNVERIFIED (D6) |
| FAIL | FAIL |
| UNVERIFIED | UNVERIFIED |
| WARN, and the `.negctl` row is PASS (CR-9) | WARN (amber: checked, with listed warnings) |
| WARN, and the `.negctl` row is FAIL or missing | UNVERIFIED (D6) |
| INFRA | INFRA (never FAIL, D7) |
| SKIP on a gate that may skip | NOT-RUN; the detail must say why; control `not-applicable` |
| SKIP on a gate that may never skip | UNVERIFIED |
| no row, or two rows, for a declared gate | UNVERIFIED |

**Gates that may never SKIP** (listed in `gates.py` as `may_skip = False`): `t0.infra.probe`,
`t0.runner.hardened`, `t0.pins.fetchable`, `t0.pins.reachable`, `t0.urls.no-ssh-public`,
`t0.vendor.no-arm-ip`, `t0.registry.schema`, `t0.registry.refs-resolve`. Only `t0.manifest.valid` (no manifest
anywhere), `t0.clone.upgrade-path` (no `upgrade_from`) and `t0.vendor.no-foundry-data` (CR-6: no foundry rule on
the host, so nothing could judge foundry data) may skip.

**The two vendor gates (CR-6).** One scan per tree feeds both; each rule belongs to one gate
(`nanosoc_rm.scan.families`). `t0.vendor.no-arm-ip` judges Arm source (the Arm banner, a known vendor file by
hash, a file that could not be read): an uncovered finding is FAIL. `t0.vendor.no-foundry-data`
judges foundry data and site inventory (foundry file types, values, site paths, licence servers, big files, and,
CR-19, the toolkit's `ident` rule: release/bundle codes and product numbers in flists, tcl and CI files, which name
a product but are not Arm source): an uncovered finding
is WARN unless the catalogue's `foundry_rules` gives its rule `severity: fail`. So foundry and site-inventory
findings show amber on every run that has them and stop no run, unless the registry owner marks a rule blocking.
Both rows need a fired control like any other (D6); a foundry WARN row rolls up WARN by the table above.

**T0 waivers (GATE2 Q1, decided 2026-10-01).** A frozen configuration's tree never changes, so a failure in
it is permanent, and a permanent red hides any new red. `eligibility.yaml` may therefore list `t0_waivers`
for a **frozen** configuration: each names one item (gate, `tree_path`, item `result`) with a reason, an
approver and a date. The service applies them after the table above, item by item (`gates.apply_waivers`):

- An item is covered when a waiver names its gate, tree path and result, or when it is `not-reached` and an
  item above it in the same gate (a tree-path prefix) is covered: it was not reached because of that item.
- CR-6: the two vendor gates read one scan, so a waiver of a `not-reached` item in `t0.vendor.no-arm-ip` also
  names that item in `t0.vendor.no-foundry-data` (an unfetched tree is unscanned for both). Only `not-reached`
  carries over; a `fail` or `unverified` waiver covers its own gate only.
- A gate whose table verdict is FAIL or UNVERIFIED becomes **WAIVED** only when its control fired, it has at
  least one non-pass item, and every non-pass item is covered. Its `raw_verdict` (FAIL or UNVERIFIED) and the
  covered items (`waived`) are stored with it.
- Anything else is untouched. A gate failure with no items behind it (a missing or doubled row, a SKIP that
  may not skip, a control that did not fire) is never waived, and neither are INFRA, NOT-RUN or PASS. One
  uncovered item keeps the whole gate FAIL or UNVERIFIED, so a new failure on a frozen tree stays red.
- The runner never sees waivers. `verdicts.tsv` and the items files are exactly what the job produced (D5).
- A waiver that covered nothing in an accepted run raises a `waiver_unused` alert, so a waiver retires once
  its cause is fixed (for example after the GATE2 Q2 push of `e908ac8` to GitHub). It never changes a verdict.
- Pages show WAIVED amber, with the raw verdict and every covered item's reason and approver beside it.

A waiver matches the item, not its cause: a different failure at the same gate and tree path would be covered
too. Frozen trees make that rare, and the item's own `why` is still shown.

**Run verdict**, first match wins:

1. any gate FAIL → **FAIL**;
2. any gate UNVERIFIED, or **no gate PASS or WARN** (nothing was checked) → **UNVERIFIED**;
3. any gate INFRA → **INFRA**;
4. any gate WAIVED → **WAIVED** (amber: only a frozen configuration's known failures);
5. any gate WARN → **WARN** (amber; CR-9: checked, passed, with warnings a catalogue entry lists, such as the
   CR-6a `warn` vendor trees, or CR-6 foundry findings at their default severity);
6. otherwise → **PASS**.

**WARN below WAIVED (CR-9).** Both are amber. A WAIVED gate hides a real failure (its raw verdict is FAIL or
UNVERIFIED) that a person accepted for a frozen tree; a WARN gate passed and lists what it warns about. When a run
has both, it shows the more serious one, WAIVED, and the WARN gate is still shown beside it. A WARN gate is never
waived (waivers apply only to FAIL and UNVERIFIED), and it needs a fired control like PASS (D6).

**Local runs (CR-11, `nanosoc-rm compat`, interfaces.md section 9a).** The same table, with one difference: in run
mode `local`, `t0.runner.hardened` is never run; its one SKIP row `local run` rolls up NOT-RUN, and any other row for
it is UNVERIFIED. The run rules below are unchanged, so a local run still needs a PASS (or WARN) gate. With
`--tier` (CR-12) the run verdict (`run_verdict`) is the ladder FAIL > UNVERIFIED > INFRA > WAIVED > WARN > PASS over
the T0 verdict and the tier's own verdict (interfaces.md section 9a "Tiers").

So a run where every gate skipped, or a run with no gates, can never be PASS (contracts review B1; the toolkit's
own doctrine that an unread report is never a pass). NOT-RUN gates are counted and shown beside the verdict.
The observation schema enforces the same rule on every stored `t0_result`.

- **Rejected bundle (D5)** or **refused bundle (D11):** the run's verdict is UNVERIFIED and the reason is shown.
- **No bundle:** INFRA when the CI API reports an infrastructure cause, else UNVERIFIED. A trigger that fails
  (HTTP error) is also recorded, as `no-bundle` with the HTTP status in the reason, so a missed night is visible.
- **Over budget (D1):** a T0 run whose `finished_at - started_at` exceeds 120 s gets `over_budget: true`, shown
  as a warning beside the verdict. It never changes the verdict. The job's 15-minute timeout is the kill limit.
- **"T0 fails" (D2, F3)** means the run verdict is not PASS or WARN. A FAIL gate proves the problem; an UNVERIFIED gate
  (for example a citation into a private canonical repo that an anonymous job cannot read) also stops the run
  from passing and says why. The service does not prove private citations from its own authenticated mirrors
  (GATE2 Q3, decided 2026-10-01): they stay UNVERIFIED. WAIVED is not PASS either: a waived run has failures,
  all of them known. WARN (CR-9) does not fail T0: the run was checked and passed, and the warnings are shown
  amber, not blocking (David's U1 decision, 2026-10-10).

## 7. Examples and what is real

| Example | Real (observed 2026-09-30, read-only) | Illustrative |
|---|---|---|
| `component-catalogue.example.yaml` | all repo paths, copies, the slcorem0p_tech alias, externals, the legacy repo, the three `ignored` repos | every `class`; canonical host of ahb_qspi and asic-flow; the choice of repos to ignore |
| `eligibility.example.yaml` | eth-rc4 commit and tags; the 7 failing items its `t0_waivers` name (the eighth, ahb_qspi, retired by CR-2) | the choice of configurations; eth-main's upgrade_from; `errata_prefix`; the waivers' approver and wording |
| `result-bundle.items.<gate>.example.jsonl` (4) | rc4's tree paths, commits, ssh URLs and the ahb_qspi gap | EXCERPTS: only the non-pass items of rc4's four non-pass gates; no T0 job has run yet |
| `observation.t0-result-waived.example.jsonl` | the gates and items above | the run (ids, times, runner); the roll-up rc4 gets once its waivers are registered |
| `defect.SL-DEF-0001.example.yaml` | every commit (full ids resolved), fix order on nanosoc_gen main, hsel_lint usage | effect-to-port mapping and netlist pattern (from the proposal, not re-checked) |
| `erratum.ETH-R0P0-ERR-001.example.yaml` | ancestry evidence (merge_base), the 7 cited commits, 57d2c76 in rc4 (ancestor of f60449b) | doc_revision; category left null |
| `pin-state.example.jsonl` | 3 rc4 pins, each host's state, with the commands that produced them | the snapshot id |
| `observation.example.jsonl` | 3 host compares (counts from rev-list in one object store), 1 containment, 13 silicon-protection rows (11 on the pinned host, all covered; 2 on the canonical host, covered by the 2026-10-01 keep tags, re-derived 2026-10-10 by CR-2), mirror status, inventory | the rewrite alert (synthetic fixture repo), the rejected T0 run, the runner settings, the credential status, the registry record and the alert (none of those systems exists yet); snapshot ids; the time of day of the ahb_qspi canonical-host row |
| `release-lock.eth-rc4.example.yaml` | all 44 pins with state, lag and tags; P1 archive md5; GDS and netlist md5 and size | errata, gate_ledger, generated_by.version; store paths and output file names are placeholders |
| `result-bundle.provenance.example.json` + `.verdicts.example.tsv` + `.rm-request.example.json` | commits, the ssh URLs, and the ahb_qspi reachability gap they report | run ids, times, harness SHA, the t0.runner.hardened rows; no T0 job has run yet; `captured.pins` lists 8 of the 44 |
| `component-manifest.example.yaml` | canonical URL | everything else; no repo has a manifest yet |
| `import-ledger.example.yaml` | source documents, their commit, item headings, the census patterns | none (dispositions left `pending`, lists deliberately incomplete) |

**Public-repo hygiene.** This repo is public (DEC8). Where a real value names the lab's GitLab host the examples
write `<gitlab-host>`; the real hostname lives only in the private service config (interfaces.md section 1).
The release-store path layout and the silicon output file names are placeholders. A branch name of a private
repo is written `<branch of a private repo>`. Logins in examples are `member-a`, `alice`, `bob`, `carol`.

### Facts found while building the fixtures

These affect acceptance tests:

1. **rc4 → TideLink `5e8bdb5` is TAGGED.** It is `v0.90` exactly, 5 behind GitHub main. The proposal listed it as ON_DEFAULT. The same commit is MISSING on the GitLab copy `soclabs/tidelink`.
2. **rc4 has 6 split components, not 4.** Pinned at more than one commit:
   - nanosoc_arch_tech, nanosoc_gen, socdebug_tech, hostio4 (the proposal's 4);
   - tidelink-gpio-phy: `6ee8418` and `8c560c5`;
   - asic-flow: `69500ef` and `46c88d3` on GitLab, and `3824aaa` on GitHub.
3. **All 11 branch-only rc4 commits are now PROTECTED_BY_TAG on their pinned host.** Multicore's branch `fix/dma250-hsel-guard` disappeared between 09-28 and 09-30. The keep tag now covers `f60449b`.
4. **GitHub's compare API returns 404 when the other host's head is absent.** That says nothing about divergence. The real answers came from one object store holding both hosts' refs:
   - AHB-QSPI: GitHub +1, GitLab +5.
   - ASIC-Flow: GitHub +4, GitLab +1. The two default branches have different names (GitHub `lpddr4-pll`, GitLab `main`), so `default_branch_mismatch` is true.
   - The mirror design follows from this (interfaces.md section 3).
5. **The SL-DEF-0001 bench `6aafc94` exists only in a local checkout.** Recording it makes `t0.registry.refs-resolve` fail until it is pushed: F3 working on real data.
6. **rc4's pin set does not fit one canonical host for two components:**
   - ahb_qspi `e908ac8` exists only on GitLab;
   - asic-flow `3824aaa` exists only on GitHub.

   Whichever host is canonical for ahb_qspi, D2's "reachable on its canonical host" check will judge rc4 by that host's copy.
7. **B5 on the canonical host differs from B5 on the pinned host.** On their pinned hosts all 11 branch-only commits are covered. On the canonical host (the catalogue example's choice), rc4's multicore arch_tech pin `ecf76aa` is BRANCH_ONLY with no tag, and ahb_qspi `e908ac8` is MISSING. The silicon-protection list therefore shows both hosts (interfaces.md section 7; question 9 in GATE2.md).

## 8. Reuse (PR9)

| Existing asset | Where it is used |
|---|---|
| `walk.py` prototype | Pin walker (interfaces.md section 4): URL normalisation, `.gitmodules` parsing, gitlink type check, dedupe by (repo, commit), recursion. Changed to keep full SHAs, to read mirrors before APIs, and to take the host table from the service config: the prototype hard-codes the lab GitLab hostname, which the port must not. rc4's 44-pin output is the B2 golden. |
| toolkit `ci/lib.sh` `_ci_record` | verdicts.tsv columns 1 to 4 and the gate-id convention, unchanged; column 5 (nonce) added; details cleaned of tabs and newlines (section 2). |
| toolkit `ci/collect-results.py` | The row parser and the roll-up doctrine (an unread report is never a pass; SKIP is not a pass). Its `VERDICT_MAP` is not reused, because it lacks INFRA and WARN (section 2). |
| toolkit `ci/check-pins.sh` | T0 reachability method: `merge-base --is-ancestor` against fetched refs, never `ls-remote` tips; `--offline` asymmetry (stale cache: PASS sound, not-found UNVERIFIED). |
| toolkit `ci/check-vendor-collateral.sh` | Bundle and tree scanner (D11, D2 no-Arm-IP). Its method is kept: arm every rule on a specimen first, census the corpus (empty = UNVERIFIED), allowlist with owner. |
| toolkit `ci/check-vacuous-gates.sh` | The same method is applied to the tool's own gates: every T0 gate has a control. |
| fpgahub stack | Layout and dependencies: Python 3.11, click CLI, FastAPI + Jinja2 + htmx, pydantic, httpx, itsdangerous sessions, hatchling, one systemd unit (H4). fpgahub has no SQLite layer, so the `db` module is new code, not reuse. |
| Artifactory | Store `artifactory` in `store_ref` (D12, G7). The P1 rc4 archive is already there. |
| compute chiplet's submodule-pin CI workflow | Gate `t0.pins.fetchable`: its probe (`git fetch --depth=1 <url> <sha>` into an empty repo, no credentials, `GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) and its failure classes: not-our-ref and auth are FAIL, unreachable host is INFRA. Extended from direct pins to the whole recursive tree. |

## 9. How the process requirements are verified

The v1 acceptance tests cover the 28 v1 Must requirements. The process Musts are checked like this:

| ID | Verified by |
|---|---|
| PR1 | `docs/REQUIREMENTS.md` v1.0 committed (3190166). |
| PR2 | [`../CHANGES.md`](../CHANGES.md) exists next to REQUIREMENTS.md and is the CR log. |
| PR3 | This directory, `schemas/`, `docs/acceptance/`, `tests/acceptance/`, and David's approval of [`../GATE2.md`](../GATE2.md). |
| PR4 | interfaces.md section 0: every capability is a function with a CLI front end; AT-H1 compares each page with its CLI command. |
| PR5 | The coverage checker runs `pytest tests/acceptance -m requirement` and maps each requirement marker to a result; the integrator runs the `live` tests on the service VM (interfaces.md section 17). |
| PR6 | Every AT has a `_negative_control` test; the checker-level controls run today as `test_selfcheck_*`; `schemas/check_examples.py` is the schemas' own control. |
| PR7 | Commits carry a `Requirements: <IDs>` trailer; `nanosoc-rm trace` (Phase 1 builder task) generates requirement → tests → commits from the markers and trailers. AT-A7 reads the same trailers. |
| PR8 | AT-A2 and AT-H3 check every v1 credential's scope; writes a person must make are listed in GATE2.md, never made by an agent. |
