# Acceptance tests: CI, T0 and errata

Gate 2 (PR3). One acceptance test (AT) per v1 Must requirement in this area: **D1, D2, D5, D6, D10, D11,
F1, F2, F3, F5**. Each AT has a negative control (PR6).

- **Requirements:** [`REQUIREMENTS.md`](../REQUIREMENTS.md) v1.0, frozen 2026-09-30.
- **Contracts exercised:** [`contracts/README.md`](../contracts/README.md), [`contracts/interfaces.md`](../contracts/interfaces.md), [`schemas/`](../../schemas/).
- **Test code:** [`tests/acceptance/test_ci_errata.py`](../../tests/acceptance/test_ci_errata.py). Every test is skipped (`not implemented: gate 2`) until its module exists.
- **Status:** draft for David's gate-2 review. Written 2026-09-30.

## How to read this page

1. **Two tests per AT.**
   - `test_AT_<ID>` checks the requirement's done-when on real data.
   - `test_AT_<ID>_negative_control` feeds a deliberately broken input or state. The check under test must report FAIL, reject, refuse or UNVERIFIED on it. If it reports PASS, the AT does not count (PR6).
2. **Controlled pairs.** For D5, D11 and F3 the done-when is itself a rejection. There the main test feeds the genuine input and must pass. The negative control feeds the same input with exactly one thing changed and must be rejected.
   - A checker that rejects everything fails the main test.
   - A checker that accepts everything fails the control.
   - Because only one thing differs, the rejection can only come from the check that reads it.
3. **Where each test runs.**
   - `hermetic`: needs only this repo and `tmp_path`. Any builder can run it.
   - `live`: needs the lab network (anonymous git to both hosts), the service's data or the registry checkout. The integrator runs these end to end on the real repos (PR5). Marked `@pytest.mark.live`.
