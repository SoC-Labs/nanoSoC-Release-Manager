# v1 module interfaces

How the v1 modules fit together, and the exact interface of each: function, CLI command, HTTP endpoint,
inputs, outputs, errors, requirement IDs. Data shapes are the schemas in [`schemas/`](../../schemas/);
this page never redefines them. Index and rules: [README.md](README.md). Every name an acceptance test relies
on is collected in section 17.

## 0. Conventions

1. **CLI first (PR4).**
   - Every capability is one Python function that returns contract documents, usually observations.
   - The CLI command and the web view are two front ends over that function; neither holds logic of its own.
   - Every command takes `--json`, which prints one JSON document per line. Without it, the command prints a table.
2. **Exit codes, every command:**

   | Code | Meaning |
   |---|---|
   | 0 | answered: no finding, or PASS |
   | 1 | finding, or FAIL |
   | 2 | could not answer: missing data, unreadable host; the UNVERIFIED case |
   | 3 | usage or config error |
3. **Facts (A3).** Anything shown is an observation ([observation.schema.json](../../schemas/observation.schema.json)). Its `sources[].command` holds the exact git argv or `METHOD path` API call. Credentials never appear in a command, a URL or a log.
4. **Read-only (A2, A6, PR8).** v1 makes exactly one kind of write call to anything outside its own disk: the CI pipeline trigger (section 10).
   - The mirrors fetch; they never push.
   - No module has a code path that writes to a repo, a fork, a tag or a setting.
   - Every outbound call that a static census could mistake for a write is declared in `WRITE_CALLS` (section 17.5).
5. **Package layout,** mirroring fpgahub (H4, PR9):
   - Python ≥ 3.11, package `nanosoc_rm` under `src/`, click CLI `nanosoc-rm`, hatchling.
   - FastAPI + Jinja2 + htmx, pydantic, httpx, SQLite (WAL). fpgahub has no SQLite layer, so `db/` is new code.
   - One long-running process, `nanosoc-rm serve`, in one systemd unit. It serves the web pages and runs the schedules (poll every 15 min, nightly T0 trigger, result fetch every 2 min) by calling the same functions as the CLI, through `schedule.tick()` (section 12a).

```
src/nanosoc_rm/
  cli.py   config.py   schedule.py   gitrun.py (the one git runner)   contracts/ (packaged schemas, validate(), WRITE_CALLS)
  registry/ (loader, imports.py census)   mirror/   walker/   analysis/ (hosts.py, contains.py, protect.py, detect.py)
  t0/ (gates.py and tiers.py shared; runner.py runs IN CI only)   ci/ (client.py, trigger.py, fetch.py, verify.py)
  scan/ (rules.py, scan.py)   credentials.py   db/   web/   auth.py   lock/ (Phase 5)
```

**Data flow:**

```
GitHub + GitLab --(read token, heads+tags only)--> mirror/ --> walker/ --> analysis/ --> db/ --> web/ + CLI
registry repo (protected main) --(read)--> registry/ ------------------------^
web/ Run button, nightly schedule --> ci/trigger --(trigger token, compat-ci only)--> GitLab CI job `t0`
GitLab CI job `t0` (t0/runner, anonymous git only) --> artifacts rm-bundle/ (scanned by the job before upload)
ci/fetch --(read token: job, variables, artifacts)--> ci/verify --> scan/ --> db/
```

## 1. Service config (private; never in this repo)

A TOML file on the service VM, path in `$NANOSOC_RM_CONFIG`. Every function reads it through
`config.load_config(path=None)`, **per call**, never cached at import (tests switch configs in one process).
Keys (sections 17.1 lists them with types):

| Section | Keys | Requirement |
|---|---|---|
| `service` | `data_dir` (holds `mirrors/`, `rm.sqlite`, `quarantine/`) | H6, A3 |
| `hosts.github`, `hosts.gitlab` | `web`, `api`, `ssh` base URLs; optional `git` (the base git fetches use; defaults to `web`); `org` (GitHub) or `group` (GitLab) to inventory | A1, B9 |
| `registry` | `repo`, `ref` (`main`), `path` (local checkout) | F1 |
| `ci` | `project`, `project_id`, `ref`, `job` (`t0`), `allowed_refs`, `allowed_runner_ids`, `runner_tag`, `service_account`, `runner_expected` (the hardened settings), `fetch_timeout_minutes` | A4, D5, D10 |
| `credentials.<name>` | `token_file`, `scope` (as issued), `expires` (date), `project` (ci_trigger only), `client_id` (github_oauth only) | H3 (expiry warning 14 days ahead) |
| `session` | `secret_file` | H2 |
| `stores.*` | `artifactory` URL and repo, `disk` path | D12, G7 |
| `roles` | `default` (roles of every signed-in member), plus optional `"<login>" = [roles]` that add to it. v1 has one role, `member` (view + Run T0). An empty table refuses everyone. | H2 |
| `schedule` | `poll_minutes` (15), `nightly_utc`, `fetch_minutes` (2) | H6, D2 |
| `paths` | `quarantine` (defaults to `<data_dir>/quarantine`) | D11 |

v1 credentials, and nothing else (A2, H3):

1. `github_read`: GitHub read-only fine-grained token (poller): contents, metadata and members read.
2. `gitlab_read`: GitLab `read_api` + `read_repository` token (poller, CI fetch). Its owner is the service account; its role on compat-ci is question 10 in GATE2.md.
3. `ci_trigger`: GitLab pipeline trigger token for the compat-ci project. It is owned by the same dedicated service account with the minimum role that may run pipelines on the protected ref, because a trigger-token pipeline runs with its owner's identity (the job's `CI_JOB_TOKEN` carries the owner's access).
4. `github_oauth`: GitHub OAuth app (login), client id and secret.

## 2. Registry loader and validator

Requirements: A1, B1, B2, F1, F2, F3 (structure), F5, and PR3 structure. F3 existence is checked by T0 (section 9).

```python
def load_registry(path: Path) -> Registry           # raises RegistryError(problems: list[Problem])
@dataclass class Problem: file: str; pointer: str; rule: str; message: str
@dataclass(frozen=True) class Repo: host: str; path: str
class Registry:
    def components(self) -> list[dict]            # catalogue entries, as schema documents
    def resolve_repo(self, repo: Repo) -> tuple[str | None, Literal["canonical","copy","alias","external","ignored","unknown"]]
    def configurations(self) -> list[dict]
    def defects(self) -> list[dict];  def errata(self) -> list[dict];  def imports(self) -> dict
    commit: str                                   # the registry commit it was read at
def normalise_url(url: str, hosts: HostTable, parent: Repo | None = None) -> tuple[UrlKind, Repo | None]   # B9; from walk.py norm()
```

Records are schema documents (plain dicts; pydantic models are allowed internally, but every public function
returns dicts or models whose `model_dump(mode="json", by_alias=True)` is the document).

- **CLI:**
  - `nanosoc-rm registry validate [PATH] [--json]`. Exit 0 = valid; 1 = problems, printed `file:pointer: message [rule]`.
  - `nanosoc-rm registry imports [PATH] [--json]`: the F5 census (section 2a). Exit 0 only when every source is censused and nothing is pending or missing.
- **Checks:**
  - every file against its schema;
  - the cross-file rules in README section 3 (rules 1 to 12);
  - `imports.yaml` items with `disposition: pending` are reported; they make `registry imports` exit 1.
- **URL normalisation (B9):**
  - https and ssh forms of both hosts, with the host names taken from the config's host table, never hard-coded;
  - strip `.git` and a trailing `/`;
  - host redirects come from catalogue `aliases`;
  - a relative URL (`../x.git`) is resolved against `parent`'s pinned URL, as git does, and reported `relative`.

  A URL that resolves to no component or external entry gives `unknown`, and the walker raises an `unknown_url` alert.
  K1 (option a, David 2026-10-10): that includes an `ignored` repo pinned in a registered tree. `ignored` keeps a repo
  out of the inventory's "unclassified" list and out of the mirrors; a registered tree that pins it makes it a
  dependency, so it is flagged to be classified, never hidden (README section 5 "Unknown URL").
- **Errors:** never partial. A registry with any problem is not loaded; the service keeps serving the last good one and shows the problems.
- **Facts (A3, H1):** each record the web shows is also a `registry_record` observation (source `git -C <registry> show <commit>:<file>`), so registry facts carry an age and a command like every other fact. K2 (option a, David 2026-10-10): the one producer, `registry.store_records`, stores the real checkout path in the command (the CLI, the JSON API and the store keep it); HTML pages show the literal placeholder `<registry>` in its place, so no page carries the service VM's filesystem layout.

