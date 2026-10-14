# Gate 2: contracts and acceptance tests

**APPROVED 2026-10-01 by David (PR3)** on the gate-2 sign-off page: V1 to V4 (contracts, acceptance tests,
review resolutions, what is left for later) agreed; Q1 to Q12 each answered with the recommended option (section
5). Q1 needed contract work before approval: frozen-configuration T0 waivers, in the same commit as this record
(section 5a). From now on `docs/contracts/`, `schemas/` and `tests/acceptance/` change only by change request
(`docs/CHANGES.md`), like the requirements. Raw answers: `signoff-answers/responses_gate2/` in the design folder.

*Written 2026-09-30 for approval; approval record and Q1 work added 2026-10-01.*

**State at approval:**

- Contracts: 11 schemas, the module interfaces, and one set of names for every test seam.
  `python3 schemas/check_examples.py` gives **84 ok, 0 failed** on jsonschema 4.19.1 and 4.26.0 (61 at
  review, plus the Q1 waiver examples).
- Acceptance tests: one AT and one negative control for each of the 28 v1 Must requirements (56 tests), plus
  32 self-checks that prove the negative controls' inputs today. `pytest --collect-only` collects **88 tests**;
  a full run gives **32 passed, 56 xfailed** on Python 3.8 and 3.11; pyflakes is clean. Each AT reports XFAIL
  until its module exists, then must pass.
- Both reviews (contracts: 1 blocker, 13 major, 15 minor; coverage: 4 high, 5 medium, 5 low) are worked
  through in section 4. The blocker, every coverage issue, 12 of the 13 majors and 13 of the 15 minors are
  fixed. M8 and m13 are fixed in part, and m15 is not fixed. Each open part waits on a question in section 5
  or on a later phase.
- Nothing was committed or pushed, and no host was written to.

## 1. What gate 2 contains

| Path | What |
|---|---|
| `docs/GATE2.md` | This page. |
| `docs/REQUIREMENTS.md` | Requirements v1.0 (frozen, unchanged). |
| `docs/CHANGES.md` | New: the change-request log (PR2), empty, with one candidate CR noted. |
| `docs/phase-gates.yaml` | New: the written entry gate of Phases 2 to 5 (A7), all `not-met`. |
| `docs/contracts/README.md` | Contract index, versioning rule, loader rules 1 to 12, observation subjects and snapshots, pin states, verdict rules, examples, reuse, how PR1 to PR8 are verified. |
| `docs/contracts/interfaces.md` | Every v1 module: functions, CLI commands, HTTP routes and page hooks, the T0 job and its gates, verify, store, auth; section 17 fixes every name the tests rely on and indexes where each test-writer gap was resolved. |
| `schemas/*.schema.json` (11) | defs, component-catalogue, eligibility, defect, erratum, import-ledger, pin-state, observation, result-bundle, component-manifest, release-lock. |
| `schemas/examples/` (17 files) | Valid examples built from real rc4 data (read-only, 2026-09-30) where possible; 5 added for Q1. |
| `schemas/examples/invalid/` (27 files) | One deliberate defect each; the schemas' own negative control; 4 added for Q1. |
| `schemas/check_examples.py` | Validates every schema and example; matches each invalid example on its validator keyword and path. |
| `docs/acceptance/pins.md`, `ci_errata.md`, `scope_web.md` | The AT specifications: Given, When, Then, fixture, negative control, why it cannot pass vacuously, what is untestable. |
| `tests/acceptance/test_pins.py`, `test_ci_errata.py`, `test_scope_web.py` | The ATs and self-checks. |
| `tests/conftest.py` | Registers the `requirement` and `live` markers; imports the package before each AT, so nothing passes by skipping. |
| `requirements-dev.txt`, `.gitignore` | Test dependencies with the versions checked; generated files kept out of git. |

## 2. The contracts

