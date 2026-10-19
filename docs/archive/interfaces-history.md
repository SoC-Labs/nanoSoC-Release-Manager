# interfaces.md history

Rationale, decision records and superseded wording moved out of
[contracts/interfaces.md](../contracts/interfaces.md) so that page holds only the contracts in force.
Nothing here is a contract or an instruction; where it disagrees with interfaces.md, the schemas or the code,
they are right. Each heading names the interfaces.md section it came from, and that section links here.
Who agreed each change and when is in [CHANGES.md](../CHANGES.md).

## Section 2. Registry loader and validator

K1 (CR-K1, why): `ignored` keeps a repo out of the inventory's "unclassified" list and out of the mirrors; a
registered tree that pins it makes it a dependency, so it is flagged to be classified, never hidden.

## Section 3. Mirror and poller

Why one object store per component: keeping both hosts in one object store is what makes B6 answerable.
GitHub's compare API returns 404 for the other host's head (README section 7, fact 4).

Why `--no-tags`: in a store holding both hosts, the first host fetched would own the bare tag names
(reproduced with git 2.43, scope_web gap G19).

Why `include_subgroups=true`: 71 top-level GitLab projects against 101 with subgroups (2026-09-30).

## Section 7. Silicon-protection list

The golden as re-derived under CR-2 (2026-10-10), verbatim:

- **Golden (`observation.example.jsonl`; CR-2, re-derived 2026-10-10):** on the example catalogue eth-rc4 gives 13 rows. The 11 pinned-host rows are all covered by `keep/eth-rc4/*` (the B5 done-when). The 2 canonical-host rows are covered since David's 2026-10-01 GitHub tags: nanosoc_arch_tech `ecf76aa` by `keep/eth-rc4/nanosoc_arch_tech`, ahb_qspi `e908ac8` by `keep/eth-rc4/ahb_qspi`. On the live registry, DEC6 also makes ethernet-mac-ahb, hostio4 and socdebug_tech canonical on GitHub, which adds 3 rows (16 in all): `44ff8b7` and `50359ee` MISSING on GitHub, `51348de` BRANCH_ONLY there (decision R1). `--uncovered` exits 1 until those 3 are pushed and tagged; with only the example catalogue it exits 0.

## Section 9. T0: definition and runner

The `t0.infra.probe` gate serves D7, a Phase 2 requirement; CR-5 took D7 off the Requirements line of sections 9
and 10 so v1 commits do not name it.

## Section 11. Bundle and tree scanner

Why `arm.banner`: the toolkit scanner has no such rule, and D11's done-when plants exactly this banner.

## Section 14b. Versions

Removed as a duplicate of section 13a ("POST routes of plug-ins"): "A plug-in's `router` factory may take the
app (`plugins.load`, additive) so it registers POST routes only when `app.state.rm.local_routes`."

## Section 17.3 CLI commands

The index as it stood, with each group's requirement and CR, verbatim:

`nanosoc-rm quality density|comments|readme|docs|all|trend` (CR-14, Q1-Q6): section 14a.

`nanosoc-rm matrix CONFIG|cell|quarantine|nightly` (D3, D4, D7, D8, D9, B7; built per CR-15): section 18.
`nanosoc-rm changelog lint`, `nanosoc-rm version next|tags|refresh|draft|approve|yank|fixed-in`, `nanosoc-rm manifest
init` (CR-VER, C1-C12, F6, G1, G5, G6): section 14b.
`nanosoc-rm bumps [CONFIG]|show|patch|mark|ledger` (E1-E7 shadow mode; built per CR-15): section 19.
`nanosoc-rm release draft|verify|list` and `nanosoc-rm forks` (CR-R1; G2, G3, G4, G7, F4, I1, I2, built under
CR-15): section 14c.
`nanosoc-rm regen run|class|pending` (C3 3/3, C7; built per CR-15): section 20.
`nanosoc-rm store index|show|search|findings` (B10, G8, D12; CR-WE-index): section 22.
`nanosoc-rm freshness scan|show` (B2, B3, H1; CR-WF-fresh): section 24.
`nanosoc-rm catalogue show|org-scan|preview` (A1, B1, H1; CR-WF-catalogue): section 23.
`nanosoc-rm executor run|check|token new|enqueue|list|show|cancel` (CR-WG-executor): section 27.7.
`nanosoc-rm store-maint compact --dry-run|--apply [--json]` (A3; CR-1015-retention): section 12; exit 3 unless exactly
one of the two flags is given. An operator command: the service stopped, the store backed up (DEPLOY.md).

