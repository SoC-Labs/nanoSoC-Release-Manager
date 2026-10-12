# Implementation plan: MVP in one week, then phases

Written 2026-10-10. Supersedes the plan in proposal-rev2 §9 for scheduling; the requirements, contracts and
phase gates it refers to are unchanged.

## Where we are

| Done | Evidence |
|---|---|
| Requirements v1.0 frozen; gate 2 approved | `docs/REQUIREMENTS.md`, `docs/GATE2.md` |
| Phase 0: rc4's tree tag-protected on both hosts | `silicon-protection eth-rc4`: 12 of 16 rows covered |
| Phase 1 code built, reviewed, merged on local `master` (`0817e3d`) | 740 tests pass, 7 fail (all frozen-test defects: CR3, CR5) |
| Real-data demo, read-only | `~/SoCLabs/wt/rm-private/phase1-demo/` |

Blocked on David since 2026-10-02: the CR batch (17 items), U1 (Arm-marked source in public repos), N1 (GitLab
projects, runner, VM). Nothing is pushed.

## MVP goal (David, 2026-10-10)

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

## Token-efficient method

- One builder agent per slice, no reviewer fan-out; the main session reviews diffs and runs the suite once per
  slice. Mutation checks only on the two new checkers (PR6), not a full coverage pass.
- Frozen contracts stay frozen. New capabilities get a CR entry (CR8, CR9 below) and their own schema `$defs`,
  so no existing test or golden moves.
- Live tests run anonymously against the real repos as in the Phase 1 demo; no new goldens that depend on
  lab tokens.
- Budget: about 6 agent runs and 4 main-session suite runs for the week.

## Week plan

| Day | Slice | Gate (must hold before the next slice) | Unit tests |
|---|---|---|---|
| Mon | **S0 unblock.** Apply agreed CRs 2–5 (stale goldens, broken frozen tests, D7 trailer). Fix the 7 failures. **CR6a (David, 10 Oct): a catalogue `vendor_allowlist` with `severity: allow` (DMA-230/PL230 files, reason recorded) or `severity: warn` (every other Arm-banner tree); a `warn` item makes its gate WARN (amber, never FAIL, never hidden), an `allow` item is clean.** Tag `v0.1.0-mvp0` locally. | `pytest -m "not live"`: 0 failures. Anonymous live: B1 B2 B4 B6 F3 pass. | existing 740 |
| Tue | **S1 branch inventory** (`nanosoc-rm branches REPO [--all]`, CR8). Per branch: tip, ahead/behind default, merged?, last commit date, keep tags on tip, registered pins reaching into it (via stored walks), verdict `MERGED / PROTECTED / PINNED / STALE / ACTIVE`. `--delete-list` prints only `MERGED` and `STALE` with no pin and no tag. Reads mirrors only. | On a hermetic 5-branch fixture every verdict occurs; the delete-list never names a pinned or tagged branch (mutation: drop the pin check → test red). On eth repo: lists `test/cpu-ss-hsel-regression` as PINNED-by-registry once the bench is cited. | ~25 |
| Wed | **S2 compatibility check** (`nanosoc-rm compat CONFIG [--pin PATH=SHA]...`, CR9). Walk with overrides, then the T0 gates locally (reuse `t0.gates` with `Context.for_tests` → a `local` mode that is allowed off CI and marks `runner.hardened` NOT-RUN). One verdict per pin plus the run roll-up. | Hermetic: an override to a nonexistent SHA → FAIL at reachable; a duplicate component → B7 flag. Live: `compat eth-rc4` reproduces the demo's raw roll-up; `compat eth-main --pin nanosoc_gen=036d381` resolves. | ~20 |
| Thu | **S3 local T1/T2** (`compat --tier t1|t2`). A per-SoC `tiers.yaml` in the registry names the commands (D1 already has the table); the tool runs them in a sandbox checkout of the resolved tree with the overrides applied, 1 VCS seat, budget enforced, verdicts.tsv out. No regeneration without `NANOSOC_GEN_OK=1` (set_env.sh is never sourced). | Hermetic: a fake tier command that exits 1 → FAIL; over budget → `over_budget`. Live, one run: eth-main T1 (lint) on the login server. | ~15 |
| Fri | **S4 hand-over.** `docs/MVP.md`: the two workflows, copy-paste commands, what the verdicts mean. Local tag `v0.1.0`. David runs both workflows on a repo of his choice. | David's run gives the right answer for one branch he knows is dead and one he knows is pinned. | — |

Total new code: about 600 lines plus tests. If Mon slips (CR answers late), Tue–Thu still run; S0 only
fixes tests.

## Gates between phases (unchanged, from `docs/phase-gates.yaml`)

| Phase | Starts when | Delivers |
|---|---|---|
| 1 remainder (gate 3) | N1 done: GitLab registry + compat-ci projects, hardened runner, service VM, tokens | nightly T0, web dashboard, Run button, live ATs |
| 2 | generator deterministic (two identical regenerations); runner ephemeral 7 nights; CPU-budgeted host | matrix D3, provenance D4, flaky handling D8, release store D12 |
| 3 | matrix nightly for 7 nights | SemVer + change classes C1–C12, tool releases G1 G5 G6 |
| 4 | OPS items closed; classifier running | bump bot in shadow mode E1–E7 |
| 5 | Phase 3 accepted | SoC releases, lock files, errata revisions |

## Decisions this plan needs (answer on the CR page or here)

1. CR2–CR5 (test and golden fixes): yes/no. Without them S0 cannot reach 0 failures.
2. CR8 branches, CR9 compat: agree the two new commands as v1 additions.
3. U1 decided 2026-10-10: DMA-230 OK; other Arm-banner trees WARN (CR6a above). The repos stay public.
