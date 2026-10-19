# 26. Test capabilities per tree, test-bump, the bump gate and the executor queue (Wave G; D1, D3, E3, PR6)

Section 26 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Requirements: D1, D3, E3, PR6 ([history](../archive/interfaces-history.md#section-26-test-capabilities-per-tree)). Rules: (1) tier runs execute on an EXECUTOR (a pull agent on the EDA host,
sandboxed and unprivileged; GitLab CI later), never on the service host; (2) each tree declares its test
capabilities IN THE REPO, read at the commit under test; the registry's `tiers.yaml` keeps only host site data;
(3) hooks on a submodule pointer change WARN locally and BLOCK in CI and in the release manager's bump gate.

## 26.1 `.nanosoc/capabilities.yaml` (schemas/capabilities.schema.json)

At each repo's root. `schema: 1`, `component` (the catalogue id), `env` (`provides` tree-relative values with
`${TREE}` = this repo's root; `site` names the per-host variables, each `optional` or not, `arm_ip`/`pdk` flags;
`siblings` names other components found where the superproject pins them), `tools` (name -> `probe`, `min`,
`licence`, `seat`), `capabilities` (each: `id`, `kind` lint|elab|sim|synth|fpga|formal|gls|cdc|lec|hw|static|unit,
`tier` t0..t6, `run` (a shell string or argv; never sources a set_env.sh), `cwd`, `tools`, `budget_s`, `runtime`
S|M|L|XL, `needs`, `inputs`, `outputs`, `affected_by` (gitlink paths or component ids whose move re-runs it),
`regenerates`, `verdict` (`exit`, `forbid` text, `require` text with `min`, `rows` {format verdicts-tsv | junit |
regex | regress-table | tidelink-sim-gate, `path`, `fail`}), `control`, `gate` block|report, `when`
on_bump|nightly|release|manual, `manual`, `from`, `waiver`), `compose` (each: `path` a gitlink of this repo, `run` ids
of THAT repo's own file, `when`). The wave-G spelling `tiers`/`command`/`workdir`/`composes` is accepted for
`capabilities`/`run`/`cwd`/`compose`, one per document or entry. Examples: `capabilities.eth-chiplet.example.yaml`,
`capabilities.tidelink.example.yaml`, `capabilities.wave-g-spelling.example.yaml`.

`capabilities.manifest`: `parse(text)` checks the schema, then: ids unique, every named tool declared, `provides`
never sets TREE/PATH/HOME/TMPDIR/NANOSOC_*, compose paths unique; any problem refuses the WHOLE file (no partial set).
Defaults: `cwd` ".", `gate` block, `when` [on_bump], `budget_s` from `runtime` (S 60, M 900, L 10800, XL 43200; else
900), `runtime` from `budget_s`. `Reader(config).at(component, commit)` reads
`git -C <mirror> cat-file -p <commit>:.nanosoc/capabilities.yaml` (a partial mirror fetches the blob from its
promisor, with the host's read credential), never a working tree: status ok | absent | invalid (with problems; also
when the file names another component) | unreadable (no mirror, the commit not in it). Settled reads (ok, absent,
invalid) are cached per (mirror, commit) across requests: a commit's content never changes.

## 26.2 Site data: registry `tiers.yaml` `hosts` (eligibility#/$defs/tiers_file)

`hosts.<host id>`: `tools` (name -> `version`, `bin`), `licences`, `env` (values of the trees' `env.site` names;
never PATH, HOME, TMPDIR or a tool variable), `needs` (pdk, network, board:<kind>). Host ids, never hostnames.
`Registry.sites()`. The CR-12 `configurations` block stays for `compat --tier` until each tree carries a file.
`capabilities.plan.host_check(cap, tools, site)` lists why a host cannot run a capability: a tool it lacks, a version
under `min` (compared part by part: numbers as numbers), a licence it lacks, a need it cannot meet (`<tool>-seat`
is met by the tool; `regen:`, `after:`, `lease:` and `siblings` are run-time needs, not host ones).

## 26.3 Composition, the bump run set, `nanosoc-rm test-bump`

- **Composition** (`plan.composed`): the root file's capabilities, plus each `compose` entry's ids from the pinned
  submodule's own file at its pinned commit, recursively. Each item keeps its owner (tree path): it runs in that
  directory with that file's `env`. A compose entry naming an unpinned path or an undeclared id is a problem line.
- **Run set of a move** (`plan.plan_bump`) of every tree path M of one component from A to B: (1) the moved tree's own
  gates: the ids its nearest ancestor composes for it (when on_bump), else its own `gate: block`, non-manual,
  on_bump capabilities; (2) every ancestor capability whose `affected_by` names the moved path (relative to that
  ancestor, or a path above it) or the component id; never `manual`. Read at A for Now, at B for After.
- **`test_bump(configuration, path, to)`** (`capabilities.run`; CLI `nanosoc-rm test-bump CONFIG PATH TO [--host H]
  [--plan] [--queue] [--json]`): TO resolves in the component's mirror (full sha; tag, then branch, canonical repo
  first; a unique 7+ hex prefix), the move applies to every tree path of the component, both trees are walked
  (never stored), the plan is built, then Now and After each run `compat` (T0, locally; `compat(on_tree=...)` hands
  the gates' git, root pin and work dir to the caller after the roll-up) and every planned capability of that side on
  the same laid-out tree (`tierrun.materialise`, `tierrun.run_tier`): PATH = the host's tool `bin`s first; the
  site values; siblings at `${TREE}/<path>`; `provides` with `${TREE}` = the owner's directory. A host that cannot run
  it: INFRA, not run; a regenerating one without NANOSOC_GEN_OK=1: NOT-RUN. Verdict rows beyond the exit status:
  `forbid` (a FAIL row `tier.<label>.forbid`), `require`, junit, regex; regress-table and tidelink-sim-gate have no
  parser yet and give one UNVERIFIED row.
- **Compare** per row: same, better, worse (green -> FAIL), same-red, new-red (a red After whose Now gave no PASS,
  WARN or FAIL), new, gone (only when After's manifest reads ok; a capability lost to an absent, invalid or
  unreadable After manifest is not-run), not-run. Functional verdict over the block rows: worse FAIL > new-red/not-run UNVERIFIED > same-red
  WARN > PASS; no block row at all is UNVERIFIED; Now and After on different root commits is UNVERIFIED. The bump
  gate lets PASS and WARN land (section 26.4). T0 per side is `hygiene`, reported and never hiding the rows
  (decision 4). Exit 0 PASS or WARN, 1 FAIL, 2 UNVERIFIED, 3 usage.
- The document (schemas/test-bump.schema.json, kind `test_bump`) is the job's result, never a stored fact.

## 26.4 The bump gate (`capabilities.gate`)

`evidence(config, configuration, tree_paths, to, root_commit)`: the newest finished `test-bump` job whose result
tested exactly this move (configuration, root commit = the walk's, moved commit, a superset of the tree paths;
not `--plan`). A PASS or WARN document whose job exited non-zero counts as UNVERIFIED (never greener than the exit
code). `bump_proposal.fact.test_bump` records it (bump-proposal schema, optional), with `configuration`.

`lands(ev)`: PASS and WARN land; FAIL, UNVERIFIED and no evidence hold (CR-WH-already-red, David 2026-10-19, D12).
A WARN means every red block gate was red before the move and is still red after it (`same-red`), so the move
makes nothing worse: it does NOT hold the bump. Its evidence `note` reads "already red before the bump: <row keys>
... still red after it, so it does not hold the bump", and the proposal's second blocker line is
`already_red(ev)` = "bump gate: WARN (job N): <note>" (advice for a person, not a hold). A new red still holds:
green to red is FAIL, and a red After whose Now did not give PASS, WARN or FAIL (it did not run, or there was no
Now) is `new-red`, UNVERIFIED: an unknown baseline is never "already red". Anything that does not land makes
`decision.would` = hold with the second blocker "bump gate: ..." naming the `nanosoc-rm test-bump` command; the E2
ledger then scores the proposal `held`, as for any held proposal. The proposal page shows a "Test this bump" row
(hook `data-bumps="test-bump"`: verdict pill, job link, note) and `nanosoc-rm bumps show` a `test-bump` line.

**Any configuration (D14, CR-WI-d14, David 2026-10-19).** A NEW red for the same move (FAIL: a block gate green
before, red after) in ANY registered configuration of the component holds the bump, even when another
configuration passes. `siblings(config, configuration)` is that configuration first, then every other registered
configuration with the same `component`; `new_red(config, configurations, tree_paths, to, root_commit)` is the
first (configuration, evidence) among them whose `evidence` is FAIL, else None. The bump engine reads its own
configuration's evidence as above; unless that is already FAIL, it asks `new_red` of the siblings, and a hit
replaces `test_bump` with `broke_in(...)`: the sibling's evidence, `configuration` = the sibling, `note` "a new red
in configuration X holds the bump (D14) although <own> is <verdict>: ...". `blocker` then names the sibling's
`nanosoc-rm test-bump X PATH TO`. Only FAIL counts: a WARN, UNVERIFIED or untested sibling changes nothing, so
WARN in one and PASS in another lands, and the proposal's own UNVERIFIED or missing evidence holds as before.

## 26.5 The executor queue (`jobs`, section 13a)

`jobs.needs_executor(kind, params)`: a compat job with a tier, a matrix cell with a tier, every `test-bump`
(`register_kind(..., executor=True | callable)`). The local worker never claims one unless the local fallback is
on (`[executor] local_fallback = true`, or NANOSOC_RM_LOCAL_TIERS=1 in the worker's environment, or
`Worker(run_tiers=True)`; the hermetic test suite sets the variable). `Worker(only=ids)` claims just those job ids:
`matrix --run --wait` works only the cells it queued, never another person's queued tier job (integration review
2026-10-16). A direct `nanosoc-rm test-bump` never takes its host from a SERVICE config (one that lists
`[[executor.agents]]`): there `[executor] host` is ignored and an explicit `--host` is refused (exit 3) unless the
local fallback is on or the agent set `NANOSOC_RM_EXECUTOR_HOST`. `claim_for_executor(config, executor,
accepts=None)` claims the oldest queued tier job (the job records `executor`; `local` is reserved);
`finish_for_executor(config, job_id, executor, rc, stdout, log)` records its end exactly as the local worker reads a
child (`_outcome`); a cancel asked while it ran ends it `cancelled`. `recover()` at service start leaves an
executor's running job alone. `executor_queue(config)` lists what waits. jobs.sqlite user_version 2 adds the
`executor` column once. The executor process itself (the pull agent) is a separate Wave G stream; it consumes
these three calls and runs `nanosoc-rm <cli_args>` with `NANOSOC_RM_EXECUTOR_HOST` set to its host id.

