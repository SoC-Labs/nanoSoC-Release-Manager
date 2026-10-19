# 19. Bump proposals in shadow mode (Phase 4 E1-E7; built per CR-15)

Section 19 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Requirements: E1, E2, E3, E5, E7 (Phase 4), H3; CR-15 and CR-WB-bumps (`docs/CHANGES.md`). Built and usable by
hand now; ENABLING it (a schedule, a bot that writes) stays behind the Phase 4 entry gate in
`docs/phase-gates.yaml`. Package `nanosoc_rm.bumps`; schema `schemas/bump-proposal.schema.json` (new file, additive).

**What it never does.** It never opens a pull request, pushes, writes a branch or tag, or calls a host's write API.
There is no bump writer credential (H3); a `[credentials.bump_writer]` or `[credentials.bot_tag]` in the config is
reported as a problem (exit 1) and never read. Every git call is a read through `gitrun.run_git` (no new
`WRITE_CALLS` entry). A proposal is a patch and commands for a person; every page and CLI text says so.

## 19.1 Functions (module paths)

| Name | Where |
|---|---|
| `proposals(configuration=None, *, config=None, store=False) -> Answer` (bump_scan first, then bump_proposal, deepest first) | `nanosoc_rm.bumps` |
| `stored(configuration=None)`, `scans()`, `get(id)`, `mark(id, login, decision, note=None)`, `marks(id=None)` | `nanosoc_rm.bumps` |
| `ledger(*, config=None) -> bump_ledger`, `schedule_status() -> dict`, `writer_problems(config)` | `nanosoc_rm.bumps` |
| `patch_text(fact)`, `commit_message(fact)`, `person_commands(fact)` | `nanosoc_rm.bumps` (`.patchfile`) |
| `scan`, `scan_all`, `proposal_id`, `BumpUsage` (exit 3), `CannotAnswer` (exit 2) | `nanosoc_rm.bumps.engine` |
| `parse_tag`, `allows`, `Version`, `RangeError` | `nanosoc_rm.bumps.versions` |
| `router` (plug-in), `job_params`/`job_args` (job kind) | `nanosoc_rm.bumps.web`, `nanosoc_rm.bumps.jobkind` |

## 19.2 The scan (E1, E3, E5)

Inputs, all read: the configuration's latest stored walk (section 4; never re-walked), the component mirrors
(section 3), the registry (catalogue, eligibility, defects) and the local job records. Per pin of the walk:

1. **Refusals first.** A frozen or silicon configuration is never proposed: its scan has `eligible: false`, a
   `refused` reason and no rows (E3, E5; the schema refuses a refused scan with a proposal).
2. **Consumer branch.** The configuration's own branch for its root (a configuration that follows a tag has none);
   else the consumer repo's default branch as the mirror recorded it. `release/*` is never a target (E5). The
   gitlink is read at that branch's tip (`git ls-tree <tip> -- <path>`), so the patch applies to the branch now.
   The same gitlink in the same consumer branch reached through two tree paths is one proposal (`duplicate` rows).
3. **Release tags.** Tags of every repo of the dependency in its mirror whose name is a release: `vX.Y.Z`,
   `X.Y.Z`, or the grandfathered `vX.Y` (C10; read as X.Y.0, marked `grandfathered`). A pre-release suffix
   (`-rc.1`, `-chiplet-verified`) is never proposed. The canonical repo's tag wins a name clash. **Newer** = the
   tag's commit is not already in the pin (`merge-base --is-ancestor`) and its version is above the newest release
   the pin contains (`from.tag`, `from.exact` when the tag names the pin itself).
4. **Range (C11).** When the consumer's `nanosoc-component.yaml` at the branch tip declares a `requires[].range`
   for that path (or that component), the proposal is the newest release the range allows; the newest overall is
   `newest`. Ranges are Cargo style (`^`, `~`, `=`, `>`, `>=`, `<`, `<=`, `X.Y.*`, `*`, bare = caret); a range that
   does not parse holds the pin with the problem named, never a guess. No manifest or no range (CR-WB-bumps-review2):
   the engine assumes the pin's major version (`range.assumed`, optional), so a release across a major version is
   held (status `held-by-range`), and a change that declares itself `breaking` is `blocked` (would `hold`).