### 2a. F5 import census

```python
@dataclass class CensusRow: path: str; commit: str; census: list[str]; missing: list[str]; extra: list[str]
                            excluded: list[dict]; pending: int; done: bool
def census(ledger: dict, read_doc: Callable[[str, str, str], str]) -> list[CensusRow]   # read_doc(component, commit, path)
```

For each source: apply `items_rule.pattern` (Python regex, multiline, first group = key) to the document at
the cited commit, drop `exclude` keys, add `include` keys. `missing` = censused keys the ledger lacks; `extra` =
ledger keys the census lacks; `done` = rule present, nothing missing, nothing extra, nothing pending.

## 3. Mirror and poller

Requirements: H6, A1 (inventory), B6 (data), B8 (hook).

**Which repos:** every repo of every catalogue component (canonical, copies; aliases are the same repo) and
every external, legacy included. Ignored and unclassified repos are listed by `inventory` but never mirrored.

**Layout:**
- One bare repo per catalogue component or external: `<data_dir>/mirrors/<id>.git`.
- It holds every repo of that component under a per-repo namespace
  `refs/r/<host>/<quote(path, safe="")>/heads/*` and `.../tags/*`, where `quote` is Python's
  `urllib.parse.quote` (so `/` in the path is `%2F`). The namespace key used in function arguments is
  `<host>/<quote(path, safe="")>`.
- Keeping both hosts in one object store is what makes B6 answerable. GitHub's compare API returns 404 for the other host's head (README section 7, fact 4).

**Fetch** (every 15 minutes; explicit refspec, never `--mirror`, so no PR/MR refs):

```
git -C <id>.git fetch --no-tags --prune --filter=blob:none <https url> \
    +refs/heads/*:refs/r/<key>/heads/*  +refs/tags/*:refs/r/<key>/tags/*
```

- `--no-tags` is required: without it git auto-follows tags and writes `refs/tags/*` outside the namespace, and in a store holding both hosts the first host fetched would own the bare tag names (reproduced with git 2.43, scope_web gap G19).
- **Mirror config:** `core.logAllRefUpdates=always`, `gc.reflogExpire=never`, `gc.reflogExpireUnreachable=never`, `gc.pruneExpire=never`. Old heads and their objects stay inspectable (B3 REWRITTEN, B8).
- **Private repos:** the read token goes in through a credential helper, never in the URL.
- **Blobs:** `.gitmodules` blobs arrive on demand and are then cached.
- **git environment:** the service's git subprocesses inherit the process environment (tests route them with `GIT_CONFIG_GLOBAL` and `url.insteadOf`). Only the T0 runner forces `GIT_CONFIG_GLOBAL=/dev/null`.

```python
def sync_component(component_id: str) -> list[dict]   # mirror_status per repo (+ rewrite_alert, section 8); stores what it returns
def inventory() -> list[dict]                         # repo_inventory for every repo in org + group (A1); stored as one snapshot per host
```

- **Inventory listing:** GitHub `GET orgs/<org>/repos?type=all&per_page=100` following `Link`; GitLab `GET groups/<group>/projects?include_subgroups=true&per_page=100` following `X-Next-Page`. Subgroups matter: 71 top-level GitLab projects against 101 with subgroups (2026-09-30).
- **CLI:**
  - `nanosoc-rm mirror sync [--component ID] [--host H]`
  - `nanosoc-rm mirror status [--json]`: one mirror_status per repo; the table prints each repo's age (now − `last_success_at`), H6.
  - `nanosoc-rm inventory [--unclassified] [--json]`: exit 1 if any repo is unclassified, A1.
- **Errors:** a failed fetch is a `mirror_status` with `ok: false`, the error text, and the previous `last_success_at`; the old refs stay, and a `mirror_failed` alert is raised. A mirror is never deleted or re-cloned automatically.
- **Unknown:** total mirror disk size for about 158 repos. Blobless fetches of two small repos took 140 KB and 340 KB. Measure on the first full sync.

## 4. Pin walker

Requirements: B2, B3, B7, B9. Reuses `walk.py`.

```python
def walk(configuration_id: str) -> list[dict]          # kind pin_state, one per pin; stored as one snapshot
```

