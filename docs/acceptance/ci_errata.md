# Acceptance tests: CI, T0 and errata

Gate 2 (PR3). One acceptance test (AT) per v1 Must requirement in this area: **D1, D2, D5, D6, D10, D11,
F1, F2, F3, F5**. Each AT has a negative control (PR6).

- **Requirements:** [`REQUIREMENTS.md`](../REQUIREMENTS.md) v1.0, frozen 2026-09-30.
- **Contracts exercised:** [`contracts/README.md`](../contracts/README.md), [`contracts/interfaces.md`](../contracts/interfaces.md), [`schemas/`](../../schemas/).
- **Test code:** [`tests/acceptance/test_ci_errata.py`](../../tests/acceptance/test_ci_errata.py). Every AT carries `NOT_BUILT` (xfail, `raises=ImportError`, strict): XFAIL until its module exists, red on any other error, red as XPASS once it passes (`tests/conftest.py`). Ten `test_selfcheck_*` tests run now: see [Negative controls run today](#negative-controls-run-today).
- **Status:** for David's gate-2 review. Written 2026-09-30; every fixture value re-checked against the hosts the same evening (read-only); revised the same day after the contracts and coverage reviews ([GATE2.md](../GATE2.md) section 4).

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
5. **Names beyond the schemas.** A name marked `PROPOSED (Gn)` in the test was a gap in the first draft. Every one is now contracted in `interfaces.md` section 17; [Contract gaps](#contract-gaps) says where.
6. **Hostnames.** The lab GitLab host is written `<gitlab-host>`, as in the contracts. The real name lives in the private service config.

## Index

| AT | Requirement | What the test checks | Runs (test / control) | Negative control |
|---|---|---|---|---|
| [AT-D1](#at-d1) | D1 test tiers | All 7 tiers have a budget; every configuration has a T0 command; every accepted nightly T0 finished inside its 120 s budget | live / hermetic | a 181 s T0 bundle is flagged over budget; a tier row with no budget fails the table check |
| [AT-D2](#at-d2) | D2 nightly T0 | Every registered configuration and the registry have tonight's accepted T0 report; rc4's raw gates go red on its known real items, and its waivers (GATE2 Q1) cover exactly those; all 44 pins are judged on their canonical host; every gate that ran fired its control | live / hermetic | the CI API refuses eth-main's nightly trigger: eth-main shows a missed night, never last night's result; and the four gates rc4 cannot exercise each PASS and FAIL on hermetic inputs |
| [AT-D5](#at-d5) | D5 pull and verify | The genuine rc4 bundle is accepted; 13 single-field tampers are each rejected by the check that owns the field; no route accepts a pushed result | hermetic / hermetic | the same bundle with `captured.superproject_commit` = rc4's real parent `3ab16f5` is rejected |
| [AT-D6](#at-d6) | D6 negative controls | Every declared gate is reported with its control and whether it fired; a PASS without a fired control never shows PASS | hermetic / hermetic | every gate in turn replaced by an always-PASS mutant: its control does not fire and the gate shows UNVERIFIED |
| [AT-D10](#at-d10) | D10 hardened runner | The real T0 job passes `t0.runner.hardened`; the runner's id and settings are recorded and match the expected ones | live / hermetic | synthetic job environments (unprotected ref, dirty HOME, dirty TMPDIR, personal user, a CI token left unscrubbed) each FAIL the gate |
| [AT-D11](#at-d11) | D11 vendor scan | The genuine rc4 bundle scans clean, is accepted and stored; the `arm.banner` rule fires on one real vendor file | live (realism step) / hermetic | the same bundle with a planted Arm banner in `logs/t0.log` is refused and never stored |
| [AT-F1](#at-f1) | F1 defects and errata | SL-DEF-0001 holds the real cpu_ss_hsel facts; one erratum each for eth, compute and QuickStart; the eth forms agree with containment | live / hermetic | the rc4 erratum copied to non-silicon eth-main with `status: permanent` is refused |
| [AT-F2](#at-f2) | F2 exposure | Every erratum shows level, method and evidence; ancestry shows amber; a detector level carries a re-checkable run | live / hermetic | `erratum.ancestry-claims-exposed.yaml` is rejected; a detector id not in the defect is refused; D2 fires on a synthetic netlist with the defect and stays clean on one without |
| [AT-F3](#at-f3) | F3 cited commits exist | With every cited commit pushed, `t0.registry.refs-resolve` passes and names each of the 10 commits it proved, including the 4 the erratum names only in free text | live / live | the real SL-DEF-0001, whose bench `6aafc94` is unpushed, FAILs; so do a copy-host-only and a local-only commit |
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
| ahb_qspi | DIVERGED, both hosts public. rc4's `e908ac843845d5b293616265f93a802804555540` exists only on GitLab (GitHub 422); `71f8d1ce46dfa76e9f1e16f7ab186e51d1b31520` exists only on GitHub (GitLab 404) | D2, F3 |
| asic-flow | rc4's `3824aaa` (ASIC/asic-flows) is only on GitHub `lpddr4-pll`, GitHub's default branch; rc4's `46c88d3` and `69500ef` are on `main` on both hosts | D2 |
| arch_tech `ecf76aa` | rc4's multicore arch_tech pin. Its keep tag is on the GitLab copy only. On GitHub (canonical) it is reachable only from branch `wip/uncommitted-2026-08-18` | D2 |
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
| compute superproject | SoC-Labs/NanoSoC-Compute-Chiplet is private on GitHub, so not anonymously fetchable; its taped-out commit waits on DEC3 | F1, D2 |
| QuickStart repo | SoC-Labs/nanoSoC-M0-QuickStart-SoC is public, so an anonymous miss there is FAIL, not UNVERIFIED | F3 |
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
1. `nanosoc-rm tiers CONFIG --json` runs for each configuration. (G1, now contracted).
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

**Exercises:** the tier table and budget check (interfaces.md section 9); result-bundle `started_at` / `finished_at`; the interfaces §9 job contract.

**Negative control**
1. The example bundle with `finished_at` moved to 02:06:31 (181 s): the budget check reports over budget, not OK.
2. A tier table whose `t2` row has no budget and no reason: table validation fails, naming `t2`.

**Cannot pass vacuously because**
- The table must hold exactly 7 tiers, so an empty table fails.
- Durations come from verified bundles, and the control shows an over-budget run is flagged.

**Open**
- Resolved: the budget is 120 s, the job's `timeout: 15m` is only the kill limit, and over budget is a warning (`over_budget: true`) that never changes the verdict (interfaces.md section 9).
- T4 and T5 have no budget in D1; each carries a written reason in the tier table.

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
   - a configuration run carries the 9 gates of interfaces §9 (CR-6 added `t0.vendor.no-foundry-data`);
   - `@registry` carries `t0.registry.schema` and `t0.registry.refs-resolve`;
   - every gate that ran has `control: fired`; only a NOT-RUN gate has `not-applicable` (an "any valid value" check would pass vacuously);
   - eth-main declares `upgrade_from`, so its `t0.clone.upgrade-path` is not NOT-RUN.
3. eth-rc4's **raw** verdicts (before waivers: `raw_verdict` on a WAIVED gate) match the golden `result-bundle.verdicts.example.tsv`:

   | Gate | Raw verdict | Detail |
   |---|---|---|
   | `t0.infra.probe` | PASS | |
   | `t0.pins.fetchable` | FAIL | fails exactly `tidelink` and `ASIC/asic-toolkit`; the 5 pins below `tidelink` are not reached; 37 of 44 pass |
   | `t0.pins.reachable` | FAIL, or UNVERIFIED | see Then 4 |
   | `t0.urls.no-ssh-public` | FAIL | names `tidelink` and `ASIC/asic-toolkit` |
   | `t0.manifest.valid` | NOT-RUN | no manifest anywhere |
   | `t0.clone.upgrade-path` | NOT-RUN | no `upgrade_from` |
   | `t0.vendor.no-arm-ip` | UNVERIFIED | 7 trees not fetched |
   | `t0.vendor.no-foundry-data` | NOT-RUN | CR-6: no foundry rule on the example host (no toolkit scanner); with one, UNVERIFIED on the same 7 unfetched trees, covered by the no-arm-ip `not-reached` waivers (README §6) |

   **3b. Waivers (GATE2 Q1).** The reference oracle `reference_apply_waivers` (README §6) is applied to those raw
   verdicts, the live items of the four non-pass gates and rc4's `t0_waivers` from the live registry. The stored
   gate verdicts and the run verdict must equal its answer. The run is FAIL or WAIVED, never PASS. Each waiver
   that covered nothing has an open `waiver_unused` alert naming its gate and tree path. With the 7 waivers of
   `eligibility.example.yaml` registered, the four gates and the run are WAIVED. The eighth, for ahb_qspi, covered
   nothing after the 2026-10-01 GitHub keep tag and was retired (CR-2).
4. **`t0.pins.reachable` on rc4:**
   - All 44 pins are judged, each on its component's canonical host (externals on their own repo). (G15, now contracted).
   - **TideLink host drift:** `tidelink` `5e8bdb5` passes on GitHub, although the GitLab copy lacks it.
   - **Keep tags:** the 10 keep-tag pins other than `ASIC/asic-toolkit` all pass.
   - `ASIC/asic-toolkit` is UNVERIFIED: its canonical repo is private.
   - The failures are exactly the pins off their canonical host. CR-2: the set is computed, not written down: each
     pin's canonical repo comes from the live catalogue, and a pin an anonymous depth-1 fetch of that repo cannot get
     is expected to fail (the private toolkit and the externals excepted). On 2026-10-10 that is ethernet-mac-ahb
     `44ff8b7` and socdebug_tech `50359ee` (DEC6 makes both canonical on GitHub; decision R1); ahb_qspi `e908ac8`
     passes since its 2026-10-01 keep tag.
   - The gate is FAIL if that set is not empty, else UNVERIFIED.
5. `@registry`: `t0.registry.refs-resolve` judges SL-DEF-0001's bench commit (it must be among the items), FAIL while it is unpushed (`6aafc94` today) and PASS once anonymously fetchable (AT-F3).
6. The dashboard lists every configuration with tonight's verdict and a data age under 24 h.

**Fixture:** REAL.
- rc4's 44-pin tree and its two ssh URLs.
- ASIC-Toolkit is private, so that pin fails fetch whatever its URL.
- **TideLink host drift:** `5e8bdb5` is `v0.90` on GitHub, which is canonical, and missing on the GitLab copy. So `tidelink` must be in the fetchable failures (ssh URL) and absent from the reachable failures (the canonical host has it).
- **Keep tags:** the 11 `keep/eth-rc4/*` tags that cover rc4's formerly branch-only commits.
- The ahb_qspi and asic-flow rows depend on question 2 in GATE2.md (canonical host). The test reads both from the catalogue, so it stays right whichever way David decides.

**Exercises:** interfaces §9 (gates and job contract), §10 (trigger, fetch, verify), §13 (dashboard and `/api/v1/runs`); observation `t0_result`.

**Negative control**
- **Setup:** the fake CI API answers HTTP 500 to tonight's `POST projects/:id/trigger/pipeline` for eth-main, and succeeds for the others.
- **Must happen:**
  - eth-main gets a `t0_result` for tonight with `acceptance: no-bundle`, a verdict other than PASS, and a reason naming the trigger failure;
  - the nightly coverage check exits 1 and names eth-main (`t0 coverage`, interfaces.md section 10);
  - the dashboard does not show eth-main's previous night as current.
- **Runs:** hermetic. Fake CI client, fake clock.
- **Part 2 (coverage review High 2):** rc4 cannot exercise `t0.manifest.valid`, `t0.clone.upgrade-path` or `t0.vendor.no-arm-ip`, and nothing real exercises `t0.registry.schema`. Each runs with its control, through the test-only `url_map` seam, on a hermetic good input (must PASS, control fired) and a broken one (must FAIL):

  | Gate | Good input | Broken input |
  |---|---|---|
  | `t0.manifest.valid` | the example manifest at the pinned commit | the same with `version: "1.0"` |
  | `t0.clone.upgrade-path` | the second commit moves the submodule to a new host while the old host still serves the new commit (C8: a fix) | the old host was frozen before the new commit, so an upgrade without `submodule sync` cannot fetch it |
  | `t0.vendor.no-arm-ip` | a clean tree | the same tree with the scanner's `arm.banner` specimen planted in a file |
  | `t0.registry.schema` | the example registry | the example registry with `component-catalogue.no-class.yaml` as its catalogue |

  `test_selfcheck_D2_gate_fixtures` proves with raw git and the schemas that each good input is good and each broken one is broken.

**Cannot pass vacuously because**
- rc4's raw verdicts must go red on named gates, for known real reasons, and each gate's per-pin results must match exact sets. Its WAIVED verdicts must equal an oracle that shares no tool code, applied item by item, so a tool that waived whole gates, or waived an item no waiver names, fails.
- `tidelink` must fail one gate and pass the other. A tool that marks everything red fails, and so does one that judges the wrong host.
- Every reachable item must carry its canonical host, and the 44-pin count rules out a gate that judged only the pins it could fetch.
- The run count must equal the number of configurations plus one, so an empty night fails.

**Known consequences (not failures of the test)**
- eth-main also pins tidelink by ssh, so its `t0.urls.no-ssh-public` is red.
- If eth-main keeps `upgrade_from: v1.0.0`, its `t0.clone.upgrade-path` is red: an anonymous recursive clone of rc4 cannot fetch rc4's ssh pins.
- Frozen rc4's raw verdicts stay red for ever. Its waivers (GATE2 Q1) make the run WAIVED, so a new failure on rc4 shows red.
- Once compute is registered (F1), its T0 is red at the root while the compute repo stays private: `t0.pins.fetchable` cannot fetch the superproject anonymously.

**Related:** rc4's arch_tech pin `ecf76aa` is reachable on GitHub only from a branch, not a tag; the silicon-protection list shows it as an uncovered canonical-host row until a GitHub keep tag covers it (AT-B5; GATE2 Q9).

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
   - POSTing the bundle's `provenance.json` as the body of either creates only a new run with a fresh nonce (pending, or `no-bundle` when the trigger cannot reach a CI host). It never creates an accepted result, and the posted nonce appears nowhere. At least the two new runs must exist, so the check cannot pass on an empty list.

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
- rc4's non-pass items (`result-bundle.items.*.example.jsonl`) and its 7 `t0_waivers` (`eligibility.example.yaml`).

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
   | `t0.vendor.no-foundry-data` | NOT-RUN | |
   | `t0.runner.hardened` | PASS | fired |

   Run verdict FAIL, 3 not run (CR-6: the foundry gate is NOT-RUN without a foundry rule).
7. **With rc4's waivers** (`gates.apply_waivers`, GATE2 Q1), the four FAIL and UNVERIFIED gates become WAIVED,
   each keeping its raw verdict and listing exactly its non-pass items as `waived`; no waiver is unused; the run
   is WAIVED, 3 not run; the roll-up passed in is unchanged.
8. The run page shows each WAIVED gate with `data-raw-verdict` and class `state-amber`, and one `data-waived`
   row per covered item.

**Fixture**
- REAL: the rc4 rows and `.gitmodules`.
- The example TSV now carries the two `t0.runner.hardened` rows (G6 fixed). The test also shows that the same TSV without them rolls up UNVERIFIED (`missing`).

**Exercises:** README §6 verdict table; observation `t0_result.gates[].control`; interfaces §9 gate table; §13 `/runs/{nonce}`.

**Negative control**
1. **Mutant gate.** `t0.urls.no-ssh-public`'s check is replaced by one that always returns PASS.
   - It runs with its own control, in one run, on the real `.gitmodules`.
   - The control must NOT fire: the `.negctl` row is FAIL.
   - The service's verdict for the gate is UNVERIFIED, never PASS.
   - This is D6's purpose: a gate that cannot fail is caught.
2. **Every gate, not just one** (coverage review Medium 1): each gate in `GATES` is replaced in turn by an always-PASS mutant and run with its own control. Each control fails to fire and each gate rolls up UNVERIFIED, which proves every control runs its gate's own check on a broken input.
3. **Roll-up rules**, each on a copy of the golden with one change:
   - `t0.infra.probe.negctl` deleted → UNVERIFIED, `missing`;
   - that row flipped to FAIL → UNVERIFIED, `not-fired`;
   - a second `t0.infra.probe` row → UNVERIFIED.
4. **Waivers never hide anything new** (GATE2 Q1), each on rc4's items and waivers with one change:
   - a new `fail` item at `tidechart` in `t0.pins.reachable` → that gate FAIL with nothing waived, and the run FAIL;
   - `tidelink/deps/tidelink-phy` turned from `not-reached` into `fail` → `t0.pins.fetchable` FAIL (only a not-reached item rides on the waiver above it);
   - `t0.pins.fetchable.negctl` flipped to FAIL → that gate FAIL, `not-fired`, nothing waived (D6);
   - the ahb_qspi item gone (as after the GATE2 Q2 push) → `t0.pins.reachable` still WAIVED (the private toolkit), and exactly the ahb_qspi waiver is returned unused.
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
- The expected settings in the private service config: `ci.service_account`, `ci.allowed_runner_ids`, `ci.runner_tag`, `ci.runner_expected` (interfaces.md section 17.1).

**When:** a nightly T0 runs and the service fetches it.

**Then**
1. The bundle has `t0.runner.hardened` PASS and `t0.runner.hardened.negctl` PASS, with the run's nonce. The gate detail records:
   - `CI_COMMIT_REF_PROTECTED=true`;
   - `HOME` and `TMPDIR` empty at job start;
   - the job user is the service account.
2. The CI API's `job.runner.id` is in `ci.allowed_runner_ids`. Verify rejects the bundle otherwise. (G7, now contracted).
3. **Settings recorded:** a `runner_settings` observation from `GET runners/:id`, observed no earlier than tonight's newest run. (G7, now contracted). It shows:
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
  4. job user = a personal login;
  5. `CI_JOB_TOKEN` left in the environment (the scrub of interfaces.md section 9 did not run).
- **Must happen:**
  - each environment gives FAIL, and the detail names the variable;
  - (G7, now contracted): the genuine bundle, with the job API reporting a runner id outside the allowlist, is `rejected` at check 1.

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
   - The file is named by `RM_ARM_REFERENCE_FILE`. contracted `scan_paths` (G8) reads it in place, read-only. No copy is made.
   - The finding carries rule, file and line only; the test prints no content.
   - This ties the invented specimen to a real banner.

**Fixture**
- REAL: the rc4 provenance and rows.
- SYNTHETIC: the log text.
- SPECIMEN: the scanner's own `arm.banner` arming specimen, (G8, now contracted). No vendor text is written into this repo.

**Exercises:** interfaces §11 scan; §10 verify check 7; observation `t0_result` (refused → UNVERIFIED, `bundle: null`).

**Negative control (the done-when)**
- **Input:** the same bundle, with the specimen appended to `logs/t0.log` at line 3. `provenance.scan.verdict` still says `clean`, as a job that missed it would claim.
- **Must happen:**
  - `refused`, at check 7;
  - the reason names `arm.banner` and `logs/t0.log:3`, and never the matched text;
  - no file holding the planted text survives anywhere under the service's data dir (a check on one quarantine directory alone could pass if the tool quarantined elsewhere);
  - nothing reaches the store;
  - `t0_result`: `acceptance: refused`, `verdict: UNVERIFIED`, `bundle: null`.
- **Second input:** an empty `rm-bundle/`. The scan is `unverified`, because the census is 0, and the run is refused, not accepted.
- **Third input, the job side ("before upload"):** the planted bundle goes through the runner's last step before the job ends. contracted `finalise_bundle` (G8).
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
2. (G10, now contracted): detector D1 of SL-DEF-0001 (hsel_lint at nanosoc_gen `adbb9ab`) runs on the unpacked rc4 archive.

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

**Exercises:** the erratum `exposure` schema and its if/then; the loader's README §3 rule 4; §13 `/errata/{configuration}`; the detector run (interfaces.md section 6a).

**Negative control (hermetic)**
1. The real invalid example `erratum.ancestry-claims-exposed.yaml` (`level: exposed`, `method: ancestry`, no detector) is rejected with `'detector' is a required property`.
2. The rc4 erratum with `exposure: {level: exposed, method: detector, detector: D9, …}` is refused by loader rule 4: SL-DEF-0001 has no D9.
3. **The detector can answer both ways** (coverage review Medium 2): `detect.run` with SL-DEF-0001's D2 (a netlist pattern) returns `fired`, forms `[A]`, on a synthetic netlist with `.cpu_ss_hsel(UNCONNECTED_12)`, and `clean` on the same netlist with the port connected. A detector that only ever answers one way cannot pass.

**Cannot pass vacuously because**
- The one real erratum sits at the amber level, so the page must render amber.
- The invalid example is exactly the shortcut F2 forbids.

**Limit:** a detector level now carries a structured `exposure.run` (input md5, detector source commit, result, command, time), and the refs-resolve gate proves the source commit. Nothing re-runs the detector to check the claim; AT-F2's live half does that for rc4.

---

<a id="at-f3"></a>
## AT-F3 · Every cited commit exists on its canonical host

**Requirement F3 (Must, v1).** "Every commit a defect or erratum cites must exist on its canonical host; T0 fails otherwise." Done when: a record citing an unpushed commit fails T0.

**Given**
- Registry state R0: the real SL-DEF-0001, ETH-R0P0-ERR-001, catalogue and import-ledger examples, with one change: `regression_test: null`. The schema allows null for "no bench yet".
- Every commit R0 still cites is pushed. The erratum now cites structurally (`cites`, loader rule 9) the four commits its free text names, so the gate proves them too (coverage review Low 2):

  | Commit | Component | Where it is reachable |
  |---|---|---|
  | `49a6c25` | arch_tech | GitHub main |
  | `b0a6069`, `036d381`, `adbb9ab`, `1d7772d` | nanosoc_gen | GitLab main |
  | `d04de2e` | nanosoc_gen | GitLab, keep tag and a branch |
  | `7a87e20` | multicore | GitLab, branch-only |
  | `57d2c76` | multicore | GitLab master |
  | `f60449b` | multicore | GitLab, keep tag and branches |
  | `66a2f698` | eth | GitHub, tag-only via `v1.0.0` |

**When:** the registry T0 job (`configuration: @registry`) runs `t0.registry.refs-resolve` anonymously (interfaces §9).

**Then**
1. The gate is PASS, and its `.negctl` fired.
2. The gate's per-citation results (items file, interfaces.md section 9) cover exactly the 10 distinct cited commits above, each `pass` on the catalogue's canonical host. This includes the branch-only `7a87e20` and the tag-only `66a2f698`.

**Fixture:** REAL, all commits verified on their hosts on 2026-09-30.

**Exercises:** interfaces §9 registry job; the defect, erratum and import-ledger `commit_ref`s; catalogue canonical hosts.

**Negative control (the done-when, real data)**
1. **R0 with the bench restored** to `6aafc943f2750be92e80b31be99f0cd60f8927e4`, which is the real record today.
   - Must happen: the gate FAILs; the detail and the gate's items (G11) name `defects/SL-DEF-0001.yaml`, pointer `/regression_test/commit` and `6aafc94`. The run verdict is FAIL.
2. **R0 plus a defect citing an ahb_qspi commit that exists only on the non-canonical host.** ahb_qspi is DIVERGED, so one exists whichever host question 2 makes canonical: `1e5117e` is GitLab-only (used while GitHub is canonical, as in the catalogue example; CR-2 replaced `e908ac8`, which is on GitHub since the 2026-10-01 keep tag) and `71f8d1c` is GitHub-only.
   - Must happen: FAIL, with the item judged on the canonical host. This proves the gate checks the canonical host, not any host.
3. **R0 plus an import source at QuickStart `1f2f06e`**, which is committed only locally.
   - Must happen: FAIL.

**Cannot pass vacuously because**
- It is a controlled pair: R0 and case 1 differ in one field.
- The positive half needs a branch-only and a tag-only commit to pass, so a checker that reads only the default branch fails the main test.
- Case 2 fails a checker that accepts a commit found on any host, and stays valid whichever canonical host David picks.

**Blocked:** the registry job needs read access to the private registry from compat-ci (question 3).

---

<a id="at-f5"></a>
## AT-F5 · Existing registers are imported

**Requirement F5 (Must, v1).** "Import the existing issue registers first: the eth known-issues, open-defects and rc4 registers, and the QuickStart cpu_ss_hsel write-up." Done when: each imported issue is either a record or explicitly dismissed.

**Given:** the live registry's `imports.yaml`, and each source document at the commit the ledger cites.

**When**
1. `nanosoc-rm registry imports --json` runs (interfaces.md section 2a).
2. Each source document is read at its cited commit and its items are extracted by the source's declared rule (`items_rule`).
3. Independently, the test fetches the two eth documents anonymously at whatever commits the ledger cites and extracts their headings itself.

**Then**
1. The ledger has exactly four sources:
   - eth `docs/tapeout/11-known-issues.md`;
   - eth `docs/tapeout/16-open-defects.md`;
   - eth `docs/tapeout/67-rc4-issue-register.md`;
   - QuickStart `docs/DEFECT-cpu-ss-hsel.md`.

   Each is cited at a commit that `t0.registry.refs-resolve` proves (AT-F3).
2. **Census.** The report covers exactly the ledger's four sources. For each source, the ledger's item keys equal the items the source's rule extracts at that commit, and the independent heading census is contained in the tool's census plus its exclusions:
   - `11-known-issues.md` @ `66a2f698`: 9 items, `a)` to `i)`;
   - `16-open-defects.md` @ `66a2f698`: `1` to `6`, plus the three "found in passing" defects (two in the body, one in the 2026-08-12 addendum). Each of the 9 is either listed or excluded by the rule with a reason.
3. No item is `pending`.
4. Every `record` item names a defect that exists. The QuickStart write-up maps to SL-DEF-0001.
5. Every `dismissed` item has a reason (schema).

**Fixture**
- REAL: the document commits and item counts.
- UNKNOWN: the item counts of the other two documents. `67-rc4-issue-register.md` is untracked, and the QuickStart write-up is committed only locally (`1f2f06e`, not on GitHub). **F5 cannot complete until both are committed and pushed.**

**Exercises:** import-ledger schema; loader §2 (pending count); the census rule and command (interfaces.md section 2a); F3 for the source commits.

**Negative control (hermetic)**
1. The example ledger as written, with 3 items all pending: the F5 check reports "not done: 3 pending" and exits 1.
2. A synthetic source with items `a)`, `b)` and `c)`, and a ledger listing only `a)` and `b)`, both disposed: the census reports `c)` missing and exits 1.

**Cannot pass vacuously because**
- The census reads the document, not the ledger's own list, so a ledger that leaves items out fails.
- Pending must be zero, so an unfinished import fails.

---

## Negative controls run today

`python3 -m pytest --strict-markers tests/acceptance/test_ci_errata.py` on 2026-10-01, after the GATE2 Q1 waivers, gave
**11 passed, 20 xfailed**, under Python 3.8 (pytest 8.3.5, jsonschema 4.19.1) and 3.11 (pytest 9.1.1,
jsonschema 4.26.0). The 20 xfailed are the 10 ATs and their controls, which wait for the tool. The 11 passed
are self-checks: each proves a control's input is what its AT claims, with an oracle that shares no tool code.

| Self-check | Good input passes | Broken inputs that fail |
|---|---|---|
| `test_selfcheck_D1_budget_inputs` | the example bundle ran 92 s | 181 s is over the 120 s budget; the tier enum is exactly t0 to t6 |
| `test_selfcheck_D2_expected_sets` | the per-pin sets match the lock, the silicon rows, the catalogue and the golden TSV | (consistency of the goldens the AT reads) |
| `test_selfcheck_D2_gate_fixtures` | the good upgrade pair updates without sync; the example manifest validates; the clean tree has no specimen; the example registry validates | the frozen-old-host pair fails without sync; `version: "1.0"` fails; the planted tree holds the specimen; the no-class catalogue fails |
| `test_selfcheck_D5_tampers` | a reference verify accepts the genuine case | each of the 13 tampers fails exactly the check it is listed under; the control fails check 5 and differs only in provenance |
| `test_selfcheck_D6_rollup_oracle` | a reference README section 6 roll-up of the golden TSV equals `EXPECTED_RC4_ROLLUP` (FAIL, 3 not run; CR-6); the schema accepts it | control deleted, flipped, doubled: UNVERIFIED; all-skip and no gates: UNVERIFIED; the schema refuses the same gates claimed PASS, and a PASS over NOT-RUN gates only |
| `test_selfcheck_D6_waiver_oracle` (GATE2 Q1) | rc4's 7 waivers turn exactly its 4 non-pass gates WAIVED (run WAIVED, 3 not run, none unused; CR-6); the stored example equals it item for item and validates | a new failure, a `fail` below a waived pin, an unfired control and an orphaned `not-reached` item each stay FAIL; the retired ahb_qspi waiver (CR-2) is unused against today's items and covers the pre-push item; the schema refuses a WAIVED run with a FAIL gate and a WAIVED gate without `raw_verdict`. Three mutants of the oracle (ignore the control, let a prefix cover any result, never report unused) each fail it |
| `test_selfcheck_D10_environments` | the good job environment breaks nothing | each synthetic environment breaks exactly its one property |
| `test_selfcheck_D11_planted_line` | the bundle writer writes exactly 3 files | the planted specimen starts at `logs/t0.log` line 3 |
| `test_selfcheck_F1_F2_variants` | the rc4 erratum validates and is permanent on a silicon configuration | the eth-main copy breaks rule 6 only; form C and D9 are not in the defect; the ancestry example fails `required` at `/exposure`; D2's pattern matches only the fired netlist |
| `test_selfcheck_F3_refs` | R0's structured citations are exactly the 10 commits; every commit named in the examples' free text is cited | the control adds exactly the bench; a planted free-text commit is flagged by rule 9 |
| `test_selfcheck_F5_census` | the example ledger with rules validates; the census finds everything listed | 3 pending; a ledger without `c)` misses exactly `c)` |

## Contract gaps

**Every gap below except G9 (data, not a contract) was resolved in the gate-2 revision (2026-09-30);** the last column says where. `docs/contracts/interfaces.md` section 17.7 has the same index. The text of each gap is kept as written, so the review trail stays readable.

Needed by the tests above and not in the gate-2 contracts. Each is a proposal. None has been added to a contract.

| # | Gap | Needed by | Proposal | Resolution (2026-09-30 revision) |
|---|---|---|---|---|
| G1 | **No tier table.** No contract gives each tier's budget, licence, manual flag or per-SoC command. The T0 budget conflicts: D1 says under 2 min, the job contract has `timeout: 15m`, interfaces §16 says "15-minute budget". No rule says what over budget means. D1 gives T4 and T5 no budget. `t0_result` carries no start or finish time. | D1 | `nanosoc_rm/t0/tiers.py`: `TIERS`, `tier_commands(config)`, `check_budget(tier, started_at, finished_at)`, `validate_tiers()`; CLI `nanosoc-rm tiers [CONFIG] --json`; `t0_result.fact.started_at`, `.finished_at`, `.over_budget`. Keep `timeout: 15m` as the kill limit, 120 s as the budget, and make over budget a WARN shown on the dashboard. | Resolved: interfaces.md section 9 (tiers; 120 s budget, 15 min kill limit, over budget = warning); observation `started_at`, `finished_at`, `over_budget` |
| G2 | **A missed night is invisible.** `trigger()` failure has no recorded outcome, and nothing defines "no report tonight". The dashboard shows only the last verdict's age. | D2 | A failed trigger writes a `t0_result` with `acceptance: no-bundle` and a reason. Add `nanosoc-rm t0 coverage [--night DATE] --json`: exit 1 when any configuration or `@registry` lacks an accepted scheduled run that night. | Resolved: interfaces.md section 10 Trigger step 5, `t0 coverage` |
| G3 | **No hermetic seam for the T0 runner.** `GIT_CONFIG_GLOBAL=/dev/null` blocks `insteadOf`, so the runner cannot be pointed at local fixture repos. Only live tests can exercise fetch and reach. | D2 (builder tests) | A test-only `--url-map FILE`, refused when `CI_JOB_ID` is set so a real job cannot fake reachability. | Resolved: interfaces.md section 9 (`url_map`, refused when `CI_JOB_ID` is set) |
| G4 | **Verify, fetch and CI client types are undefined.** `Verdict` and `RunRecord` have no fields. Nothing stores a `RunRecord`. There is no seam to inject a fake CI API. Interfaces §1 names the config sections but not their keys, nor the data directory. | D2, D5, D10, D11 | `Verdict(acceptance, check: int \| None, reason)`; `RunRecord` = the `runs` table columns, importable from `nanosoc_rm.ci.verify`; `db.put_run(run)`; a `CIClient` protocol, and a `FakeCI(pipelines, jobs, variables, artifacts, fail_trigger_for)` for tests, as fpgahub's `FakeAuthenticator`; `trigger(..., client=)` and `fetch_pending(client=)`; keys `[service] data_dir`, `[ci] project, project_id, ref, job, allowed_refs, fetch_timeout_minutes`, `[paths] quarantine`. | Resolved: interfaces.md section 10 (`CIClient`, `FakeCI`, `RunRecord`, `Verdict`), 17.1 keys |
| G5 | **No app factory, no test auth, no page markup.** Tests cannot find a verdict, a control or a colour on a page. No JSON route serves defects and errata. The scope/web tests propose `create_app(config)` taking a loaded config; the two proposals must be reconciled. | D2, D5, D6, F2 | `nanosoc_rm.web.create_app(config_path, auth=)` and `nanosoc_rm.auth.FakeMembership(login=)`; data attributes: dashboard rows `data-configuration`, `data-verdict`, `data-age-seconds`; run page rows `data-gate`, `data-verdict`, `data-control`; errata rows `data-erratum`, `data-exposure`, `data-method`, plus class `state-amber` / `state-red` / `state-green`; `GET /api/v1/errata/{configuration}`. | Resolved: interfaces.md sections 13 and 14 (`create_app(config_path, auth=)`, merged hooks, `/api/v1/errata`) |
| G6 | **The gate table's shape is undefined.** Missing: the `Gate` fields, a context object, a way to run a gate with its control, and the roll-up function. Nothing makes the control run the gate's own code. `control` has no value for a NOT-RUN gate. The golden `verdicts.tsv` example has no `t0.runner.hardened` rows, so it rolls up UNVERIFIED. | D2, D6, D10, F3 | `Gate(id, scope, reqs, check, control)` as a dataclass, with `check(ctx) -> (status, detail)` and `control(ctx) -> Context` returning the broken input only, so `run_with_control` always runs the same `check` on it; `GATES: dict[id, Gate]`; `Context.for_tests(nonce, workdir, parents=, env=, service_account=, registry_dir=)` and `ctx.items(gate)`; `run_with_control(id, ctx) -> (row, negctl_row)`; `rollup(rows, scope)`; `run_verdict(items) -> (verdict, not_run)`; add control value `not-applicable` (an enum value, so no bump); add the two rows to the example. | Resolved: interfaces.md section 9 (Gate API); observation `control: not-applicable`; the two rows added to the example |
| G7 | **Runner identity and settings are neither recorded nor checked.** The gate checks only the job's environment. Verify ignores `job.runner`. Nothing enforces "not a shared or untagged lab runner, not a shared login server". No field holds CPU time. | D10 | Config `ci.allowed_runner_ids` and `ci.service_account`; verify check 1 adds `job.runner.id ∈ allowed`; a new observation kind `runner_settings` (an enum value, so no bump) from `GET runners/:id`, with a `drift` list; `nanosoc-rm runner --json`, exit 1 on drift; the job learns the expected service account from a non-secret compat-ci variable (e.g. `RM_SERVICE_ACCOUNT`), since the job holds no config; provenance `cpu_seconds` (Phase 2). | Resolved: interfaces.md sections 1 and 10 (verify check 1), observation `runner_settings`, 17.3; `cpu_seconds` in Phase 2 |
| G8 | **The scanner's test surface is undefined, and the job uploads before anyone checks.** No specimen access, armed-rule report, census count or quarantine location. The job contract uploads `rm-bundle/` with `when: always`, so content the runner's own scan flags is uploaded to CI artifacts anyway: D11 says "before upload". Where `arm.banner` lives is open (question 4). | D11 | `scan.rules.specimen(rule_id)`; `scan.scan_paths(paths)`, which reads files in place, for the lab-only realism check; `ScanResult(verdict, findings, files_scanned, armed)`; `[paths] quarantine`; the runner's `finalise_bundle(dir)` deletes flagged files and keeps only their locations before the job ends; `provenance.scan` gains an optional `findings: [{rule, file, line}]`. | Resolved: interfaces.md sections 9 (`finalise_bundle`, `access: developer`, compat-ci private) and 11; result-bundle `scan.findings` |
| G9 | Compute and QuickStart are neither catalogued nor registered. | F1 | Data, not contract: register them once DEC3 and question 8 are answered. | Open (data, not contract): DEC3 and question 8 in GATE2.md |
| G10 | **Nothing runs a detector, and exposure evidence is free text.** A record can claim `exposed` by `detector` with no run behind it. In v1 only rc4 has built output (the P1 archive); no v1 tier regenerates `build_soc`. | F2 | `nanosoc_rm.analysis.detect.run(defect, detector, built_output) -> DetectorRun(result, forms, input_md5, detector_source, command, observed_at)`; an optional structured `exposure.run` on the erratum, required by the loader when `method: detector`. | Resolved: interfaces.md section 6a; erratum `exposure.run` |
| G11 | **Gate details are free text.** A test cannot tell which pins or record pointers a gate checked, or the result of each. The registry job also needs registry read access (question 3). A citation into a private canonical repo can only be UNVERIFIED, never FAIL, from an anonymous job. | D2, F3, F5 | Each gate writes `rm-bundle/items/<gate>.jsonl`: `{tree_path or file+pointer, component, commit, host, result: pass\|fail\|unverified\|not-reached, why}`; `nanosoc-rm t0 items NONCE GATE --json`. Decide whether UNVERIFIED on a private citation satisfies "T0 fails otherwise". | Resolved: interfaces.md section 9 (items files, `t0 items`); README section 6 ("T0 fails" = not PASS); private citations: question 3 |
| G12 | **No import census.** The ledger cannot say which headings count as items. `16-open-defects.md` shows the judgement is real: 6 numbered items, 2 unnumbered paragraphs and an addendum with a third. Pending items do not change the exit code. | F5 | Import-ledger source field `doc.items_rule: {pattern, exclude: [{key, reason}]}`, where the pattern's first group is the item key; `nanosoc_rm.registry.imports.census(ledger, read_doc) -> [CensusRow(path, commit, census, missing, extra, excluded, pending, done)]`; `nanosoc-rm registry imports --json`, exit 1 while any item is pending or the census differs. | Resolved: interfaces.md section 2a; import-ledger `items_rule` |
| G13 | **Test infrastructure.** Markers `requirement` and `live` are unregistered. The lab inputs have no agreed names. | all | `tests/conftest.py` registers both markers. Lab inputs: `NANOSOC_RM_CONFIG`, `NANOSOC_RM_REGISTRY`, `RM_ARM_REFERENCE_FILE`, `RM_RC4_BUILD_SOC_TAR`. A live test without its input fails; it never passes. | Resolved: interfaces.md 17.4, `tests/conftest.py` |
| G14 | **Record and value types are undefined.** `Registry` returns `Component`, `Configuration`, `Defect`, `Erratum` and `Repo` types with no stated fields. | F1, F2, F3, F5 | Records are schema documents (plain dicts, or pydantic models with `model_dump()`); `Repo(host, path)` importable from `nanosoc_rm.registry`. The tests read records through `as_doc()`. | Resolved: interfaces.md 17.2 (records are schema dicts; `Repo`) |
| G15 | **`t0.pins.reachable`'s pin set is undefined when `t0.pins.fetchable` could not fetch a parent.** rc4's 5 pins below `tidelink` are unreachable by the pinned ssh URL but readable through the canonical https URL. The golden row names no unreached pin, which implies the walk goes through canonical URLs, but no contract says so. | D2 | The reachable gate walks the tree through the canonical https URLs it fetches, so it judges every pin (44 on rc4); a pin below an unreadable canonical repo is `not-reached`, which makes the gate UNVERIFIED. Items carry `component` and `host`. | Resolved: interfaces.md section 9 (`t0.pins.reachable` walks the canonical https URLs) |

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
