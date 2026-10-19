# 24. Submodule-tree freshness (CR-WF-fresh; B2, B3, H1, A3)

Section 24 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

How far each pin of each registered configuration lags its upstream default branch, when its parent last moved
it, and one verdict per tree, for Home, the freshness grid and the detail panel ([history](../archive/interfaces-history.md#section-24-submodule-tree-freshness)). v1 monitoring, no
later-phase automation: the job and route names carry no LATER_AUTOMATION word.

## 24.1 The scan (`nanosoc_rm.freshness.scan`)

```python
def scan(configurations: list[str] | None = None, *, config=None, store: bool = True,
         today: datetime.date | None = None) -> list[dict]   # FreshnessUsage for an unregistered id (exit 3)
```

- **Inputs, read-only:** the latest stored walk per configuration (`walker.stored_pins`; nothing is re-walked),
  the catalogue (`mirror.catalogue.load_catalogue`), the latest `mirror_status` per repo, and the component
  mirrors through `gitrun` (`git -C <mirror> --no-optional-locks ...`): `rev-list -n1 --missing=print --no-walk`
  (is the commit here; never lazy-fetches), `log -1 --format=%cI`, `for-each-ref --contains` (every host's heads
  and tags), `rev-parse --verify` (the canonical default head), `rev-list --count A..B` both ways, `merge-base
  --is-ancestor`, and in the parent's mirror `log -1 --format=%H%x1f%cI%x1f%an%x1f%s <parent commit> -- <path>`
  and `rev-list --count <parent commit> -- <path>` (the bump). No network, no write outside the store. A pin
  shared by several trees is measured once.
- **Default branch:** the canonical repo's, as the mirror records it (`layout.default_branch`), else the stored
  walk's, else `main`/`master` if present.
- **`lower_bound`:** the canonical repo's latest `mirror_status` is not ok but its refs are still in the mirror:
  the counts are against the last good sync, so upstream may be further ahead.
- **`unpushed`:** no head or tag of any host of the entry holds the pin and every host's refs are from a good sync;
  null (not decided) when a host could not be read. **`not_measured`** (string) says why `behind` is null;
  **`caveat`** what was not measured although `behind` was.
- **Output:** one snapshot per configuration, scope `freshness:<configuration>`, holding a `level: tree` document
  (subject `freshness:<configuration>`) and one `level: pin` document per pin (subject
  `freshness:<configuration>:<first tree path>`). Facts: observation.schema.json `$defs/freshness_pin` and
  `$defs/freshness_tree`. `observed_at` is the oldest of the walk and the canonical repo's last good sync.
  A configuration with no stored walk gets only its tree document, `state: not_measured`, with the reason.
- **Stored `state`** is the judgement on `judged_on`. Pages re-judge (23.3): "not bumped for 90 days" moves with
  the calendar.

## 24.2 The rule (`nanosoc_rm.freshness.rules`, pure: no git, store or clock)

Per pin, first match wins: **frozen** (the configuration is `frozen` or `silicon`: `would_be` holds the ladder's
answer, never counted) > **unpushed** > **not_measured** (`behind` is null) > **current** (0 behind) >
**outdated** (`behind >= OUTDATED_BEHIND` (10), or behind and `days_since_bump >= OUTDATED_DAYS` (90)) >
**behind** (1 to 9, bumped within 90 days, or the bump date is unknown). A pin not on the default branch counts
by its commits behind like any other (the view flags it `diverged`, or `unmerged` with 0 behind).

Per tree (`judge_tree(frozen, pins, today)`): `state` = the worst DIRECT pin (depth 1) by `SEVERITY` = unpushed 4 >
outdated 3 > behind 2 > not_measured 1 > current 0, so a tree is Current only when every direct pin was measured
at its head; no pins = not_measured; a frozen tree is `frozen` with `would_be` = that verdict. CR-1015-unmeasured:
when that worst is `not_measured` and at least one direct pin is `current`, the
verdict is **`current_except`** ("Current except N not measured", `tree_word(state, direct_not_measured)`; amber, its
own shape on the pages); worse states (unpushed, outdated, behind) still win; a tree none of whose direct pins was
measured stays `not_measured`; a frozen tree stays `frozen` (its `would_be` may be `current_except`).
`direct_not_measured` = how many direct pins could not be measured. `current_except` is a tree verdict only
(`TREE_STATES` = `STATES` + it): no pin, node rollup or grid cell has it. `counts` per live
state over the whole tree; `lagging`/`direct_lagging` = unpushed + outdated + behind pins (0 when frozen;
`would_be_lagging`/`would_be_direct_lagging` keep a frozen tree's numbers); `total_behind`, `any_lower_bound`;
`newest_bump` / `newest_direct_bump` {tree_path, component, date, days}; `updated_days` = days since the newest
bump in the tree and `age` = fresh (< 30) / aging (30 to 89) / stale (>= 90); `stalest_lagging` (longest unbumped
lagging pin) and `worst_lagging` (most severe, then most behind). A frozen tree is old on purpose and never reads as
lagging: its `counts` are all 0, `age`, `stalest_lagging` and `worst_lagging` are null, and its live numbers sit
under `would_be_counts`, `would_be_stalest_lagging` and `would_be_worst_lagging` (null for a live tree);
`updated_days` stays as a fact. Days are whole UTC calendar days (`days_since(ts, today)`).

```python
STATES, LIVE_STATES, TREE_STATES, CURRENT_EXCEPT, WORDS, SEVERITY, LAGGING, OUTDATED_BEHIND, OUTDATED_DAYS,
FRESH_DAYS, STALE_DAYS, AGE_WORDS
def tree_word(state, direct_not_measured) -> str | None   # "Current except 2 not measured"; else WORDS[state]
def judge_pin(pin_fact: dict, today: date) -> dict   # {state, would_be, live, lagging, days_since_bump, pin_age_days}
def judge_tree(frozen: bool, pin_facts: list[dict], today: date) -> dict   # the tree fields above
def live_state(pin_fact, today) -> str;  def worst(states) -> str | None;  def age_word(days) -> str | None
def days_since(ts: str | None, today: date) -> int | None;  def day_of(ts) -> date | None
```

## 24.3 What pages call (`nanosoc_rm.freshness.view`; stored facts only)

Every function reads `db.latest("freshness")` and re-judges with `rules` on `today` (default: today, UTC). None
runs git, starts a job or touches the network (`test_no_view_function_runs_git`). The stored registry records
(`registry_record`, kept current by the pages' registry refresh) decide which configurations are listed and
whether each is frozen (frozen or silicon): a configuration dropped from `eligibility.yaml` leaves the pages, and
a newly frozen one reads Frozen, without waiting for a scan. With no registry record stored at all, every stored
scan is listed as stored. Each returned row carries the
`subject` and `observed_at` of its observation, so a template prints the section 13 hooks with `fact(row, label)`.

```python
def stored(*, config=None) -> dict[str, list[dict]]           # configuration -> [tree doc, pin docs by path]
def trees(*, today=None, config=None) -> list[dict]             # one summary per scanned tree, live first
def tree(configuration, *, today=None, config=None) -> {"tree": summary, "nodes": [node]} | None
def pin(configuration, tree_path, *, today=None, config=None) -> node + {configuration, same_pin_lags_in} | None
def grid(*, today=None, config=None, live_only=False) -> {"components", "configurations", "cells"}
def same_pin_lags(component, commit, exclude, all_docs, today) -> [configuration]
```

- **summary** = `judge_tree` fields + `configuration`, `frozen`, `silicon`, `root` {component, commit, date, days},
  `walk` {snapshot, observed_at}, `not_measured`, `scanned_on`, `word`, `would_be_word`, `age_word` (display words:
  "Outdated", "Not measured", "Current except 2 not measured" (`rules.tree_word`), "Fresh"...), `subject`,
  `observed_at`.
- **node** (one per tree path; a pin reached at two paths shows at both, `first_path` marks its own) = `tree_path`,
  `name`, `display_depth`, `parent_path`, `children` [tree paths], `component`, `catalogued_as`, `pinned`,
  `pin_date`, `behind`, `ahead`, `lower_bound`, `on_default`, `diverged`, `unmerged`, `reachable`, `unpushed`,
  `branches`, `branches_more`, `upstream` {repo, default_branch, head, head_date}, `bump` {commit, date, author,
  subject, total}, `not_measured`, `caveat`, `parent_component`, `parent_commit`, `frozen`, `judge_pin` fields
  (`state`, `would_be`, `live`, `lagging`, `days_since_bump`, `pin_age_days`), `word`, `would_be_word`, `rollup`
  (worst live state at or under it, for "a parent row shows the worst state beneath it"; `frozen` on every node
  of a frozen tree, which carries the live answer as `would_be_rollup`, null otherwise), `rollup_word`,
  `rollup_behind`, `subject`, `observed_at`. Nodes come depth first, siblings by path; grouping siblings by kind
  (SoC, custom peripherals, tooling) is the page's, from the catalogue.
- **`same_pin_lags_in`**: other LIVE configurations pinning the same component at the same commit and lagging on it.
- **grid cell** (component x configuration; the worst pin of that component in that tree) = `state`, `live`,
  `would_be`, `word`, `behind`, `lower_bound`, `days_since_bump`, `tree_path`, `pins` (how many pins of it the
  tree has), `counted` (false for a frozen tree), `subject`, `observed_at`. `components` puts the most lagging
  live rows first; `configurations` live first.

## 24.4 Job, schedule, CLI, route

- **Job kind** `freshness-scan` (`nanosoc_rm.freshness.jobkind`, in `jobs.KIND_MODULES`): params
  `{configuration: id | null}`; runs `nanosoc-rm freshness scan [CONFIG] --store --json`; outcome PASS, or
  UNVERIFIED when a tree had no stored walk; result `{trees: {id: state}, pins, not_measured}`; ceiling 900 s.
- **Scheduler** job `freshness_scan` (every `poll_minutes`, immediately after `poll`, so after its walks): queues one
  `freshness-scan` job unless one is queued or running. Not a CI job: it runs in a local deployment too.
- **CLI:** `nanosoc-rm freshness scan [CONFIG]... [--store] [--today YYYY-MM-DD] [--json]` (exit 0 answered, 2 a tree
  has no stored walk, 3 unknown configuration or bad date; an outdated tree is not exit 1: the scan measures, it
  does not gate). `nanosoc-rm freshness show [CONFIG] [--tree] [--today] [--json]` prints the stored facts
  re-judged (exit 2 when none is stored).
- **Routes** (plug-in `nanosoc_rm.freshness.web`; no navigation entry, `NAV_LABEL = None`). CR-1015-retire: the
  `/freshness/` page is retired.
  `GET /freshness` and `GET /freshness/` answer 301 to `/`; `GET /freshness/{rest:path}` likewise. When a registered
  configuration is named (`?configuration=ID`, or the first path segment: `/freshness/ID`) they answer 302 to its
  Home row (`/?v=1&o=p:<root>&o=c:ID&sel=c:ID#row-...`, `freshness.web.home_url`, from the stored registry records;
  302 because it depends on them). A GET reads the stored registry records only. `POST /freshness/scan` (field
  `configuration`, optional; local deployments only, CSRF-checked; elsewhere 403 with the CLI command) stays, as
  Home's "Scan <id> now": it queues the job, or finds the one already queued that covers it, and 303s to the
  configuration's Home row (or `/`). 400 for an unregistered configuration. The CLI (`freshness scan`, `freshness
  show`) is unchanged.
- **Observation kind** `freshness` (set kind; `db.SET_KINDS`); example `observation.freshness.example.jsonl`
  (hermetic fixture, no lab data).