5. **Change.** The changelog (CHANGELOG.md, CHANGELOG, CHANGES.md, HISTORY.md at the tag) sections in
   (from version, to version]; the class each declares (`Class: fix` line, else `### Breaking|Added|Fixed`
   headings), the most severe of them, `unknown` when any section declares nothing (C2: never from the version
   number). The versioning module's classifier replaces it when built (18.6). The commits `from..to` (count and
   20 subjects); `diverged` when the pin is not an ancestor of the tag.
6. **Defects (registry).** `introduced`: an `origin` commit of the dependency that the tag contains and the pin
   does not; `fixed`: a `fixed_in` commit likewise, with its forms. A defect whose origin AND every form's fix
   both arrive with the move is `within_range` (optional field): the pin never had it and the tag
   has the fix, so it is neither fixed nor introduced. A commit the mirror lacks is `unjudged`.
7. **Matrix cell (T0-T2).** The matrix module's stored cell for exactly this move (18.6), else the finished local
   compat what-if jobs whose `--pin` list is exactly `<tree path>=<to sha>` for every tree path of the move AND
   whose result ran on the walked tree (`compat_result.fact.commit` is the walk's root commit; another tree, or
   no result, is stale and named in `matrix.note`). The newest such job per tier fills `matrix.tiers` (optional
   field: `t0` = no `--tier`, `t1`, `t2`). A `--tier` what-if runs T0 first and its verdict is the
   ladder over both, so the cell is PASS only when a t1 and a t2 what-if both PASS; any non-PASS tier makes the
   cell that verdict; otherwise it is null (incomplete). A T0-only PASS is never the cell. The page offers one
   Run per missing tier (a POST that queues that compat job with `tier`).
8. **Reachability.** The tag's commit must be reachable on the repo the consumer's .gitmodules names; else the
   proposal is `blocked` (the bot would hold; a person moves the URL or pushes the tag first).
9. **Order (E1).** Deepest first: `order` 1 is applied first; `waits_for` lists proposals inside the dependency
   itself (they land and are tagged before this one).