## Section 18. Compatibility matrix

What CR-WD-matrix-t1 added to the schemas (additive, `schema` stayed 1), verbatim:

   - **Schema** (additive, `schema` stays 1): `matrix.schema.json` `$defs/tier`, `$defs/tier_run`, `$defs/ip_read`;
     optional `attempt.tier|checkout|ip`, `matrix_cell.tier|force_tier`, `provenance.checkout`; `cell_view.status`
     gains `no-tool`, `no-command` and optional `reason`; `matrix_view` optional `tiers`, `pinned_tiers`,
     `rows[].tier_cells`. `eligibility.schema.json` `tier_command.ip_files`. Examples
     `matrix.cell-t1.example.json`, invalid `matrix.cell-t1-checkout-moved-fails.yaml`.

## Section 23. Catalogue characterisation

The section's opening, verbatim (the plate / meat / potatoes picture):

The front page groups the catalogue as a ladder: a **project** (the
top-level repo that builds a chip or a chiplet: "the plate") wraps a **SoC** ("the meat"), which integrates
**subsystems** and **components** ("the potatoes"), built from **arch** tech units and **generators**; **toolkits**,
**flows**, technology **libs**, **infra** and **legacy** sit beside it. This section is the data model and the two
read paths the GUI unit builds on. Nothing here writes the registry. Package `nanosoc_rm.catalogue`.

CR-1015-orgscan was David's 2026-10-15 answer "Nightly + Scan button".

## Section 24. Submodule-tree freshness

The ask behind it (David 2026-10-14): "a very clear indication of whether a submodule tree is outdated or not and
how long since it's been updated last".

Measured cost of the scan: four eth/multicore/quickstart trees (140 pins), 4.1 s on the service host.

CR-1015-retire (David 2026-10-15, "Retire it"): the `/freshness/` page duplicated Home and the Matrix's
Freshness tab.

## Section 25. The front pages

CR-WF-gui reversed CR-W1, which had put the inbox at `/`.

## Section 26. Test capabilities per tree

The decision record, verbatim:

Requirements: D1, D3, E3, PR6. Agreed: David 2026-10-16 (interactive): evaluation fix wave, decisions 1 to 4:
(1) tier runs execute on an EXECUTOR (a pull agent on the EDA host, sandboxed and unprivileged; GitLab CI later),
never on the service host; (2) each tree declares its test capabilities IN THE REPO, read at the commit under test;
the registry's `tiers.yaml` keeps only host site data; (3) hooks on a submodule pointer change WARN locally and
BLOCK in CI and in the release manager's bump gate; (4) all four streams built now.

The eth-chiplet and tidelink capability examples began as the 2026-10-16 evaluation's drafts.

## Section 27. The remote tier executor

The field lists of `$defs/task`, `$defs/claim` and `$defs/result` as this page restated them (the schema is the
contract; section 0 says this page never redefines a data shape), verbatim:

- **task** (`$defs/task`): `subject {configuration|null, component, capability, side?, pair?}`, `mirror` (catalogue
  entry id: `<mirror_root>/<id>.git` on the agent, the section 3 layout), `commit` (40 hex), `pins [{path, mirror,
  commit}]` laid out inside the root, `run {tier t1..t6, command (shell string or argv), cwd?, env?, budget_s,
  verdict {exit = 0, forbid [...], verdicts?}, control?}`, `labels` (every one must be granted to the claiming
  agent), `requested_by`, `reason?`.

- **claim** (`$defs/claim`, `type: task`): `{task_id, lease (32 hex), nonce (32 hex), lease_seconds, task}`.
- **result** (`$defs/result`): `{schema, task_id, nonce, executor, sandbox bwrap|fallback|none, started_at,
  finished_at, verdict PASS|FAIL|UNVERIFIED|INFRA|WARN, reason, exit_code, killed, seconds, over_budget, rows
  [verdict_row], log {bytes, sha256, truncated, uploaded}, log_tail, checkout {laid_out, missing}}`.