| Contract | Purpose | Requirement IDs |
|---|---|---|
| defs | Shared types: SHA, host, repo, ids, timestamps, `source`, `snapshot`, URL kinds, pin states, gate id | A1, A3, B3, B4, B9, C1, C2, D6, F3 |
| component-catalogue | Every repo grouped into components, one class and one canonical host each; copies, aliases, known rewrites, externals, ignored repos | A1, B1, B3, B6, B8, B9 |
| eligibility | Registered configurations: ref, frozen commit, silicon, errata prefix, upgrade path; Phase 4 auto-merge scope | B2, B5, D2, E3, E5, F1, H1 |
| defect | Root cause: origin, forms, trigger, detectors, fixes, effects, bench pinned by commit, cited commits | F1, F2, F3, F6, F7 |
| erratum | Effect on one configuration: forms, exposure and how it was proven (structured detector run), category, workaround, status, doc revision, cited commits | F1, F2, F3, F4 |
| import-ledger | Every issue in the old registers, the rule that finds them, and each one's disposition | F3, F5 |
| pin-state | One pin of a tree with its state on each host | A3, B2, B3, B7, B9 |
| observation | The envelope of every fact (source, time, snapshot) and 12 fact kinds: pin state, host compare, containment, silicon protection, rewrite alert, mirror status, repo inventory, T0 result, runner settings, credential status, registry record, alert | A1, A2, A3, B4, B5, B6, B8, D1, D2, D5, D6, D7, D10, D11, H1, H3, H6 |
| result-bundle | What a CI job was asked (`RM_REQUEST`), ran and found (`provenance.json`, 5-column `verdicts.tsv`) | A4, D2, D4, D5, D6, D7, D10, D11, PR9 |
| component-manifest | A repo's own manifest: id, class, version, provides, requires, re-exports, variants, tests (Phase 3; T0 validates it from v1) | C1, C6, C7, C11, D1, D6, E1, E3, I2 |
| release-lock | A release's full pin list with states, archive and output hashes (Phase 5) | A3, B7, C10, G2, G3, G7 |
| interfaces.md sections 0 to 16 | Modules, CLI, web and JSON API, T0 job and gates, trigger, fetch and verify, scanner, store, auth | every v1 Must |
| interfaces.md section 17 | Config keys, module paths, extra CLI commands, test markers and live inputs, write allowlist, phase-gate register format | PR4, PR5, PR6, A2, A6, A7, H3 |
| `docs/phase-gates.yaml` | Each later phase's entry gate and how it is checked | A7 |
| `docs/CHANGES.md` | The CR log | PR2 |

## 3. Acceptance-test index

Each requirement has `test_AT_<ID>` and `test_AT_<ID>_negative_control`, both calling one checker. "Live" needs
the service VM (private config, registry checkout, network); "hermetic" and "offline" need only git and
Python. The specs are in `docs/acceptance/`.