4. **Real or synthetic.** Fixture values marked OBSERVED were read on 2026-09-30 with read-only git and API calls. A SYNTHETIC input is used only where real data cannot produce the case.
5. **PROPOSED interfaces.** A few tests need an interface the contracts do not define yet. Each such name is marked `PROPOSED (Gn)` in the test and listed under [Contract gaps](#contract-gaps).
6. **Hostnames.** The lab GitLab host is written `<gitlab-host>`, as in the contracts. The real name lives in the private service config.

## Index

| AT | Requirement | What the test checks | Runs (test / control) | Negative control |
|---|---|---|---|---|
| [AT-D1](#at-d1) | D1 test tiers | All 7 tiers have a budget; every configuration has a T0 command; every accepted nightly T0 finished inside its 2-minute budget | live / hermetic | a 181 s T0 bundle is flagged over budget; a tier row with no budget fails the table check |
| [AT-D2](#at-d2) | D2 nightly T0 | Every registered configuration and the registry have tonight's accepted T0 report; rc4 goes red on exactly its 3 known real gates | live / hermetic | the CI API refuses eth-main's nightly trigger: eth-main shows a missed night, never last night's result |
| [AT-D5](#at-d5) | D5 pull and verify | The genuine rc4 bundle is accepted; 13 single-field tampers are each rejected by the check that owns the field; no route accepts a pushed result | hermetic / hermetic | the same bundle with `captured.superproject_commit` = rc4's real parent `3ab16f5` is rejected |
| [AT-D6](#at-d6) | D6 negative controls | Every declared gate is reported with its control and whether it fired; a PASS without a fired control never shows PASS | hermetic / hermetic | a mutant `t0.urls.no-ssh-public` that always passes: its control does not fire and the gate shows UNVERIFIED |
| [AT-D10](#at-d10) | D10 hardened runner | The real T0 job passes `t0.runner.hardened`; the runner's id and settings are recorded and match the expected ones | live / hermetic | synthetic job environments (unprotected ref, dirty HOME, dirty TMPDIR, personal user) each FAIL the gate |
| [AT-D11](#at-d11) | D11 vendor scan | The genuine rc4 bundle scans clean, is accepted and stored; the `arm.banner` rule fires on one real vendor file | live (realism step) / hermetic | the same bundle with a planted Arm banner in `logs/t0.log` is refused and never stored |
| [AT-F1](#at-f1) | F1 defects and errata | SL-DEF-0001 holds the real cpu_ss_hsel facts; one erratum each for eth, compute and QuickStart; the eth forms agree with containment | live / hermetic | the rc4 erratum copied to non-silicon eth-main with `status: permanent` is refused |
| [AT-F2](#at-f2) | F2 exposure | Every erratum shows level, method and evidence; ancestry shows amber; a detector level carries a re-checkable run | live / hermetic | `erratum.ancestry-claims-exposed.yaml` is rejected; a detector id not in the defect is refused |
| [AT-F3](#at-f3) | F3 cited commits exist | With every cited commit pushed, `t0.registry.refs-resolve` passes and names each commit it proved | live / live | the real SL-DEF-0001, whose bench `6aafc94` is unpushed, FAILs; so do a copy-host-only and a local-only commit |
| [AT-F5](#at-f5) | F5 import registers | All 4 registers are imported; each document's items are censused against the ledger; nothing is pending | live / hermetic | the example ledger (all pending) and a ledger missing one censused item are both reported not done |

## Real facts the fixtures use

OBSERVED 2026-09-30, read-only (`gh api`, `glab api`, `git` on local clones).

| Fact | Value | Used by |
|---|---|---|
| rc4 superproject | SoC-Labs/NanoSoC-Ethernet-Chiplet `ab73e40924317e45e7b3b941f52a7ed35f20832e`, tags `v1.0.0`, `silicon/eth-chiplet-r0p0`, `candidate/rc4-20260829` | all |
| rc4's first parent | `3ab16f500dc43cd9ee436ef3a5dc753c1d5a70cd` | D5 |
| rc4 tree | 44 pins, depth ≤ 5; states 32 ON_DEFAULT, 11 PROTECTED_BY_TAG, 1 TAGGED | D2, D6 |
| rc4 ssh pins | `tidelink` (5 pins below it) and `ASIC/asic-toolkit` | D2, D6 |
| ASIC-Toolkit visibility | private on GitHub: not anonymously fetchable over any URL | D2 |
| TideLink host drift | rc4's `5e8bdb5` is exactly `v0.90` on GitHub (canonical), MISSING on the GitLab copy; copy relation AHEAD 298 | D2 |
| ahb_qspi | rc4's `e908ac843845d5b293616265f93a802804555540` exists only on the GitLab copy | D2, F3 |
| keep tags | 11 `keep/eth-rc4/*` tags cover all 11 branch-only rc4 commits | D2 |
| eth main | `8e6d0434fc8331191607ad5b2af73646e995c892` (2026-08-07) still pins tidelink by ssh | D2 |
| SL-DEF-0001 commits | origin arch_tech `49a6c25` (GitHub main) and nanosoc_gen `b0a6069` (main); fixes `036d381` (form A) and `adbb9ab` (form B), both on nanosoc_gen main | F1, F3 |
| rc4 generators | nanosoc_gen `d04de2e` and `1d7772d`; neither contains `036d381` or `adbb9ab` (`contains` golden: `no` on gitlab) | F1 |
| SL-DEF-0001 bench | eth `6aafc943f2750be92e80b31be99f0cd60f8927e4`: GitHub answers 422, exists only locally | F3 |
| erratum workaround | multicore `7a87e20051d6335abd3de690cde332bf3bd935a9`, reachable only from branch `feat/soc-completeness-gate` | F3 |
| import source commit | eth `66a2f698d6650e6e4e5beba1434b22231846696a`: not on main, an ancestor of `ab73e40`, so reachable from tag `v1.0.0` | F3, F5 |
| `11-known-issues.md` @ `66a2f698` | 9 items, `a)` to `i)` | F5 |
| `16-open-defects.md` @ `66a2f698` | 6 numbered items, plus 2 unnumbered paragraphs under "Two things found in passing", plus one more "found in passing" defect in the 2026-08-12 addendum: 9 candidates | F5 |
| `67-rc4-issue-register.md` | untracked in the eth repo: no commit to cite yet | F5 |
| QuickStart `docs/DEFECT-cpu-ss-hsel.md` | committed only locally, `1f2f06e4303de5eaa7316caf965ec0b070a89028` on branch `record/qs-platform`; GitHub answers 422 | F3, F5 |
| rc4 generated RTL | P1 archive `build_soc.tar.gz`, md5 `80eb7ce7e81fee653b52c6821cea3942`, 332 files | F2 |
| compute superproject | SoC-Labs/NanoSoC-Compute-Chiplet is not anonymously readable; its taped-out commit waits on DEC3 | F1, D2 |
| registry and CI project | `soclabs/nanosoc-registry` and `soclabs/nanosoc-compat-ci` are not found by the read API (not created yet, or not visible) | D2, D10, F3 |

---

<a id="at-d1"></a>
## AT-D1 · Every tier has a command and a budget

**Requirement D1 (Must, all phases).** "Use these test tiers: T0 resolve (under 2 min, no licence); T1 static and unit (~10 min); T2 smoke simulation (5–15 min per SoC, 1 VCS seat); T3 regression (1–3 h); T4 FPGA build; T5 on hardware (manual); T6 ASIC flow (~6.5 h, manual)." Done when: each tier has a documented command per SoC and a runtime budget.

**Given**
- the registered configurations in `eligibility.yaml`;
- the tier table;
- the accepted scheduled T0 runs of the last 7 nights.

**When**
1. `nanosoc-rm tiers CONFIG --json` runs for each configuration. PROPOSED (G1).
2. Each run's bundle start and finish times are read.

**Then**
1. The table has exactly the tiers `t0` to `t6`, the result-bundle `tier` enum.
2. Every tier has a budget in seconds, or `null` with a written reason:

   | Tier | Budget | Source |
   |---|---|---|
   | t0 | 120 | D1 "under 2 min" |
   | t1 | 600 | D1 "~10 min" |
   | t2 | 900 per SoC | upper end of D1 "5–15 min" |
   | t3 | 10800 | upper end of D1 "1–3 h" |
   | t4 | null + reason | D1 gives none (see G1) |
   | t5 | null + reason, manual | D1 gives none (see G1) |
   | t6 | 23400, manual | D1 "~6.5 h" |
3. For every configuration, `t0` has a command. In v1 that is `nanosoc-rm t0 trigger <config>`, which runs compat-ci job `t0` → `nanosoc-rm t0 run --out rm-bundle`. `t1` to `t6` show their phase and no command yet.
4. Every accepted scheduled T0 run satisfies `finished_at − started_at ≤ 120 s`. The times come from the D5-verified bundle, not the job's exit.

**Fixture**
- REAL: configurations eth-rc4, eth-main and multicore-master, from the eligibility example.
- ILLUSTRATIVE: the bundle example runs 02:03:30 → 02:05:02, which is 92 s. No real T0 has run yet.

**Exercises:** the PROPOSED tier table and budget check (G1); result-bundle `started_at` / `finished_at`; the interfaces §9 job contract.

**Negative control**
1. The example bundle with `finished_at` moved to 02:06:31 (181 s): the budget check reports over budget, not OK.
2. A tier table whose `t2` row has no budget and no reason: table validation fails, naming `t2`.

**Cannot pass vacuously because**
- The table must hold exactly 7 tiers, so an empty table fails.
- Durations come from verified bundles, and the control shows an over-budget run is flagged.

**Open**
- The job contract's `timeout: 15m`, and interfaces §16's "15-minute budget", disagree with D1's 2 minutes. Which one is the budget, and whether over budget is WARN or INFRA, is gap G1.
- T4 and T5 have no budget in D1.

---

<a id="at-d2"></a>
## AT-D2 · T0 runs nightly on every configuration

**Requirement D2 (Must, v1).** "Run T0 nightly on every registered configuration. T0 checks that every pin can be fetched without credentials and is reachable from a branch or tag on its canonical host; that no public component uses SSH URLs; that manifests are valid; that an upgrade-path clone works; that no Arm IP has leaked; and that every commit cited in a defect record exists." Done when: a nightly T0 report exists for every configuration, with a negative control.

**Given**
- The live registry's configurations. At gate 2 these are eth-rc4, eth-main and multicore-master.
- The registry job `@registry`.
- The service has run its nightly schedule once.

**When:** `nanosoc-rm t0 runs --json` and `GET /api/v1/runs` are read after the night.

**Then**
1. For every configuration and for `@registry`, there is exactly one run with `trigger: schedule` inside tonight's window, and it is `accepted`.
2. Its gates cover every D2 check:
   - a configuration run carries the 8 gates of interfaces §9;
   - `@registry` carries `t0.registry.schema` and `t0.registry.refs-resolve`;
   - every gate carries a `control` value.
3. eth-rc4 matches the golden `result-bundle.verdicts.example.tsv`:

   | Gate | Verdict | Detail |
   |---|---|---|
   | `t0.infra.probe` | PASS | |
   | `t0.pins.fetchable` | FAIL | names `tidelink` and `ASIC/asic-toolkit`; 37 of 44 pins fetched |
   | `t0.pins.reachable` | FAIL | names ahb_qspi `e908ac8`; does not name `tidelink` |
   | `t0.urls.no-ssh-public` | FAIL | names `tidelink` and `ASIC/asic-toolkit` |
   | `t0.manifest.valid` | NOT-RUN | no manifest anywhere |
   | `t0.clone.upgrade-path` | NOT-RUN | no `upgrade_from` |
   | `t0.vendor.no-arm-ip` | UNVERIFIED | 7 trees not fetched |

   The run verdict is FAIL.
4. `@registry`: `t0.registry.refs-resolve` is FAIL and names `6aafc94`, until that commit is pushed (AT-F3).
5. The dashboard lists every configuration with tonight's verdict and a data age under 24 h.

**Fixture:** REAL.
- rc4's 44-pin tree and its two ssh URLs.
- ASIC-Toolkit is private, so that pin fails fetch whatever its URL.
- **TideLink host drift:** `5e8bdb5` is `v0.90` on GitHub, which is canonical, and missing on the GitLab copy. So `tidelink` must be in the fetchable failures (ssh URL) and absent from the reachable failures (the canonical host has it).
- The ahb_qspi row depends on question 2 in the gate-2 summary (canonical host). The test reads the expectation from the catalogue's canonical host for ahb_qspi.

**Exercises:** interfaces §9 (gates and job contract), §10 (trigger, fetch, verify), §13 (dashboard and `/api/v1/runs`); observation `t0_result`.

**Negative control**
- **Setup:** the fake CI API answers HTTP 500 to tonight's `POST projects/:id/trigger/pipeline` for eth-main, and succeeds for the others.
- **Must happen:**
  - eth-main gets a `t0_result` for tonight with `acceptance: no-bundle`, a verdict other than PASS, and a reason naming the trigger failure;
  - the nightly coverage check exits 1 and names eth-main (PROPOSED, G2);
  - the dashboard does not show eth-main's previous night as current.
- **Runs:** hermetic. Fake CI client (G4), fake clock.

**Cannot pass vacuously because**
- rc4 must go red on exactly three gates, for known real reasons.
- `tidelink` must appear in one gate's list and not the other's. A tool that marks everything red fails, and so does one that judges the wrong host.
- The run count must equal the number of configurations plus one, so an empty night fails.

**Known consequences (not failures of the test)**
- eth-main also pins tidelink by ssh, so its `t0.urls.no-ssh-public` is red.
- If eth-main keeps `upgrade_from: v1.0.0`, its `t0.clone.upgrade-path` is red: an anonymous recursive clone of rc4 cannot fetch rc4's ssh pins.
- Frozen rc4 stays red for ever unless waived (question 1).

---

<a id="at-d5"></a>
## AT-D5 · Results are pulled from CI and checked

**Requirement D5 (Must, v1).** "The tool fetches results from the CI system and checks the project, commit and settings they came from. It never accepts results pushed to it." Done when: a result with a mismatched commit is rejected.

**Given**
- A stored run for eth-rc4: nonce `9c1e4f7a…`, and the exact `RM_REQUEST` bytes built from the example provenance's `request`.
- CI API records (pipeline, job, variables) in GitLab's shape, consistent with the example `provenance.json`.
- The bundle `rm-bundle/`: the example `provenance.json` and `verdicts.tsv`, plus a runner log.

**When:** `verify(run, pipeline, job, variables, bundle)` (interfaces §10).

**Then**
1. The genuine bundle is `accepted`, and its roll-up verdict is FAIL (rc4's red gates).
2. Each single tamper below is `rejected`, and the reason names the check that owns the field:

   | # | Tamper | Check |
   |---|---|---|
   | 1 | pipeline from another project | 1 |
   | 2 | ref `feat/x`, not in `allowed_refs` | 1 |
   | 3 | job name `t0-debug` | 1 |
   | 4 | pipeline id differs from the run's | 1 |
   | 5 | the API's `RM_REQUEST` differs by one commit | 2 |
   | 6 | `provenance.json` has no `run_nonce` (as in the real invalid example) | 3 |
   | 7 | `run_nonce` of another run | 4 |
   | 8 | provenance `request.upgrade_from` changed | 4 |
   | 9 | `ci.harness_sha` differs from the API's pipeline SHA | 4 |
   | 10 | `verdicts.tsv` with 4 columns (real invalid example) | 6 |
   | 11 | a row with a foreign nonce (real invalid example) | 6 |
   | 12 | a row timestamped 02:01:00, before the job started | 6 |
   | 13 | an empty `verdicts.tsv` | 6 |
3. **No push path.**
   - The app's non-GET routes are exactly `POST /configurations/{id}/runs` and `POST /api/v1/configurations/{id}/runs`.
   - POSTing the bundle's `provenance.json` as the body of either creates only a new pending run with a fresh nonce. It never creates an accepted result, and the posted nonce appears nowhere.

**Fixture**
- REAL: the rc4 commit, request and `canonical_urls`; the two invalid `verdicts.tsv` examples.
- SYNTHETIC: pipeline and job ids, the harness SHA (none exist yet), and the CI project's numeric id.

**Exercises:** interfaces §10 Verify, checks 1–7; result-bundle schema; README §6 (rejected → UNVERIFIED); §13 route table.

**Negative control (the done-when)**
- **Input:** the genuine bundle with only `captured.superproject_commit` = `3ab16f500dc43cd9ee436ef3a5dc753c1d5a70cd`, rc4's real first parent. This is what a job reports when it checked out the wrong commit.
- **Must happen:**
  - `rejected`, at check 5;
  - the reason names `superproject_commit` and both SHAs;
  - the stored `t0_result` has `acceptance: rejected`, `verdict: UNVERIFIED` and `bundle: null`.
- **Runs:** hermetic.

**Cannot pass vacuously because**
- It is a controlled pair: the genuine bundle must be accepted, and the control differs from it in one field.
- A verifier that rejects everything fails part 1; one that skips the commit check fails the control.

---

<a id="at-d6"></a>
## AT-D6 · Every gate carries a negative control

**Requirement D6 (Must, v1).** "Every gate runs a negative control in the same run. If the control does not fire, the gate is UNVERIFIED, not PASS." Done when: each gate's report shows its control and whether it fired.

**Given**
- The golden rc4 `verdicts.tsv`.
- The gate table in `nanosoc_rm/t0/gates.py`.
- rc4's real `.gitmodules` at `ab73e40` (5 entries, 2 ssh URLs).

**When**
1. The rows are rolled up (README §6).
2. `GET /runs/{nonce}` is rendered.
3. `nanosoc-rm t0 runs eth-rc4 --json` is read.
4. `t0.urls.no-ssh-public` runs with its control on the real `.gitmodules`.

**Then**
1. Every gate in the table has a control, for both configuration and `@registry` scope.
2. Every declared gate appears exactly once in the roll-up, with a verdict and a control in `fired` / `not-fired` / `missing`.
3. Every PASS gate has `control: fired`.
4. The run page shows a control column for every gate.
5. On the real `.gitmodules`, `t0.urls.no-ssh-public` is FAIL, names `tidelink` and `ASIC/asic-toolkit`, and its `.negctl` row is PASS (fired), carrying the same nonce.
6. The rc4 roll-up is:

   | Gate | Verdict | Control |
   |---|---|---|
   | `t0.infra.probe` | PASS | fired |
   | `t0.pins.fetchable` | FAIL | fired |
   | `t0.pins.reachable` | FAIL | fired |
   | `t0.urls.no-ssh-public` | FAIL | fired |
   | `t0.manifest.valid` | NOT-RUN | |
   | `t0.clone.upgrade-path` | NOT-RUN | |
   | `t0.vendor.no-arm-ip` | UNVERIFIED | fired |
   | `t0.runner.hardened` | PASS | fired |

   Run verdict FAIL, 2 not run.

**Fixture**
- REAL: the rc4 rows and `.gitmodules`.
- The example TSV has **no `t0.runner.hardened` rows** (gap G6). As written it rolls up UNVERIFIED (`missing`). The test adds that gate's two rows before rolling up, and records the example defect.

**Exercises:** README §6 verdict table; observation `t0_result.gates[].control`; interfaces §9 gate table; §13 `/runs/{nonce}`.

**Negative control**
1. **Mutant gate.** `t0.urls.no-ssh-public`'s check is replaced by one that always returns PASS.
   - It runs with its own control, in one run, on the real `.gitmodules`.
   - The control must NOT fire: the `.negctl` row is FAIL.
   - The service's verdict for the gate is UNVERIFIED, never PASS.
   - This is D6's purpose: a gate that cannot fail is caught.
2. **Roll-up rules**, each on a copy of the golden with one change:
   - `t0.infra.probe.negctl` deleted → UNVERIFIED, `missing`;
   - that row flipped to FAIL → UNVERIFIED, `not-fired`;
   - a second `t0.infra.probe` row → UNVERIFIED.
- **Runs:** hermetic.

**Cannot pass vacuously because**
- A control that writes PASS without running the gate's code on a broken input would "fire" for the always-pass mutant too. The mutant's gate would then show PASS, which fails the control test.
- The control must also be the gate's own code, run in the same run (same nonce).

---

<a id="at-d10"></a>
## AT-D10 · T0 runs on the hardened runner

**Requirement D10 (Must, v1).** "Run CI jobs on a dedicated, hardened runner: a service account, protected branches only, and a fresh home and temp directory per job. Not a shared or untagged lab runner, and not a shared login server. Budget in CPU-hours, not licence seats." Done when: the runner's settings are recorded and checked.

**Given**
- The compat-ci `t0` job on the dedicated runner.
- The expected settings in the private service config: service account, allowed runner ids, runner tag. PROPOSED (G7).

**When:** a nightly T0 runs and the service fetches it.

**Then**
1. The bundle has `t0.runner.hardened` PASS and `t0.runner.hardened.negctl` PASS, with the run's nonce. The gate detail records:
   - `CI_COMMIT_REF_PROTECTED=true`;
   - `HOME` and `TMPDIR` empty at job start;
   - the job user is the service account.
2. The CI API's `job.runner.id` is in `ci.allowed_runner_ids`. Verify rejects the bundle otherwise. PROPOSED (G7).
3. **Settings recorded:** a `runner_settings` observation from `GET runners/:id`, observed no earlier than tonight's newest run. PROPOSED (G7). It shows:
   - `run_untagged: false`;
   - `access_level: ref_protected`;
   - `locked: true`;
   - `is_shared: false`;
   - the hardened tag in `tag_list`.
4. **Settings checked:** each recorded setting equals its expected value. A drift raises an alert and turns the dashboard runner status red.

**Fixture**
- LIVE: the real runner once it exists. Neither the CI project nor the runner exists yet.
- The expected values are the proposal §4.3 settings above.

**Exercises:** interfaces §9 gate `t0.runner.hardened` and job contract; §10 verify check 1; §13 runner status (H1).

**Negative control (hermetic)**
- **Input:** the gate's check run on synthetic job environments. Each differs from the good one in exactly one variable:
  1. `CI_COMMIT_REF_PROTECTED=false` (the gate's contracted control);
  2. `HOME` holding a file left by a previous job;
  3. `TMPDIR` not empty;
  4. job user = a personal login.
- **Must happen:**
  - each environment gives FAIL, and the detail names the variable;
  - PROPOSED (G7): the genuine bundle, with the job API reporting a runner id outside the allowlist, is `rejected` at check 1.

**Cannot pass vacuously because**
- The same gate code must PASS in the real job and FAIL on each one-variable change.
- The runner-settings check compares against expected values, not against themselves.

**Not testable in v1:** "Budget in CPU-hours, not licence seats". No contract records CPU time or a CPU budget; see [Untestable](#untestable-or-only-partly-testable).

---

<a id="at-d11"></a>
## AT-D11 · Bundles are scanned for vendor content

**Requirement D11 (Must, v1).** "Scan every result bundle and release file for vendor content before upload." Done when: a bundle containing a planted Arm banner is refused.

**Given:** the genuine rc4 bundle from AT-D5 (`provenance.json`, `verdicts.tsv`, `logs/t0.log`), which passes checks 1–6.

**When:** verify runs check 7, `scan_dir` (interfaces §11).

**Then**
1. The scan is `clean`: more than 0 files scanned, and every rule, including `arm.banner`, armed on its specimen first.
2. The run is `accepted`, and the bundle is written to the `ci` store (`bundle` is a store_ref, not null).
3. **Realism, live, lab only.** Scanning one real vendor source file reports `arm.banner` at line ≤ 30.
   - The file is named by `RM_ARM_REFERENCE_FILE`. PROPOSED `scan_paths` (G8) reads it in place, read-only. No copy is made.
   - The finding carries rule, file and line only; the test prints no content.
   - This ties the invented specimen to a real banner.

**Fixture**
- REAL: the rc4 provenance and rows.
- SYNTHETIC: the log text.
- SPECIMEN: the scanner's own `arm.banner` arming specimen, PROPOSED (G8). No vendor text is written into this repo.

**Exercises:** interfaces §11 scan; §10 verify check 7; observation `t0_result` (refused → UNVERIFIED, `bundle: null`).

**Negative control (the done-when)**
- **Input:** the same bundle, with the specimen appended to `logs/t0.log` at line 3. `provenance.scan.verdict` still says `clean`, as a job that missed it would claim.
- **Must happen:**
  - `refused`, at check 7;
  - the reason names `arm.banner` and `logs/t0.log:3`, and never the matched text;
  - the quarantine directory is emptied;
  - nothing reaches the store;
  - `t0_result`: `acceptance: refused`, `verdict: UNVERIFIED`, `bundle: null`.
- **Second input:** an empty `rm-bundle/`. The scan is `unverified`, because the census is 0, and the run is refused, not accepted.
- **Third input, the job side ("before upload"):** the planted bundle goes through the runner's last step before the job ends. PROPOSED `finalise_bundle` (G8).
  - Must happen: `logs/t0.log` is gone from `rm-bundle/`, and `provenance.scan` says `finding` and lists `arm.banner` at `logs/t0.log:3`.
  - Why: the job contract uploads `rm-bundle/` with `when: always`. Without this step, the planted text reaches CI artifacts before the service can refuse it.
- **Runs:** hermetic.

**Cannot pass vacuously because**
- It is a controlled pair: the genuine bundle must be accepted and stored.
- The job's own scan says clean in both cases, so only the service's scan can decide.
- The realism check shows the specimen is the kind of text a real vendor file carries.

**Scope:** release files are Phase 5 (G3, G7); v1 has only bundles.

---

<a id="at-f1"></a>
## AT-F1 · Defects and errata are recorded

**Requirement F1 (Must, v1).** "Keep two kinds of record in the registry: a defect (the root cause in a component: where it came from, its forms, triggers, detectors, fixes, effects and a regression bench pinned by commit), and an erratum (its effect on one SoC release or silicon revision: forms present, exposure, category A/B/C, conditions, workaround, status and document revision)." Done when: the cpu_ss_hsel case is recorded as SL-DEF-0001 with errata for eth, compute and QuickStart.

**Given:** the live registry checkout (`NANOSOC_RM_REGISTRY`) and the service's mirrors.

**When**
1. `nanosoc-rm registry validate --json` runs.
2. `load_registry()` loads the registry.
3. `contains()` runs for `036d381` and `adbb9ab` against eth-rc4.

**Then**
1. Validate exits 0.
2. `defects/SL-DEF-0001.yaml` holds the real facts:

   | Field | Value |
   |---|---|
   | `component` | `nanosoc_gen` |
   | origins | arch_tech `49a6c25…`, nanosoc_gen `b0a6069…` |
   | form A | fixed in `036d381…` |
   | form B | fixed in `adbb9ab…` |
   | detectors | `D1` generated-rtl, source nanosoc_gen `adbb9ab…`; `D2` netlist pattern |
   | effects | `E1` to `E5` |
   | bench | eth `6aafc94…` at `verif/cpu_ss_hsel`, expect `{shipped: defect, gated: clean}` |
3. **One SL-DEF-0001 erratum per product.** The product is found through `resolve_repo()` on its real repo, so no component id is guessed:
   - eth: SoC-Labs/NanoSoC-Ethernet-Chiplet → `ETH-R0P0-ERR-001` on eth-rc4, forms `[A, B]`, `status: permanent`;
   - compute: SoC-Labs/NanoSoC-Compute-Chiplet;
   - QuickStart: SoC-Labs/nanoSoC-M0-QuickStart-SoC.

   Each erratum has every F1 field.
4. **The eth forms agree with containment.** On gitlab, eth-rc4 answers `no` for both fixes: neither nanosoc_gen pin (`d04de2e`, `1d7772d`) contains them.

**Fixture**
- REAL: SL-DEF-0001 and ETH-R0P0-ERR-001, as in the examples.
- UNKNOWN: the compute and QuickStart configurations. Compute waits on DEC3. Which QuickStart configuration counts is open (question 8). The test fails until both are registered, which is correct: F1 is not done.

**Exercises:** defect and erratum schemas; registry loader §2 and the cross-file rules in README §3; `resolve_repo`; `contains` §6.

**Negative control (hermetic)**
- **Input:** the example registry, plus the real ETH-R0P0-ERR-001 copied to `errata/eth-main/ERR-001.yaml` with `configuration: eth-main`. eth-main is not silicon, and the copy still says `status: permanent`. This is an easy copy-paste mistake.
- **Must happen:** the loader refuses with README §3 rule 6.
- **Second input:** the rc4 erratum with `forms_present: [A, C]`. The loader refuses with rule 4.

**Cannot pass vacuously because**
- Exact values from real data are asserted.
- The per-product lookup cannot pass by finding zero errata.
- The containment cross-check ties the recorded forms to an independent fact.

---

<a id="at-f2"></a>
## AT-F2 · Exposure says how it was proven

**Requirement F2 (Must, v1).** "Prove exposure by running detectors on each configuration's built output (generated RTL or netlist). Ancestry alone only shows 'probably exposed' in amber." Done when: each erratum shows how its exposure was established.

**Given**
- The live registry.
- The rc4 P1 archive of generated RTL: md5 `80eb7ce7e81fee653b52c6821cea3942`, 332 files. The test reads a local copy named by `RM_RC4_BUILD_SOC_TAR`.

**When**
1. `/errata/<configuration>` is rendered for every configuration with errata.
2. PROPOSED (G10): detector D1 of SL-DEF-0001 (hsel_lint at nanosoc_gen `adbb9ab`) runs on the unpacked rc4 archive.

**Then**
1. Every erratum has `exposure.level`, `method` and `evidence`. The page prints the method and evidence beside the level.
2. **Colours:**
   - `probably-exposed` renders amber;
   - `exposed` and `affected` render red;
   - `not-affected` renders green.

   The page carries each as a `data-exposure` attribute and a state class (G5).
3. Every `exposed`, `affected` or `not-affected` erratum has `method: detector`, a detector id that exists in its defect, and a recorded run naming the input md5 and the detector source commit (G10).
4. ETH-R0P0-ERR-001 today is `probably-exposed` by `ancestry`, shows amber, and its evidence names md5 `80eb7ce7…`.
5. The D1 run returns `fired` or `clean`, never an error, with input md5 `80eb7ce7…`.
   - If `fired`, the forms reported are a subset of D1's forms `[A, B]`.
   - Once the run is recorded, the erratum's level must agree with it.
   - Expected, not yet observed: `fired`, because rc4 carries both forms.

**Fixture**
- REAL: the rc4 erratum and the P1 archive.
- The D1 result on rc4 has never been observed.

**Exercises:** the erratum `exposure` schema and its if/then; the loader's README §3 rule 4; §13 `/errata/{configuration}`; the PROPOSED detector run (G10).

**Negative control (hermetic)**
1. The real invalid example `erratum.ancestry-claims-exposed.yaml` (`level: exposed`, `method: ancestry`, no detector) is rejected with `'detector' is a required property`.
2. The rc4 erratum with `exposure: {level: exposed, method: detector, detector: D9, …}` is refused by loader rule 4: SL-DEF-0001 has no D9.

**Cannot pass vacuously because**
- The one real erratum sits at the amber level, so the page must render amber.
- The invalid example is exactly the shortcut F2 forbids.

**Limit:** the evidence is free text today. Nothing re-checks that a claimed detector run happened (G10).

---

<a id="at-f3"></a>
## AT-F3 · Every cited commit exists on its canonical host

**Requirement F3 (Must, v1).** "Every commit a defect or erratum cites must exist on its canonical host; T0 fails otherwise." Done when: a record citing an unpushed commit fails T0.

**Given**
- Registry state R0: the real SL-DEF-0001, ETH-R0P0-ERR-001, catalogue and import-ledger examples, with one change: `regression_test: null`. The schema allows null for "no bench yet".
- Every commit R0 still cites is pushed:

  | Commit | Component | Where it is reachable |
  |---|---|---|
  | `49a6c25` | arch_tech | GitHub main |
  | `b0a6069`, `036d381`, `adbb9ab` | nanosoc_gen | GitLab main |
  | `7a87e20` | multicore | GitLab, branch-only |
  | `66a2f698` | eth | GitHub, tag-only via `v1.0.0` |

**When:** the registry T0 job (`configuration: @registry`) runs `t0.registry.refs-resolve` anonymously (interfaces §9).

**Then**
1. The gate is PASS, and its `.negctl` fired.
2. The gate's per-ref results (PROPOSED `refs.jsonl`, G11) list each distinct cited commit once, all reachable on the catalogue's canonical host. This includes the branch-only `7a87e20` and the tag-only `66a2f698`.

**Fixture:** REAL, all commits verified on their hosts on 2026-09-30.

**Exercises:** interfaces §9 registry job; the defect, erratum and import-ledger `commit_ref`s; catalogue canonical hosts.

**Negative control (the done-when, real data)**
1. **R0 with the bench restored** to `6aafc943f2750be92e80b31be99f0cd60f8927e4`, which is the real record today.
   - Must happen: the gate FAILs; the detail and `refs.jsonl` name `defects/SL-DEF-0001.yaml`, pointer `/regression_test/commit` and `6aafc94`. The run verdict is FAIL.
2. **R0 plus a defect citing ahb_qspi `e908ac8`.** The commit exists only on the GitLab copy; the canonical host is GitHub.
   - Must happen: FAIL. This proves the gate checks the canonical host, not any host.
3. **R0 plus an import source at QuickStart `1f2f06e`**, which is committed only locally.
   - Must happen: FAIL.

**Cannot pass vacuously because**
- It is a controlled pair: R0 and case 1 differ in one field.
- The positive half needs a branch-only and a tag-only commit to pass, so a checker that reads only the default branch fails the main test.
- Case 2 fails a checker that accepts a commit found on any host.

**Watch:** `7a87e20` is one branch deletion away from failing F3. Multicore's `fix/dma250-hsel-guard` was deleted between 09-28 and 09-30. Protecting `7a87e20` with a tag is a write, so it is David's call (PR8).

**Blocked:** the registry job needs read access to the private registry from compat-ci (question 3).

---

<a id="at-f5"></a>
## AT-F5 · Existing registers are imported

**Requirement F5 (Must, v1).** "Import the existing issue registers first: the eth known-issues, open-defects and rc4 registers, and the QuickStart cpu_ss_hsel write-up." Done when: each imported issue is either a record or explicitly dismissed.

**Given:** the live registry's `imports.yaml`, and each source document at the commit the ledger cites.

**When**
1. PROPOSED (G12): `nanosoc-rm registry imports --json` runs.
2. Each source document is read at its cited commit and its items are extracted by the source's declared rule.

**Then**
1. The ledger has exactly four sources:
   - eth `docs/tapeout/11-known-issues.md`;
   - eth `docs/tapeout/16-open-defects.md`;
   - eth `docs/tapeout/67-rc4-issue-register.md`;
   - QuickStart `docs/DEFECT-cpu-ss-hsel.md`.

   Each is cited at a commit that `t0.registry.refs-resolve` proves (AT-F3).
2. **Census.** For each source, the ledger's item keys equal the items the source's rule extracts at that commit:
   - `11-known-issues.md` @ `66a2f698`: 9 items, `a)` to `i)`;
   - `16-open-defects.md` @ `66a2f698`: `1` to `6`, plus the three "found in passing" defects (two in the body, one in the 2026-08-12 addendum). Each of the 9 is either listed or excluded by the rule with a reason.
3. No item is `pending`.
4. Every `record` item names a defect that exists. The QuickStart write-up maps to SL-DEF-0001.
5. Every `dismissed` item has a reason (schema).

**Fixture**
- REAL: the document commits and item counts.
- UNKNOWN: the item counts of the other two documents. `67-rc4-issue-register.md` is untracked, and the QuickStart write-up is committed only locally (`1f2f06e`, not on GitHub). **F5 cannot complete until both are committed and pushed.**

**Exercises:** import-ledger schema; loader §2 (pending count); PROPOSED census rule and command (G12); F3 for the source commits.

**Negative control (hermetic)**
1. The example ledger as written, with 3 items all pending: the F5 check reports "not done: 3 pending" and exits 1.
2. A synthetic source with items `a)`, `b)` and `c)`, and a ledger listing only `a)` and `b)`, both disposed: the census reports `c)` missing and exits 1.

**Cannot pass vacuously because**
- The census reads the document, not the ledger's own list, so a ledger that leaves items out fails.
- Pending must be zero, so an unfinished import fails.

---

## Contract gaps

Needed by the tests above and not in the gate-2 contracts. Each is a proposal. None has been added to a contract.

| # | Gap | Needed by | Proposal |
|---|---|---|---|
| G1 | **No tier table.** No contract gives each tier's budget, licence, manual flag or per-SoC command. The T0 budget conflicts: D1 says under 2 min, the job contract has `timeout: 15m`, interfaces §16 says "15-minute budget". No rule says what over budget means. D1 gives T4 and T5 no budget. `t0_result` carries no start or finish time. | D1 | `nanosoc_rm/t0/tiers.py`: `TIERS`, `tier_commands(config)`, `check_budget(tier, started_at, finished_at)`, `validate_tiers()`; CLI `nanosoc-rm tiers [CONFIG] --json`; `t0_result.fact.started_at`, `.finished_at`, `.over_budget`. Keep `timeout: 15m` as the kill limit, 120 s as the budget, and make over budget a WARN shown on the dashboard. |
| G2 | **A missed night is invisible.** `trigger()` failure has no recorded outcome, and nothing defines "no report tonight". The dashboard shows only the last verdict's age. | D2 | A failed trigger writes a `t0_result` with `acceptance: no-bundle` and a reason. Add `nanosoc-rm t0 coverage [--night DATE] --json`: exit 1 when any configuration or `@registry` lacks an accepted scheduled run that night. |
| G3 | **No hermetic seam for the T0 runner.** `GIT_CONFIG_GLOBAL=/dev/null` blocks `insteadOf`, so the runner cannot be pointed at local fixture repos. Only live tests can exercise fetch and reach. | D2 (builder tests) | A test-only `--url-map FILE`, refused when `CI_JOB_ID` is set so a real job cannot fake reachability. |
| G4 | **Verify, fetch and CI client types are undefined.** `Verdict` and `RunRecord` have no fields. Nothing stores a `RunRecord`. There is no seam to inject a fake CI API. Interfaces §1 names the config sections but not their keys, nor the data directory. | D2, D5, D10, D11 | `Verdict(acceptance, check: int \| None, reason)`; `RunRecord` = the `runs` table columns, importable from `nanosoc_rm.ci.verify`; `db.put_run(run)`; a `CIClient` protocol, and a `FakeCI(pipelines, jobs, variables, artifacts, fail_trigger_for)` for tests, as fpgahub's `FakeAuthenticator`; `trigger(..., client=)` and `fetch_pending(client=)`; keys `[service] data_dir`, `[ci] project, project_id, ref, job, allowed_refs, fetch_timeout_minutes`, `[paths] quarantine`. |
| G5 | **No app factory, no test auth, no page markup.** Tests cannot find a verdict, a control or a colour on a page. No JSON route serves defects and errata. | D2, D5, D6, F2 | `nanosoc_rm.web.create_app(config_path, auth=)` and `nanosoc_rm.auth.FakeMembership(login=)`; data attributes: dashboard rows `data-configuration`, `data-verdict`, `data-age-seconds`; run page rows `data-gate`, `data-verdict`, `data-control`; errata rows `data-erratum`, `data-exposure`, `data-method`, plus class `state-amber` / `state-red` / `state-green`; `GET /api/v1/errata/{configuration}`. |
| G6 | **The gate table's shape is undefined.** Missing: the `Gate` fields, a context object, a way to run a gate with its control, and the roll-up function. `control` has no value for a NOT-RUN gate. The golden `verdicts.tsv` example has no `t0.runner.hardened` rows, so it rolls up UNVERIFIED. | D2, D6, D10, F3 | `Gate(id, scope, reqs, check, control)` as a dataclass, with `check(ctx) -> (status, detail)`; `GATES: dict[id, Gate]`; `Context.for_tests(nonce, workdir, parents=, env=, service_account=, registry_dir=)` and `ctx.items(gate)`; `run_with_control(id, ctx) -> (row, negctl_row)`; `rollup(rows, scope)`; `run_verdict(items) -> (verdict, not_run)`; add control value `not-applicable` (an enum value, so no bump); add the two rows to the example. |
| G7 | **Runner identity and settings are neither recorded nor checked.** The gate checks only the job's environment. Verify ignores `job.runner`. Nothing enforces "not a shared or untagged lab runner, not a shared login server". No field holds CPU time. | D10 | Config `ci.allowed_runner_ids` and `ci.service_account`; verify check 1 adds `job.runner.id ∈ allowed`; a new observation kind `runner_settings` (an enum value, so no bump) from `GET runners/:id`, with a `drift` list; `nanosoc-rm runner --json`, exit 1 on drift; the job learns the expected service account from a non-secret compat-ci variable (e.g. `RM_SERVICE_ACCOUNT`), since the job holds no config; provenance `cpu_seconds` (Phase 2). |
| G8 | **The scanner's test surface is undefined, and the job uploads before anyone checks.** No specimen access, armed-rule report, census count or quarantine location. The job contract uploads `rm-bundle/` with `when: always`, so content the runner's own scan flags is uploaded to CI artifacts anyway: D11 says "before upload". Where `arm.banner` lives is open (question 4). | D11 | `scan.rules.specimen(rule_id)`; `scan.scan_paths(paths)`, which reads files in place, for the lab-only realism check; `ScanResult(verdict, findings, files_scanned, armed)`; `[paths] quarantine`; the runner's `finalise_bundle(dir)` deletes flagged files and keeps only their locations before the job ends; `provenance.scan` gains an optional `findings: [{rule, file, line}]`. |
| G9 | Compute and QuickStart are neither catalogued nor registered. | F1 | Data, not contract: register them once DEC3 and question 8 are answered. |
| G10 | **Nothing runs a detector, and exposure evidence is free text.** A record can claim `exposed` by `detector` with no run behind it. In v1 only rc4 has built output (the P1 archive); no v1 tier regenerates `build_soc`. | F2 | `nanosoc_rm.analysis.detect.run(defect, detector, built_output) -> DetectorRun(result, forms, input_md5, detector_source, command, observed_at)`; an optional structured `exposure.run` on the erratum, required by the loader when `method: detector`. |
| G11 | **Gate details are free text.** A test cannot tell which pins or record pointers a gate checked, or the result of each. The registry job also needs registry read access (question 3). A citation into a private canonical repo can only be UNVERIFIED, never FAIL, from an anonymous job. | D2, F3, F5 | Each gate writes `rm-bundle/items/<gate>.jsonl`: `{tree_path or file+pointer, component, commit, host, result: pass\|fail\|unverified\|not-reached, why}`; `nanosoc-rm t0 items NONCE GATE --json`. Decide whether UNVERIFIED on a private citation satisfies "T0 fails otherwise". |
| G12 | **No import census.** The ledger cannot say which headings count as items. `16-open-defects.md` shows the judgement is real: 6 numbered items, 2 unnumbered paragraphs and an addendum with a third. Pending items do not change the exit code. | F5 | Import-ledger source field `doc.items_rule: {pattern, exclude: [{key, reason}]}`, where the pattern's first group is the item key; `nanosoc_rm.registry.imports.census(ledger, read_doc) -> [CensusRow(path, commit, census, missing, extra, excluded, pending, done)]`; `nanosoc-rm registry imports --json`, exit 1 while any item is pending or the census differs. |
| G13 | **Test infrastructure.** Markers `requirement` and `live` are unregistered. The lab inputs have no agreed names. | all | `tests/conftest.py` registers both markers. Lab inputs: `NANOSOC_RM_CONFIG`, `NANOSOC_RM_REGISTRY`, `RM_ARM_REFERENCE_FILE`, `RM_RC4_BUILD_SOC_TAR`. A live test without its input fails; it never passes. |
| G14 | **Record and value types are undefined.** `Registry` returns `Component`, `Configuration`, `Defect`, `Erratum` and `Repo` types with no stated fields. | F1, F2, F3, F5 | Records are schema documents (plain dicts, or pydantic models with `model_dump()`); `Repo(host, path)` importable from `nanosoc_rm.registry`. The tests read records through `as_doc()`. |

## Untestable, or only partly testable

| Item | Why |
|---|---|
| D10 "Budget in CPU-hours, not licence seats" | A planning rule. No contract records CPU time or a budget (G7). Testable in Phase 2 once provenance carries CPU seconds. |
| D10 negative case on a real bad runner | Producing a job on the open runner or the login server needs a runner registration, a shared-infrastructure write (PR8). The control is synthetic only. |
| D1 T1–T6 commands per SoC | They do not exist until Phase 2/3 manifests (`tests.<tier>.run`). The v1 AT checks T0's command and all seven budgets. |
| D11 "release file" scanning | Release files are Phase 5. v1 scans bundles only. |
| D11 specimen realism in public CI | Proving the specimen resembles a real Arm banner needs a real vendor file, available only on the lab network. It is a lab-only step. |
| D2 "fetchable without credentials" as an outsider sees it | The job runs on the lab network, which can reach GitLab (question 5). What an outside user can fetch cannot be observed from inside. |
| D5 "never accepts results pushed to it" | A universal negative. The test proves it for this app's route table and for the one POST route. It cannot rule out a second process on the host. |
| F1 compute and QuickStart errata | Blocked on data (DEC3, question 8). The test is written and fails until then. |
| F2 on configurations other than rc4 | No built output exists for them in v1 (G10), so their exposure can only be ancestry, shown amber. |
