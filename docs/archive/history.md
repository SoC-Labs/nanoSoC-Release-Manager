# History

What the project's documents used to say and no longer need to: plans that finished, gate-2 working notes and
upgrade notes for versions nobody runs any more. Nothing here is a contract or an instruction; where it disagrees
with [contracts/](../contracts/), [DEPLOY.md](../DEPLOY.md) or the code, they are right. Who agreed each change and
when is in [CHANGES.md](../CHANGES.md); what changed for users is in [CHANGELOG.md](../../CHANGELOG.md).

Contents:
[The MVP week plan (2026-10-10)](#the-mvp-week-plan-2026-10-10) ·
[T0 on real data (2026-10)](#t0-on-real-data-2026-10) ·
[Gap index (gate 2)](#gap-index-gate-2) ·
[Mirror disk size (gate 2)](#mirror-disk-size-gate-2) ·
[Store upgrade to schema 2 (2026-10-14)](#store-upgrade-to-schema-2-2026-10-14) ·
[Store growth before keep-on-change (2026-10-15)](#store-growth-before-keep-on-change-2026-10-15)

## The MVP week plan (2026-10-10)

Was `docs/PLAN.md`. The week finished: v0.1.0 was tagged on 2026-10-12 and the repository is public. The test
counts, "nothing is pushed" and the blockers below describe 2026-10-10.

Written 2026-10-10. Supersedes the plan in proposal-rev2 §9 for scheduling; the requirements, contracts and
phase gates it refers to are unchanged.

### Where we are

| Done | Evidence |
|---|---|
| Requirements v1.0 frozen; gate 2 approved | `docs/REQUIREMENTS.md`, `docs/GATE2.md` |
| Phase 0: rc4's tree tag-protected on both hosts | `silicon-protection eth-rc4`: 12 of 16 rows covered |
| Phase 1 code built, reviewed, merged on local `master` (`0817e3d`) | 740 tests pass, 7 fail (all frozen-test defects: CR3, CR5) |
| Real-data demo, read-only | a private demo folder (not in this repository) |

Blocked on David since 2026-10-02: the CR batch (17 items), U1 (Arm-marked source in public repos), N1 (GitLab
projects, runner, VM). Nothing is pushed.

### MVP goal (David, 2026-10-10)

Two uses, on the command line, from a laptop or the login server, no service VM:

1. **Collapse branches within a project.** For a repo: every branch, what it holds that the default branch does
   not, which registered SoC pins it (so it cannot be deleted), and whether it is merged, stale, or a keep tag
   already protects its tip. Output: a per-repo table and a delete-list a person reviews. The tool never deletes.
2. **Check submodule compatibility.** For a SoC configuration (or a proposed set of pins): resolve the tree,
   judge every pin (fetchable, reachable, state on its canonical host, duplicates, URL kind), then run the cheap
   tiers locally: T0 resolve and, when the SoC has a `make`/sim target, T1 lint and T2 smoke. Answer: these pins
   work together, or this pin breaks it, with the evidence.

Everything above except branch inventory and the local T1/T2 runner exists in Phase 1. The MVP adds two
commands and one runner mode, and spends nothing on web pages, CI, tokens or the service VM.

### Token-efficient method

- One builder agent per slice, no reviewer fan-out; the main session reviews diffs and runs the suite once per
  slice. Mutation checks only on the two new checkers (PR6), not a full coverage pass.
- Frozen contracts stay frozen. New capabilities get a CR entry (CR8, CR9 below) and their own schema `$defs`,
  so no existing test or golden moves.
- Live tests run anonymously against the real repos as in the Phase 1 demo; no new goldens that depend on
  lab tokens.
- Budget: about 6 agent runs and 4 main-session suite runs for the week.

### Week plan

| Day | Slice | Gate (must hold before the next slice) | Unit tests |
|---|---|---|---|
| Mon | **S0 unblock.** Apply agreed CRs 2–5 (stale goldens, broken frozen tests, D7 trailer). Fix the 7 failures. **CR6a (David, 10 Oct): a catalogue `vendor_allowlist` with `severity: allow` (DMA-230/PL230 files, reason recorded) or `severity: warn` (every other Arm-banner tree); a `warn` item makes its gate WARN (amber, never FAIL, never hidden), an `allow` item is clean.** Tag `v0.1.0-mvp0` locally. | `pytest -m "not live"`: 0 failures. Anonymous live: B1 B2 B4 B6 F3 pass. | existing 740 |
| Tue | **S1 branch inventory** (`nanosoc-rm branches REPO [--all]`, CR8). Per branch: tip, ahead/behind default, merged?, last commit date, keep tags on tip, registered pins reaching into it (via stored walks), verdict `MERGED / PROTECTED / PINNED / STALE / ACTIVE`. `--delete-list` prints only `MERGED` and `STALE` with no pin and no tag. Reads mirrors only. | On a hermetic 5-branch fixture every verdict occurs; the delete-list never names a pinned or tagged branch (mutation: drop the pin check → test red). On eth repo: lists `test/cpu-ss-hsel-regression` as PINNED-by-registry once the bench is cited. | ~25 |
| Wed | **S2 compatibility check** (`nanosoc-rm compat CONFIG [--pin PATH=SHA]...`, CR9). Walk with overrides, then the T0 gates locally (reuse `t0.gates` with `Context.for_tests` → a `local` mode that is allowed off CI and marks `runner.hardened` NOT-RUN). One verdict per pin plus the run roll-up. | Hermetic: an override to a nonexistent SHA → FAIL at reachable; a duplicate component → B7 flag. Live: `compat eth-rc4` reproduces the demo's raw roll-up; `compat eth-main --pin nanosoc_gen=036d381` resolves. | ~20 |
| Thu | **S3 local T1/T2** (`compat --tier t1|t2`). A per-SoC `tiers.yaml` in the registry names the commands (D1 already has the table); the tool runs them in a sandbox checkout of the resolved tree with the overrides applied, 1 VCS seat, budget enforced, verdicts.tsv out. No regeneration without `NANOSOC_GEN_OK=1` (set_env.sh is never sourced). | Hermetic: a fake tier command that exits 1 → FAIL; over budget → `over_budget`. Live, one run: eth-main T1 (lint) on the login server. | ~15 |
| Fri | **S4 hand-over.** `docs/MVP.md` (since merged into [user-guide.md](../user-guide.md)): the two workflows, copy-paste commands, what the verdicts mean. Local tag `v0.1.0`. David runs both workflows on a repo of his choice. | David's run gives the right answer for one branch he knows is dead and one he knows is pinned. | — |

Total new code: about 600 lines plus tests. If Mon slips (CR answers late), Tue–Thu still run; S0 only
fixes tests.

### Gates between phases (unchanged, from `docs/phase-gates.yaml`)

| Phase | Starts when | Delivers |
|---|---|---|
| 1 remainder (gate 3) | N1 done: GitLab registry + compat-ci projects, hardened runner, service VM, tokens | nightly T0, web dashboard, Run button, live ATs |
| 2 | generator deterministic (two identical regenerations); runner ephemeral 7 nights; CPU-budgeted host | matrix D3, provenance D4, flaky handling D8, release store D12 |
| 3 | matrix nightly for 7 nights | SemVer + change classes C1–C12, tool releases G1 G5 G6 |
| 4 | OPS items closed; classifier running | bump bot in shadow mode E1–E7 |
| 5 | Phase 3 accepted | SoC releases, lock files, errata revisions |

### Decisions this plan needs (answer on the CR page or here)

1. CR2–CR5 (test and golden fixes): yes/no. Without them S0 cannot reach 0 failures.
2. CR8 branches, CR9 compat: agree the two new commands as v1 additions.
3. U1 decided 2026-10-10: DMA-230 OK; other Arm-banner trees WARN (CR6a above). The repos stay public.

## T0 on real data (2026-10)

Was interfaces.md section 9, "Expected on real data today". The rc4 rows of
`schemas/examples/result-bundle.verdicts.example.tsv` are the contract's example; this is what the live trees gave
in October 2026.

- eth-rc4's raw T0 (README section 6 table, before waivers) is FAIL on three gates and UNVERIFIED on one:
  - `t0.pins.fetchable`: TideLink and ASIC-Toolkit use ssh URLs (and ASIC-Toolkit is private); the 5 pins below TideLink are `not-reached`;
  - `t0.urls.no-ssh-public`: same two URLs;
  - `t0.pins.reachable`: on the example catalogue only ASIC/asic-toolkit is `unverified` (private), so the gate is UNVERIFIED: ahb_qspi `e908ac8` is on GitHub since the 2026-10-01 keep tag (CR-2). On the live registry (DEC6) ethernet-mac-ahb `44ff8b7` and socdebug_tech `50359ee` fail until decision R1 is carried out;
  - `t0.vendor.no-arm-ip`: UNVERIFIED, the 7 trees that were never fetched are `not-reached`;
  - `t0.vendor.no-foundry-data` (CR-6): NOT-RUN where no foundry rule is configured (the local GUI host); with the
    toolkit scanner, UNVERIFIED for the same 7 trees, and every foundry finding WARN unless `foundry_rules` says fail.
- With the 7 `t0_waivers` of `eligibility.example.yaml` registered, every one of those items is covered, so the
  four gates and the run roll up **WAIVED** (`observation.t0-result-waived.example.jsonl`). The eighth waiver, for
  ahb_qspi `e908ac8`, covered nothing once that commit was on GitHub (`waiver_unused`) and was retired (CR-2).
- `t0.manifest.valid` and `t0.clone.upgrade-path` are NOT-RUN on rc4 (no manifest; no `upgrade_from`). eth-main declares `upgrade_from: v1.0.0`, so its upgrade-path gate runs (and fails today on rc4's ssh pins).
- The registry job is FAIL on `t0.registry.refs-resolve`: bench `6aafc94` is not pushed.

## Gap index (gate 2)

Was interfaces.md section 17.7: where each gap that the three acceptance files logged before gate 2 was resolved.
Section numbers are interfaces.md's. The gap tables in `docs/acceptance/` keep the gaps' own text.

Where each gap the three acceptance files logged is resolved:

| Gap | Resolved in |
|---|---|
| pins G1, ci_errata G4 (keys), scope_web G2 | 17.1 |
| pins G2, ci_errata G14, scope_web G1 | 17.2 |
| pins G3 | 1 (config read per call) |
| pins G4 | 4 step 2, pin-state `fact.state` |
| pins G5 | 5 (UNKNOWN, nullable heads) |
| pins G6 | 4 step 4, README section 5 |
| pins G7, scope_web G14 | 3 (namespace and key), examples fixed |
| pins G8 | 4 step 3 |
| pins G9 | 4 step 2, pin-state `branch_field` |
| pins G10 | 6 step 4 |
| pins G11 | 7 |
| pins G12 | 8, 12 |
| pins G13 | 4 step 1, eligibility `commit` |
| pins G14, ci_errata G13, scope_web G18 | 17.4 |
| pins G15 | 3 (git environment) |
| pins G16 | 4 (storage) |
| ci_errata G1 | 9 (tiers and budget) |
| ci_errata G2, scope_web G8 | 10 Trigger step 5, `coverage` |
| ci_errata G3 | 9 (`url_map`) |
| ci_errata G4 | 10 (types, `CIClient`, `FakeCI`) |
| ci_errata G5, scope_web G4, G5 | 13 (hooks, `/facts/{subject}`, `/api/v1/errata`), 14 |
| ci_errata G6 | 9 (Gate API), observation `control: not-applicable`, example rows added |
| ci_errata G7, scope_web G7 | 1 (`ci.*`), 10 verify check 1, observation `runner_settings`, 17.3 |
| ci_errata G8 | 9 (`finalise_bundle`), 11, result-bundle `scan.findings` |
| ci_errata G9 | data, not contract: questions 8 and DEC3 |
| ci_errata G10 | 6a, erratum `exposure.run` |
| ci_errata G11 | 9 (items files, `t0 items`), README section 6 ("T0 fails") |
| ci_errata G12 | 2a, import-ledger `items_rule` |
| ci_errata G15 | 9 (`t0.pins.reachable` walks canonical URLs) |
| scope_web G3 | 14 |
| scope_web G6, G20 | observation `registry_record` and `alert`; 13 JSON API |
| scope_web G9 | 17.3 |
| scope_web G10 | 17.5 |
| scope_web G11 | 10 CLI, 14 Logging |
| scope_web G12 | 17.6, `docs/phase-gates.yaml` |
| scope_web G13 | 12a |
| scope_web G15 | 3 (which repos) |
| scope_web G16 | 12 (history) |
| scope_web G17 | README section 3 rule 10; deletion: question 12 |
| scope_web G19 | 3 (`--no-tags`) |
| scope_web G21 | 17.4 |
| scope_web G22 | 13 (host drift) |

## Mirror disk size (gate 2)

Was interfaces.md section 3, an open unknown at gate 2:

- **Unknown:** total mirror disk size for about 158 repos. Blobless fetches of two small repos took 140 KB and 340 KB. Measure on the first full sync.

## Store upgrade to schema 2 (2026-10-14)

Was in DEPLOY.md, Operate. The store is at schema 3 now; DEPLOY.md, "Upgrades", is the current note.

The first start after this update migrates `rm.sqlite` to schema version 2 (a new `current_snapshots` table, backfilled
from the stored snapshots; measured 0.26 s for 100,000 snapshots) and stamps `jobs.sqlite` at version 1. It runs once,
under a write lock; page reads continue meanwhile. Take the usual backup first. Do not run an older install of the CLI
against the same data directory afterwards: it would stamp the store back to version 1, and every later connection
of the new code would repeat the (harmless, but writing) backfill until the old install is gone.

## Store growth before keep-on-change (2026-10-15)

Was in DEPLOY.md, "Store growth and retention".

Before this change `rm.sqlite` grew by about 122 MB a day on the live host. Nearly all of it was set-kind history:
every 15-minute poll stored a complete `pin_state` snapshot of each configuration, a `freshness` snapshot, a
`mirror_status` snapshot of each component and so on, whether or not anything had changed (about 3.7 GB a month).

Now a producer run whose content equals the scope's current snapshot is not stored again: the current snapshot
records the check (`confirmed_at`, `confirmations`, and the run's check times and snapshot id). Content means the
documents with their check times and snapshot ids taken out (interfaces.md section 12). Pages are unchanged: they
read the last check's documents, so a data age still says "checked at" the last poll. A change stores a new
snapshot as before. Measured on a synthetic store at the live cadence (`scripts/measure_store_growth.py`: 57
scopes, 543 set-kind rows per poll, 96 polls): 164.7 MB a day before; 0.8 MB a day with no content changes,
6.8 MB a day with 24 (one configuration's walk changing each hour), 25 MB a day with 96.

The first start after this update migrates `rm.sqlite` to schema version 3 (six columns and two indexes on
`snapshots`; `ADD COLUMN` does not rewrite the table). An older install run against the store afterwards would show
stale check times for unchanged scopes (it does not read confirmations) and store every run again; it does not
damage the store. Roll back by reinstalling the older version only if you accept that.