## 26.6 Pages

| Route | Shows or does | Module |
|---|---|---|
| `GET /capabilities/` | every configuration: trees declaring capabilities; registry hosts; tier jobs waiting for an executor | `capabilities.web` (plug-in, no nav entry) |
| `GET /capabilities/{configuration}` | per tree of the latest stored walk: file status at the pinned commit, each capability (kind, tier, gate, runtime, command, tools, hosts that can run it and why not the others, last test-bump result at that commit) | `capabilities.web`, `capabilities.view` |
| `GET /capabilities/{configuration}/bump?path=[&to=]` / `?job=N` | the Test this bump form (TO defaults to the pin's default branch) and earlier tests / one job's Now vs After rows, hygiene, functional verdict | `capabilities.web` |
| `POST /capabilities/{configuration}/test-bump` | fields `path`, `to`; queues one `test-bump` job as the login (local deployments only; CSRF); 303 to `?job=N&queued=1` | `capabilities.web` |

Home's detail panel (section 25) gains a Capabilities section (what that tree can run, on which host, its last
result) for a pin or a configuration, and, for a live pin in a local deployment, a "Test this bump (to <default
branch>)" form posting to the route above. The submodule tree's pin aside links "Test this bump" for a live pin.
Every GET reads stored facts, mirrors, the registry and job records only, and never fetches: the pages read
manifests with `Reader(config, fetch=False)` (lazy fetching off, every transport refused, no credential), so a
blob the partial-clone mirror lacks reads `unreadable` until `capabilities show` or a test-bump reads it
(integration review 2026-10-16).