| ID | Acceptance test | Negative control (must go red) | Runs |
|---|---|---|---|
| A1 | `inventory` lists every repo in the org and group (count = the host's own total: 57 + 101 today); each is classified; one canonical per component; `--unclassified` is empty | Catalogue without secworks-sha256-project: exactly that repo is flagged, exit 1; GitLab page 2 must be fetched | live / offline |
| A2 | A static census finds exactly one write (POST `trigger/pipeline`), covered by `WRITE_CALLS`; exactly 4 v1 credentials; GitLab confirms the poller's scopes; each manual Run has one `actions` row | Planted push, POST and DELETE, a second write, scope `api`, a Run with its `actions` row deleted: each caught | live / offline |
| A3 | Every page shows one data age, measured from its oldest fact; every fact links to `/facts/{subject}` with its exact commands; every API line is a valid observation; `db.put` refuses a fact without `observed_at` | A link removed, the age missing, the age taken from the newest fact, a stored row without sources: each caught | live / offline |
| A4 | Run on eth-rc4 starts a trigger pipeline with one `t0` job; its `RM_REQUEST` carries the run's nonce and `ab73e40`; the result carries that job's id and a verdict; the service never imports the runner | Trigger answers 401: exactly one `no-bundle` run, no pipeline, reason names 401. Foreign nonce: rejected at verify check 2 | live + Run / offline |
| A5 | Every route in the app's route table: a member is never refused, a signed-in non-member gets 403, anonymous gets 401 or the login redirect, no refused body holds rc4 data | An app whose auth trusts non-member bob, and a raw route added after construction: both caught | offline |
| A6 | No call site writes to a user's fork or touches a silicon release; the schemas refuse yanking or un-freezing rc4; a full CLI cycle over a tree that pins a fork pushes nothing and changes no refs | A planted fork write, a push to an unresolvable URL and a silicon tag delete are caught; the git runner refuses a push | offline |
| A7 | Phases 2 to 5 each have a checkable entry gate in `docs/phase-gates.yaml`; no `src/` commit names a later-phase ID before its gate; no later-phase route or command exists early | A D3 code commit before the Phase 2 gate, a `/releases` route and a `lock` command: caught; a contracts-only commit is not | offline |
| B1 | The real registry loads; every component has exactly one of the 8 classes; a legacy entry is listed and never tested | 5 registries with one defect each (no class, two classes, unknown class, duplicate id, a configuration on the legacy repo): all refused | live / hermetic |
| B2 | `walk eth-rc4` gives exactly rc4's 44 pin keys, commits, repos and tree paths; depths 5/9/17/12/1 | eth main's real tree (43 pins) is reported as not rc4's; a golden missing one pin is reported | live |
| B3 | rc4 (44 + 7) and multicore (35 + 4) host states match the goldens; `branch=` is ignored; a hermetic table yields all 11 states; a person signs the hand check | `keep/eth-rc4/nanosoc_gen` deleted: `d04de2e` reads BRANCH_ONLY. Tag `v1.0.0` deleted: the TAGGED pin reads ON_DEFAULT | live + hermetic / shadow + hermetic |
| B4 | `036d381` gives eth-rc4 `no` on GitLab, both generator pins proven; `yes`, `partial`, `unknown` and `absent` all occur on real data; exit 0 on `no`, 2 on `unknown` | `d04de2e` kept as an object but reachable from no ref: `unknown`, never `no`; `0000000`: exit 3 | live / shadow |
| B5 | rc4 gives 13 rows: the 11 commits on their pinned host, all covered by keep tags, and 2 canonical-host rows (`ecf76aa`, `e908ac8` on GitHub), uncovered; `--uncovered` lists exactly those 2 | `keep/eth-rc4/tidechart` deleted: tidechart joins the uncovered list | live / shadow |
| B6 | On the 2026-09-30 heads: TideLink AHEAD 298/0, AHB-QSPI DIVERGED 1/5, ASIC-Flow DIVERGED 4/1, counted by rev-list in one mirror; DIVERGED exits 1; live rows equal raw git | AHB-QSPI's copy synced reads SAME and fails; ASIC-Flow with its GitLab refs gone reads UNKNOWN | live + shadow / shadow |
| B8 | A force-push to a local test repo's main gives exactly one alert: old and new head, 3 lost commits, the lost pin; the pin then reads REWRITTEN | A fast-forward and a force-push of a non-default branch: no alert | hermetic |
| D1 | 7 tiers, each with a budget or a written reason (T0 120 s); every configuration has a T0 command; every accepted nightly T0 took at most 120 s | A 181 s bundle is flagged over budget; t2 without a budget fails validation | live / hermetic |
| D2 | Tonight there is one accepted scheduled T0 per configuration and for `@registry`; rc4's raw gates are red on its known items and its waivers cover exactly those (Q1); all 44 pins are judged on their canonical host; every gate that ran fired its control | The trigger returns HTTP 500 for eth-main: the missed night is recorded and `t0 coverage` exits 1. The 4 gates rc4 cannot exercise each PASS and FAIL on hermetic inputs | live / hermetic |
| D5 | The genuine rc4 bundle is accepted; 13 single-field tampers are each rejected at the check that owns the field; only the two Run POST routes exist | `captured.superproject_commit` set to rc4's parent `3ab16f5`: rejected at check 5, stored UNVERIFIED with no bundle | hermetic |
| D6 | Every gate is reported with its control and whether it fired; rc4 rolls up to the golden (FAIL, 2 not run; 3 since CR-6), and WAIVED with its waivers (Q1); no PASS without a fired control | Every gate in turn made always-PASS: its control does not fire, so the gate is UNVERIFIED. A control row deleted, flipped or doubled: UNVERIFIED. A new failure, a `fail` below a waived pin, or an unfired control: never WAIVED | hermetic |
| D10 | Real jobs pass `t0.runner.hardened`; the runner id is allowlisted; the runner's settings are recorded, fresh and without drift | 5 environments, each with one variable wrong (ref, HOME, TMPDIR, user, `CI_JOB_TOKEN`), each FAIL; a runner outside the allowlist is rejected at check 1 | live / hermetic |
| D11 | The genuine bundle scans clean (more than 0 files, every rule armed) and is stored; `arm.banner` fires on a real vendor file (lab only) | A banner planted at `logs/t0.log:3` is refused and never stored, with no matched text anywhere; an empty bundle is refused; the job drops the file before upload | live / hermetic |
| F1 | SL-DEF-0001 holds the real facts (origins, fixes `036d381` and `adbb9ab`, D1 and D2, E1 to E5, bench); one erratum each for eth, compute and QuickStart; containment says `no` for both fixes | The rc4 erratum copied to eth-main, still `permanent`: rule 6. Form C: rule 4 | live / hermetic |
| F2 | Every erratum shows level, method and evidence; ancestry shows amber; detector levels carry a structured run; D1 runs on the rc4 archive | Ancestry claiming `exposed` is rejected; detector D9 is refused; D2 fires on a netlist with the defect and stays clean on one without | live / hermetic |
| F3 | With every cited commit pushed, refs-resolve passes and proves the 10 cited commits on their canonical hosts | The real bench `6aafc94` (unpushed) fails; a commit only on the non-canonical ahb_qspi host fails; the local-only QuickStart `1f2f06e` fails | live |
| F5 | The ledger has exactly the 4 sources; each census matches its document at the cited commit; nothing is pending | The example ledger (3 pending) is not done; a ledger missing `c)` is not done | live / hermetic |
| H1 | Each v1 page on live data shows exactly its CLI's facts; the dashboard has the 6 items and one CSRF Run form per configuration; Run shows the pending run and disables the button | A missing runner status, a missing row, a form without CSRF, a wrong count, a missing or invented fact: each caught | live + Run / offline |
| H2 | Only `active` members get a session (a pending invitee and a non-member get 403); scope `read:org` only; `state` checked; the user's token is stored nowhere; roles come from the config; the Run is logged | The fake says bob is `active`: his session and the pages he opened are reported | offline + live probe |
| H3 | Exactly the 4 v1 credentials, each within its role and checked against the host where the host can say; never `ok` unchecked; an expiry alert 14 days ahead; no Phase 4 credential before its gate | The fake GitLab reports `api` for the poller token: a finding, exit 1 | live / offline |
| H6 | Every catalogued repo's mirror is at most 20 minutes old in two snapshots 16 minutes apart, and advanced; the table prints each age; heads and tags only; equal to `ls-remote` | `clone --mirror`, and a fetch without `--no-tags`, are caught; with its host removed a mirror reads `ok: false` and is stale 16 minutes later | live / offline |

**Self-checks that run today (31):** 9 in `test_pins.py` (each B checker on real and broken input; a raw-git
oracle for the B3 and B8 fixtures), 10 in `test_ci_errata.py` (reference roll-up, verify and hardened-runner
oracles; the hermetic gate fixtures proven with raw git; citation and census oracles), 12 in
`test_scope_web.py` (the 11 A and H checkers, and the phase-gate register).

## 4. Review log

### Contracts review (1 blocker, 13 major, 15 minor)

| Issue | Status | How, or why not |
|---|---|---|
| **B1** A run could PASS with nothing checked | FIXED | README section 6: PASS needs at least one PASS gate; nothing checked is UNVERIFIED; 8 gates may never SKIP (a SKIP there is UNVERIFIED). The observation schema enforces the rule on every stored `t0_result`. 3 new invalid examples (`observation.t0-pass-*`); a reference roll-up self-check. |
| **M1** B5 judged on the pinned host, not the canonical one | FIXED; semantics are question 9 | Rows on both hosts: `host` in the subject, `pinned`, `canonical` and `state` in the fact. The rc4 golden is now 13 rows: the done-when's 11, all covered, plus `ecf76aa` (BRANCH_ONLY) and `e908ac8` (MISSING) on GitHub, both uncovered. |
| **M2** T0 budget contradicted D1 | FIXED | 120 s budget, 15-minute kill limit, over budget is a warning. Tier table in interfaces section 9; `started_at`, `finished_at` and `over_budget` on `t0_result`. |
| **M3** Facts that disappear stayed current | FIXED | `snapshot {id, scope}` on every set kind; the current value is the latest complete snapshot per scope (README section 4); `db.put_snapshot`. |
| **M4** Commits cited in free text were never checked | FIXED | `cites` on defect and erratum; loader rule 9 refuses a free-text commit without a structured citation; the erratum example cites its 7; refs-resolve reads `cites`, so AT-F3 now proves 10 commits. |
| **M5** Containment answer not tied to its pins | FIXED | A conditional per answer in the schema; new invalid example `observation.containment-no-with-undecided-pin.yaml`. |
| **M6** Bundle uploaded before the scan could stop it | FIXED | `finalise_bundle()` deletes flagged files before the upload step and keeps only their locations (`provenance.scan.findings`, required on `finding`); `artifacts: access: developer`; compat-ci stated private. |
| **M7** The D5 commit check had no source | FIXED | `superproject_commit` = `rev-parse FETCH_HEAD^{commit}`; `captured.pins` = the gitlinks the walk read; D5's authority stated to rest on verify checks 1 to 4 and 6. |
| **M8** Pipeline variables and trigger identity unverified | FIXED in part | Verify check 1 requires `source == trigger`; check 2 names a missing `RM_REQUEST`; the runner writes no rows without it; the trigger token is owned by a dedicated service account. No new gate: verify already rejects the run. **Not fixed:** compat-ci does not exist yet, so the variables setting and the role needed to read variables are unknown (question 10). |
| **M9** "No credentials of any kind" was false | FIXED | The runner scrubs every CI token, password and JWT variable before its first git call (one registry read excepted, question 3); `t0.runner.hardened` checks the scrub; the D10 control gains a `CI_JOB_TOKEN` case. |
| **M10** Runner identity and status had no data | FIXED | `runner` on `t0_result`; verify rejects a runner outside `ci.allowed_runner_ids`; new `runner_settings` kind and `nanosoc-rm runner`; the dashboard's runner status reads it. Whether the read token may read runner settings: question 10. |
| **M11** No credential check or expiry data | FIXED | `nanosoc-rm credentials check`, kind `credential_status`, alert `token_expiry` (interfaces 17.3). |
| **M12** No detector path; exposure evidence free text | FIXED | `nanosoc-rm detect` / `analysis.detect.run` (interfaces 6a), run by a person or CI, never by the service (A4); `exposure.run` required for detector levels and proved by refs-resolve. |
| **M13** Private canonical repos can only be UNVERIFIED | FIXED as a rule | README section 6: "T0 fails" means the run is not PASS, and UNVERIFIED blocks PASS. Whether the service should prove private citations itself: question 3. |
| **m1** INFRA and WARN unmapped in `collect-results.py`; `lib.sh` does not strip tabs | FIXED | Declared in README section 2; the runner cleans details; the row schema says so. |
| **m2** fpgahub has no SQLite layer | FIXED | README section 8 and interfaces section 0: `db` is new code. |
| **m3** GitLab listing needs subgroups; non-nanoSoC repos | FIXED | `include_subgroups=true` and pagination in interfaces section 3; catalogue `ignored` with reasons, inventory role `ignored`, 3 examples. Which families to ignore: question 6. |
| **m4** Import items could not become errata | FIXED | `record` accepts defect or erratum ids; a pending item may carry neither record nor reason. |
| **m5** Erratum prefix not tied to a configuration | FIXED | Eligibility `errata_prefix`; loader rule 11. |
| **m6** No "ancestry says fixed"; detector allowed with probably-exposed | FIXED | Level `probably-not-affected`; a detector method only proves exposed, affected or not-affected; new invalid example. |
| **m7** BEHIND unconstrained; UNKNOWN unwritable; ASIC-Flow compares two branch names | FIXED | Per-relation count rules; UNKNOWN counts null, heads nullable; `default_branch_mismatch`; the rewrite detector is keyed on branch name; new invalid example. |
| **m8** pin-state accepted two pinned hosts, empty hosts, wrong url_kind | FIXED | Exactly one pinned host; hosts required for decidable pins; `url_kind` tied to the URL; new invalid example. |
| **m9** Envelope time rule not expressible | FIXED | A `db.put` rule (README section 4). |
| **m10** `RM_REQUEST` had no schema; equality undefined | FIXED | `$defs/rm_request` with an example; the stored request minus `schema` and `nonce` must equal `provenance.request`. |
| **m11** No CR log; PR1 to PR8 unverified | FIXED | `docs/CHANGES.md`; README section 9. |
| **m12** Relative submodule URLs undefined | FIXED | `url_kind: relative`, resolved against the parent's pinned URL. |
| **m13** Public-repo hygiene | FIXED in part | Placeholders for the release-store path, output file names, a private repo's branch and every login; `<gitlab-host>` everywhere; the compute workflow path made generic; advisory notes on unprotected commits removed. Kept, pending question 11: GDS and netlist md5 and size, private repo names. |
| **m14** The walk.py port must not hard-code the lab host | FIXED | README section 8, interfaces section 2. |
| **m15** POL-1 has no gate | NOT FIXED | Proposal intent, not required by any v1 Must; B5's `--uncovered` exit 1 carries the signal; recorded for Phase 5 in interfaces section 15. |
| Unused `observed` and `schema_version` defs | FIXED | Removed. |
| Broken-document probe: 12 accepted | FIXED | Re-run on the revised schemas: 3 of its 22 documents are still accepted, each for a rule JSON Schema cannot express, now enforced by `db.put` (subject derived from the fact; envelope time) or by loader rule 10 (a silicon erratum marked `fixed`). A fourth (an import item recorded as an erratum) is now intended (m4). |

### Coverage review (4 high, 5 medium, 5 low)

| Issue | Status | How, or why not |
|---|---|---|
| **High 1** Four tests could not pass against the contracts | FIXED | A3: `configurations` and `alerts` now return `registry_record` and `alert` observations. B6: UNKNOWN rows allowed. H6: `--no-tags` in the section 3 fetch. D1: budget 120 s. |
| **High 2** Three D2 checks never exercised | FIXED | AT-D2's control now runs `t0.manifest.valid`, `t0.clone.upgrade-path`, `t0.vendor.no-arm-ip` and `t0.registry.schema` on hermetic good and broken inputs, through a test-only `url_map` seam the runner refuses in real jobs; AT-D2 requires eth-main's upgrade-path gate to run; a self-check proves the fixtures with raw git. |
| **High 3** D and F controls never shown to fire | FIXED | 10 self-checks in `test_ci_errata.py`; the 11 scope and web checker controls run in pytest; 31 self-checks pass on Python 3.8 and 3.11. |
| **High 4** Skips read as green | FIXED | `NOT_BUILT` = xfail(raises=ImportError, strict) on every AT, with `conftest.py` importing the package first; a live test without its inputs fails (test_pins changed from skip); AT-B3 runs its hermetic half first. |
| **Medium 1** D6 control proven for one gate | FIXED | The always-PASS mutant runs over every gate in `GATES`. |
| **Medium 2** F2 detector never shown to fire | FIXED | AT-F2's control runs D2 on a netlist with the defect (fired, form A) and one without (clean). |
| **Medium 3** `check_examples.py` failed on jsonschema 4.26 | FIXED | Matching on validator keyword and instance path; 61 ok on 4.19.1 and 4.26.0; AT-F2 matches a pointer, not wording; `requirements-dev.txt`. |
| **Medium 4** The three test files assumed different interfaces | FIXED | interfaces section 17: one set of config keys, module paths, env vars and live doctrine. `[service] data_dir`; hosts carry `ssh` plus an optional `git` fetch base (the two files meant different things, both kept). |
| **Medium 5** Three vacuous branches | FIXED | A4's control requires one `no-bundle` run with a null pipeline and 401 in the reason; B4's oracle must hold pins; B1 requires the real registry. |
| **Low 1** Always-true or silently skipping assertions | FIXED | D2: every gate that ran must be `fired`; the bench must be judged; F5 censuses at whatever commit is cited and requires all 4 paths; D5 requires the new runs to exist; D11 searches the whole data dir. |
| **Low 2** H6 never checked the reported age; F3 missed free-text commits | FIXED | H6 reads the table's `age` column; F3 via `cites` and rule 9. |
| **Low 3** A6 and H3 would false-fail in Phase 4 | FIXED | A6 checks forks and silicon only; H3 reads the Phase 4 gate. |
| **Low 4** Missing conftest, dependencies, .gitignore; oracle fetch without `--no-tags` | FIXED | All added; the oracle uses `--no-tags`. |
| **Low 5** Notes on unprotected commits in a public repo | FIXED | Removed from the acceptance docs. The facts stay as test inputs; the two uncovered canonical-host rows are raised as questions 2 and 9. |

### Test writers' contract gaps (53)

pins G1 to G16, ci_errata G1 to G15 and scope_web G1 to G22. **51 are resolved**: each doc's gap table has a
resolution column, and interfaces section 17.7 indexes them. The contract defect scope_web G19 (no
`--no-tags`) is fixed. **Two remain:** ci_errata G9 is data, not a contract (compute and QuickStart are not
registered: question 8); scope_web G17 is resolved in part (a silicon erratum can no longer be `fixed`, but
deleting a silicon configuration is not detected: question 12). scope_web G13 is contracted
(`schedule.tick(now)`), but AT-H6 still waits 16 minutes on purpose, because it tests the live schedule.

## 5. Questions for David: decided 2026-10-01

Every question was answered with its recommendation (a):

| Q | Decision | Effect on the contracts |
|---|---|---|
| Q1 | Waivers for frozen configurations | Added before approval: section 5a |
| Q2 | GitHub canonical for ahb_qspi and asic-flow | None (the catalogue example already says so); David pushes `e908ac8` (5b) |
| Q3 | compat-ci's job token reads the registry (allowlist) | None; registry setting when both projects exist (5b) |
| Q4 | Scanner rules stay private, pinned in compat-ci | None |
| Q5 | T0 judges from inside the lab in v1, and says so | None; an outside probe only if an outsider failure appears (Phase 2) |
| Q6 | nanoSoC repos only; other families `ignored` with a reason | None (catalogue example note updated) |
| Q7 | Dashboard banners in v1, email digest in Phase 2 | interfaces section 8 wording |
| Q8 | Register `quickstart-main` now, compute after DEC3 | None; AT-F1 stays red until both exist |
| Q9 | B5 judges both hosts (13 rows) | None; the B5 CR candidate is withdrawn (`docs/CHANGES.md`); David tags `ecf76aa` on GitHub (5b) |
| Q10 | Service account: Maintainer on compat-ci only, `read_api` token | None; confirm both reads when compat-ci is created |
| Q11 | Real values in the public repo: accept as is | None |
| Q12 | Code-owner approval on `eligibility.yaml` | README section 3 wording; registry setting (5b) |

### 5a. Q1: frozen-configuration T0 waivers (added 2026-10-01)

- **Rule (README section 6):** a frozen configuration may list `t0_waivers`, each naming one item (gate, tree
  path, item result) with a reason, approver and date. A FAIL or UNVERIFIED gate becomes **WAIVED** only when its
  control fired and every non-pass item is covered. A `not-reached` item rides on a covered item above it; a
  `fail` below a waived pin does not. The raw verdict is stored beside it. The run ladder gains WAIVED between
  INFRA and PASS. A waiver that covers nothing raises `waiver_unused`. The runner never sees waivers.
- **Schemas:** `eligibility` (`t0_waivers`, frozen only, never on the job or registry gates or a control);
  `result-bundle` (`gate_item`, the items line, now formal; gate verdict `WAIVED`); `observation` (gate
  `raw_verdict` and `waived`; run verdict `WAIVED`; alert `waiver_unused`).
- **Examples:** rc4's 8 waivers; its non-pass items for the four gates (excerpts); a WAIVED rc4 run; 4 invalid
  examples (waiver on an unfrozen configuration, waiver on `t0.runner.hardened`, WAIVED gate whose control did
  not fire, WAIVED run hiding a FAIL gate).
- **Tests:** AT-D2 checks the live waived roll-up item by item against an oracle; AT-D6 and its control apply
  them and prove nothing new can hide; AT-A4 accepts FAIL or WAIVED for rc4. New self-check
  `test_selfcheck_D6_waiver_oracle`, shown to fail under three mutants of the oracle.
- **rc4 outcome:** its four non-pass gates and the run roll up WAIVED, and a new failure is FAIL. After the Q2
  push the ahb_qspi waiver raises `waiver_unused` and is removed from the registry by merge request.

### 5b. Writes these answers need (David, PR8)

| When | Write | How |
|---|---|---|
| Now | GitHub AHB-QSPI tag `keep/eth-rc4/ahb_qspi` → `e908ac8`, which uploads the commit and its history (Q2) | `gate2-github-keep-tags.sh` in the design folder |
| Now | GitHub nanoSoC-Arch-Tech tag `keep/eth-rc4/nanosoc_arch_tech` → `ecf76aa` (Q9) | same script |
| When compat-ci and the registry exist | Registry project: add compat-ci to the job-token allowlist (Q3) | GitLab settings |
| When compat-ci exists | Service account: Maintainer on compat-ci only, `read_api` token (Q10) | GitLab settings |
| When the registry exists | Code-owner approval on `eligibility.yaml` (Q12) | `CODEOWNERS` plus a protected-branch rule |

### The questions as asked (2026-09-30)

**Blocks approval** (the answer changes a contract or a golden):

**Q1.** **Frozen rc4's T0 will be red every night.** Its ssh URLs, the private toolkit and (Q2) ahb_qspi cannot change. A permanent red hides any new red.
- *Recommendation:* yes, add waivers now: `t0_waivers: [{gate, tree_path, reason, approved_by}]` per configuration in `eligibility.yaml`. A waived item shows amber and leaves the verdict alone; any new failure is still FAIL. About 2 hours of contract work before approval.

**Q2.** **Canonical host of ahb_qspi and asic-flow.** Both are diverged and public, so DEC6 makes GitHub canonical for both. rc4 pins ahb_qspi `e908ac8`, which exists only on GitLab.
- *Recommendation:* GitHub for both, as the catalogue example already has it. You then push `e908ac8` with its history to GitHub AHB-QSPI as tag `keep/eth-rc4/ahb_qspi` (your write, PR8). After that rc4's reachability gate has no FAIL; only the private toolkit stays UNVERIFIED.

**Q3.** **Registry read access from compat-ci, and private canonical repos.** The `@registry` job must read the private registry. Citations into a private canonical repo (the ASIC toolkit) can only be UNVERIFIED from an anonymous job.
- *Recommendation:* let compat-ci's job token read the registry, using GitLab's job-token allowlist on the registry project (your settings write, PR8). The job uses it once, then scrubs it. Private citations stay UNVERIFIED, which already blocks PASS. Do not give the job wider credentials.

**Q9.** **Which host B5 judges.** The contract judges both the pinned host and the canonical host. The 11 done-when commits are covered on their pinned host. On GitHub, `ecf76aa` is branch-only and `e908ac8` is missing.
- *Recommendation:* keep both hosts. Close the two rows with a GitHub keep tag for `ecf76aa` (your write, PR8) and Q2's push. The alternatives each have a cost: the pinned host alone hides two real risks; the canonical host alone needs a CR on B5's "11 commits" (see `docs/CHANGES.md`).

**Needed before the Phase 1 build ends:**

**Q4.** **Where the scanner's rule set lives.** The toolkit scanner is in a private repo, and its rules describe foundry and Arm file shapes.
- *Recommendation:* keep the rules out of this public repo. compat-ci (private) carries a pinned copy and passes its path to `nanosoc-rm scan`. The public repo carries only the engine and the invented `arm.banner` specimen.

**Q5.** **T0's network vantage.** The runner sits inside the lab network, so it judges "fetchable without credentials" from the inside.
- *Recommendation:* accept this for v1 and say so on the run page. Add an outside probe in Phase 2 if a real outsider failure appears.

**Q6.** **Which org repos A1 tracks.** The org and the group also hold milliSoC, MegaSoC and megaSoC-Linux repos, plus housekeeping repos.
- *Recommendation:* list them as `ignored` with a reason in v1, because A1 covers nanoSoC-related repos only. Revisit if those families adopt the tool.

**Q7.** **Alert delivery.** Rewrite, token-expiry, runner-drift and mirror-failure alerts are shown only on the dashboard.
- *Recommendation:* dashboard banners only in v1; add a daily email digest in Phase 2.

**Q8.** **F1 needs the compute and QuickStart configurations.** Compute waits on DEC3. QuickStart main pins nanosoc_gen `c2ca528`, which lacks `036d381`.
- *Recommendation:* register `quickstart-main` now, and compute's taped-out commit once you answer DEC3. AT-F1 fails until both exist, which is correct.

**Q10.** **The service account's role on compat-ci.** Reading pipeline variables (verify check 2) and runner settings (D10) may need more than Reporter, and a new project may refuse pipeline variables from the trigger's owner.
- *Recommendation:* give the service account Maintainer on compat-ci only, with a `read_api`-scoped token (the token scope keeps it read-only, so A2 holds). Confirm both reads when compat-ci is created.

**Q11.** **Real values in this public repo.** Kept: the GDS and netlist md5 and size, the rc4 archive md5, private repo names (public `.gitmodules` files already show them). Replaced with placeholders: store paths, output file names, a private repo's branch name, logins, the lab host name.
- *Recommendation:* accept as is.

**Q12.** **Deleting a silicon configuration from `eligibility.yaml`** would silently drop its silicon-protection rows.
- *Recommendation:* add a code-owner approval rule for `eligibility.yaml` on the registry (your settings write, PR8). No tool rule in v1.

## 6. What gate 2 leaves to later phases

- **Later-phase Musts get their acceptance tests at their own gate 2**, after that phase's entry gate in `docs/phase-gates.yaml` is met (A7):
  - Phase 2: D3, D4, D7, D8, D9;
  - Phase 3: C1 to C8, C10 to C12, G1, G5, G6;
  - Phase 4: E1 to E6;
  - Phase 5: F4, G2, G3, G7.
- **All-phase Musts** (A6, A7, D1, H3) are tested now in their v1 form. Their later-phase parts are tested at later gates: D1's T1 to T6 commands, and H3's bump-writer and bot-tag scopes.
- **Formats fixed now for later phases** (interfaces section 15): release-lock (Phase 5), component-manifest (Phase 3; T0 validates it from v1), D4 capture fields, `cpu_seconds`, the FLAKY/XFAIL/XPASS verdicts, `auto_merge`, and the POL-1 gate (with G2).
- **Should requirements** have no acceptance test at any gate 2 (PR3 asks for one per Must). The v1 Shoulds are still in the contracts: B7 (`split`, `effective`), B9 (URL normalisation, `unknown_url` alert), F7 (defect `issues`), H4 and H5 (stack, one systemd unit), PR9 (README section 8).
- **Done-whens that need a person**, by design:
  - B3's hand check (`HAND_CHECK` in `test_pins.py`);
  - B8 on a hosted scratch repo (PR8);
  - H1's live demonstration and H2's GitHub consent page, both at the gate-3 demo;
  - every live + Run test, which makes the one write (PR8).
- **Deliberately not fixed:** page markup beyond the listed hooks, table layouts other than `mirror status`'s `age` column, internal module structure below section 17.2, and alert delivery (question 7).