**Algorithm** (walk.py's, reading mirrors first):
1. Resolve the configuration to a commit on its canonical host. A frozen configuration always uses its recorded `commit`, and its `ref` is informational: if a **tag** `ref` no longer peels to that commit, raise a `tag_moved` alert; a branch `ref` that moves on raises nothing.
2. At each (repo, commit):
   - read `.gitmodules` with `git cat-file -p <commit>:.gitmodules`;
   - for each entry, read `git ls-tree <commit> -- <path>` and record `entry_type`. Anything but a gitlink is pin-level state NO_GITLINK (`fact.state`) and is kept, never dropped.
   - `branch_field` is the entry's `branch=` value, or null when absent.
3. Normalise the URL (section 2), passing the parent repo for relative URLs. A local path is pin-level state LOCAL_URL. Recurse into (child repo, child commit) once, deduplicated by (repo, commit), with depth ≤ 10. Record every tree path at which the pin occurs. `parent.repo` is the repo the parent was fetched from: the one its own pinned URL names, after alias normalisation.
4. **Per host** holding a copy of the child (canonical, copies), apply the precedence in README section 5, using only that host's namespace:
   - branches containing the commit: `git for-each-ref --contains <c> refs/r/<key>/heads`;
   - tags: same for `tags`, and `--points-at <c>` for TAGGED;
   - lag: `git rev-list --count <c>..refs/r/<key>/heads/<default>`;
   - a commit reachable from no head or tag **of that host** (even if the object is in the shared mirror) is decided by that host's reflog in the mirror (REWRITTEN), then catalogue `known_rewrites`, then that host's API: GitHub `GET repos/{r}/commits/{sha}` and `.../pulls`; GitLab `GET projects/{id}/repository/commits/{sha}` and `.../merge_requests`. This separates PR_ONLY, ORPHAN and MISSING.
5. `split` = the child component appears at more than one commit in this tree (B7). `effective` stays `unknown` until Phase 2 captures file sets.

- **CLI:**
  - `nanosoc-rm walk CONFIG [--json]`
  - `nanosoc-rm pins CONFIG [--state S] [--host H]`

  Exit 1 if any pin is red on its pinned host, or has pin-level state LOCAL_URL or NO_GITLINK. BRANCH_ONLY counts as red in a frozen or silicon configuration.
- **Errors:** unknown configuration → exit 3. An unreadable repo is state UNREADABLE, not an exception.
- **Storage:** `walk()` stores its pin_state observations as one snapshot; `contains()`, `silicon_protection()` and the rewrite detector read the latest stored tree, never re-walk.
- **Golden (B2):** `walk eth-rc4` gives exactly the 44 (parent repo, parent commit, path) keys in [`release-lock.eth-rc4.example.yaml`](../../schemas/examples/release-lock.eth-rc4.example.yaml). Maximum depth 5.

## 5. Host comparator

Requirement: B6.

```python
def compare_hosts(component_id: str) -> list[dict]     # kind host_compare, one per copy; stored as one snapshot
```

- **Counts,** in the component's mirror, between the two default branches:
  - `canonical_only = rev-list --count copy..canonical`
  - `copy_only = rev-list --count canonical..copy`
- **Relation:** SAME 0/0; AHEAD n/0; BEHIND 0/n; DIVERGED n/m; no merge base → UNRELATED (counts null); either side unreadable → UNKNOWN (counts null).
- **Unreadable** = the side's latest `mirror_status` is not ok, or its default-branch ref is absent from the mirror. Its `head` (and `default_branch`, if unknown) is null.
- **`default_branch_mismatch`** is true when the two default branches have different names. The rows still compare the two default branches, and the page says so (ASIC-Flow: GitHub `lpddr4-pll` against GitLab `main`).
- **CLI:** `nanosoc-rm hosts [COMPONENT] [--json]`. Exit 1 if any relation is DIVERGED (red, B6); 2 if any is UNKNOWN and none DIVERGED.
- **Golden:** as observed on 2026-09-30 in `observation.example.jsonl`:
  - tidelink: AHEAD 298;
  - ahb_qspi: DIVERGED 1/5;
  - asic-flow: DIVERGED 4/1, default branches differ.

## 6. Containment query

Requirement: B4.

```python
def contains(commit: str, component: str | None = None,
             configurations: list[str] | None = None) -> list[dict]   # kind containment; stored as one snapshot per query
```

1. Resolve the prefix to one full SHA in the mirrors. If `component` is not given, search every mirror. Ambiguous or not found → exit 3, listing the candidates.
2. For each host where the component has a repo, prove X exists there:
   `git for-each-ref --count=1 --contains X refs/r/<key>/heads refs/r/<key>/tags`
   An object that is present but not reachable from a head or tag of that host counts as not there.
3. For each configuration (`None` = every registered configuration), and each pin of that component in its latest stored tree (all split pins):
   - prove the pin commit P exists on the host the same way;
   - then `git merge-base --is-ancestor X P`;
   - if either proof fails, `contains: null` with the reason. It is never `false`.
4. **Rows:** exactly one per (configuration, host where the component has a repo), `absent` included. The answer follows from the pins, and the schema refuses any other combination: `yes` all true; `no` all false; `partial` both, no null; `unknown` at least one null; `absent` no pins.
5. `sources` of a decided row carry the existence proofs of both commits (A3).

- **CLI:** `nanosoc-rm contains SHA [--component ID] [--config ID] [--json]`. Exit 0 = answered (including `no`), 2 = any `unknown`, 3 = the SHA resolves to nothing or to several commits.
- **Golden:** `contains 036d381 --component nanosoc_gen --config eth-rc4` gives `no` on gitlab (both generator pins, merge-base verified). The component has no GitHub repo, so there is no github row.

### 6a. Detector runs (F2)

```python
@dataclass class DetectorRun: result: Literal["fired","clean"]; forms: list[str]; input: dict; detector_source: dict | None
                              command: str; observed_at: str      # = erratum.schema.json#/$defs/detector_run
def run(defect_id: str, detector_id: str, built_output: Path) -> DetectorRun   # module nanosoc_rm.analysis.detect
```

- **CLI:** `nanosoc-rm detect DEFECT DETECTOR --input PATH [--json]`. Exit 0 clean, 1 fired, 2 could not run.
- A person or a CI job runs it on a configuration's built output (a generated-RTL archive or a netlist) and pastes the result into the erratum's `exposure.run`. The service never runs it (A4). A command detector's code is fetched at its pinned `source` commit; a pattern detector is applied by the tool.
- `input.md5` is the md5 of the archive or file given; for a directory, of its md5 manifest.

## 7. Silicon-protection list

Requirement: B5.

```python
def silicon_protection(configuration: str | None = None) -> list[dict]   # kind silicon_protection; stored as one snapshot per configuration
```

- **Rows:** for every pin of a `frozen` or `silicon` configuration, judged on **two hosts**: the host the pinned URL names (`pinned: true`, what a recursive clone uses) and the component's canonical host (`canonical: true`, where C10 protective tags belong). One row when they are the same host. A row exists when that host's default branch cannot reach the commit.
- **Coverage:** `covering_tags` are the tags on that host containing the commit; `covered` = at least one. Coverage is judged by containment, never by tag name: `keep/eth-rc4/tidelink-phy` covers component tidelink-gpio-phy `8c560c5` (a split component needs one tag per commit, so C10's `keep/<config>/<component>` cannot name both; existing keep tags are grandfathered).
- A configuration that is neither frozen nor silicon gives `[]`, not an error.
- Which hosts B5 judges is question 9 in GATE2.md; this contract implements the recommendation (both).
- **CLI:** `nanosoc-rm silicon-protection [CONFIG] [--uncovered] [--json]`. Exit 1 if any row is uncovered.
- **Golden (`observation.example.jsonl`; CR-2, re-derived 2026-10-10):** on the example catalogue eth-rc4 gives 13 rows. The 11 pinned-host rows are all covered by `keep/eth-rc4/*` (the B5 done-when). The 2 canonical-host rows are covered since David's 2026-10-01 GitHub tags: nanosoc_arch_tech `ecf76aa` by `keep/eth-rc4/nanosoc_arch_tech`, ahb_qspi `e908ac8` by `keep/eth-rc4/ahb_qspi`. On the live registry, DEC6 also makes ethernet-mac-ahb, hostio4 and socdebug_tech canonical on GitHub, which adds 3 rows (16 in all): `44ff8b7` and `50359ee` MISSING on GitHub, `51348de` BRANCH_ONLY there (decision R1). `--uncovered` exits 1 until those 3 are pushed and tagged; with only the example catalogue it exits 0.

## 7a. Branch inventory (CR8, MVP S1)

Requirement: CR8 (`docs/CHANGES.md`); reads only sections 3 and 4 data.

```python
def branches(target: str, *, all_repos=False, stale_days=90, sync=False) -> Answer   # kind branch_state; one snapshot per repo
def delete_list(docs) -> list[dict]                                                   # the deletable rows
def delete_commands(docs, remote="origin") -> list[str]                                # TEXT for a person; never run
```

- **Target:** a catalogue component or external id (its canonical repo; `all_repos` adds the copies), or one repo as
  `<host>:<path>`. Everything is read from the component mirror; no host is asked. No mirror → CannotAnswer (exit 2)
  unless `sync=True` fetched it first (`mirror.sync_component`).
- **Per branch** (every head of the repo's namespace but the default branch): `tip`; `ahead`/`behind` the default
  branch (`rev-list --left-right --count`); `merged` (`merge-base --is-ancestor tip default`); `last_commit_at`
  (committer date); `tags` whose tip IS the branch tip (`keep_tags` the `keep/*` subset); `pins`: every pin of every
  stored walk (section 4 "Storage", never re-walked) of this component whose commit the tip reaches and the default
  branch does not; `cited`: the registry's structured citations of this component (section 9 `registry_citations`)
  that this branch holds. **CR-10:** a citation the default branch does not reach holds at most one branch: none
  when a tag of the repo reaches it; else the only branch that reaches it; else one of the branches that reach it
  that is already kept (tag on the tip, or a pin), otherwise the one whose tip is fewest commits past it (`rev-list
  --count`), ties by name. So deleting every deletable branch never loses a cited commit, and a commit shared by many
  branches no longer pins all of them. Pins are unchanged. A commit the mirror does not hold is reached by nothing.
- **Verdict**, first match: `MERGED`, `PROTECTED` (a tag on the tip), `PINNED` (a pin or a citation), `STALE` (unmerged,
  unheld, last commit older than `stale_days`), `ACTIVE`. `deletable` = MERGED or STALE with no pin, no citation and
  no tag; the schema refuses `deletable: true` beside a pin, a citation or a tag.
- **CLI:** `nanosoc-rm branches TARGET [--all] [--delete-list] [--stale-days N] [--sync] [--json]`. `--delete-list`
  prints one `git push origin --delete <branch>` line per deletable branch under a comment naming the repo, as text
  for a person (A2: the tool never deletes; the git runner refuses push). Exit 0 nothing deletable, 1 something is,
  2 no mirror or a repo not judged, 3 unknown target.
- **Observation:** `branch_state` (observation.schema.json `$defs/branch_state`, added as an optional kind without a
  schema bump; README section 4 row). Example `observation.branch-state.example.jsonl`.

## 8. Rewrite detector and alerts

Requirement: B8. Runs inside `sync_component` for each repo's default branch.

```python
def detect_rewrite(mirror: Path, key: str, branch: str, old: str, new: str) -> dict | None   # key = "<host>/<quote(path, safe='')>"
```

- **Fires when** the default branch keeps its NAME and `git merge-base --is-ancestor <old> <new>` is false. A change of which branch is the default is not a rewrite (the host_compare row shows it).
- **`lost_commits`** = `git rev-list --count <old> --not --glob=refs/r/<key>/*`.
- **`pinned_lost`** = pins, in the latest stored pin_state snapshot of every registered configuration, whose commit is among the lost ones.
- **Output:** a `rewrite_alert` observation, stored, plus an `alerts` row that the dashboard shows as a banner until someone clears it.

```python
def alerts(open_only: bool = True) -> list[dict]        # module nanosoc_rm.db; kind alert
def clear_alert(subject: str, by: str) -> None          # logged in `actions` as clear_alert
```

- **CLI:** `nanosoc-rm alerts [--all] [--json]`; `nanosoc-rm alerts clear SUBJECT --by LOGIN`. Clearing is a local write to the service's own database, not a write to anything outside it.
- **Alert kinds:** `rewrite`, `unknown_url`, `token_expiry`, `runner_drift`, `tag_moved`, `mirror_failed`, `waiver_unused` (section 10, README section 6).
- Alerts are dashboard banners only in v1; a daily email digest comes in Phase 2 (GATE2 Q7, decided 2026-10-01).
- **Test:** a local bare repo stands in for a host; force-push, sync, expect one alert. Done-when on live data needs a scratch repo that a person creates (PR8).

## 9. T0: definition and runner

Requirements: D1, D2, D6, D10, D11, C11, F3. (The `t0.infra.probe` gate serves D7, a Phase 2 requirement; CR-5 took D7 off this line so v1 commits do not name it.)

T0 runs **only** in the GitLab CI job `t0` of the compat-ci project (A4). The service never runs it.
compat-ci is a **private** project: bundles can carry registry content and pinned-tree paths.

### Tiers and budget (D1)

`nanosoc_rm/t0/tiers.py` holds the tier table shared by the CLI, the dashboard and the roll-up:

```python
@dataclass(frozen=True) class Tier: id: str; budget_s: int | None; budget_reason: str | None; manual: bool; phase: str
TIERS: dict[str, Tier]                     # exactly t0..t6, the result-bundle `tier` enum
def validate_tiers(tiers) -> list[str]     # every tier has budget_s, or budget_reason
def tier_commands(configuration: str) -> dict[str, str | None]
@dataclass class Budget: ok: bool; seconds: int; budget_s: int
def check_budget(tier: str, started_at: str, finished_at: str) -> Budget
```

| Tier | Budget | Command in v1 |
|---|---|---|
| t0 | 120 s ("under 2 min"). Over budget = `over_budget: true`, a warning; the job's `timeout: 15m` is the kill limit | `nanosoc-rm t0 trigger <config>` → compat-ci job `t0` → `nanosoc-rm t0 run --out rm-bundle` |
| t1 | 600 s | none before Phase 2 |
| t2 | 900 s per SoC | none before Phase 2 |
| t3 | 10800 s | none before Phase 2 |
| t4 | null; reason: D1 gives none, set in Phase 6 | none |
| t5 | null; manual; reason: D1 gives none | none |
| t6 | 23400 s, manual | none |

- **CLI:** `nanosoc-rm tiers [CONFIG] [--json]`: one row per tier `{tier, budget_s, budget_reason, manual, phase, command}`.
- The budget is measured on the D5-verified bundle's `finished_at − started_at`, never on the job's exit.

### Job contract (compat-ci `.gitlab-ci.yml`)

```yaml
t0:
  rules: [{if: '$CI_PIPELINE_SOURCE == "trigger" && $CI_COMMIT_REF_PROTECTED == "true"'}]
  tags: [<hardened-runner tag>]            # D10
  timeout: 15m                             # kill limit; the budget is 120 s (D1)
  script: [nanosoc-rm t0 run --out rm-bundle]
  artifacts: {when: always, access: developer, paths: [rm-bundle/], expire_in: 90 days}   # D12 log retention
```

`access: developer` keeps bundles away from guests even inside the private project. `when: always` is safe
only because `t0 run` ends with `finalise_bundle()` (below), which removes anything the scan flags before the
upload step runs.

**Input:** one pipeline variable, `RM_REQUEST` ([`result-bundle.schema.json#/$defs/rm_request`](../../schemas/result-bundle.schema.json), example [`result-bundle.rm-request.example.json`](../../schemas/examples/result-bundle.rm-request.example.json)):
- compact JSON `{schema:1, nonce, configuration, repo, commit, upgrade_from, canonical_urls}`;
- the tool package version is pinned by the compat-ci commit, which is the harness.
- **Missing or invalid `RM_REQUEST`:** the runner writes no verdict rows (it has no nonce to stamp), logs why, and exits 3. The service then sees no bundle, and verify's reason names the missing variable (section 10). If a new compat-ci project refuses pipeline variables from the trigger's owner, every run fails this way and says so.

**Environment:**
- fresh `HOME` and `TMPDIR`;
- `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`;
- `GIT_TERMINAL_PROMPT=0`;
- ssh with `BatchMode=yes` and no agent;
- **credentials scrubbed:** GitLab puts `CI_JOB_TOKEN` (and, depending on settings, `CI_REGISTRY_PASSWORD`, `CI_DEPENDENCY_PROXY_PASSWORD`, `CI_JOB_JWT*`) in every job. The runner removes every `CI_*TOKEN*`, `CI_*PASSWORD*` and `CI_JOB_JWT*` variable from its environment before its first git call. The one exception is the `@registry` job's single fetch of the private registry (question 3 in GATE2.md), which uses `CI_JOB_TOKEN` once and then drops it. `t0.runner.hardened` checks the scrub;
- needs git and Python ≥ 3.11 only.

**Output:** `rm-bundle/provenance.json`, `rm-bundle/verdicts.tsv` (5-column rows, every row stamped with the nonce), `rm-bundle/items/<gate>.jsonl` and `rm-bundle/logs/`.

1. **Captured (D4, D5):** `captured.superproject_commit` = `git rev-parse FETCH_HEAD^{commit}` of the superproject fetch; `captured.pins` = the gitlinks the `t0.pins.fetchable` walk actually read (`git ls-tree <parent> -- <path>`), null below a parent that could not be fetched. T0 never checks out a working tree, so `git submodule status` has nothing to read and is not used.
2. **Items (G11):** each gate writes one JSON line per item it judged: `{tree_path | file+pointer, component, commit, host, result: pass|fail|unverified|not-reached, why}`. `nanosoc-rm t0 items NONCE GATE --json` prints them from the stored bundle.
3. **`finalise_bundle(dir)`** is the job's last step: it scans `rm-bundle/` (section 11), deletes every flagged or unscannable file, and records the scan in `provenance.scan` with `findings: [{rule, file, line}]` (locations only, never text).
4. Exit code: 0 all PASS or SKIP, 1 any FAIL or UNVERIFIED, 2 INFRA, 3 no usable RM_REQUEST. The service ignores the exit code and reads the bundle.

### Gates

Each gate lives in `nanosoc_rm/t0/gates.py` as a table shared by the runner and the service's roll-up:

```python
@dataclass(frozen=True) class Gate: id: str; scope: Literal["configuration","registry"]; reqs: tuple[str, ...]
                                     may_skip: bool; check: Callable[[Context], tuple[str, str]]   # (status, detail)
                                     control: Callable[[Context], Context]                          # returns the BROKEN input only
GATES: dict[str, Gate]
class Context:                       # what the runner hands a gate: nonce, workdir, env, fetched parents, registry dir
    @classmethod
    def for_tests(cls, *, nonce, workdir, request=None, parents=None, env=None, service_account=None,
                  registry_dir=None, url_map=None) -> "Context"     # request: an rm_request minus schema and nonce
    def items(self, gate_id: str) -> list[dict]
def run_with_control(gate_id: str, ctx: Context) -> tuple[dict, dict]    # (row, negctl row); both verdict_row dicts
def rollup(rows: list[dict], scope: str) -> list[dict]                  # one t0_result gate item per declared gate (README section 6)
def apply_waivers(gates: list[dict], items: dict[str, list[dict]], waivers: list[dict]) -> tuple[list[dict], list[dict]]
    # README section 6 "T0 waivers": (gates with WAIVED, raw_verdict and waived set where every non-pass item is
    # covered, the waivers that covered nothing). items = {gate id: gate_item lines}; waivers = the configuration's
    # t0_waivers. Pure: the same inputs give the same output, and rows, items and waivers are never changed.
def run_verdict(items: list[dict]) -> tuple[str, int]                    # (run verdict, NOT-RUN count)
```

`run_with_control` always runs the gate's own `check` twice: on the real input, and on `control(ctx)`. So the
control can never be a separate always-red function, and a gate whose check cannot fail has a control that
does not fire (D6). `url_map` is a test-only seam: `{pinned or canonical URL: local path}`; the runner refuses
it when `CI_JOB_ID` is set, so a real job can never fake reachability.

The control's row is `<gate>.negctl`, PASS when it fired.

| Gate id | May skip | Check | Negative control (must go red) | Req |
|---|---|---|---|---|
| `t0.infra.probe` | no | anonymous `git ls-remote` of one known public repo per host succeeds; if not, host-dependent gates record INFRA, not FAIL | the probe against a repo path that does not exist | D7 |
| `t0.runner.hardened` | no | `CI_COMMIT_REF_PROTECTED=true`; `HOME` and `TMPDIR` empty at start; job user equals `RM_SERVICE_ACCOUNT` (a non-secret compat-ci variable); no `CI_*TOKEN*`/`CI_*PASSWORD*`/`CI_JOB_JWT*` variable left after the scrub | the same check on a synthetic environment with an unprotected ref | D10 |
| `t0.pins.fetchable` | no | walk the tree by probing every pin as compute's pin-check workflow does: `git fetch --depth=1 <url as pinned> <sha>` into an empty repo, then read `.gitmodules` and recurse. Not-our-ref and auth are FAIL. Unreachable host is INFRA. Pins below an unfetched parent are items `not-reached` | a synthetic pin at a nonexistent commit of a real public repo | D2 |
| `t0.pins.reachable` | no | walk the tree through the **canonical https URLs** (`canonical_urls`, else the pinned URL with ssh rewritten to https), fetching each canonical repo's heads and tags anonymously (blobless), so every pin is judged, including those below an ssh-pinned parent; then `merge-base --is-ancestor`, the method of the toolkit's `check-pins.sh`. A canonical repo not readable anonymously makes its pins `unverified`, and pins below it `not-reached` | a synthetic commit absent from the fetched refs | D2 |
| `t0.urls.no-ssh-public` | no | no public parent (one that is fetchable anonymously) pins by an ssh URL (`git@…`, `ssh://…`) | a synthetic public parent with a `git@` URL | D2 |
| `t0.manifest.valid` | yes | every `nanosoc-component.yaml` at every pinned commit validates against component-manifest v1. None anywhere → SKIP | the example manifest with `version: "1.0"` | D2, C11 |
| `t0.clone.upgrade-path` | yes | if `upgrade_from`: anonymous recursive clone of it, check out `commit`, `git submodule update --init --recursive` **without** sync must resolve every pin; then `git submodule sync --recursive` and update again. No `upgrade_from` → SKIP | a synthetic pair where the second commit moves a submodule to a new URL and pins a commit the old URL does not serve (a host move whose old host was frozen: breaking under C8) | D2 |
| `t0.vendor.no-arm-ip` | no | scan every fetched tree at its pinned commit with the scanner (section 11). A tree that was not fetched is not scanned, so the gate is UNVERIFIED. **CR-6a:** a finding the catalogue `vendor_allowlist` covers with `allow` is clean (recorded as allowed in the item's `why`); one covered with `warn` keeps its item `pass` with a `why` starting `WARN:` and makes the row **WARN** when every finding in the run is warn-covered and every tree was scanned (FAIL > UNVERIFIED > WARN > PASS). **CR-9:** README section 6 rolls a WARN row with its control fired up as gate verdict **WARN** (amber); a warn-only run is WARN, never PASS and never FAIL. The allowlist comes from the registry checkout the job fetched; the item's `component` is the catalogue component of the pin's canonical repo | a scratch copy with a planted Arm banner file (its reserved URL names no catalogue component, so a `component` entry never covers it) | D2, D11 |

**Registry job** (`configuration: "@registry"`, `repo` = the registry repo, `commit` = its `main` head):

| Gate id | May skip | Check | Negative control | Req |
|---|---|---|---|---|
| `t0.registry.schema` | no | the registry loads with no problem (section 2) | an invalid example from `schemas/examples/invalid/` | D2, PR3 |
| `t0.registry.refs-resolve` | no | every structured citation (`commit_ref`, `cites`, bench, detector source, workaround, import-source commit, `exposure.run.detector_source`) is reachable from a branch or tag on its component's canonical host (anonymous fetch; `unverified` for a private canonical repo). Loader rule 9 guarantees that free-text commits are among them | a synthetic record citing a commit that does not exist | D2, F3 |

The registry is private, so this job needs read access to it from compat-ci (question 3 in GATE2.md).

**Expected on real data today:**
- eth-rc4's raw T0 (README section 6 table, before waivers) is FAIL on three gates and UNVERIFIED on one:
  - `t0.pins.fetchable`: TideLink and ASIC-Toolkit use ssh URLs (and ASIC-Toolkit is private); the 5 pins below TideLink are `not-reached`;
  - `t0.urls.no-ssh-public`: same two URLs;
  - `t0.pins.reachable`: on the example catalogue only ASIC/asic-toolkit is `unverified` (private), so the gate is UNVERIFIED: ahb_qspi `e908ac8` is on GitHub since the 2026-10-01 keep tag (CR-2). On the live registry (DEC6) ethernet-mac-ahb `44ff8b7` and socdebug_tech `50359ee` fail until decision R1 is carried out;
  - `t0.vendor.no-arm-ip`: UNVERIFIED, the 7 trees that were never fetched are `not-reached`.
- With the 7 `t0_waivers` of `eligibility.example.yaml` registered, every one of those items is covered, so the
  four gates and the run roll up **WAIVED** (`observation.t0-result-waived.example.jsonl`). The eighth waiver, for
  ahb_qspi `e908ac8`, covered nothing once that commit was on GitHub (`waiver_unused`) and was retired (CR-2).
- `t0.manifest.valid` and `t0.clone.upgrade-path` are NOT-RUN on rc4 (no manifest; no `upgrade_from`). eth-main declares `upgrade_from: v1.0.0`, so its upgrade-path gate runs (and fails today on rc4's ssh pins).
- The registry job is FAIL on `t0.registry.refs-resolve`: bench `6aafc94` is not pushed.

`result-bundle.verdicts.example.tsv` shows the rc4 rows.

## 10. CI trigger, fetch and verify

Requirements: A2, A4, D2, D5, D10, H2. (CR-5: D7 removed.)

```python
class CIClient(Protocol):           # nanosoc_rm.ci.client; the real one uses httpx and the config's tokens
    def trigger(self, ref: str, variables: dict[str, str]) -> dict          # POST projects/:id/trigger/pipeline
    def pipeline(self, pid: int) -> dict;  def jobs(self, pid: int) -> list[dict]
    def variables(self, pid: int) -> list[dict];  def artifacts(self, job_id: int) -> bytes
class FakeCI(CIClient): ...         # test seam: FakeCI(pipelines=, jobs=, variables=, artifacts=, fail_trigger_for=)
@dataclass class RunRecord: nonce: str; configuration: str; commit: str; request: bytes; trigger: str
                            triggered_by: str; triggered_at: str; pipeline_id: int | None; job_id: int | None; state: str
@dataclass class Verdict: acceptance: str; check: int | None; reason: str | None     # check = the verify step that failed
def trigger(configuration: str, by: str, kind: Literal["schedule","manual"], client: CIClient | None = None) -> dict   # t0_result
def fetch_pending(client: CIClient | None = None) -> list[dict]                                                     # t0_result, final
def verify(run: RunRecord, pipeline: dict, job: dict, variables: list[dict], bundle: Path) -> Verdict
def coverage(night: date) -> list[dict]       # configurations (and @registry) with no accepted scheduled run that night
```

- **CLI:**
  - `nanosoc-rm t0 trigger CONFIG|--all [--by LOGIN]`: a shell user's trigger is `manual`, requires `--by`, and writes an `actions` row (`run_t0`, target = configuration) like the button.
  - `nanosoc-rm t0 fetch`
  - `nanosoc-rm t0 runs [CONFIG] [--json]`
  - `nanosoc-rm t0 items NONCE GATE [--json]`
  - `nanosoc-rm t0 coverage [--night DATE] [--json]`: exit 1 when any configuration or `@registry` lacks an accepted scheduled run that night (D2).
- **Schedule (D2):** `serve` calls `trigger(id, by="schedule", kind="schedule")` nightly for every configuration and `@registry`. The Run button calls `trigger(id, by=<login>, kind="manual")` (A4, H1).

### Trigger

1. Build `RM_REQUEST`:
   - the commit resolved from the mirror;
   - `canonical_urls` from the walk;
   - `nonce = secrets.token_hex(16)`.
2. Store the `RunRecord` (`db.put_run`): nonce, request bytes, configuration, commit, trigger, who, when.
3. `POST projects/:id/trigger/pipeline` with `ref=<ci.ref>` and `variables[RM_REQUEST]`. This is the only write call in v1 (A2).
4. Record `pipeline_id`. A manual run also writes an `actions` row (H2).
5. **If the POST fails** (any HTTP error or timeout): the run is stored as `t0_result` with `acceptance: no-bundle`, `pipeline_id: null`, verdict UNVERIFIED (INFRA for 5xx or a timeout), and a reason naming the trigger and the HTTP status. The run is then no longer pending, so the Run button is enabled again, and a missed night is visible.

At most one pending run per configuration: the Run button is disabled while one is pending.

### Fetch

Every 2 minutes, for each pending run:
- `GET projects/:id/pipelines/:pid`
- `GET projects/:id/pipelines/:pid/jobs`
- `GET projects/:id/pipelines/:pid/variables`
- `GET projects/:id/jobs/:job_id/artifacts`: unzip `rm-bundle/` into `<paths.quarantine>/<nonce>/`.

A run still unfinished after the timeout (2 h) is `no-bundle` / INFRA. The role the read token needs for the
variables call is not yet known (question 10 in GATE2.md).

### Verify (D5)

Checks run in this order. The first failure makes the run `rejected`, with the check named in `reason`.

1. The API says: the project is the configured CI project, `pipeline.source == "trigger"`, the ref is in `ci.allowed_refs`, the job name is `t0`, the pipeline id matches the run, and `job.runner.id` is in `ci.allowed_runner_ids` (D10).
2. The API's `RM_REQUEST` variable is byte-equal to the stored request. If the variable is absent, the reason says `RM_REQUEST not delivered to the pipeline` (a project setting that refuses pipeline variables causes this).
3. `provenance.json` validates against result-bundle v1.
4. `run_nonce` equals the run's nonce, and `request` equals the stored request minus its `schema` and `nonce` fields.
   - `ci.pipeline_id` and `ci.job_id` equal the API's values.
   - `ci.harness_sha` equals the API's pipeline SHA.
5. `captured.superproject_commit` equals `request.commit`. This is the D5 done-when. It is a consistency check on what the job fetched: D5's authority rests on checks 1 to 4 and 6, which tie the bundle to the CI API's own record.
6. `verdicts.tsv`:
   - at least one row, 5 fields each, each row valid;
   - every nonce is the run's nonce;
   - every timestamp lies inside the job's API start/finish ±60 s.
7. The service's own scan (section 11): a finding, or a scan that could not run (zero files), makes the run `refused`. The quarantined bundle is deleted, and only the finding locations are kept.

Then:
- **Roll-up:** `gates.rollup`, then `gates.apply_waivers` with the configuration's `t0_waivers` (frozen
  configurations only; the `@registry` job has none), then `gates.run_verdict`, as in README section 6. Each
  waiver that covered nothing raises a `waiver_unused` alert (`about` = the run's subject; the message names
  the gate and tree path). A waiver only ever turns FAIL or UNVERIFIED into WAIVED; it never touches the
  stored bundle.
- **Times and runner:** `started_at` and `finished_at` from the verified provenance, `over_budget` from `tiers.check_budget`, `runner` from the job API.
- **Storage:** the accepted bundle goes to the `ci` store in v1, and to `artifactory` from Phase 2 (D12).

**No bundle:** the job's `failure_reason` decides.
- `runner_system_failure`, `stuck_or_timeout_failure`, `job_execution_timeout`, `scheduler_failure`, `api_failure`, or exit 137 → INFRA.
- Anything else → UNVERIFIED.

## 11. Bundle and tree scanner

Requirements: D11, D2. Reuses the toolkit's `check-vendor-collateral.sh`.

```python
@dataclass class ScanResult: verdict: Literal["clean","finding","unverified"]; findings: list[dict]   # [{rule, file, line}]
                             files_scanned: int; armed: dict[str, bool]                            # rule id -> fired on its specimen
def scan_dir(path: Path) -> ScanResult          # nanosoc_rm.scan
def scan_paths(paths: list[Path]) -> ScanResult # reads files in place, read-only (the lab-only realism check)
def specimen(rule_id: str) -> str               # nanosoc_rm.scan.rules: the invented text each rule is armed on
```

- **CLI:** `nanosoc-rm scan bundle DIR [--json]`. Exit 0 clean, 1 finding, 2 unverified.
- **Findings** report locations and rule ids, never matched text: the log of a scan is itself a copy.
- **Method (reused):**
  1. Arm every rule against an invented specimen, and a counter-specimen for the near miss, before scanning. A rule that does not fire on its specimen makes the scan `unverified`.
  2. Census the corpus: zero files scanned is `unverified`.
  3. Allowlist entries each carry a reason and an owner.
  4. **CR-6a, catalogue `vendor_allowlist`** (component-catalogue.schema.json `vendor_allow`; `nanosoc_rm.scan.allowlist`):
     `{rule, match: {component?, path_glob?, file_sha?}, severity: allow|warn, reason, approved_by, approved_on}`.
     `scan_tree(..., allowlist=, component=)` sorts findings into `findings` (uncovered), `warnings` (warn-covered)
     and `allowed`, each covered one naming its entry; the verdict is `finding` > `warn` > `clean`, and `unverified`
     is never changed by the allowlist. `scan_dir` (bundles) takes no allowlist: a bundle is deleted or kept, never
     warned about. An `allow` entry wins over a `warn` entry for one finding.
- **Rules:**
  - the toolkit scanner's rule set: foundry file, value and inventory shapes, and Arm product codes;
  - one new rule, `arm.banner`, for the Arm confidential-and-proprietary file header. The toolkit scanner has no such rule. D11's done-when plants exactly this banner. Its specimen is invented; no vendor text is ever written into this repo.
- **How it runs:** the toolkit scanner reads git trees. For a bundle directory, the tool builds a throwaway git repo over the extracted files and runs the scanner with `--rev HEAD`.

  Where the rule set lives (a pinned private toolkit copy, or rules loaded from compat-ci) is question 4 in GATE2.md.

## 12. Store

Requirement: A3.

SQLite in WAL mode at `<data_dir>/rm.sqlite`:

| Table | Columns | Rebuildable? |
|---|---|---|
| `observations` | kind, subject, observed_at, snapshot_id, doc JSON | yes, from mirrors, registry and CI |
| `snapshots` | id, scope, kind, completed_at | yes |
| `runs` | nonce, configuration, commit, request, trigger, triggered_by, triggered_at, pipeline_id, job_id, state | **no**: back it up |
| `actions` | at, login, action, target, result | **no**: back it up (H2) |
| `alerts` | kind, subject, raised_at, cleared_at, cleared_by | yes |

```python
def put(obs: dict) -> None                       # validates (schema + README section 4 rules) before insert; raises on any breach
def put_snapshot(scope: str, docs: list[dict]) -> str   # writes a whole snapshot atomically, marks it complete, returns its id
def latest(kind: str, subject_prefix: str = "") -> list[dict]   # set kinds: latest complete snapshot per scope; events: latest per subject
def data_age(obs: list[dict]) -> timedelta       # now - min(observed_at)
def put_run(run: RunRecord) -> None
def alerts(open_only: bool = True) -> list[dict];  def clear_alert(subject: str, by: str) -> None
```

- All `db` functions find the database through the config (`service.data_dir`), read per call.
- **History:** every observation is kept in v1 (the volume is small: about 200 set-kind rows per poll). Pruning is a later decision.

### 12a. Scheduler

```python
def tick(now: datetime) -> list[str]             # nanosoc_rm.schedule: runs every job that is due at `now`; returns their names
```

`serve` calls `tick(utcnow())` every 30 s. Tests call it with an injected time, so a schedule test never waits
on the wall clock. Job names: `poll`, `nightly_t0`, `fetch`, `inventory`, `credentials`, `runner`.

## 13. Web views and JSON API

Requirements: H1, A3, A4, A5.

Every route is behind auth (section 14). Every page prints its data age and links each fact to its sources (A3).

| Route | Shows | Function |
|---|---|---|
| `GET /` dashboard | per configuration: last T0 verdict, its age, over-budget warning and not-run count; red pins; open errata (status `open` or `permanent`); host drift; runner status; data age; **Run T0**; open alerts as banners | `latest()` over t0_result, pin_state, host_compare, runner_settings, registry_record; `alerts()` |
| `GET /configurations/{id}` | pin table (tree path, component, commit, state per host, lag, tags, split); T0 history; **Run T0** | sections 4, 10 |
| `GET /components/{id}` | repo detail: tags per host, host comparison (with the default-branch mismatch), consumers and their lag, defects citing it | sections 3, 5 |
| `GET /contains?sha=&component=` | containment per configuration and host, with proof commands | section 6 |
| `GET /silicon-protection` | section 7 rows by configuration and host, uncovered first | section 7 |
| `GET /defects`, `/defects/{id}`, `/errata/{configuration}` | records, exposure level, method and evidence (amber for probably-*, red for exposed/affected, green for not-affected, F2) | section 2 |
| `GET /runs/{nonce}` | a T0 run: acceptance, reason, times and budget, runner, gates, controls | section 10 |
| `GET /facts/{subject}` | one fact: its observation, `observed_at`, and every `sources[].command` verbatim (the page each fact links to) | `latest()` |
| `POST /configurations/{id}/runs` | Run T0 (htmx form with CSRF token); returns the pending run fragment | `trigger(id, login, "manual")` |

- **Host drift (H1):** a component in the configuration's tree whose copy relation is AHEAD, BEHIND, DIVERGED or UNRELATED. UNKNOWN is shown as "unknown", not as drift.
- **Runner status (H1):** the latest `runner_settings` observation (`verdict` ok / drift / unverified) and the age of any pending job.

**Page hooks.** The contracts fix routes and these attributes; everything else in the markup is free. Tests read pages only through them.

| Attribute | On | Meaning |
|---|---|---|
| `data-age-seconds`, `data-oldest-observed-at` | one element per page | A3 data age: request time minus the oldest `observed_at` shown |
| `data-subject`, `data-observed-at` | each fact | the observation subject and its time |
| `data-source-for="<subject>"` | an `<a>` per fact | link to `/facts/<subject>` |
| `data-configuration` | a dashboard row | configuration id |
| `data-item` | a dashboard cell | `t0-result`, `red-pins`, `open-errata`, `host-drift`, `runner-status`, `data-age` (H1) |
| `data-verdict` | a dashboard row, a run page row | the run or gate verdict; WAIVED also carries class `state-amber` and, on a run page row, `data-raw-verdict` |
| `data-waived` | a run page item row | the tree path of a waived item; the row shows the waiver's reason and approver (README section 6) |
| `data-run-t0` | a `<form>` | the Run T0 button: POST to `/configurations/{id}/runs` with field `csrf_token` |
| `data-gate`, `data-control` | a run page row | gate id and control state (D6) |
| `data-erratum`, `data-exposure`, `data-method` | an errata row | erratum id, exposure level and method; plus class `state-amber`, `state-red` or `state-green` (F2) |

**JSON API:** `GET /api/v1/...`, same auth, returns observations as JSON lines, every line a valid observation:

| Route | Returns |
|---|---|
| `configurations` | registry_record (one per configuration) |
| `configurations/{id}/pins` | pin_state |
| `components/{id}/hosts` | host_compare |
| `contains/{sha}?component=` | containment |
| `silicon-protection?configuration=` | silicon_protection |
| `runs?configuration=` | t0_result |
| `errata/{configuration}` | registry_record (the configuration's errata) |
| `mirrors` | mirror_status |
| `inventory?unclassified=1` | repo_inventory |
| `runner` | runner_settings |
| `alerts` | alert |

`POST /api/v1/configurations/{id}/runs` is the same action as the button. These two POST routes are the app's
only non-GET routes; neither accepts a result, so nothing can push a verdict to the service (D5).

## 14. Auth and roles

Requirements: A5, H2.

- **Paths:** `/auth/login` (redirects to GitHub's authorize URL with `scope=read:org`, the client id and a `state`), `/auth/callback`, `/auth/logout`. Everything auth lives under `/auth`; static assets under `/static` carry no data.
- **Login:** a GitHub OAuth app with scope `read:org`. The OAuth code exchange (`POST login/oauth/access_token`) is a POST that writes nothing (declared `read-post` in `WRITE_CALLS`). On callback, `state` must match; then `GET /user/memberships/orgs/SoC-Labs` must say `state: active` (a `pending` invitee is refused). The user's token is then discarded: it is kept in neither the session nor the database.
- **Session:** a signed cookie (itsdangerous, as in fpgahub), valid 8 h.
- **Refused session:** a user who completed the callback but is not an active member, or has no role, gets a signed "refused" session and **403** on every route, including `/api`. An anonymous user (no session) gets a redirect to `/auth/login` for pages and 401 for `/api`.
- **Re-check:** membership is re-checked at most hourly with the service's read token: `auth.is_member(config, login)` calls `GET /orgs/SoC-Labs/members/{login}`, which must return 204. K3 (option a, David 2026-10-10): when GitHub cannot answer, the session is kept and the re-check is retried 10 minutes later (not on every request, and not an hour later), so a revoked member is caught soon after GitHub is back. The session still ends 8 hours after sign-in.
- **Roles:** from the service config (H2): `roles.default` for every active member, plus per-login additions. v1's only role, `member`, allows viewing and Run T0. An empty table refuses everyone.
- **CSRF:** every form carries `csrf_token`, bound to the session.
- **Logging:** every write action (in v1, Run T0 and clearing an alert) is logged in `actions` with the login (A2, H2): `action` is `run_t0` or `clear_alert`, `target` the configuration id or the alert subject.
- **Test seam:** `create_app(config_path, auth=None)`; `auth=FakeMembership(login=...)` signs every request in as that login (still subject to roles).

## 15. Formats fixed now for later phases

| Phase | What | Contract |
|---|---|---|
| 2 | D4 capture | `captured.pins`, `host`, `ip_hash` in result-bundle; a moved pin makes the run UNVERIFIED |
| 2 | D10 CPU budget | `cpu_seconds` in result-bundle |
| 2 | Matrix and flaky handling (D3, D8) | FLAKY / XFAIL / XPASS gate verdicts |
| 3 | Manifests | T0 validates component-manifest (C11) |
| 4 | Bump bot (E3, E5) | `auto_merge` in eligibility; `workarounds` in manifests |
| 5 | Lock writer (G3, G7) | `nanosoc-rm lock CONFIG --out release.lock.yaml` emits release-lock, built from section 4 plus a release spec (archive refs, output hashes) |
| 5 | POL-1 (proposal section 3.4): T0 on a silicon ref fails on an uncovered commit | a gate `t0.pins.silicon-covered`, added with G2; until then B5's `--uncovered` exit 1 carries the signal |

## 16. v1 Must requirements → interface → golden

For the coverage checker (PR5):

| Req | Interface | Golden or fixture |
|---|---|---|
| A1 | section 3 `inventory`, section 2 catalogue (incl. `ignored`) | no repo_inventory with role `unclassified`, once the full catalogue is written |
| A2 | section 0.4, section 10 trigger, section 17.5 `WRITE_CALLS` | the only write call is `trigger/pipeline`; `actions` row per manual Run |
| A3 | observation envelope; section 12 `put`; section 13 `/facts/{subject}` | `observation.without-observed-at.yaml` is rejected |
| A4 | sections 9, 10 | a Run T0 produces a pending, then a final, t0_result |
| A5, H2 | section 14 | a non-member gets 403 on `/` and `/api/v1/configurations` |
| A6 | section 0.4, section 17.5 | no write path exists; silicon errata are `permanent`, never `fixed` or yanked |
| A7 | `docs/phase-gates.yaml` (section 17.6) | every later phase has a written, checkable entry gate |
| B1 | catalogue `class` | `component-catalogue.no-class.yaml` is rejected |
| B2 | section 4 | the 44 pin keys of `release-lock.eth-rc4.example.yaml` |
| B3 | section 4, README section 5 | the 3 pins in `pin-state.example.jsonl`, plus a hand check of rc4 and multicore-master |
| B4 | section 6 | 036d381 → eth-rc4 `no` on gitlab |
| B5 | section 7 | 11 pinned-host rows, all covered; 2 canonical-host rows, covered since 2026-10-01; on the live registry 3 more canonical-host rows (R1), uncovered (CR-2) |
| B6 | section 5 | tidelink AHEAD 298; ahb_qspi and asic-flow DIVERGED |
| B8 | section 8 | local fixture force-push → one rewrite_alert |
| D1 | section 9 tiers | T0 budget 120 s; t0..t6 each with a budget or a reason |
| D2 | section 9 gates, section 10 `coverage` | expected rc4 rows in `result-bundle.verdicts.example.tsv` |
| D5 | section 10 verify | a bundle whose `captured.superproject_commit` differs is `rejected` |
| D6 | `.negctl` rows, README section 6 | `observation.pass-without-control.yaml` and the three `observation.t0-pass-*` examples are rejected |
| D10 | gate `t0.runner.hardened`, verify check 1, `runner_settings` | its control on an unprotected ref |
| D11 | section 11, `finalise_bundle` | a planted Arm banner makes the bundle `refused` |
| F1 | defect and erratum | SL-DEF-0001 example, plus errata for eth (example), compute and QuickStart (need their configurations) |
| F2 | erratum `exposure` (+ `run`), section 6a | `erratum.ancestry-claims-exposed.yaml` is rejected |
| F3 | gate `t0.registry.refs-resolve`, loader rule 9 | SL-DEF-0001 fails while 6aafc94 is unpushed |
| F5 | import ledger, section 2a census | no `pending` items and nothing missing |
| H1 | section 13 | each v1 page on live data, including one manual Run |
| H3 | section 1 credentials, section 17 `credentials check` | four credentials, each with its scope and expiry recorded and checked |
| H6 | section 3 | mirror_status age at most 20 minutes, advancing every poll |

## 17. Names the acceptance tests rely on

The three acceptance files (`tests/acceptance/test_{pins,ci_errata,scope_web}.py`) proposed about fifty names
the earlier sections did not fix, and disagreed on some. This section is the one decision for all three. A
builder who wants a different name changes it here, through a CR after gate 2, and in the tests' one helper.

### 17.1 Config keys (TOML)

```toml
[service]      data_dir = "/srv/nanosoc-rm"
[hosts.github] web = "https://github.com"  api = "https://api.github.com"  ssh = "git@github.com:"  org = "SoC-Labs"
               # optional: git = "<base URL for git fetches>" (defaults to web; offline tests point it at file://)
[hosts.gitlab] web = "https://<gitlab-host>"  api = "https://<gitlab-host>/api/v4"  ssh = "git@<gitlab-host>:"  group = "soclabs"
[registry]     repo = "soclabs/nanosoc-registry"  ref = "main"  path = "/srv/nanosoc-rm/registry"
[ci]           project = "soclabs/nanosoc-compat-ci"  project_id = <int>  ref = "main"  job = "t0"
               allowed_refs = ["main"]  allowed_runner_ids = [<int>]  runner_tag = "<tag>"  service_account = "<login>"
               runner_expected = {run_untagged = false, locked = true, access_level = "ref_protected", is_shared = false}
               fetch_timeout_minutes = 120
[credentials.github_read]  token_file = "..."  scope = [...]  expires = "YYYY-MM-DD"
[credentials.gitlab_read]  token_file = "..."  scope = ["read_api", "read_repository"]  expires = "YYYY-MM-DD"
[credentials.ci_trigger]   token_file = "..."  scope = ["trigger"]  project = "soclabs/nanosoc-compat-ci"  expires = "YYYY-MM-DD"
[credentials.github_oauth] client_id = "..."  token_file = "..."  scope = ["read:org"]
[session]      secret_file = "..."
[stores.disk]  path = "..."          # [stores.artifactory] url, repo (Phase 2)
[paths]        quarantine = "..."    # optional; default <data_dir>/quarantine
[roles]        default = ["member"]  # plus "<login>" = [...]
[schedule]     poll_minutes = 15  nightly_utc = "02:00"  fetch_minutes = 2
```

GitLab projects are addressed in API paths by their URL-encoded path (`projects/soclabs%2Fnanosoc_gen`), as the
contract examples do.

### 17.2 Module paths

| Name | Where |
|---|---|
| `load_config(path=None)` | `nanosoc_rm.config` |
| `load_registry`, `RegistryError`, `Problem`, `Repo`, `normalise_url` | `nanosoc_rm.registry` |
| `census`, `CensusRow` | `nanosoc_rm.registry.imports` |
| `sync_component`, `inventory`, `detect_rewrite` | `nanosoc_rm.mirror` |
| `walk` | `nanosoc_rm.walker` |
| `compare_hosts` / `contains` / `silicon_protection` / `run`, `DetectorRun` | `nanosoc_rm.analysis.hosts` / `.contains` / `.protect` / `.detect` |
| `Gate`, `GATES`, `Context`, `run_with_control`, `rollup`, `apply_waivers`, `run_verdict` | `nanosoc_rm.t0.gates` |
| `TIERS`, `Tier`, `validate_tiers`, `tier_commands`, `check_budget` | `nanosoc_rm.t0.tiers` |
| `finalise_bundle` (and the runner's `main`) | `nanosoc_rm.t0.runner` (imported only in CI; the service never imports it) |
| `CIClient`, `FakeCI` / `trigger` / `fetch_pending`, `coverage` / `verify`, `RunRecord`, `Verdict` | `nanosoc_rm.ci.client` / `.trigger` / `.fetch` / `.verify` |
| `scan_dir`, `scan_paths`, `ScanResult` / `specimen` | `nanosoc_rm.scan` / `nanosoc_rm.scan.rules` |
| `check`, `CredentialStatus` | `nanosoc_rm.credentials` |
| `put`, `put_snapshot`, `latest`, `data_age`, `put_run`, `alerts`, `clear_alert` | `nanosoc_rm.db` |
| `tick` | `nanosoc_rm.schedule` |
| `create_app(config_path, auth=None)` | `nanosoc_rm.web` |
| `FakeMembership(login=)`, `is_member(config, login)` | `nanosoc_rm.auth` |
| `WRITE_CALLS` | `nanosoc_rm.contracts` |

Every function that returns documents returns dicts, or pydantic models whose
`model_dump(mode="json", by_alias=True)` is the document. The tests read both through one `as_doc()` helper.

### 17.3 CLI commands added by this section

`nanosoc-rm credentials check [--json]` (H3): one `credential_status` observation per credential; exit 0 all
`ok`, 1 any `finding`, 2 any `unverified` and no finding. It reads GitLab `GET personal_access_tokens/self` for
the poller token, and GitHub `GET user` headers for the GitHub token (a fine-grained token cannot report its
permissions, so it is `unverified` with that reason unless the header proves a classic token, which is a
finding). A credential expiring within 14 days raises a `token_expiry` alert. No `bump_writer` or `bot_tag`
credential may exist before the Phase 4 entry gate is met.

`nanosoc-rm runner [--json]` (D10): the `runner_settings` observation from `GET runners/:id`; exit 1 on drift, 2
when unreadable. Drift raises a `runner_drift` alert.

`nanosoc-rm trace [--json]` (PR7): requirement → tests → commits, from the `requirement` markers and the
`Requirements:` commit trailers.

### 17.4 Test infrastructure

- **Markers** (registered in `tests/conftest.py`): `requirement("<ID>")` on every acceptance test; `live` on every test that needs the lab.
- **Not built yet:** an acceptance test whose module does not exist carries `@NOT_BUILT` = `pytest.mark.xfail(raises=ImportError, strict=True)`. It reports XFAIL while its import fails, FAILS on any other error, and FAILS as XPASS(strict) once it passes, which forces the builder to remove the marker. `conftest.py` imports `nanosoc_rm` before each such test, so before the package exists every acceptance test reports XFAIL whatever its inputs.
- **Lab inputs** (environment): `NANOSOC_RM_CONFIG` (the private service config), `NANOSOC_RM_REGISTRY` (a checkout of the registry's main), `NANOSOC_RM_TEST_DATA` (optional test-owned data dir for the pins mirrors), `NANOSOC_RM_TEST_MEMBER` (a SoC-Labs member's login), `RM_ARM_REFERENCE_FILE` (one real vendor file, read in place), `RM_RC4_BUILD_SOC_TAR` (the P1 archive).
- **Live doctrine:** a live test without its inputs FAILS; it never skips and never passes. Off the VM, deselect with `-m "not live"`.
- **Dev dependencies:** [`requirements-dev.txt`](../../requirements-dev.txt).

### 17.5 Write allowlist (`WRITE_CALLS`, A2, A6)

`nanosoc_rm.contracts.WRITE_CALLS` is a list of entries `{file, kind: http|exec, method, target, effect, guard, reason}`.
A static census of the package (AT-A2) must find every entry, and every call it finds must be covered by one:

| effect | Meaning | Allowed |
|---|---|---|
| `write` | an outbound write | exactly one: `POST projects/:id/trigger/pipeline`, guard "target is `ci.project` only" |
| `read-post` | a POST that writes nothing | only the OAuth code exchange `login/oauth/access_token` |
| `local` | a call the census mistakes for HTTP (a sqlite `delete`) | only in a file that imports no HTTP library |
| `git-runner` | the one dynamic git runner (`nanosoc_rm.gitrun`) | at most one; guard: refuses `push`, `send-pack`, `receive-pack` at run time |

### 17.6 Phase-gate register (A7)

`docs/phase-gates.yaml`: `phases: [{phase: "Phase N", capabilities: [ids], entry_gate: [{criterion, check}],
status: not-met|gate-met|started, checked: {at, by, evidence}}]`. A phase moves to `gate-met` only with
`checked.at` and `checked.by` filled in by a person. A commit that changes `src/` and names (in its message or
its `Requirements:` trailer) a requirement of a later phase must come after that phase's `checked.at`.
Contracts, schemas, docs and tests for later phases are gate-2 work and do not count (section 15).
**Exemptions (CR-5):** an optional top-level `exemptions: [{commit, ids, cr, reason}]` names a commit that named a
later-phase id before a CR corrected the text that told it to. It takes exactly `ids` out of that one commit (full
sha), and counts only while `docs/CHANGES.md` has a row whose CR column is `cr` and which names the commit's first 7
characters. The one entry today is build/ci `a8736cb` (D7, CR-5). History is not rewritten.

### 17.7 Gap index

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