## 26.7 `GET /api/v1/bump-check` (the repositories' CI and pre-push hook; integration review 2026-10-16)

Query `component` (catalogue id of the asking repository), `path` (the gitlink's tree path), `to` (40 hex),
`root` (40 hex: the asking repository's commit BEFORE the move, a CI range's base), optional `from`. Answer 200
`{verdict, url, command, configurations, configuration, note}` (`configuration`: the one whose evidence answers,
null when none does): for each registered configuration of `component`,
`gate.evidence(config, configuration, [path], to, root)`. A FAIL (new red) in ANY one answers FAIL, even when
another passes (D14, section 26.4), with `configuration` that one, its job page, the `nanosoc-rm test-bump` command
for it, and a `note` naming it and what each other configuration gave; otherwise a PASS in any one answers PASS
with its job page; otherwise a WARN (already red before the bump, section 26.4) answers WARN with its job page, `command` null and the
"already red before the bump" `note`; a caller lets the move land on PASS or WARN; otherwise the first other
verdict found, or null with a `note` (none finished, other root commits, no configuration of that component). `command` is the `nanosoc-rm test-bump CONFIG PATH TO` to run. 400 a malformed
parameter. Who may ask: a signed-in member (session), or a bearer READER token: `[[api.readers]]` in the service
config (`name`, `token_sha256`, optional `revoked`; `nanosoc-rm executor token new NAME --out F --reader`). The
Guard answers a bearer request for this one path before its session check (`capabilities.bumpcheck`); a reader
token opens no other route and an executor token does not open this one (the two digest lists are separate, and
config load refuses a digest in both). Read-only: job records and the registry; nothing runs, queues or fetches.