10. **Decision.** Always `mode: shadow`, `would: open-pull-request` (or `hold` when blocked), `auto_merge: false`,
    `goes_to: person`, with `blockers`: shadow mode first, then every E3 condition the tool cannot show holds:
    no T0-T2 PASS cell; the configuration does not pin the consumer at the branch tip (not the
    exact commits); controls fired and no test green to red (not checked yet: always listed); class not fix or
    feature; the source not a release tag; the target not the consumer repo's default branch (or its default
    unknown); E5: a GitHub consumer whose repo_inventory visibility is not `public` (or no inventory); diverged;
    the pin is `split` in the walk; a workaround in the consumer manifest whose `retires_when` commit arrives
    with the move; part of a train (`waits_for`, or another proposal waits for it); a defect introduced; and
    build-affecting unknown until C7 exists. Frozen and silicon configurations and release/* branches never get
    this far (refused).
11. **Patch.** Gitlink move only (`regenerated: false`): E1's regenerated RTL needs the generator, which this tool
    never runs; the note says so.

**Proposal id:** `bp-` + the first 12 hex of sha1(`<consumer host>:<path>|<branch>|<gitlink path>|<from>|<to>`):
the same move has the same id in every configuration and every run.

## 19.3 The patch (`<id>.diff`)

Plain text first (the proposal, the commands for a person, a suggested commit message), then one
`diff --git a/<path> b/<path>` / `index <from>..<to> 160000` / `-Subproject commit <from>` / `+Subproject commit
<to>`. A person applies it with `git apply --index` (a gitlink lives only in the index, so a plain `git apply`
changes nothing when the submodule is not checked out). It does not apply to a branch that moved the gitlink.

## 19.4 Records: `<data_dir>/bumps.sqlite`

Not rebuildable, not facts, so apart from `rm.sqlite` (as `jobs.sqlite` is): `proposals` (id, first and last time
computed, the configurations, the latest bump_proposal observation), `proposal_docs` (the latest bump_proposal
per proposal AND configuration: one move proposed for two configurations keeps both documents, so
each configuration's tree paths, order, waits_for and matrix pins stay its own), `marks` (proposal, at, login,
decision `done` or `declined`, note: every mark is a write action logged with its login, H2), `scans` (the latest
bump_scan per configuration) and `ledger` (the latest bump_ledger and when it was computed). The bump kinds are not in `db.EVENT_KINDS`, so `/facts/<subject>` does not serve them;
`/bumps/facts/<subject>` does, with the same hooks.

## 19.5 The shadow ledger (E2)

Kept proposals are grouped per gitlink opportunity (consumer repo, branch, path, `from`) and judged on the consumer
branch's HISTORY (CR-WB-bumps-review2): the first commit since the proposal's base (`git log --first-parent
--reverse base..tip -- path`) whose gitlink is not `from` is ONE person decision, and later moves never change it.
**agreed** (that move pinned exactly this proposal's commit; `person.commit` is the commit), **disagreed** (it pinned
something else that is not a newer release containing the bot's latest proposal, or, with no move yet, the latest
proposal was `declined`), **pending** (no move yet; a `done` mark the mirror does not show yet stays pending with a
note), **superseded** (a newer proposal for the same gitlink, another proposal of the group decides the move, or the
move went to a newer release containing it), **held** (optional value: the bot would hold this proposal, so it is
never scored), **unknown** (the mirror cannot answer). Threshold: 4 weeks since the first proposal AND 20 decided
(agreed + disagreed), whichever is later; then auto-merge may be PROPOSED for a class (fix, feature) with zero
disagreements. Switching it on is a registry change a person makes after the Phase 4 gate; `auto_merge.enabled`
is always false here and a non-empty `eligibility.yaml auto_merge` is a problem. `observed_at` is the oldest
consumer mirror sync it read. The ledger reads the mirrors, so it is computed only by the CLI or a job (`bumps
scan --store` keeps it after the scan, `bumps ledger --store` alone) and the pages show the kept one with the
time it was computed: no git and no ledger computation inside a request.

## 19.6 Interfaces this module needs from other modules

Looked up at run time (`nanosoc_rm.bumps.interfaces`); a module not built here is not an error and the fallback
is named in the fact (`class_source`, `matrix.source`). Both providers exist (CR-WB-int):
`matrix.bump_cell` answers only the newest stored cell whose overrides are exactly the move (FLAKY reads as
UNVERIFIED), and `versioning.classify_bump` returns None when no C3 signal gives a class:

| Provider | Function | Fallback |
|---|---|---|
| versioning (C2, C3) | `nanosoc_rm.versioning.classify_bump(component, from_commit, to_commit, *, config) -> {"class", "detail", "sources"} \| None` | the changelog's declared class, else `unknown` |
| matrix (D3) | `nanosoc_rm.matrix.bump_cell(configuration, overrides: {tree_path: sha}, *, config) -> {"verdict", "observed_at", "href", "detail"} \| None` | a finished compat what-if job with exactly those pins |

## 19.7 CLI and job

```
nanosoc-rm bumps [CONFIG] [--store] [--json]      # = bumps scan; every registered configuration without CONFIG
nanosoc-rm bumps queue [CONFIG] [--json]          # the kept proposals
nanosoc-rm bumps show ID [--configuration CONFIG] [--json]
nanosoc-rm bumps patch ID [--out FILE]            # stdout, or a NEW file (never overwritten: exit 3)
nanosoc-rm bumps mark ID done|declined --by LOGIN [--note TEXT]
nanosoc-rm bumps ledger [--store] [--json]       # --store keeps it for the pages
nanosoc-rm bumps schedule [--json]
```

`--json`: scan prints the bump_scan then the bump_proposal observations, one per line. Exit: 0 answered (a
proposal is work for a person, not a failure), 1 a problem finding (a bump writer credential configured, auto_merge
not empty), 2 could not answer (no stored walk, a registry that does not load), 3 usage (unknown configuration or
proposal, an existing `--out`, a bad mark decision).

**Job kind `bumps`** (`nanosoc_rm.jobs.register_kind`, section 13a; `nanosoc_rm.bumps.jobkind` registers
`job_params(params)` and `job_args(params)`): `nanosoc-rm bumps scan [CONFIG] --store --json` (params `{"configuration"}`), or `nanosoc-rm
bumps ledger --store --json` (params `{"ledger": true}`, queued once after a mark), in the local worker, so the
mirror reads (which may fetch blobs anonymously from a partial mirror's promisor remote) never run inside a
request.

**Schedule:** ships DISABLED. `schedule_status()` is always `{"enabled": false, "gate": "Phase 4", "gate_status":
<docs/phase-gates.yaml status>}` (schema: `enabled` const false); `nanosoc_rm.schedule.JOBS` has no bump job.
Enabling needs the Phase 4 gate checked by a person and a CR that adds the job.

## 19.8 Pages (plug-in `nanosoc_rm.bumps.web`, navigation "Bumps")

| Route | Shows or does |
|---|---|
| `GET /bumps/?configuration=&state=&cls=&outcome=&component=&job=` | the queue (kept proposals with filters), per-configuration scans with pins by status, the ledger summary, the schedule banner (DISABLED, gate named), Scan now |
| `GET /bumps/p/{id}?configuration=&job=` | one proposal (that configuration's document when named): move, range, class, changelog, commits, defects, matrix cell with its tiers or one Run per missing tier (a compat what-if for exactly this move that is queued, running or finished after the scan is named with a rescan note instead of a second Run), waits-for, blockers, patch, commands for a person, marks |
| `GET /bumps/p/{id}/patch.diff` | the patch (`text/x-diff`, attachment `<id>.diff`) |
| `GET /bumps/ledger` | E2 ledger: outcomes, agreement, threshold, by class, auto-merge OFF |
| `GET /bumps/facts/{subject}` | one bump observation with its sources verbatim |
| `POST /bumps/scan` | queue the `bumps` job (field `configuration`, optional; an id that is not a registered configuration answers 400); an identical queued or running job is reused |
| `POST /bumps/p/{id}/mark` | "mark as done by a person" (`decision` done or declined, `note`, `configuration`), with the signed-in login; then queues one ledger refresh job |

The POSTs act only in a local deployment (the app's `local_routes`, CR-16/CR-17); elsewhere they answer 403 with the
CLI command, and the pages show the command instead of the buttons. Both are behind the section 14 guard and its
CSRF check. Colours: README section 6 and `web.page` classes, always with the text (`proposed` info, `blocked`
amber; outcomes agreed green, disagreed red, pending, superseded and held grey, unknown amber; class breaking red,
feature info, fix green, none grey, unknown amber). Row hooks: `data-proposal`, `data-state`, `data-outcome`,
`data-configuration`; page hooks `data-shadow`, `data-schedule`.

**Observation kinds** (observation.schema.json, enum values plus one `allOf` entry each; event kinds, no snapshot):

| kind | subject | fact |
|---|---|---|
| bump_proposal | `bump_proposal:<configuration>:<id>` | `bump-proposal.schema.json#/$defs/proposal` |
| bump_scan | `bump_scan:<configuration>` | `#/$defs/scan` |
| bump_ledger | `bump_ledger:all` | `#/$defs/ledger` |

Examples: `bump-proposal.example.yaml`, `observation.bumps.example.jsonl` (ILLUSTRATIVE: synthetic tags on real
commits, labelled); invalid: `bump-proposal.auto-merge-in-shadow-mode.yaml`, `.release-branch-target.yaml`,
`.claims-regenerated-rtl.yaml`, `.blocked-but-would-open-a-pr.yaml`, `observation.bump-scan-frozen-with-proposal.yaml`.
