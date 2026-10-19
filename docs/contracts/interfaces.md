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
4. **Read-only (A2, A6, PR8).** Outside its own disk the tool writes in two places only: the CI pipeline trigger
   (section 10) and a person's confirmed publish to the release store (section 21). The executor agent's POSTs go
   to the service's own API (section 27.5).
   - The mirrors fetch; they never push.
   - No module has a code path that writes to a repo, a fork, a tag or a setting.
   - Every outbound call that a static census could mistake for a write is declared in `WRITE_CALLS` (section 17.5).
5. **Package layout,** mirroring fpgahub (H4, PR9):
   - Python ≥ 3.11, package `nanosoc_rm` under `src/`, click CLI `nanosoc-rm`, hatchling.
   - FastAPI + Jinja2 + htmx, pydantic, httpx, SQLite (WAL). fpgahub has no SQLite layer, so `db/` is new code.
   - One long-running process, `nanosoc-rm serve`, in one systemd unit. It serves the web pages and runs the schedules (poll every 15 min, nightly T0 trigger, result fetch every 2 min) by calling the same functions as the CLI, through `schedule.tick()` (section 12a).

```
src/nanosoc_rm/
  cli.py  cliutil.py  config.py  schedule.py (12a)  gitrun.py (the one git runner)  contracts/ (schemas, validate(), WRITE_CALLS)
  registry/ (2, 2a)  mirror/ (3)  walker/ (4)  analysis/ (5, 6, 6a, 7, 7a, 9a: hosts, contains, detect, protect, branches, compat, tierrun)
  t0/ (9: gates.py, tiers.py, local.py; runner.py runs IN CI only)  ci/ (10)  scan/ (11)  db/ (12)  credentials.py (17.3)
  web/ (13, 13a, 25)  jobs.py (13a)  auth.py (14)  quality/ (14a)  versioning/ (14b)  releases/ (14c)  phasegates.py (17.6)
  matrix/ (18)  bumps/ (19)  regen/ (20)  publish/ (21)  store/ (22)  catalogue/ (23)  freshness/ (24)
  capabilities/ (26)  executor/ (27)  trace.py (17.3)
```

**Data flow:**

```
GitHub + GitLab --(read token, heads+tags only)--> mirror/ --> walker/ --> analysis/ --> db/ --> web/ + CLI
registry repo (protected main) --(read)--> registry/ ------------------------^
web/ Run button, nightly schedule --> ci/trigger --(trigger token, compat-ci only)--> GitLab CI job `t0`
GitLab CI job `t0` (t0/runner, anonymous git only) --> artifacts rm-bundle/ (scanned by the job before upload)
ci/fetch --(read token: job, variables, artifacts)--> ci/verify --> scan/ --> db/
```

A local deployment (section 13a) has no CI legs: Run queues a local job, and tier jobs wait for an executor
(sections 26.5, 27).

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
| `stores.*` | `artifactory` URL, release repo, token file, repos, projects, and the publish unit's keys (section 22); `disk` path | D12, G7, B10, G8 |
| `roles` | `default` (roles of every signed-in member), plus optional `"<login>" = [roles]` that add to it. v1 has one role, `member` (view + Run T0). An empty table refuses everyone. | H2 |
| `schedule` | `poll_minutes` (15), `nightly_utc`, `fetch_minutes` (2) | H6, D2 |
| `paths` | `quarantine` (defaults to `<data_dir>/quarantine`) | D11 |
| `auth` | `mode` (`github` or `local`), `[[auth.users]]` (section 14) | H2 |
| `quality`, `regen` | optional (sections 14a, 20) | Q1-Q6, C3, C7 |
| `executor`, `[[executor.agents]]`, `[[api.readers]]` | optional (sections 26.5, 26.7, 27.5) | D1, A2 |

v1 credentials, and nothing else (A2, H3):

1. `github_read`: GitHub read-only fine-grained token (poller): contents, metadata and members read.
2. `gitlab_read`: GitLab `read_api` + `read_repository` token (poller, CI fetch). Its owner is the service account: Maintainer on compat-ci only, with a `read_api` token (GATE2 Q10).
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
    def characterisations(self) -> dict[str, Characterisation]   # section 23.1 (CR-WF-catalogue)
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
  - the cross-file rules in README section 3 (rules 1 to 15);
  - `imports.yaml` items with `disposition: pending` are reported; they make `registry imports` exit 1.
- **URL normalisation (B9):**
  - https and ssh forms of both hosts, with the host names taken from the config's host table, never hard-coded;
  - strip `.git` and a trailing `/`;
  - host redirects come from catalogue `aliases`;
  - a relative URL (`../x.git`) is resolved against `parent`'s pinned URL, as git does, and reported `relative`.

  A URL that resolves to no component or external entry gives `unknown`, and the walker raises an `unknown_url` alert.
  K1: that includes an `ignored` repo pinned in a registered tree. `ignored` keeps a repo
  out of the inventory's "unclassified" list and out of the mirrors; a registered tree that pins it makes it a
  dependency, so it is flagged to be classified, never hidden (README section 5 "Unknown URL").
- **Errors:** never partial. A registry with any problem is not loaded; the service keeps serving the last good one and shows the problems.
- **Facts (A3, H1):** each record the web shows is also a `registry_record` observation (source `git -C <registry> show <commit>:<file>`), so registry facts carry an age and a command like every other fact. K2: the one producer, `registry.store_records`, stores the real checkout path in the command (the CLI, the JSON API and the store keep it); HTML pages show the literal placeholder `<registry>` in its place, so no page carries the service VM's filesystem layout.

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
- **Private repos, local deployment (CR-PR1, optional, off by default):** `[hosts.<host>] private_read` names one
  READ-ONLY method: `"ssh"` (the service user's existing key; `ssh -o BatchMode=yes`, no password or keyboard
  prompt; `{method = "ssh", identity_file = "..."}` restricts it to one key with `IdentitiesOnly`) or
  `{method = "token", token_file = "...", username = "..."?}` (a read-scope token through a credential helper that
  reads it from the environment; the user's own helpers are reset for the host's URL, so no `store` helper sees it).
  It is used only when the anonymous fetch fails as a private repo does (`private_read.RETRY_CAUSES`: auth,
  not-found), and only for that repo's `ls-remote --symref` and `fetch`, with `--filter=blob:limit=1m` so the
  walker and the quality reports need no later lazy fetch:
  ```
  git -C <id>.git fetch --no-tags --prune --filter=blob:limit=1m <ssh or https url> +refs/heads/*:... +refs/tags/*:...
  ```
  `gitrun` enforces the scope: an environment carrying `NANOSOC_RM_PRIVATE_READ` may run only `fetch` and
  `ls-remote` (no `--recurse-submodules*` option unless its value is `no`, no global `-c` or `--config-env`), on top of every other refusal (`gitrun.check_credentialed`). The
  credential is in no argv, URL, observation, alert or error text; a missing or empty token file is cause `config`,
  naming the host only. The T0 gates, `compat`, tiers and matrix cells never read it: they judge public
  fetchability and stay anonymous (section 9a).
- **How a repo was read** (CR-PR1, optional `mirror_status` fields): `access` = `anonymous` | `credentials`
  (private, readable with credentials) | `unreadable`; `cause` of a failure = `auth` | `ssh-key` | `ssh-host` |
  `not-found` | `network` | `timeout` | `config` | `other` (`private_read.classify` on git's stderr); `method` =
  the method tried (`ssh` | `token`) or null. `nanosoc-rm mirror status` prints an `access` column
  (`anonymous`, `credentials:<method>`, `unreadable:<cause>`); `mirror status --summary [--json]` counts repos per
  (access, cause, method) with a per-host split; `/mirrors` shows the same summary above the table.
- **Alerts by cause:** the `mirror_failed` text leads with its cause (`failure_message`): "failed with read
  credentials (<method>)", "private_read is misconfigured", "private or missing, no private_read for <host>", or the
  plain error. `web.alertgroups` groups them as `private-creds`, `private-read-config`, `private-anon` (and the
  existing ssh-key, repo-gone, unreachable). An alert is raised again, open even if a person cleared it, when the
  method tried changes (the repo now fails WITH credentials) or when the failure becomes or stops being a
  `private_read` misconfiguration (cause `config`), so it moves to the right group; other cause changes (a network
  flap) do not raise it again; a recovery does not clear an alert (a person
  does, one group at a time, CR-W3).
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

## 4. Pin walker

Requirements: B2, B3, B7, B9. Reuses `walk.py`.

```python
def walk(configuration_id: str) -> list[dict]          # kind pin_state, one per pin; stored as one snapshot
```

Optional keywords (additive): `store`, `alerts`, `overrides` (CR-11) and `at=SHA` (CR-R7: walk the tree from that
root commit instead of the one the configuration resolves to now; a what-if like `overrides`, never stored and
raising no alert; used by `release verify` to re-judge the drafted commit's tree).

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
- **Errors:** unknown configuration → exit 3. An unreadable repo is state UNREADABLE, not an exception. A host answer that decides nothing (5xx, unreachable, rate-limited, or a 200 that is not JSON) is WalkError (exit 2); so is a node tree the mirror cannot read (ROB-faults-9).
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
- B5 judges both hosts (GATE2 Q9).
- **CLI:** `nanosoc-rm silicon-protection [CONFIG] [--uncovered] [--json]`. Exit 1 if any row is uncovered.
- **Golden (`observation.example.jsonl`; CR-2, re-derived 2026-10-10):** on the example catalogue eth-rc4 gives 13 rows. The 11 pinned-host rows are all covered by `keep/eth-rc4/*` (the B5 done-when). The 2 canonical-host rows are covered since David's 2026-10-01 GitHub tags: nanosoc_arch_tech `ecf76aa` by `keep/eth-rc4/nanosoc_arch_tech`, ahb_qspi `e908ac8` by `keep/eth-rc4/ahb_qspi`. On the live registry, DEC6 also makes ethernet-mac-ahb, hostio4 and socdebug_tech canonical on GitHub, which adds 3 rows (16 in all): `44ff8b7` and `50359ee` MISSING on GitHub, `51348de` BRANCH_ONLY there (decision R1). `--uncovered` exits 1 until those 3 are pushed and tagged; with only the example catalogue it exits 0.

## 7a. Branch inventory (CR8)

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
- **Output:** a `rewrite_alert` observation, stored, plus an `alerts` row. It shows until someone clears it: as a
  banner on Home's configuration board, on `/alerts` (grouped by cause) and as an inbox card.

```python
def alerts(open_only: bool = True) -> list[dict]        # module nanosoc_rm.db; kind alert
def clear_alert(subject: str, by: str) -> None          # logged in `actions` as clear_alert
```

- **CLI:** `nanosoc-rm alerts [--all] [--json]`; `nanosoc-rm alerts clear SUBJECT --by LOGIN`. Clearing is a local write to the service's own database, not a write to anything outside it.
- **Alert kinds:** `rewrite`, `unknown_url`, `token_expiry`, `runner_drift`, `tag_moved`, `mirror_failed`, `waiver_unused` (section 10, README section 6).
- No alert is sent by email (GATE2 Q7 left a digest for Phase 2).
- **Test:** a local bare repo stands in for a host; force-push, sync, expect one alert. Done-when on live data needs a scratch repo that a person creates (PR8).

## 9. T0: definition and runner

Requirements: D1, D2, D6, D10, D11, C11, F3. (The `t0.infra.probe` gate serves D7, a Phase 2 requirement; CR-5 took D7 off this line so v1 commits do not name it.)

T0 runs **only** in the GitLab CI job `t0` of the compat-ci project (A4). The service never runs it. A developer
may run the same gates on their own machine with `nanosoc-rm compat` (section 9a, CR-11, run mode `local`); a local
run never produces a bundle and the service never accepts one.
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
| t1 | 600 s | none in CI before Phase 2; locally `nanosoc-rm compat <config> --tier t1` runs the registry's tiers.yaml command (CR-12, section 9a) |
| t2 | 900 s per SoC | as t1, `--tier t2` (1 VCS seat: `needs: [vcs-seat]`) |
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
- **credentials scrubbed:** GitLab puts `CI_JOB_TOKEN` (and, depending on settings, `CI_REGISTRY_PASSWORD`, `CI_DEPENDENCY_PROXY_PASSWORD`, `CI_JOB_JWT*`) in every job. The runner removes every `CI_*TOKEN*`, `CI_*PASSWORD*` and `CI_JOB_JWT*` variable from its environment before its first git call. The one exception is the `@registry` job's single fetch of the private registry (GATE2 Q3: the registry's job-token allowlist), which uses `CI_JOB_TOKEN` once and then drops it. `t0.runner.hardened` checks the scrub;
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
| `t0.vendor.no-arm-ip` | no | scan every fetched tree at its pinned commit with the scanner (section 11) and judge the **arm** rule family (CR-6: `arm.banner` and any private `arm.*` rule, the toolkit's `file.sha` known-vendor-file hash, `scan.unscannable`; CR-19: the toolkit's `ident` release/bundle-code rule is foundry-family, so amber in `t0.vendor.no-foundry-data`, never red here; `nanosoc_rm.scan.families`). Findings of every other rule belong to `t0.vendor.no-foundry-data` and are not reported here. A tree that was not fetched is not scanned, so the gate is UNVERIFIED. **CR-6a:** a finding the catalogue `vendor_allowlist` covers with `allow` is clean (recorded as allowed in the item's `why`); one covered with `warn` keeps its item `pass` with a `why` starting `WARN:` and makes the row **WARN** when every finding in the run is warn-covered and every tree was scanned (FAIL > UNVERIFIED > WARN > PASS). **CR-9:** README section 6 rolls a WARN row with its control fired up as gate verdict **WARN** (amber); a warn-only run is WARN, never PASS and never FAIL. The allowlist comes from the registry checkout the job fetched; the item's `component` is the catalogue component of the pin's canonical repo | a scratch copy with a planted Arm banner file (its reserved URL names no catalogue component, so a `component` entry never covers it) | D2, D11 |
| `t0.vendor.no-foundry-data` | yes | **CR-6.** The same scans (one scan per tree per run, shared with no-arm-ip), cut to the **foundry** rule family: every rule that is not arm-family, in practice the toolkit scanner's foundry and site-inventory rules (foundry file types, transcribed layer values, site paths, licence servers, big files) and any private rule not named `arm.*`. The catalogue `vendor_allowlist` applies first (`allow` clean, `warn` amber). An uncovered finding takes its rule's severity from the catalogue's optional `foundry_rules` (component-catalogue `$defs/foundry_rule`): `fail` makes the item `fail` and the row FAIL; any other rule is **WARN by default** (item `pass`, `why` starting `WARN:`; the row WARN, amber, never FAIL). Trees not fetched make it UNVERIFIED, as for no-arm-ip; a frozen configuration's no-arm-ip `not-reached` waiver covers the same tree here (README section 6, T0 waivers). No foundry rule on the host (no toolkit scanner and no private non-arm rule): SKIP `no foundry rule on this host: ...`, so NOT-RUN | a scratch copy holding a committed `negctl/planted.lef` (invented text; the toolkit's file-type rule) and each private foundry rule's specimen, judged with every foundry rule at severity `fail` (`Context.foundry_fail_all`): WARN is the policy, the control proves the detection can go red | D2, D11 |

**Registry job** (`configuration: "@registry"`, `repo` = the registry repo, `commit` = its `main` head):

| Gate id | May skip | Check | Negative control | Req |
|---|---|---|---|---|
| `t0.registry.schema` | no | the registry loads with no problem (section 2) | an invalid example from `schemas/examples/invalid/` | D2, PR3 |
| `t0.registry.refs-resolve` | no | every structured citation (`commit_ref`, `cites`, bench, detector source, workaround, import-source commit, `exposure.run.detector_source`) is reachable from a branch or tag on its component's canonical host (anonymous fetch; `unverified` for a private canonical repo). Loader rule 9 guarantees that free-text commits are among them | a synthetic record citing a commit that does not exist | D2, F3 |

The registry is private: compat-ci's job token reads it through the registry's job-token allowlist (GATE2 Q3).

`result-bundle.verdicts.example.tsv` shows rc4's rows; what they were on real data in October 2026 is in
[the archive](../archive/history.md#t0-on-real-data-2026-10).

## 9a. Compatibility check and the local run mode (CR-11)

Requirements: D1, D2, B2, B7, CR-11 (`docs/CHANGES.md`). Reuses sections 4 and 9 unchanged; adds a run mode.

```python
def compat(configuration, overrides: dict[str, str] | None = None, *, config=None, store=False, keep_work=None,
           url_map=None) -> dict                                     # one compat_result observation
# nanosoc_rm.t0.gates
MODES = ("ci", "local"); LOCAL_NOT_RUN = {"t0.runner.hardened": "local run"}
Context.for_local(*, nonce, workdir, request, registry_dir=None, host_bases=None, url_map=None) -> Context
def rollup(rows, scope, mode="ci") -> list[dict]                    # mode="local": see below
# nanosoc_rm.t0.local
def run_local(ctx, scope="configuration", extra_not_run=None) -> tuple[list[dict], dict[str, list[dict]]]
```

1. **Tree.** `walker.walk(configuration, overrides=..., store=False)`: each override (`--pin TREE_PATH=SHA`, the full
   40-hex sha) replaces the gitlink at that tree path; the tree below it is walked from the overriding commit's own
   `.gitmodules`, so an override can add, drop or move pins (B2), and `split` (B7) is computed on the resulting tree.
   A tree path with no gitlink pin (also one that only an earlier override removed) is `OverrideError`, exit 3.
   Every override is shown (`overrides: [{tree_path, commit, replaces}]`). A walk with overrides is never stored and
   raises no alert.
2. **Request.** Exactly what the trigger builds (`ci.trigger.build_request`, section 10), canonical_urls from this
   walk, minus `schema` and `nonce`, plus the local-only `pin_overrides` ({tree path: sha}). RM_REQUEST and its
   schema are unchanged; `gitwork.GitWork.walk` applies `pin_overrides` the same way as the walker, so every gate
   that walks the tree (fetchable, reachable, no-ssh, manifest, both vendor gates) judges the overridden pins. The controls'
   scratch superprojects drop `pin_overrides`. An override the gates' anonymous walk does not reach (below a tree
   no anonymous fetch can read) is listed in `unjudged_overrides` and makes the run UNVERIFIED, unless the
   gates judged a FAIL elsewhere (a failing item at a tree path that is not an unjudged move or below one, a
   registry item, or a FAIL gate with no failing item: `compat.judged_failures`): that run stays FAIL, and its
   `reason` names both.
3. **Local run mode.** `Context.for_local`: allowed off CI (no `CI_JOB_ID` needed); every git call isolated and
   anonymous: HOME, XDG_CONFIG_HOME and TMPDIR are empty directories inside the work directory (so `~/.netrc`,
   `~/.git-credentials` and the user's git config are never read; unit-tested against an http host that asks for
   a password), `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, no ssh agent, no prompt. `url_map` is the
   same test-only seam as `for_tests`, refused under `CI_JOB_ID`. `run_local` runs every configuration gate with
   its control (`run_with_control`), except:
   - `t0.runner.hardened`: one SKIP row with detail `local run`, no control row. `rollup(mode="local")` rolls it up
     NOT-RUN (control `not-applicable`); any other row for it in local mode is UNVERIFIED. Never PASS, never FAIL.
   - with overrides and an `upgrade_from`: `t0.clone.upgrade-path` is SKIP with its reason (it clones the registered
     commit, not the what-if tree), so NOT-RUN.
   - when `t0.infra.probe` is INFRA, the host-dependent gates are INFRA without running, as in the job.
4. **Roll-up.** `rollup(rows, "configuration", mode="local")`, then `apply_waivers` with the configuration's
   `t0_waivers` (`ci.configs.waivers`), then `run_verdict` (README section 6 unchanged: NOT-RUN gates are counted,
   never block PASS, and a run still needs a PASS or WARN gate).
- **CLI:** `nanosoc-rm compat CONFIG [--pin TREE_PATH=SHA]... [--json] [--keep-work DIR] [--store]`. Text: one line
  per pin (tree path, component, commit, overridden and what it replaced, URL kind, state on its pinned host, B7
  split, each gate's item result at that tree path), one line per B7 split, the gate table, the run verdict with
  NOT-RUN count, unused waivers and seconds (over the 120 s T0 budget is a warning). `--json`: one `compat_result`
  observation. Exit 0 PASS/WAIVED/WARN, 1 FAIL, 2 UNVERIFIED/INFRA (and a walk or registry that cannot answer),
  3 usage (unknown configuration, malformed `--pin`, no pin at a tree path, `--store` with `--pin`, a non-empty
  `--keep-work`).
- **Storage:** none by default: a what-if is not a fact. `--store` (no overrides only) stores the walk (as `walk`
  does) and the compat_result as an event observation (`compat_result:<configuration>:<nonce>`).
- **Observation:** `compat_result` (observation.schema.json `$defs/compat_result`, an optional kind without a schema
  bump; README section 4 row). The schema refuses a `t0.runner.hardened` gate other than NOT-RUN `local run`.
  Example `observation.compat-result.example.jsonl`; invalid `observation.compat-hardened-pass.yaml`.
- **Work directory:** a temporary directory, deleted afterwards; `--keep-work DIR` (new or empty) keeps it.

### Tiers: local T1/T2 runs (CR-12)

Requirements: D1, CR-12 (`docs/CHANGES.md`).

```python
# nanosoc_rm.analysis.compat
def compat(..., tier: str | None = None, force_tier=False, environ=None, lock_dir=None) -> dict
def ladder(*verdicts) -> str                  # FAIL > UNVERIFIED > INFRA > WAIVED > WARN > PASS; NOT-RUN ignored
# nanosoc_rm.analysis.tierrun
RUNNABLE = ("t1", "t2"); KILL_FACTOR = 1.5
class TierUsage(ValueError)                   # exit 3
def check_spec(spec, tier, configuration, environ=None) -> dict
def materialise(git, root: Pin, dest, pins=None) -> Layout          # Layout(laid_out, missing, not_laid_out)
def run_tier(spec, tier, tree, work, nonce, *, layout=None, lock_dir=None) -> dict   # one compat_tier
def parse_verdicts(path, tier, nonce) -> list[dict]                 # verdict_row dicts
# nanosoc_rm.registry.loader.Registry / nanosoc_rm.ci.configs
Registry.tiers(configuration) -> dict; configs.tiers(configuration, config=None) -> dict
```

1. **Registry file.** `tiers.yaml` at the registry root, optional (`eligibility.schema.json#/$defs/tiers_file`):
   `configurations: {<configuration>: {t1: <tier_command>, t2: <tier_command>}}`. A tier_command has `command`
   (argv list, run as is, or shell string, run with `bash --noprofile --norc -c`), `cwd` (tree-relative), `env`
   (explicit key/values), `budget_s`, optional `needs` (`vcs-seat`, `network`), `verdicts` (tree-relative path of
   a toolkit-format verdicts.tsv the command writes), `pins` (tree paths of the pins it needs, each with its
   subtree; absent = all) and `regenerates`. The loader refuses a tiers.yaml naming a configuration that
   eligibility.yaml does not register (README section 3 rule 14) and, through the schema, a command that sources
   a set_env.sh. The file is private (it names tool installs and licence servers): it lives only in the registry.
2. **Refusals (exit 3, before any work).** No command for that tier; a command that sources a set_env.sh (also
   checked in code); `env` setting `TREE`, `NANOSOC_RM_TIER`, `NANOSOC_RM_VERDICTS` or `NANOSOC_GEN_OK`;
   `regenerates: true` while the caller's environment lacks `NANOSOC_GEN_OK=1`; `--force-tier` without `--tier`.
3. **Order.** The local T0 runs first (steps 1 to 4 above). T0 FAIL skips the tier: `tier.verdict` NOT-RUN with the
   reason. WAIVED, WARN, UNVERIFIED and INFRA do not skip it. `--force-tier` runs it past a T0 FAIL; the tier then
   carries `forced: true` and a reason starting `--force-tier:` (a person's override; the result is UNVERIFIED
   for release, and `run_verdict` stays FAIL).
4. **Sandbox checkout.** Under the work directory, `tree/` is the configuration's resolved tree from the T0 gates'
   own walk, overrides applied: the root and each needed pin are a fresh git repo at the pin's tree path, checked
   out detached at its commit. Objects are borrowed (alternates) from the T0 run's anonymous `fetch --depth=1`;
   a pin whose pinned URL no anonymous fetch can use (ssh) is fetched once, anonymously, from its canonical https
   URL. A duplicate pin (same repo and commit) gets its first occurrence's subtree. A needed pin that cannot be laid
   out is listed in `missing_paths` and the tier is UNVERIFIED without running.
5. **Run.** Environment: `PATH=/usr/bin:/bin`, `HOME` and `TMPDIR` in the work directory, `LANG=LC_ALL=C.UTF-8`,
   `USER`/`LOGNAME`, `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, a `GIT_SSH_COMMAND` with no agent and no key
   (so git in the command is anonymous), `TREE` (the checkout root), `NANOSOC_RM_TIER`, `NANOSOC_RM_VERDICTS` (absolute, when declared),
   then `env` in file order, where `${NAME}` expands a name already set (an unknown name is exit 3), then
   `NANOSOC_GEN_OK=1` only for `regenerates: true`. Nothing else is inherited: `ARM_IP_LIBRARY_PATH` is absent unless
   `env` names it. The declared verdicts file is deleted before the run, and so is a declared `ip_files` list
   (CR-WD-matrix-t1: optional tree-relative path of a file list the command writes, one path per line, naming the
   files it read; a matrix cell hashes them, section 18 item 8). `needs: [vcs-seat]` takes a host-local
   lock (`$TMPDIR/nanosoc-rm-vcs-seat.lock`) for the run; held elsewhere = INFRA, not run. An absolute path in the
   tier's own `env` (any `:`-separated part) that does not exist on this host, outside the sandbox, is INFRA, not
   run, with reason `tool not on this host: VAR=path does not exist`. The command runs in its
   own process group with stdin closed and output to `tier-<tier>.log`; at `budget_s x 1.5` the group gets SIGTERM
   (SIGKILL 10 s later) and the tier is FAIL (`killed`). After it exits, the group is killed so nothing outlives it.
   Longer than `budget_s` is `over_budget: true`, a warning.
6. **Rows and verdict.** Rows are verdict_row dicts with this run's nonce: always `tier.<tier>` (PASS on exit 0;
   FAIL on a nonzero exit or a kill), plus one `tier.<tier>.<gate>` per verdicts-file row (status kept; the gate
   lower-cased with other characters as `-`); a declared file that is missing, empty or has a malformed line adds a
   `tier.<tier>.verdicts` UNVERIFIED row. Tier verdict: FAIL > UNVERIFIED > INFRA > WARN > PASS over the rows.
7. **Result.** `compat_result.fact.tier` (observation `$defs/compat_tier`) and `fact.run_verdict` =
   `ladder(T0 verdict, tier verdict)`; `fact.verdict` stays the T0 roll-up. The schema requires `run_verdict` FAIL
   for a FAIL tier and refuses NOT-RUN with a T0 verdict other than FAIL. Env values and licence servers are not
   recorded (`env_keys` only); `log_tail` keeps the last 20 lines.
- **WRITE_CALLS (section 17.5):** the tier command's `subprocess.Popen` in `analysis/tierrun.py` is allowlisted as
  `exec ? unresolved argv`, effect `local`: only `compat --tier` on a person's machine runs it, never the service.
- **CLI:** `nanosoc-rm compat CONFIG [--pin ...] --tier t1|t2 [--force-tier] [--keep-work DIR]`. Text adds the tier
  line (verdict, seconds of budget, OVER BUDGET, KILLED, command, cwd), its rows, the reason, the log tail when not
  PASS, and `run verdict (T0 + <tier>)`. Exit follows `run_verdict` (0/1/2), 3 for the refusals above.

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

A run still unfinished after the timeout (2 h) is `no-bundle` / INFRA. The service account is Maintainer on
compat-ci (GATE2 Q10); that this role can make the variables call is confirmed when compat-ci is created.

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
  5. **CR-6, rule families** (`nanosoc_rm.scan.families`): `family(rule_id)` is `arm` for `arm.banner`, any
     `arm.*` rule, `file.sha` and `scan.unscannable`, else `foundry` (CR-19: `ident` is foundry); `split(res, family)` gives one family's view
     of a raw ScanResult (an `unverified` scan stays unverified in both). `scan_tree(..., shas=dict)` fills the
     blob id of each scanned file so the T0 gates apply the allowlist per family. `apply_allowlist(...,
     uncovered=fn)` takes the severity of an uncovered finding (`fn(rule) -> (severity, by)`; `warn` moves it to
     `warnings`); the arm gate passes none (uncovered = finding). Catalogue `foundry_rules`
     (`scan.allowlist.load_foundry_rules`) gives a foundry rule `severity: fail|warn`; the default is warn.
- **Rules:**
  - the toolkit scanner's rule set: foundry file, value and inventory shapes, and Arm product codes (CR-6: its
    `file.sha` is arm-family; the rest, `ident` included (CR-19), are foundry-family);
  - one new rule, `arm.banner`, for the Arm confidential-and-proprietary file header. The toolkit scanner has no such rule. D11's done-when plants exactly this banner. Its specimen is invented; no vendor text is ever written into this repo.
- **How it runs:** the toolkit scanner reads git trees. For a bundle directory, the tool builds a throwaway git repo over the extracted files and runs the scanner with `--rev HEAD`.

  The rule set stays private: compat-ci carries a pinned copy and passes its path to the scanner (GATE2 Q4).

## 12. Store

Requirement: A3.

SQLite in WAL mode at `<data_dir>/rm.sqlite`:

| Table | Columns | Rebuildable? |
|---|---|---|
| `observations` | kind, subject, observed_at, snapshot_id, doc JSON | yes, from mirrors, registry and CI |
| `snapshots` | id, scope, kind, completed_at; confirmed_id, confirmed_at, confirmed_vals, confirmations, confirmed_after, confirmed_seq (keep-on-change, CR-1015-retention) | yes |
| `current_snapshots` | scope, kind, snapshot_id: the current snapshot of each scope, kept by triggers on `snapshots` (CR-ROB-store) | yes |
| `runs` | nonce, configuration, commit, request, trigger, triggered_by, triggered_at, pipeline_id, job_id, state | **no**: back it up |
| `actions` | at, login, action, target, result | **no**: back it up (H2) |
| `alerts` | kind, subject, raised_at, cleared_at, cleared_by | yes |

```python
def put(obs: dict) -> None                       # validates (schema + README section 4 rules) before insert; raises on any breach
def put_snapshot(scope: str, docs: list[dict]) -> str   # writes a whole snapshot atomically, marks it complete, returns its id
def latest(kind: str, subject_prefix: str = "") -> list[dict]   # set kinds: the last completed snapshot per scope; events: latest per subject
def newest_per(kind: str, group, subject_prefix: str = "") -> list[dict]   # events: newest per group(subject); decodes only those (CR-ROB-store)
def subjects(kind: str, subject_prefix: str = "") -> list[str]    # events: the subjects latest() answers, no document decoded (CR-ROB-int)
def data_age(obs: list[dict]) -> timedelta       # now - min(observed_at)
def history(subject: str) -> list[dict]           # stored order; a confirmed set-kind state twice: first stored, last confirmed
def compact(*, apply: bool = False) -> list[dict]   # collapse runs of identical snapshots (CR-1015-retention); one row per set kind
def put_run(run: RunRecord) -> None
def alerts(open_only: bool = True) -> list[dict];  def clear_alert(subject: str, by: str) -> None
```

- All `db` functions find the database through the config (`service.data_dir`), read per call.
- **Current snapshot** (CR-ROB-store): the snapshot of a scope that completed last, in commit order, never by the
  wall-clock `completed_at` (a clock stepped back must not hide a newer run). Reading it costs the current rows, not
  the history.
- **Schema version** (CR-ROB-store): `rm.sqlite` is at `PRAGMA user_version = 3` (3: keep-on-change columns, CR-1015-retention) and `jobs.sqlite` at 1. A connection
  runs the DDL only when the store is below its version (once, under `BEGIN IMMEDIATE`); otherwise opening it
  writes nothing, so a read never waits for the write lock.
- **Keep-on-change** (CR-1015-retention). `put_snapshot` compares the run with the scope's
  **current** snapshot (the last committed, as above, never the newest wall-clock one). Two runs have the same
  content when their documents, in order, are equal once the volatile tokens are taken out: the top-level
  `snapshot.id`, and every UTC timestamp inside any string (`YYYY-MM-DDTHH:MM:SS`, optional fraction, `Z` or
  offset), with a snapshot id's `#<8 hex>` suffix when one follows it. So check times (`observed_at`, a host
  state's `observed_at`, `last_success_at`) and snapshot ids quoted in sources or facts never count as a change;
  any other field does (a date without a time, such as freshness `judged_on`, does). A run with the same content
  stores no rows: the current snapshot's `confirmed_id`, `confirmed_at`, `confirmed_vals` (zlib JSON of the run's
  tokens, one list per row) and its place among the stored rows (`confirmed_after`, `confirmed_seq`) are
  overwritten, and `confirmations` counts the runs. `latest` returns the documents the run produced (its tokens put
  back), in the order the rows would have had if stored, so pages and data ages (A3) read exactly as before. A run
  with other content stores a new snapshot as before. `put(doc)` naming a run kept only as a confirmation first
  stores that run in full. A snapshot id kept as a confirmation cannot be reused.
- **History:** every distinct state is kept. `history(subject)` returns each stored observation and, after a
  confirmed one, its last confirmation, so a run of identical checks shows its first and last observed times.
  `nanosoc-rm store-maint compact --dry-run|--apply` (`db.compact`) collapses runs of identical snapshots stored
  before keep-on-change the same way (first snapshot kept, the last check as its confirmation); one transaction,
  safe to rerun; a scope whose current snapshot is not its last stored one is skipped. Event kinds and the
  `runs` and `actions` tables are never compacted. Operator steps: DEPLOY.md.
- **Other stores** (not facts, not rebuildable, apart from `rm.sqlite`): `<data_dir>/jobs.sqlite` (section 13a),
  `<data_dir>/bumps.sqlite` (section 19.4) and `<data_dir>/gui.sqlite` (CR-W2: `nanosoc_rm.web.uistate`, mode
  0600, `PRAGMA user_version = 2`; version 2 adds `pref`, section 25): per-user inbox state `inbox_state(login, card_id, state, fingerprint, until,
  at)` and the review basket `review_decision(login, component, commit_sha, key, decision, text, at)` and
  `basket_flag(login, component, commit_sha, name, value, at)`. Each row carries its login and time; none changes
  system state, so none is logged in `actions`.

### 12a. Scheduler

```python
def tick(now: datetime) -> list[str]             # nanosoc_rm.schedule: runs every job that is due at `now`; returns their names
```

`serve` calls `tick(utcnow())` every 30 s. Tests call it with an injected time, so a schedule test never waits
on the wall clock. Job names: `poll`, `freshness_scan` (section 24.4, right after `poll`), `nightly_t0`, `fetch`, `inventory`,
`credentials`, `runner`, `store_index` (section 22.4), `org_scan` (section 23.2; nightly; CR-1015-orgscan).
A `Job` carries optional `phase` (default `"v1"`; CR-VER-a), `enabled` (default true) and `gate` (CR-R8). A later
phase's job may be registered built with `enabled=False` and `gate="Phase N"` (CR-15); `tick` never runs a disabled
job, and the phase-gate check (section 17.6, rule 5) counts every enabled job of a later phase, by its declared
`phase`, by its name, or by what its code reaches.

## 13. Web views and JSON API

Requirements: H1, A3, A4, A5.

Every route is behind auth (section 14). Every page prints its data age and links each fact to its sources (A3).

| Route | Shows | Function |
|---|---|---|
| `GET /` | the projects home (section 25). Its configuration board, closed under the tree, is this section's dashboard: per configuration the last T0 verdict, its age, over-budget warning and not-run count; red pins; open errata (status `open` or `permanent`); host drift; runner status; data age; **Run T0**; open alerts as banners | `latest()` over t0_result, pin_state, host_compare, runner_settings, registry_record; `alerts()` |
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

## 13a. The local GUI: local sign-in and local jobs (CR-16, CR-17)

Requirements: H1, H2, A3, A4, A5, PR4, Q6 (page shell); CR-16 and CR-17 (`docs/CHANGES.md`). Section 13 stays as
it is; this section adds what a single-host deployment (no GitHub OAuth app, no CI project) needs.

**Routes added** (all behind the section 14 guard, which also checks the CSRF token on every non-GET request):

| Route | Shows or does | Function |
|---|---|---|
| `GET /compat?configuration=&job=` | pick a configuration; move pins from the component's mirror tags and branches; choose a tier; queue; the job's live log and result table (per gate and per pin, README section 6 colours with text) | `analysis.compat` via `jobs` |
| `GET /branches?component=&all=1&verdict=` | the branch inventory with verdict filters and the delete list as copyable text (computed and stored only when none is stored yet, as `contains` does) | `analysis.branches.branches` |
| `GET /hosts?component=` | stored host comparisons | `db` |
| `GET /mirrors?job=` | mirror_status rows; Sync | `mirror.sync_component` via `jobs` |
| `GET /alerts` | open and cleared alerts; Clear (logged `clear_alert`) | `db.alerts`, `db.clear_alert` |
| `GET /components`, `GET /errata` | the catalogue; every erratum | registry records |
| `GET /jobs/{id}/fragment`, `GET /jobs/{id}/log` | one job as an HTML fragment (htmx polls it every 2 s while it runs) / its log as text | `jobs.get`, `jobs.log_text` |
| `POST /compat/jobs` | queue `compat CONFIG [--pin ...] [--tier t1|t2 [--force-tier]]` | `jobs.submit` |
| `POST /configurations/{id}/walk`, `POST /mirrors/sync` | queue `walk CONFIG` / `mirror sync [--component C]`; a configuration the registry does not list is 404, never queued | `jobs.submit` |
| `POST /jobs/{id}/cancel` | cancel a queued job, stop a running one (its whole process tree) | `jobs.cancel` |
| `POST /alerts/clear` | clear one alert (field `subject`) | `db.clear_alert` |
| `POST /branches/refresh`, `POST /hosts/refresh` | re-read one component's mirror now / compare the host copies now (fields `component`, `all`); store and show the result | `analysis.branches.branches`, `analysis.hosts.compare_hosts` |
| `POST /auth/login`, `POST /auth/logout` | local sign-in form (local mode only) and sign-out (both modes, CR-ROB-auth: needs the session's CSRF token and revokes the session id in `<data_dir>/sessions.sqlite` until it would have expired) | section 14 |

The POST routes above other than `POST /auth/logout` are registered only when the app is built for a local deployment (`[auth] mode = "local"`
or no `[ci] project`); a GitHub + CI deployment keeps section 13's two Run routes and sign-out (CR-ROB-auth) as its only writes, and its pages
show the CLI command instead of the local action buttons. None of these accepts a result (D5): a job's result is
what the child process printed. Every page keeps the
section 13 hooks and its one data-age element.

**Run without CI (CR-17).** When the config has no `[ci] project`, `POST /configurations/{id}/runs` queues a local
compat job (`nanosoc-rm compat ID --store`) instead of calling `trigger`; the API form returns `{"job": {...}}`
(202, or 409 with the job already queued or running). With `[ci] project` set, Run triggers CI as before.

```python
# nanosoc_rm.jobs
KINDS = ("compat", "mirror-sync", "walk"); STATES = ("queued", "running", "done", "failed", "cancelled", "infra")
def submit(config, kind, params, by) -> dict          # JobUsage when the CLI would exit 3; compat logs run_t0
def cancel(config, job_id, by) -> dict;  def get(config, job_id) -> dict | None;  def jobs(config, ...) -> list[dict]
def log_text(config, job_id) -> str;  def recover(config) -> list[int]
class Worker(config_path=None, *, argv_prefix=None, poll_s=1.0)   # start(), stop(), step(); argv_prefix: test seam
```

- **Worker.** One per data directory (`<data_dir>/jobs.lock`), one job at a time, oldest first, started by
  `serve` (not with `--no-worker`). A job is a child process of the package's own CLI, in its own session, with
  `NANOSOC_RM_JOB=<id>` in its environment; the service never imports the gates or the tier runner. Budgets are the
  CLI's; the worker adds a ceiling (30 min, plus 1.5 x the tier budget) past which the job is INFRA.
- **Results.** A job without `--pin` runs with `--store`, so its compat_result (mode `local`) and its walk are
  stored as the CLI stores them; the dashboard's `compat-result` cell and the configuration page read them. A
  what-if (with `--pin`) is never stored (section 9a); its compat_result stays in the job record.
- **Records** in `<data_dir>/jobs.sqlite` (not rebuildable, not facts); logs in `<data_dir>/jobs/<id>/`. A tier
  job's work directory exists only while it runs (the page tails the tier log from it); the tier log is kept.
- **States.** done (the command answered; `verdict` is its run verdict, or PASS/FAIL/UNVERIFIED from exit 0/1/2 for
  sync and walk), failed (exit 3), cancelled, infra (no answer: a crash, the ceiling, or the service stopped or
  restarted while it ran; `recover()` at start marks such jobs INFRA with the reason and stops their process).
- **Plug-ins** (`nanosoc_rm.web.plugins`): a module in `PLUGINS` with a FastAPI `router` is mounted with a
  navigation entry (`Quality` for `nanosoc_rm.quality.web`, `Releases` for `nanosoc_rm.releases.web`) and renders
  through `render_page(request, template, title, environment=...)`. Its routes sit behind the same guard and CSRF
  check (CR-R6).
- **Job kinds from plug-ins (CR-R2; one seam since CR-WC-seams):** `jobs.register_kind(name, params, args, *,
  outcome=None, limit_s=None, action=None)` returns a `jobs.JobKind`; `jobs.registered(name)` looks one up and
  `jobs.all_kinds()` lists the three built-in kinds (`jobs.KINDS`: compat, mirror-sync, walk) and every registered
  one. The worker imports the modules in `jobs.KIND_MODULES` once, on first use of a kind it does not know. Kinds
  today: `matrix-cell`, `bumps`, `quality`, `release-draft`, `release-verify`, `forks`, `versions-refresh`,
  `version-draft`. A kind without `outcome` ends `done` with the verdict of its exit code (0 PASS, 1 FAIL,
  2 UNVERIFIED), `failed` on exit 3, as walk and sync do. Another module's kind cannot be replaced.
- **POST routes of plug-ins:** a plug-in `router` factory that takes the app registers its POST routes only when
  `app.state.rm.local_routes` is true (`web.plugins.local_routes(app)`), so a GitHub + CI deployment serves
  exactly section 13's two Run routes and `POST /auth/logout` (CR-ROB-auth) as writes (D5). The census reads `web.routers.writes(app)`, which counts the
  routes of included routers too.

## 14. Auth and roles

Requirements: A5, H2.

- **Paths:** `/auth/login` (redirects to GitHub's authorize URL with `scope=read:org`, the client id and a `state`), `/auth/callback`, `/auth/logout`. Everything auth lives under `/auth`; static assets under `/static` carry no data.
- **Login:** a GitHub OAuth app with scope `read:org`. The OAuth code exchange (`POST login/oauth/access_token`) is a POST that writes nothing (declared `read-post` in `WRITE_CALLS`). On callback, `state` must match; then `GET /user/memberships/orgs/SoC-Labs` must say `state: active` (a `pending` invitee is refused). The user's token is then discarded: it is kept in neither the session nor the database.
- **Session:** a signed cookie (itsdangerous, as in fpgahub), valid 8 h.
- **Refused session:** a user who completed the callback but is not an active member, or has no role, gets a signed "refused" session and **403** on every route, including `/api`. An anonymous user (no session) gets a redirect to `/auth/login` for pages and 401 for `/api`.
- **Re-check:** membership is re-checked at most hourly with the service's read token: `auth.is_member(config, login)` calls `GET /orgs/SoC-Labs/members/{login}`, which must return 204. K3: when GitHub cannot answer, the session is kept and the re-check is retried 10 minutes later (not on every request, and not an hour later), so a revoked member is caught soon after GitHub is back. The session still ends 8 hours after sign-in.
- **Roles:** from the service config (H2): `roles.default` for every active member, plus per-login additions. v1's only role, `member`, allows viewing and Run T0. An empty table refuses everyone.
- **CSRF:** every form carries `csrf_token`, bound to the session. Tokens are compared as bytes, so a non-ASCII
  token is a 403, never a 500 (CR-ROB-auth).
- **Logout (CR-ROB-auth, both modes):** `POST /auth/logout` with the session's CSRF token (403 without it) revokes the
  session id in `<data_dir>/sessions.sqlite` until it would have expired, so a copied cookie is refused in GitHub
  mode as in local mode. `GET /auth/logout` signs nobody out: it shows a sign-out button that makes that POST.
- **Alert messages** pass through `web.shell.redact` wherever they are shown: `/alerts`, the inbox, `/facts/alert:*`
  and `/api/v1/alerts` (CR-ROB-auth).
- **Logging:** every write action (in v1, Run T0 and clearing an alert) is logged in `actions` with the login (A2, H2): `action` is `run_t0` or `clear_alert`, `target` the configuration id or the alert subject.
- **Test seam:** `create_app(config_path, auth=None)`; `auth=FakeMembership(login=...)` signs every request in as that login (still subject to roles).
- **Local mode (CR-16):** `[auth] mode = "local"` with `[[auth.users]]` (`login`, `hash`, optional `roles`, default
  `["member"]` when the key is absent; an explicit `roles = []` adds no role, CR-ROB-auth). `hash` is `scrypt$<n>$<r>$<p>$<salt>$<key>` (base64; `hashlib.scrypt`, n = 2^14, r = 8, p = 1), printed by
  `nanosoc-rm user add LOGIN [--generate] [--role R]...`, which writes no file. `/auth/login` is a form (POST with a
  double-submit token in cookie `rm_login_csrf`); five failed sign-ins of one login in 5 minutes refuse it for the
  rest of the window (429). Each password check reserves its slot before it runs, so concurrent guesses are held to
  the same five; logins whose window has passed are forgotten (CR-ROB-auth). The session cookie is the same signed cookie, `HttpOnly`, `SameSite=Strict`, 8 h; a login
  removed from the config is signed out on its next request. Without `[session] secret_file` the secret is created
  once in `<data_dir>/session.secret` (0600); an empty file is replaced with a new secret, and an empty secret never
  signs anything (CR-ROB-auth). `/auth/callback` is 404.
  `serve` refuses a non-loopback `--host` in local mode (exit 3) unless `--i-know-this-is-exposed`: the service is
  reached through an SSH tunnel. **CR-20 (team access):** `serve --tls-cert PATH --tls-key PATH` serves HTTPS
  (uvicorn `ssl_certfile`/`ssl_keyfile`); exit 3 when only one is given or either is not a readable file. A
  non-loopback `--host` in local mode with `--i-know-this-is-exposed` is still refused (exit 3) without TLS unless
  `--allow-plain-http` is given. With TLS on, every cookie the app sets is `Secure` (as for an https request).
  Deployment: `docs/DEPLOY.md`, Team access over HTTPS. GitHub mode (`mode = "github"`, the default) is unchanged.

## 14a. Code and documentation quality (CR-14)

Requirements: Q1-Q6 (`docs/REQUIREMENTS.md` group Q). Package `nanosoc_rm.quality`; every function returns one
observation; the CLI and the `/quality` pages are front ends over them (section 0.1).

```python
open_source(target, *, ref=None, path=None, config=None) -> Source   # mirror at a ref, a [quality] paths name, or --path
density(target, *, ref=None, path=None, config=None, store=False, thresholds=None) -> dict   # quality_density   (Q1)
comments(target, ...) -> dict                                                              # quality_finding   (Q2)
readme(target, ..., include_docs=False) -> dict                                            # readme_check      (Q3)
docs(target, ...) -> dict                                                                  # doc_score         (Q4)
comment_patch(source, obs) -> (unified diff, edits not applied)                           # Q2, Q5
readmes.layout_patch(source) -> unified diff                                               # Q3
rewrite.rewrite_sections(source, doc_score_fact, *, config=None, client=None) -> dict      # Q4, needs an API key
trend(kind, target) -> [{commit, worktree, observed_at, value}]                            # Q5
```

1. **Sources.** A catalogue component is read from its mirror (`git ls-tree`, one `git cat-file --batch`,
   `git blame` for TODO ages; no checkout). `--ref` is a branch or tag of any of the component's repos
   (`refs/r/<key>/heads|tags/<ref>`) or a sha; the default is the canonical repo's default branch. `--path DIR`
   reads a local directory read-only (tracked files of a git work tree, else every file); `--path DIR --ref R`
   reads that repository's objects at R. Submodule contents are never read.
2. **Lexers (Q1).** One per language: Python (`tokenize` + `ast` docstrings), Verilog/SystemVerilog, C family,
   Tcl, shell, make, YAML, TOML; Markdown is documentation, counted separately. Comment markers inside string
   literals are never comments. Ratio = (comment + docstring lines) / (code + comment + docstring lines).
3. **Thresholds.** Registry `quality.yaml` (optional; `eligibility.schema.json#/$defs/quality_file`) over the
   defaults in `quality.languages.DEFAULTS`; `components.<id>` overrides per component; `--thresholds FILE` for a
   one-off. Outlier: ratio over the language's `max_ratio` with at least `min_lines` non-blank lines.
4. **Findings (Q2).** Kinds `commented_out_code`, `restates_code`, `banner`, `boilerplate`, `stale_todo`,
   `unknown_identifier`; each with path, line, evidence, a suggestion and, where the change is mechanical, an
   `edit` (whole lines replaced). Only edits go into the patch. Licence and copyright lines are never edited.
   No edit for a guess: `unknown_identifier` gives `did_you_mean` only; `commented_out_code` edits only a run
   of two or more lines with no prose line beside it (a single line, or a run next to prose, is reported without
   an edit; a run inside a paragraph or an introduced example is not reported).
5. **README check (Q3).** Paths, links, make targets (with includes, best-effort), scripts and executable bits,
   submodules, flows (target → recipe → script) and layout drawings; statuses `ok`, `missing`, `unverified`
   (could not confirm), `ignored` (gitignored output, inside a submodule). The generated Repository layout
   section covers the top two directory levels with each directory README's first line as its purpose.
6. **Conciseness (Q4).** Words, sentence length, filler and hedge words, passive share, repeated sentences,
   paragraphs duplicated across files (5-word shingles, Jaccard ≥ 0.8); score 0-100. The rewrite runs only with
   an API key (`$ANTHROPIC_API_KEY` or `[quality] api_key_file`; model `[quality] model`, default
   `claude-sonnet-5-5`); without one no client is built and no connection is opened. A rewrite that drops any
   inline code span, link target or code block, or is not shorter, is rejected.
7. **Storage (Q5).** Event kinds `quality_density`, `quality_finding`, `readme_check`, `doc_score`, subject
   `<kind>:<target>:<commit>` (`<commit>+worktree` for a work-tree read). Stored on `--store` and by every page
   view that had to analyse. A work-tree fact carries `worktree_state` (digest of listed paths, modes, sizes,
   mtimes); a page reuses a stored work-tree result only while the digest matches the disk. No write path: patches are text for a person (the `--patch` file is never
   overwritten); the Messages API call is the `read-post` WRITE_CALLS entry of `quality/rewrite.py`.
8. **Pages (Q6).** `GET /quality/` (every target: ratio, outliers, findings, README mismatches, doc score, trend
   sparklines), `/quality/{target}` (summary), `/{target}/density|comments|readme|docs|trend`,
   `/{target}/comments.diff`, `/{target}/readme.diff` (404 with a plain-text reason when there is nothing to
   patch). Query `ref`. A page never analyses inside its request (CR-R2), and a GET never queues work (link
   prefetchers and previews would start analyses; `analyse=1` is ignored): with
   no stored result for the ref it answers 200 "not analysed yet" (hook `data-not-analysed`) with an Analyse
   button, or, while a quality job for that target and ref is queued or running, 202 "analysing, refresh" with
   the job's card (`comments.diff`: 404 / 202 as text). The button (and "Analyse again" on an analysed page) is
   `POST /quality/{target}/analyse` (form `ref`, `next`; local deployments, CSRF-guarded): it queues job kind
   `quality` (`nanosoc-rm quality all TARGET [--ref R] --store`, one per target and ref at a time) and answers
   303 to `next` (a /quality/ path) or the summary. Otherwise GET only;
   mounted through the plug-in seam (`web/plugins.py`: navigation entry "Quality", the app's layout, sign-in and
   CSRF token); behind the section 14 guard; the same rows as the CLI's `--json`. Row hooks: `data-path` (density, docs),
   `data-kind` (comments, readme), `data-commit` (trend), `data-target` (overview).
- **CLI:** `nanosoc-rm quality density|comments|readme|docs|all [TARGET] [--ref R] [--path DIR] [--store] [--json]`,
  `comments --kind K --patch OUT`, `readme --docs --patch OUT`, `docs --rewrite --patch OUT`, `quality trend KIND
  TARGET`. Exit 0 clean, 1 findings, 2 could not answer, 3 usage or config error.
9. **Precision (CR-WD-Q1..Q5; additive).**
   - *Decoration rule (Q1).* `banner` and `boilerplate` are one bulk rule per repository: each such finding
     carries `bulk: "decoration"` and the fact `bulk_rules: [{rule, kinds, count, files, edits, by_kind}]`. The
     review queue, the default comments table, `comments.diff` and the Inbox count leave the rule out;
     `?kind=decoration` lists it (or `?kind=banner|boilerplate`), `?banners=1` and the review basket flag
     `banner` add all of its edits to the patch at once (off by default).
   - *Severity (Q2).* `severity` per finding, one level per kind (`quality.findings.SEVERITY_BY_KIND`):
     stale_todo High, commented_out_code Medium, unknown_identifier Check, restates_code/banner/boilerplate Low.
     README rows: missing Medium, others Check. A fact stored without the field gets the same level by kind.
   - *Review order (Q3).* Findings with a suggested `edit` first, then High, Medium, Check, Low, then path and
     line (`quality.review_text.rank`), in the review queue, the comments table and the Inbox card.
   - *Context (CR-W4, now written).* `context: {start, lines}` per finding: the finding and its whole edit, plus
     3 lines either side (a bulk finding: the edit's lines only, up to 8; none when the span exceeds 60 lines). Previews
     show the real deleted text from it; "line N" only for an older fact with no context and no local blob.
   - *Fewer false findings (Q4).* `unknown_identifier` needs a code-shaped name (`a_b`, `aB`, `f()`) or a
     code-like comment line, and skips plurals, case variants, expansion fragments (`{m,s}_x`, `x_`, `_x`),
     naming-convention variants of a close name (`x`/`u_x`/`x_i`, digits, a placeholder letter `x_N_y`) and
     short-word siblings (`u_tx_x`/`u_rx_x`). `commented_out_code` treats as prose: `VAR=1 means ...`, option
     lists (`A=1 / B=1`), a command name followed by a function word, hyphenated words, HDL bit ranges and
     `name (words)` in Python, a line ending an unfinished sentence, a one-line label naming what the next code
     line uses, lines a TODO introduces, and indented examples under a `label:` line. `restates_code` ignores
     string literals and a noun-phrase heading that the next two code lines both match. README check: in a shell
     block with prompts only prompted lines are commands; `console`/`shell-session` blocks need a prompt; an
     untagged block needs a known command or `./x` first; URLs, host names, branch names and commit ids on a
     submodule line, home and absolute paths, `.venv*` paths and `NN_x` placeholders are not checked; build
     outputs (`.bin .bit .xsa .hwh .elf .rpt .log` and waveform/checkpoint files) are `ignored`; link targets
     inside a submodule are `ignored`; in a repository with submodules a bare HDL/IP file name (`x.sv`,
     `x.flist`, `x.lib`) and a bare name on a submodule line are `unverified`.
   - *Overview filter (Q5).* `GET /quality/?scope=mine|all`: `mine` (the default whenever any repo is in a
     configuration) keeps targets that are a registered configuration's component or a child in a stored
     `pin_state` tree (`quality.web.my_components`); a chip row links each scope with its count.

## 14b. Versions, change classes and tool releases (CR-VER; built per CR-15)

Requirements: C1-C12, F6, G1, G5, G6 (Phase 3: built and used locally; nothing scheduled, nothing written to a
repo, host or the registry). Package `nanosoc_rm.versioning`; user guide `docs/versioning.md`. Every function
returns observations validated by `nanosoc_rm.contracts` (facts in `tool-release.schema.json#/$defs/*`).

```python
changelog_lint(target, *, ref=None, path=None, config=None, store=False) -> dict      # changelog_lint   (C2, C5)
plan(target, *, ref=None, path=None, config=None, store=False, regenerator=None) -> dict   # version_plan (C1 C3 C6-C9 C11 C12)
tag_check(target, *, path=None, config=None, store=False, alerts=False) -> dict       # tag_check        (C10)
manifest_init(target, *, ref=None, path=None, config=None) -> (dict, patch)          # manifest_proposal (C11)
draft(target, out_dir=None, *, config=None, date=None, regenerator=None) -> dict     # tool_release_draft (G1, G5)
approve(draft_dir, step, login, *, config=None, password=None, session_login=None) -> step record  # G5 (CR-VER-a)
yank(target, version, reason, by, *, config=None) -> (dict, patch)                   # yank_proposal    (G6)
defect_trailers(target, *, ref=None, config=None) -> (dict, patch)                   # defect_trailers  (F6)
is_yanked(component, version, *, config=None) -> bool; allowed(component, versions, *, config=None) -> list
class Regenerator(Protocol): name; available(); consumers(component); regenerate(consumer, component, commit)
```

1. **Reads.** A component's mirror (canonical namespace for tags and the baseline; files at a commit via one
   batch blob fetch and one `cat-file --batch`, as quality reads) or `--path DIR`. The registry's optional
   `versioning.yaml` (`tool-release#/$defs/versioning_file`: `tag_scheme_from`, `grandfathered`, `yanked`).
2. **Class (C3).** Signals `declared`, `api`, `consumers` (C3), `reexports` (C6), `build` (C7). `consumers` and
   `build` need a `Regenerator`; the default `NoGenerator` reports them `unmeasured` with the reason. A class is
   `complete` only when the three C3 signals are measured, else `partial` (some measured) or `unmeasured`.
   Verdict PASS / WARN / UNVERIFIED / FAIL as in docs/versioning.md; the schema enforces PASS = complete.
   An unclassed Unreleased entry makes `declared` unmeasured, and a next version over a changelog that fails
   lint is a finding (FAIL). `draft` refuses (`VersioningRefused`, exit 1) while the plan has any finding
   (CR-VER-a). With no `vX.Y.Z` tag and grandfathered tags whose highest version is not the newest, the plan's
   `current.ambiguous` is true, `next.version` is null and the verdict UNVERIFIED until a `grandfathered` entry
   of `versioning.yaml` carries `baseline: true` (optional, CR-VER-a).
3. **Writes.** None outside the service's disk. Patches go to stdout or a new file (`--patch`, never overwritten);
   `draft` writes only into a new or empty directory (default `<data_dir>/drafts/<component>/<version>`);
   `approve` rewrites only the draft's `release.json`, and only for a PROVEN login: its local password (CLI,
   `--password-stdin` or a prompt; local sign-in only) or the GUI's signed-in session; one login fills at most
   one role of a step. `--store` and `--alerts` take a mirror read only: with `--path` they are a usage error
   (exit 3), and the pages never show a stored work-tree plan as the component's plan. `--alerts` raises
   `tag_moved` (about `tag_check:<component>:<tag>`) for a moved or deleted release tag; `--store` stores the
   observation. `tag_check` compares the mirror with every stored `tag_check` of the component: a release tag
   once seen on the canonical host and now gone is a row with `deleted: true`; one at another commit is moved.
4. **Jobs.** `jobs.register_kind(name, params, args)` (section 13a) adds `versions-refresh`
   (`version refresh [COMPONENT]...`) and `version-draft` (`version draft COMPONENT`).
5. **Pages** (plug-in `nanosoc_rm.versioning.web`, navigation "Versions"): `GET /versions/` (per component:
   current tag, Unreleased by class, class and status, next version, verdict, changelog, manifest, API.md, tags),
   `GET /versions/{component}` (signals, entries, notes, findings, lint table, tag table, drafts),
   `GET /versions/{component}/manifest.diff`, `GET /versions/{component}/drafts/{version}/{file}`. In a local
   deployment only (section 13a): `POST /versions/refresh`, `POST /versions/{component}/draft` (jobs; 404 for a
   component outside the catalogue), `POST /versions/{component}/drafts/{version}/approve` (G5, the signed-in
   login; CR-VER-a). Row hooks:
   `data-component`, `data-verdict`, `data-signal`, `data-rule`, `data-tag`. A plug-in's `router` factory may
   take the app (`plugins.load`, additive) so it registers POST routes only when `app.state.rm.local_routes`.
- **CLI:** `nanosoc-rm changelog lint`, `version next|tags|refresh|draft|approve|yank|fixed-in`, `manifest init`
  (docs/versioning.md). Exit 0 PASS or WARN, 1 finding or FAIL, 2 could not answer, 3 usage.

## 14c. Releases, errata documents and forks in local mode (CR-R1)

Requirements: G2, G3, G4, G7, F4, I1, I2 (Phase 5), built under CR-15: usable locally now; the Phase 5 entry gate
still governs enabling (published releases, tool-made tags). Package `nanosoc_rm.releases`. Nothing in it tags,
pushes, uploads or writes to a host: each such action is a person step with its command, and every page and
generated file says so.

```python
# nanosoc_rm.releases (builder, forkscan, errata, archive, notes, checklist, lock)
draft(configuration, version, *, kind=None, archive=None, generator=None, rtl_committed=False, gds=None,
      netlist=None, artefacts=(), waivers=(), since=None, candidate=None, out=None, replace=False,
      store=True) -> dict                         # one release_draft observation; writes the draft folder
verify(ref, *, against=None, store=True) -> dict  # one release_verify observation
drafts(config=None, configuration=None) -> [{dir, configuration, version, doc, problems}]
forks(component=None, *, get=None, store=False) -> [fork_status observation]
ReleaseUsage (exit 3), ReleaseError (exit 2)
```

1. **Inputs.** A fresh walk of the configuration over the mirrors (`walker.walk(..., store=False, alerts=False)`),
   the registry loaded whole, the tags in the mirrors, and stored `compat_result` / `t0_result` facts of the same
   commit for the gate evidence. Nothing is fetched; nothing is written outside `<data_dir>`.
2. **Folder.** `<data_dir>/releases/<configuration>/<version>/` (or `--out`), written beside its place and swapped
   in whole: `release.lock.yaml` (release-lock.schema.json) when nothing is pending, else
   `release.lock.draft.yaml` (`$defs/draft_lock`: the same fields plus `pending: [{field, reason}]`);
   `ERRATA.md`, `ERRATA.html`; `RELEASE_NOTES.md`; `CHECKLIST.md`; `GATES.md`; `README.txt`; with `--archive`,
   `build_soc.md5sums.txt` and a copy of the archive (up to 256 MB); `draft.json` (the observation). An existing
   non-empty folder is exit 3 without `--replace`; with `--replace` it is exit 3 unless it holds a `draft.json`
   (CR-R7: --replace rebuilds an earlier draft, it never deletes another directory), and a target that is a file
   or a symlink is exit 3.
3. **Lock (G3).** One pin per walked pin with its pinned host's state, lag, tags and optional `protection`
   (`by`: release-tag, keep-tag, default-branch or none, judged on the pinned host; `hosts`: the pinned and
   canonical hosts' own answers, `not-held` when a host lacks the commit, `unreadable` when the host refused the
   read: not judged, so `lock.pins-protected` is UNVERIFIED for it, never FAIL). A pin with no state is `pending`
   (`pins`), never emitted. Lock `sources` are the walk's commands with the mirror directory shown as `<mirrors>`.
   `generated_rtl` needs `--archive` (G7: md5 manifest of every regular file, sorted, `md5sum` format; the
   generator is the shallowest `nanosoc_gen` pin unless `--generator TREE_PATH`). A silicon configuration's
   `outputs` need `--gds` and `--netlist` (hashed, never copied, never scanned).
4. **Checks** (`release_check`: id, verdict, detail, person, requirement), rolled up by README section 6's ladder:
   `lock.pins-stated`, `lock.pins-protected` (FAIL on a frozen or silicon configuration, else WARN, for a pin
   protected by nothing on its pinned host), `lock.pins-canonical` (WARN: canonical-host gaps, B5 decision R1),
   `lock.pins-tagged` (G2: every pin release-tagged or keep-tagged; `--waiver PATHS=REASON` makes it WAIVED),
   `release.tag` and `release.silicon-tag` (C10: PASS already at the commit, FAIL at another commit, UNVERIFIED
   person step when absent), `release.version-order` (C1), `errata.revision` (F4, below), `errata.category-a`
   (G2), `rtl.archive` (G7), `outputs.hashes` (G3), `tier.t0` ... `tier.t6` by release kind (fix T0-T3, feature
   +T4, tapeout +T6: a stored result for this commit, else UNVERIFIED with the command; T3, T4, T6 are person
   checks), `clone.clean` and `clone.upgrade-path` (from the stored T0 gates), `ledger.signed` (person),
   `lock.complete`, `lock.artefacts` (G3, CR-R7: WARN naming which of file lists, bitstream and logs no
   `--artefact` gave), `notes.classes` (C2: WARN for an unclassed change), `vendor.scan` (D11: every shareable
   file, GATES.md, CHECKLIST.md and README.txt included; a finding FAILs) and `vendor.scan-private` (the archive's members and `--artefact` files, which go only to the
   private store: a finding is WARN). `ready` = lock complete and every non-person check PASS, WARN, WAIVED or
   NOT-RUN. Exit: 0 PASS/WARN/WAIVED, 1 FAIL, 2 UNVERIFIED/INFRA, 3 usage.
5. **Errata document (F4).** Rendered from the configuration's records only (summary, one section per erratum,
   revision history). Its revision is the highest `doc_revision` of its records (1 with none); a silicon
   configuration's document is its revision's own. `digest` = md5 of the entries below the header. Against every
   earlier draft of the same configuration (other drafts on disk, the draft a `--replace` rebuilds, and every
   stored `release_draft` of the configuration: CR-R7): entries changed with the revision unchanged, or a lower
   revision, is FAIL. A release draft's ERRATA.md, ERRATA.html and RELEASE_NOTES.md carry the draft note. A new revision adds the G5 step "David and a second reviewer".
6. **Notes.** Classed changes of the SoC since the previous release tag (`--since`, default the newest older
   vX.Y.Z), and of each pin that moved since the newest earlier draft. Interface for the versioning module (C2):
   `nanosoc_rm.versioning.changelog_entries(component, old, new, *, config) -> [{class, summary, commit}]` is used
   when it exists (it does since CR-WB-int: the CHANGELOG.md entries the range brings, LookupError when the
   component has no changelog at `new`); otherwise the mirror's `git log old..new` classed by `Change-Class:` trailer, then breaking
   marker, then conventional-commit type, else `unclassed`.
7. **Person steps.** Keep-tag commands for each untagged, unwaived pin on its component's canonical host
   (`keep/<configuration>/<component>`, or the tree path's last part when the component is split; "push the
   commit there first" when that host lacks it), the release and silicon tag commands (`git tag -a ...` then
   `git push <ssh URL> refs/tags/...`), the candidate tag with `--candidate N`, the train (G4: catalogued pins
   with no release tag, deepest first, finalised bottom-up), the G5 approvals (a SoC release: David; tapeout or
   silicon: David and a second reviewer), signing GATES.md, and the upload after a clean vendor scan.
8. **Verify.** Re-reads `draft.json`; every listed file's md5; a re-walk compared pin by pin (commit, state,
   protection) of the drafted commit's tree (a moved branch is WARN, not drift, and the tree is then walked with
   `at=` the drafted commit: CR-R7);
   the release and silicon tags; the errata digest and revision; the archive copy against its manifest; with
   `--against DIR`, the manifest against DIR's `build_soc/` file for file (G7 done-when). Any drift is FAIL.
   CR-R8: a release or silicon tag whose draft check was PASS and that is gone now is drift (C10: tags are never
   deleted); one that was never created is NOT-RUN. GATES.md may differ from its drafted md5 only in the cells of
   the rows under `## Signatures` (each row read back as `| <role> | | | |` must give the drafted md5): signing is
   not drift, any other edit is. New check `ledger.signed`: PASS naming the roles that signed, else NOT-RUN
   (person). An archive with a path that appears twice (an appended tar) is refused (exit 3).
9. **Forks (I1, I2 check half).** For each `example` component (or the one named) and each of its repos, the host's
   fork list read anonymously (GitHub `GET repos/<path>/forks`, GitLab `GET projects/<path>/forks`, 100 per page,
   at most 5 pages; a refused listing is one UNVERIFIED row). Each fork's default branch is fetched anonymously
   (`fetch --filter=blob:none`, git config isolated) into a temporary repository that borrows the upstream
   mirror's objects: merge base, ahead/behind, `based_on` (newest upstream tag in the merge base),
   `releases_behind` (vX.Y.Z tags it lacks), open errata of the upstream's configurations, and, only when the
   upstream's `nanosoc-component.yaml` declares `fork_contract`, the changed files outside `user_paths` (`git diff
   --name-only --no-renames`, CR-R8: a file moved into a user path changes its old path too; UNVERIFIED when git
   cannot list them)
   (`contract.upgrade_steps`: README or UPGRADE.md shows a merge and `git submodule update`). Not done: forks
   registered by URL (the registry has no field yet) and writing a fork contract into a project (a repo change for
   a person).
- **Observations:** event kinds `release_draft` (`release_draft:<configuration>:<version>`), `release_verify`
  (same key), `fork_status` (`fork_status:<component>:<host>:<fork path>`); `release_draft.published` is always
  false. Stored by `draft`, `verify` and `forks --store`.
- **CLI:** `nanosoc-rm release draft CONFIG VERSION [--kind fix|feature|tapeout] [--archive TAR] [--generator
  TREE_PATH] [--rtl-committed] [--gds F --netlist F] [--artefact KIND=PATH]... [--waiver PATHS=REASON]... [--since
  TAG] [--candidate N] [--out DIR] [--replace] [--json]`; `release verify DRAFT [--against DIR] [--json]` (DRAFT
  a folder or CONFIG/VERSION); `release list [CONFIG] [--json]`; `forks [COMPONENT] [--store] [--json]` (exit 1
  when a fork is behind or changes paths outside its contract, 2 when one could not be read). Section 15's
  Phase 5 `lock` command is this `release draft`.
- **Pages** (web/plugins.py, navigation "Releases"): `GET /releases/`, `/releases/forks`,
  `/releases/{configuration}/{version}` (checks with `data-check`/`data-verdict`, pins with `data-pin`/
  `data-protection`, person steps, train, files), `.../errata`, `.../download.zip`, `.../files/{name}`. CR-R8
  (D11, D12): the archive copy is withheld unless the draft's `vendor.scan-private` is PASS: `.../files/{name}`
  answers 403 naming why, the zip leaves it out and carries `WITHHELD.txt`, and the files table shows WITHHELD
  (`data-withheld`) instead of a link;
  `POST /releases/jobs`, `/releases/{configuration}/{version}/verify`, `/releases/forks/jobs` queue job kinds
  `release-draft`, `release-verify`, `forks` (404 in a deployment without local actions). A draft's input files
  are chosen only from `<data_dir>/release-inputs/`.

## 15. Formats fixed now for later phases

Contracts, schemas, docs and tests for later phases are gate-2 work: they may name later-phase ids (section 17.6).

| Phase | What | Contract | Now |
|---|---|---|---|
| 2 | D4 capture | `captured.pins`, `host`, `ip_hash` in result-bundle; a moved pin makes the run UNVERIFIED | matrix cells capture it (section 18) |
| 2 | D10 CPU budget | `cpu_seconds` in result-bundle | reserved |
| 2 | Matrix and flaky handling (D3, D8) | FLAKY / XFAIL / XPASS gate verdicts | FLAKY built (section 18) |
| 3 | Manifests | T0 validates component-manifest (C11) | built (`t0.manifest.valid`) |
| 4 | Bump bot (E3, E5) | `auto_merge` in eligibility; `workarounds` in manifests | shadow mode built (section 19); `auto_merge` must stay empty |
| 5 | Lock writer (G3, G7) | release-lock, built from section 4 plus a release spec (archive refs, output hashes) | built as `nanosoc-rm release draft` (section 14c) |
| 5 | POL-1 (proposal section 3.4): T0 on a silicon ref fails on an uncovered commit | a gate `t0.pins.silicon-covered`, added with G2 | reserved; B5's `--uncovered` exit 1 carries the signal |

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

The one list of names the acceptance tests (`tests/acceptance/test_{pins,ci_errata,scope_web}.py`) rely on. A
builder who wants a different name changes it here, through a CR, and in the tests' one helper.

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
[stores.artifactory]  url = "http://localhost:8082/artifactory"  release_repo = "asic-release"   # sections 21, 22
               token_file = "..."  token_expires = "YYYY-MM-DD"  publishers = ["<login>"]  shared_logins = ["<login>"]
               signing_key = "..."  allowed_signers = "..."  signer_identity = "nanosoc-rm"  ssh_keygen = "..."
               repos = ["<repo>"]  projects = { <component> = "<store project>" }   # all optional
[paths]        quarantine = "..."    # optional; default <data_dir>/quarantine
[roles]        default = ["member"]  # plus "<login>" = [...]
[schedule]     poll_minutes = 15  nightly_utc = "02:00"  fetch_minutes = 2
               # optional (CR-PR1), per host: private_read = "ssh"  or  { method = "token", token_file = "..." }
[auth]         mode = "github"       # CR-16: or "local", with [[auth.users]] login = "..."  hash = "scrypt$..."  roles = [...]
[quality]      api_key_file = "..."  model = "claude-sonnet-5-5"  paths = { <name> = "<local checkout>" }   # CR-14, all optional
[regen]        work_dir = "..."  read_only_roots = ["..."]  nice = 15   # section 20, all optional
[executor]     lease_seconds = 300  max_attempts = 2   # section 27 (CR-WG-executor); section 26 adds host, local_fallback
[[executor.agents]]  name = "eda-1"  token_sha256 = "<64 hex>"  labels = ["verilator", "vcs", "jobs"]  revoked = false
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
| `run_local` (CR-11, section 9a) / `compat` | `nanosoc_rm.t0.local` / `nanosoc_rm.analysis.compat` |
| `check_spec`, `materialise`, `run_tier`, `parse_verdicts`, `TierUsage` (CR-12, section 9a) | `nanosoc_rm.analysis.tierrun` |
| `TIERS`, `Tier`, `validate_tiers`, `tier_commands`, `check_budget` | `nanosoc_rm.t0.tiers` |
| `finalise_bundle` (and the runner's `main`) | `nanosoc_rm.t0.runner` (imported only in CI; the service never imports it) |
| `CIClient`, `FakeCI` / `trigger` / `fetch_pending`, `coverage` / `verify`, `RunRecord`, `Verdict` | `nanosoc_rm.ci.client` / `.trigger` / `.fetch` / `.verify` |
| `scan_dir`, `scan_paths`, `ScanResult` / `specimen` | `nanosoc_rm.scan` / `nanosoc_rm.scan.rules` |
| `check`, `CredentialStatus` | `nanosoc_rm.credentials` |
| `put`, `put_snapshot`, `latest`, `data_age`, `put_run`, `alerts`, `clear_alert` | `nanosoc_rm.db` |
| `tick` | `nanosoc_rm.schedule` |
| `submit`, `cancel`, `get`, `jobs`, `log_text`, `recover`, `Worker`, `JobUsage` (CR-17, section 13a) | `nanosoc_rm.jobs` |
| `hash_password`, `verify_password`, `LocalAuth`, `ConfiguredAuth` (CR-16) | `nanosoc_rm.auth` |
| `PLUGINS`, `load`, `render_page` (section 13a) | `nanosoc_rm.web.plugins` |
| `create_app(config_path, auth=None)` | `nanosoc_rm.web` |
| `FakeMembership(login=)`, `is_member(config, login)` | `nanosoc_rm.auth` |
| `WRITE_CALLS` | `nanosoc_rm.contracts` |
| `open_source`, `density`, `comments`, `readme`, `docs`, `trend`, `comment_patch` (CR-14, section 14a) | `nanosoc_rm.quality` |
| `matrix`, `queue`, `nightly_plan`, `NIGHTLY`, `bump_cell` / `run_cell`, `compare`, `baseline_for`, `quarantine`, `set_owner` / `capture`, `moved` (section 18; `bump_cell` for section 19.6) | `nanosoc_rm.matrix` / `.cells` / `.provenance` |
| `changelog_lint`, `plan`, `tag_check`, `manifest_init`, `draft`, `approve`, `yank`, `defect_trailers`, `is_yanked`, `allowed`, `classify_bump`, `Regenerator`, `NoGenerator` (CR-VER, section 14b; `classify_bump` for section 19.6) | `nanosoc_rm.versioning` |
| `draft`, `verify`, `drafts`, `forks`, `ReleaseUsage`, `ReleaseError` (CR-R1, section 14c) | `nanosoc_rm.releases` |
| `render`, `measure_class`, `stored_signals`, `pending`, `tree_paths`, `SandboxRegenerator`, `RegenUsage`, `RegenerationError` (section 20) | `nanosoc_rm.regen` |
| `regen_signals` (section 20.3) | `nanosoc_rm.versioning.consumers` |
| `scan`, `FreshnessUsage` / `judge_pin`, `judge_tree`, `age_word`, `days_since`, `SEVERITY` / `stored`, `trees`, `tree`, `pin`, `grid`, `same_pin_lags` (section 23) | `nanosoc_rm.freshness.scan` / `.rules` / `.view` |
| `JobKind`, `register_kind`, `registered`, `KIND_MODULES`, `all_kinds` (CR-VER, CR-R2, CR-WC-seams, section 13a) | `nanosoc_rm.jobs` |
| `ROUTERS`, `WAVE_C_POSTS`, `DELEGATES`, `mount`, `optional`, `route_table`, `writes` (CR-W1, CR-W6) | `nanosoc_rm.web.routers` |
| `deciding`, `causes`, `next_action`, `unused_waivers`, `Decision`, `Cause`, `GATE_LABEL`, `GATE_ITEM` (CR-W1) | `nanosoc_rm.web.verdict` |
| `rail`, `search`, `crumbs`, `component_flags`, `redact`, `uniform`, `TONE` (CR-W1) | `nanosoc_rm.web.shell` |
| `path`, `card_state`, `set_card`, `undo_card`, `decisions`, `decide`, `flags`, `set_flag`, `clear_basket` (CR-W2) | `nanosoc_rm.web.uistate` |
| `trailer_text`, `build_allowed`, `build_phases`, `LATER_AUTOMATION`, `GOVERNED_AUTOMATION`, `AUTOMATION_CODE`, `scheduled_jobs`, `enabled_automation`, `automation_reached`, `automation_in_use`, `outbound_writes` (CR-R3, CR-R4, CR-VER-a, CR-18-matrix-r1, CR-15c) | `nanosoc_rm.phasegates` |
| `automation_phase(name, declared=None)`, `LATER_SUBCOMMANDS`, `scheduled_jobs(enabled_only=True)` (CR-R8, CR-15c) | `nanosoc_rm.phasegates` |
| `settings`, `StoreClient`, `check_request`, `StoreError`, `StoreRefused`, `AQL_PATH` (section 22) | `nanosoc_rm.store.client` |
| `build_index`, `index`, `classify`, `parse_manifest`, `cache_collateral` (section 22) | `nanosoc_rm.store.index` |
| `latest`, `shown`, `taped_out`, `timeline`, `component_versions`, `release_rows`, `collateral`, `findings`, `search`, `commit_link` (section 22) | `nanosoc_rm.store.views` |
| `enqueue`, `claim`, `heartbeat`, `store_log`, `complete`, `cancel`, `expire`, `get`, `tasks`, `LeaseLost`, `TaskRefused` / `check_task`, `check_result` / `new_token`, `digest`, `identify` / `serve`, `PREFIX` / `available`, `claim`, `heartbeat`, `finish`, `expire` (section 27) | `nanosoc_rm.executor.queue` / `.task` / `.tokens` / `.api` / `.jobbridge` |
| `run_once`, `run_loop`, `run_job`, `execute`, `judge` / `ExecutorClient`, `check_path` / `mode_for`, `exposure`, `clean_env`, `argv_for`, `run` / `lay_out` / `load_agent_config`, `read_token` (section 27) | `nanosoc_rm.executor.agent` / `.client` / `.sandbox` / `.checkout` / `.agentconfig` |

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

`nanosoc-rm user add LOGIN [--generate] [--role R]...` (CR-16): prints an `[[auth.users]]` stanza; exit 3 for a bad
login or a password under 8 characters. `nanosoc-rm serve [--no-worker] [--i-know-this-is-exposed] [--tls-cert PATH --tls-key PATH] [--allow-plain-http]`
(section 13a).

`nanosoc-rm trace [--json]` (PR7): requirement → tests → commits, from the `requirement` markers and the
`Requirements:` commit trailers.

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

### 17.4 Test infrastructure

- **Markers** (registered in `tests/conftest.py`): `requirement("<ID>")` on every acceptance test; `live` on every test that needs the lab.
- **Not built yet:** an acceptance test whose module does not exist carries `@NOT_BUILT` = `pytest.mark.xfail(raises=ImportError, strict=True)`. It reports XFAIL while its import fails, FAILS on any other error, and FAILS as XPASS(strict) once it passes, which forces the builder to remove the marker. `conftest.py` imports `nanosoc_rm` before each such test, so before the package exists every acceptance test reports XFAIL whatever its inputs.
- **Lab inputs** (environment): `NANOSOC_RM_CONFIG` (the private service config), `NANOSOC_RM_REGISTRY` (a checkout of the registry's main), `NANOSOC_RM_TEST_DATA` (optional test-owned data dir for the pins mirrors), `NANOSOC_RM_TEST_MEMBER` (a SoC-Labs member's login), `RM_ARM_REFERENCE_FILE` (one real vendor file, read in place), `RM_RC4_BUILD_SOC_TAR` (the P1 archive).
- **Live doctrine:** a live test without its inputs FAILS; it never skips and never passes. Off the VM, deselect with `-m "not live"`.
- **Dev dependencies:** [`requirements-dev.txt`](../../requirements-dev.txt).

### 17.5 Write allowlist (`WRITE_CALLS`, A2, A6)

`nanosoc_rm.contracts.WRITE_CALLS` is a list of entries `{file, kind: http|exec, method, target, effect, guard, reason,
sites?}`. A static census of the package (AT-A2) must find every entry, and every call it finds must be covered by one.
The census resolves each file's imports, so an alias (`import subprocess as proc`), a from-import (`from subprocess
import Popen`, `from os import execvp`), a `getattr(obj, "delete")(...)` call and the `os.exec*`/`spawn*`/
`posix_spawn*` family are censused like the plain forms; a `getattr` call with a computed name is an unresolved
HTTP call (method `?`). `sites` (default 1) is how many call sites the census cannot pin to one target (target or
method `?`) one entry covers: one more in that file fails A2:

| effect | Meaning | Allowed |
|---|---|---|
| `write` | an outbound write | exactly one: `POST projects/:id/trigger/pipeline`, guard "target is `ci.project` only" |
| `read-post` | a POST that writes nothing | the OAuth code exchange `login/oauth/access_token`; the Messages API `v1/messages` (CR-14, Q4 rewrite, only with a configured key); the store's AQL search `api/search/aql` (section 22, CR-WE-index) |
| `local` | a call the census mistakes for HTTP (a sqlite `delete`) | only in a file that imports no HTTP library |
| `git-runner` | the one dynamic git runner (`nanosoc_rm.gitrun`) | at most one; guard: refuses `push`, `send-pack`, `receive-pack` at run time |
| `executor` | CR-WG-executor: the pull agent's POSTs to the release manager's OWN executor API (section 27.5); writes only the service's executor queue, never a git host, CI or store | only in `nanosoc_rm/executor/client.py`, POST, target `/executor/v1/`, with a guard: `check_path` admits the six API paths of the configured server before any I/O |
| `publish` | CR-ART-publish: a person's confirmed write to the release store (section 21) | only in `nanosoc_rm/publish/`, PUT or POST, with a guard: `Store.check()` allows new files under `<release_repo>/<project>/<version>/`, `api/build` and a status-only `api/build/promote/<name>/<number>`, both for the tool's own `rm-release-*` / `rm-tapeout-*` builds only, before any I/O; refused in the web process |

### 17.6 Phase-gate register (A7)

`docs/phase-gates.yaml`: `phases: [{phase: "Phase N", capabilities: [ids], entry_gate: [{criterion, check}],
automation: [names], status: not-met|gate-met|started, checked: {at, by, evidence}}]`. **CR-15:** a later phase's
code, routes and commands may be built before its gate; the gate governs enabling its `automation` (a name there
must not be a `schedule.JOBS` job until the phase is `gate-met`), and the commit rule below applies only to
commits made before `build_allowed.from` (2026-10-12). An automation is enabled when a `schedule.JOBS` job has its name, or when it is
REACHED (`phasegates.automation_reached`, CR-18-matrix-r1): `AUTOMATION_CODE` names each automation's code
(`nightly_matrix`: the package `nanosoc_rm.matrix`, the job kind `matrix-cell`, the flag `NIGHTLY.enabled`), and a
job whose code, read statically from its defining module and every `nanosoc_rm` module that module imports (at
any depth, including imports inside functions and `__import__`/`import_module` with a literal name), imports
that package or names that job kind outside the job runner's own kind table, enables it under any job name; so
does the flag set True. **Limit:** the static read does not follow a module name built at run time, and an
automation with no `AUTOMATION_CODE` entry (Phases 3 to 5 have no code yet) is matched by name only; bot pull
requests and published releases are not scheduler jobs at all (hard rule: the tool never writes to a forge). A phase moves to `gate-met` only with
`checked.at` and `checked.by` filled in by a person. A commit that changes `src/` and names (in its
`Requirements:` trailer) a requirement of a later phase must come after that phase's `checked.at`.
Contracts, schemas, docs and tests for later phases are gate-2 work and do not count (section 15).
**Exemptions (CR-5):** an optional top-level `exemptions: [{commit, ids, cr, reason}]` names a commit that named a
later-phase id before a CR corrected the text that told it to. It takes exactly `ids` out of that one commit (full
sha), and counts only while `docs/CHANGES.md` has a row whose CR column is `cr` and which names the commit's first 7
characters. The one entry today is build/ci `6946076` (D7, CR-5). History is not rewritten.
**Trailer only (CR-R3):** a commit names an id only in its `Requirements:` trailer lines; the subject and body are
never read (a review label `G1:` is not a claim).
**Build and enable (CR-15; CR-15a, CR-15b and CR-R4, merged as CR-15c).** An optional
top-level `build_allowed: {cr, from, phases}` counts only while `docs/CHANGES.md` has a row whose CR column is
`cr` (today CR-15, from 2026-10-12, Phase 2 to 5). For each listed phase (default all four) whose gate is not met,
implementation commits made from `from` on (default any time) that name its ids, and its routes and commands, are
reported as `built` (not problems); an earlier commit is judged by the commit rule above. Enabling stays gated for
every later phase whose gate is not met (rule 5): (a) an enabled `schedule.JOBS` job that declares that `phase`,
or whose name holds one of `phasegates.LATER_AUTOMATION[phase]` as a word anywhere (CR-R8: words split on
anything but letters and digits, case ignored, plural allowed; `nightly_matrix`, `auto-bump` and
`versions_refresh` count; a job registered `enabled=False` does not); (b) an automation the phase's `automation`
list (default `GOVERNED_AUTOMATION`) names, enabled by a job of that name or REACHED as described above; (c) for
Phase 4 and 5, any `WRITE_CALLS` entry with effect `write` other than the CI trigger.
**Subcommands (CR-R8):** the command list holds each top-level command and `group sub` for each subcommand. A
later phase may claim subcommands (`phasegates.LATER_SUBCOMMANDS`: Phase 5 claims `release draft`, `release
verify`, `release list`); a bare group then counts for its own phase (Phase 3's `release`, `version`) only when it
has a subcommand no other phase claims.

### 17.7 Gap index

The gate-2 index of where each acceptance-file gap was resolved is in
[the archive](../archive/history.md#gap-index-gate-2). The gap tables in `docs/acceptance/` keep their own copy.

## 18. Compatibility matrix (built per CR-15)

Requirements: D3, D4, D7, D8, D9 (Phase 2), B7; CR-15 (build now, enable at the Phase 2 gate) and CR-18-matrix
(`docs/CHANGES.md`). Package `nanosoc_rm.matrix`; front ends `nanosoc-rm matrix` and `/matrix/` over the same
functions (section 0.1). Local mode only: every cell is a local compat T0 run (section 9a) queued as a local job
(section 13a). Nothing is scheduled: the nightly matrix ships disabled.

```python
# nanosoc_rm.matrix
COLUMNS = ("pinned", "in-range", "latest-tag", "head")
def matrix(configuration, *, columns=None, components=None, config=None, now=None) -> dict   # matrix_view (not stored)
def queue(configuration, *, columns=None, components=None, cells=None, by, config=None) -> list[dict]   # jobs
def nightly_plan(configuration, now, *, config=None, weekly_day=6) -> list[tuple]           # what it WOULD queue
NIGHTLY = Nightly(name="nightly_matrix", enabled=False, gate="Phase 2 entry gate ...", covers=...)
# nanosoc_rm.matrix.cells
def run_cell(configuration, column, component=None, *, config=None, store=False, by="cli", job=None,
             compat_fn=None, capture_fn=None, url_map=None, policy=None) -> list[dict]  # [matrix_cell, matrix_quarantine...]
def compare(cell_fact, baseline, quarantined=frozenset()) -> dict;  def baseline_for(cell_fact, stored) -> dict | None
def quarantine(configuration, config) -> list[dict];  def set_owner(configuration, test, owner, by, *, config) -> dict
# nanosoc_rm.matrix.provenance
def capture(run_dir, root_commit, requested, overrides, scratch) -> dict;  def moved(requested, captured) -> list
# nanosoc_rm.matrix.versions (VersionPolicy: latest, parse_range, latest_in_range; the Phase 3 versioning module
# may supply its own policy with these three functions)
```

1. **Rows and columns.** One row per component of the configuration's latest stored walk (section 4), in tree
   order; a component pinned at more than one commit (B7) is one row listing every pin, and its cells move every
   pin (a pin below another moved pin of the same component is left to the outer override). Columns, each a commit
   of the component's canonical repo read from its mirror: `pinned` (no override; one run serves every row and is
   the D9 baseline); `in-range` (the highest release tag inside the range the parent's `nanosoc-component.yaml`
   `requires[]` gives this id and gitlink path; else "no range"); `latest-tag` (the highest release tag
   `vMAJOR.MINOR[.PATCH]`, no pre-release; keep/, archive/ and freeze names are not versions); `head` (the default
   branch head the host advertised at the last sync). No commit: the cell says why and runs nothing. A commit equal
   to every pinned commit: "same as pinned", which shows the pinned result.
2. **A cell** (`run_cell`): `analysis.compat(configuration, overrides)` with its work directory kept; after the
   run the tree is re-read from the run's own pin fetches (`provenance.capture`, offline: one bare repo borrowing
   the objects of the `fetch-*` slots only, never the reachability clones `refs-*`, the negative controls or the
   upgrade work) and the directory is deleted. A pin is captured only when the run fetched its commit; a pin the
   run did not fetch (`t0.pins.fetchable` "not fetched", or below one) is null, "not read", and never counts as
   "captured equals requested". T0 checks out nothing and runs no setup script, so on a T0 run the capture can
   only confirm or fail to read a pin, never find one moved; the capture's `note` and the cell page say so. The
   rule (moved = UNVERIFIED) is for runs that can move a pin (a later tier with setup scripts). Recorded per attempt: requested and captured
   commits, the moved pins, gates (id, verdict, detail), compat's subject, host CPUs and 1-minute load at start and
   end; per cell: tool versions (git, python, nanosoc_rm), the harness, `ip_hash` null (T0 reads no IP file list).
3. **Verdict.** D7: MemoryError, OSError, TimeoutError, or a FAIL whose every failing gate's detail matches an
   infrastructure signature is INFRA, never FAIL. Signatures are whole phrases a host prints (`timed out`, `timeout
   after`, `out of memory`, `killed by signal`, `OOM-killer`, `licence checkout denied`, `no space left on device`,
   ...), never a bare word, so a real finding that names a file such as `watchdog_timeout.v` stays FAIL; one
   failing gate without a signature keeps the cell FAIL. Overloaded host: each attempt records `host.overloaded`
   (1-minute load above `provenance.OVERLOAD_PER_CPU` = 2.0 per CPU at start or end); a FAIL on an overloaded host
   whose re-run is green is INFRA, not FLAKY (and never counts toward quarantine); red twice stays FAIL, with the
   overload noted in the reason. D4: a pin captured at another commit than requested makes the attempt UNVERIFIED. D8: a FAIL attempt is run
   once more in the same job (the worker runs one job at a time: alone); FAIL again = FAIL, then PASS/WARN/WAIVED =
   FLAKY (`flaky_gates`: the gates that failed only the first time; `blocks_auto_merge`), anything else UNVERIFIED.
4. **Quarantine (D8).** A test is one T0 gate of one configuration (`cell` when no gate differs). The second FLAKY
   of a test within 14 days stores a `matrix_quarantine` with `owner` (the login that ran the cell), `since` and
   `expires_at` = since + 30 days. Expiry needs no action: an entry past `expires_at` is listed as expired and no
   longer counts. `nanosoc-rm matrix quarantine CONFIG --owner GATE=LOGIN` names another owner.
5. **Baseline (D9).** A cell is compared gate by gate with the pinned cell of the same configuration commit whose
   start is nearest within 12 hours (the same night); the comparison names that run (`baseline.subject`, `nonce`).
   With none, it says so and compares with nothing: never with older history. A pinned run with no gate results
   (INFRA, unanswered) or one that read moved pins is used only when no other run of that night is usable; it is
   then named, and the comparison has no rows and a note saying why it was not compared. Each row's `change` is
   `same`, `new-failure` (the cell FAILs or is UNVERIFIED where the baseline was not), `fixed` (the baseline failed and
   the cell answered PASS, WARN or WAIVED), `unanswered` (the baseline failed and the cell gave no answer: INFRA,
   NOT-RUN or no row; never shown as fixed) or `changed`.
6. **Jobs.** Kind `matrix-cell` (params `configuration`, `component`, `column`) through `jobs.register_kind`
   (`nanosoc_rm.matrix.jobkind`: params, cli_args, outcome, limit_s, action): the worker runs
   `nanosoc-rm matrix cell CONFIG --column COL [--component C] --store --json`; the cell takes its job id from
   `NANOSOC_RM_JOB` and the login from that job's record. Ceiling 3600 s (two T0 runs). Submitting logs `run_t0`
   (H2). `queue` skips cells already queued or running, no-target and same-as-pinned cells, and queues pinned first.
   A job that ended `infra` with no stored cell shows the cell INFRA (D7).
7. **Nightly (D3), disabled.** `NIGHTLY.enabled` is False and no `schedule.JOBS` job reaches the matrix; the
   Phase 2 register lists `nightly_matrix` under `automation`, so the A7 check fails if it is ever enabled before
   the gate (CR-15): a job of that name, any job whose code reaches `nanosoc_rm.matrix` or the `matrix-cell` job
   kind, or `NIGHTLY.enabled` set True (section 17.6, `phasegates.automation_reached`).
   `nightly_plan` / `nanosoc-rm matrix nightly CONFIG` print what it would queue: the pinned column, every non-head
   cell whose commit moved since its last stored run, and the head column once a week.
- **Observations** (`schemas/matrix.schema.json`, kinds added to observation.schema.json without a bump):
  `matrix_cell` (event, `matrix_cell:<configuration>:<component or ->:<column>:<nonce>`), `matrix_quarantine`
  (event, `matrix_quarantine:<configuration>:<test>`; latest is current), `matrix_view` (derived, never stored).
  The schema refuses a FAIL cell with fewer than two FAIL attempts, a FLAKY cell that is not FAIL-then-green, a cell
  or attempt with a moved pin that is not UNVERIFIED or INFRA, a NOT-RUN cell with an attempt, an INFRA attempt
  under a FAIL or green cell, and a quarantine with fewer than two FLAKY cells.
- **CLI:** `nanosoc-rm matrix CONFIG [--columns a,b] [--component C]... [--run [--wait]] [--json]` (the grid; `--run`
  queues, `--wait` works the queue in this process unless the service's worker holds it); `matrix cell CONFIG
  --column COL [--component C] [--store] [--json]`; `matrix quarantine CONFIG [--owner GATE=LOGIN] [--json]`;
  `matrix nightly CONFIG [--json]`. Exit: view 1 when a shown cell is FAIL or FLAKY; else 2 when a shown cell
  is UNVERIFIED or INFRA (could not answer, section 0.2) or no cell has a green result; else 0 (NOT-RUN cells, which
  have no commit to run, count neither way); cell 0 PASS/WARN/WAIVED, 1 FAIL/FLAKY, 2 UNVERIFIED/INFRA/NOT-RUN; 3 usage (unknown configuration, component
  or column; `--component` with the pinned column).
- **Pages** (plug-in `nanosoc_rm.matrix.web`, navigation "Matrix"): `GET /matrix/`, `GET /matrix/{configuration}`
  (`?component=` repeatable, `&columns=`), `GET /matrix/{configuration}/cell?component=&column=`, `POST
  /matrix/{configuration}/run` (`scope` = cell | row | column | all, `component`, `column`; 303 back to the grid
  with `queued=N`, and N = 0 is shown as "Nothing was queued"). The cell page answers 400 for a non-pinned column
  without `component`, or the pinned column with one (the CLI's exit 3).
  The POST answers 404 unless the app has local routes (section 13a), and then the pages show the CLI command
  instead of buttons. Hooks: `data-matrix`, `data-column`, `data-component`, `data-cell="<component or ->/<column>"`
  with `data-verdict` and `data-status`, `data-matrix-run="<scope>"`, `data-quarantine`, `data-nightly`, and on the
  cell page `data-attempt`, `data-baseline`, `data-change`. Colours: README section 6 with FLAKY amber, always with
  the verdict as text.
8. **Tier cells (CR-WD-matrix-t1; D3, D4, D7).** A view or a cell may add tier t1 (`matrix(..., tiers=["t1"])`,
   `queue(..., tiers=)`, `run_cell(..., tier="t1", force_tier=False)`; package `nanosoc_rm.matrix.tiers`, `TIERS =
   ("t1",)`). A tier cell is a separate stored cell of the same (component, column): `fact.tier` = "t1",
   `fact.force_tier`; T0-only cells carry no `tier` and read as before (`cells.latest_cells(configuration, config,
   tier=None)` returns T0-only cells; `bump_cell` reads only T0-only cells). The run is `analysis.compat(...,
   tier="t1", force_tier=...)` (section 9a "Tiers"): T0, then the registry's tiers.yaml t1 command in a sandbox
   checkout of the same tree; a T0 FAIL skips the tier unless `force_tier` (a person's `--force-tier`; the job param
   `force_tier`; never from the grid's Run), and the cell then stays FAIL. Per attempt: the tier rows join `gates`
   (`tier.t1`, `tier.t1.<gate>`; SKIP read as NOT-RUN), `tier` holds the tier's run (verdict, reason, forced,
   seconds, exit code, killed, over_budget, missing paths, the last 20 log lines), and the attempt verdict is
   compat's `run_verdict`. D7 applies to the tier rows as to the T0 gates.
   - **Preflight** (`tiers.preflight(configuration, tier, *, config, environ=None) -> Ready`), before anything
     runs, in the view too (it reads tiers.yaml and stats paths; it queues nothing): no t1 command, or one
     `tierrun.check_spec` refuses, is NOT-RUN with the reason (a registry fact, not the host's; counted neither
     way); a tool the spec names that this host lacks is INFRA with reason `tool not on this host: ...` and one
     attempt that ran nothing (not even T0): an absolute path in the tier's own `env` that does not exist
     (`tierrun.missing_tools`), or the command's program (argv[0], or a shell string's first plain word that is
     not a shell builtin) not found on the tier's clean PATH or not executable. The view shows such cells with
     status `no-command` or `no-tool` and the reason; `cell_requests`/Run never queue them.
   - **D4 moved pins.** After the command, the tier's working checkout is re-read
     (`provenance.capture_checkout(tree, root_commit, requested)`): per requested tree path the commit checked out
     there (`rev-parse HEAD`); when that equals the request, the parent's index gitlink is compared with the
     gitlink the parent's own commit records, and a differing index entry (re-pointed by the command) is what is
     captured. The superproject is the repo at the tree root. A path with no repo (not laid out: the tier's `pins`
     did not need it) is null, not read. Recorded as `attempt.checkout` and `provenance.checkout`; a moved pin
     from either capture is in `moved` and makes the attempt and the cell UNVERIFIED (the schema rule of item 3).
     Just before the command `tierrun.run_tier` writes `<work>/tree-layout.json` (`tierrun.LAYOUT_RECORD`): the
     tree paths laid out and, per laid-out repo, its tracked changes then (`git diff HEAD`, path -> sha256 of the
     working file). With it the re-read adds `laid_out`, `lost` (laid-out paths, "" = the superproject, that hold
     no readable repo after the command) and `dirty` (per laid-out repo, tracked files the command changed:
     changed against HEAD and not identical to the before-state; the tier's declared `verdicts` and `ip_files`
     excepted). Lost or dirty makes the attempt UNVERIFIED (D4: inputs not captured, or other contents than the
     requested commits read), and so does a run whose command ran with no record (`laid_out` null): without it a
     removed repo cannot be told from one not laid out.
   - **IP hash (D4).** When the tier declares `ip_files` and the run wrote it, `provenance.ip_hash` = sha256 over
     the sorted distinct lines `<sha256 of the file>  <path>` of the listed files. Each listed path is normalised
     first: an absolute path inside the sandbox tree (`$TREE/...`, either spelling of the tree) becomes
     tree-relative, since the sandbox directory differs per run; `./x` is `x`; an absolute path outside the tree is
     kept. `attempt.ip` = {hash, note, files}. Otherwise null with the reason in `ip_hash_note`: no list declared,
     not written by this run, empty, more than `provenance.IP_LIST_MAX` entries, a listed path that is not a
     regular file (device, FIFO, directory: never read), listed files totalling more than
     `provenance.IP_BYTES_MAX` (2 GiB) bytes, or a listed file unreadable (a hash of part of the inputs is not a
     hash of the inputs). A T0-only cell keeps `ip_hash` null ("T0 reads no IP
     file list").
   - **D9.** A tier cell's baseline is the pinned cell of the same tier and the same night.
   - **Job.** Params gain optional `tier` and `force_tier`; the worker runs `matrix cell ... --tier t1
     [--force-tier] --store --json`; ceiling 3600 s + 2 x the tier's kill limit (budget x 1.5).
   - **CLI / pages.** `matrix CONFIG --tiers t1` (a "+ t1" line under each row; the tier's preflight is printed;
     exit codes count tier cells like any other); `matrix cell CONFIG --column COL [--component C] --tier t1
     [--force-tier]` (3 for `--force-tier` without `--tier`). `GET /matrix/{configuration}?tiers=t1` adds a row
     `data-tier-row="t1"` per component with cells `data-cell="<component or ->/<column>@t1"` `data-tier="t1"`
     and a `data-tier-preflight="t1"` banner (`data-ready`); `GET .../cell?...&tier=t1` (400 for another tier);
     `POST .../run` takes `tier` (scope cell) and `filter_tiers` (row, column, all).
   - **Schema** (additive, `schema` stays 1): `matrix.schema.json` `$defs/tier`, `$defs/tier_run`, `$defs/ip_read`;
     optional `attempt.tier|checkout|ip`, `matrix_cell.tier|force_tier`, `provenance.checkout`; `cell_view.status`
     gains `no-tool`, `no-command` and optional `reason`; `matrix_view` optional `tiers`, `pinned_tiers`,
     `rows[].tier_cells`. `eligibility.schema.json` `tier_command.ip_files`. Examples
     `matrix.cell-t1.example.json`, invalid `matrix.cell-t1-checkout-moved-fails.yaml`.
- **Not built here:** T2 cells (the matrix adds only t1), CI-run cells (needs gate 3).
## 19. Bump proposals in shadow mode (Phase 4 E1-E7; built per CR-15)

Requirements: E1, E2, E3, E5, E7 (Phase 4), H3; CR-15 and CR-WB-bumps (`docs/CHANGES.md`). Built and usable by
hand now; ENABLING it (a schedule, a bot that writes) stays behind the Phase 4 entry gate in
`docs/phase-gates.yaml`. Package `nanosoc_rm.bumps`; schema `schemas/bump-proposal.schema.json` (new file, additive).

**What it never does.** It never opens a pull request, pushes, writes a branch or tag, or calls a host's write API.
There is no bump writer credential (H3); a `[credentials.bump_writer]` or `[credentials.bot_tag]` in the config is
reported as a problem (exit 1) and never read. Every git call is a read through `gitrun.run_git` (no new
`WRITE_CALLS` entry). A proposal is a patch and commands for a person; every page and CLI text says so.

### 19.1 Functions (module paths)

| Name | Where |
|---|---|
| `proposals(configuration=None, *, config=None, store=False) -> Answer` (bump_scan first, then bump_proposal, deepest first) | `nanosoc_rm.bumps` |
| `stored(configuration=None)`, `scans()`, `get(id)`, `mark(id, login, decision, note=None)`, `marks(id=None)` | `nanosoc_rm.bumps` |
| `ledger(*, config=None) -> bump_ledger`, `schedule_status() -> dict`, `writer_problems(config)` | `nanosoc_rm.bumps` |
| `patch_text(fact)`, `commit_message(fact)`, `person_commands(fact)` | `nanosoc_rm.bumps` (`.patchfile`) |
| `scan`, `scan_all`, `proposal_id`, `BumpUsage` (exit 3), `CannotAnswer` (exit 2) | `nanosoc_rm.bumps.engine` |
| `parse_tag`, `allows`, `Version`, `RangeError` | `nanosoc_rm.bumps.versions` |
| `router` (plug-in), `job_params`/`job_args` (job kind) | `nanosoc_rm.bumps.web`, `nanosoc_rm.bumps.jobkind` |

### 19.2 The scan (E1, E3, E5)

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

### 19.3 The patch (`<id>.diff`)

Plain text first (the proposal, the commands for a person, a suggested commit message), then one
`diff --git a/<path> b/<path>` / `index <from>..<to> 160000` / `-Subproject commit <from>` / `+Subproject commit
<to>`. A person applies it with `git apply --index` (a gitlink lives only in the index, so a plain `git apply`
changes nothing when the submodule is not checked out). It does not apply to a branch that moved the gitlink.

### 19.4 Records: `<data_dir>/bumps.sqlite`

Not rebuildable, not facts, so apart from `rm.sqlite` (as `jobs.sqlite` is): `proposals` (id, first and last time
computed, the configurations, the latest bump_proposal observation), `proposal_docs` (the latest bump_proposal
per proposal AND configuration: one move proposed for two configurations keeps both documents, so
each configuration's tree paths, order, waits_for and matrix pins stay its own), `marks` (proposal, at, login,
decision `done` or `declined`, note: every mark is a write action logged with its login, H2), `scans` (the latest
bump_scan per configuration) and `ledger` (the latest bump_ledger and when it was computed). The bump kinds are not in `db.EVENT_KINDS`, so `/facts/<subject>` does not serve them;
`/bumps/facts/<subject>` does, with the same hooks.

### 19.5 The shadow ledger (E2)

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

### 19.6 Interfaces this module needs from other modules

Looked up at run time (`nanosoc_rm.bumps.interfaces`); a module not built here is not an error and the fallback
is named in the fact (`class_source`, `matrix.source`). Both providers exist (CR-WB-int):
`matrix.bump_cell` answers only the newest stored cell whose overrides are exactly the move (FLAKY reads as
UNVERIFIED), and `versioning.classify_bump` returns None when no C3 signal gives a class:

| Provider | Function | Fallback |
|---|---|---|
| versioning (C2, C3) | `nanosoc_rm.versioning.classify_bump(component, from_commit, to_commit, *, config) -> {"class", "detail", "sources"} \| None` | the changelog's declared class, else `unknown` |
| matrix (D3) | `nanosoc_rm.matrix.bump_cell(configuration, overrides: {tree_path: sha}, *, config) -> {"verdict", "observed_at", "href", "detail"} \| None` | a finished compat what-if job with exactly those pins |

### 19.7 CLI and job

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

### 19.8 Pages (plug-in `nanosoc_rm.bumps.web`, navigation "Bumps")

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

## 20. The regenerator: C3 consumer regeneration and C7 (built per CR-15)

Requirements: C3, C7 (Phase 3; built and used locally, nothing scheduled). Package `nanosoc_rm.regen`.

A configuration opts in with a `regen` entry in the registry's `tiers.yaml` (`eligibility#/$defs/regen_command`):
a tier command plus `output`, the tree-relative directory it writes (build_soc), and optional `include`/`exclude`
globs over it. Example: `schemas/examples/eligibility.tiers.example.yaml` (`multicore-master`).

### 20.1 A render (`regen.render`, `nanosoc-rm regen run`)

1. **Request.** The configuration's commit is resolved once (`ci.trigger.build_request`); every tree path where the
   stored walk pins the component (`regen.tree_paths`; a B7 split moves together) is overridden to the commit
   (`pin_overrides`, CR-11). Both renders of a measurement share that root commit.
2. **Tree.** The overridden tree is walked anonymously in a fresh sandbox under `[regen] work_dir`
   (`t0.gates.Context.for_local`: empty HOME, no credential, no user git config) and the spec's `pins` are checked
   out (`tierrun.materialise`). An override the walk did not reach: not run, UNVERIFIED. A pin not laid out or a
   tool absent on this host (`tierrun.missing_tools`): not run, INFRA.
3. **Run.** `tierrun.run_tier` (clean environment, PATH=/usr/bin:/bin, budget, kill at 1.5 x), plus:
   PYTHONDONTWRITEBYTECODE=1 always; `nice -n <[regen] nice>` (default 15); the `output` directory deleted first.
   Refused before any work (RegenUsage, exit 3): no `regen` entry; a set_env.sh sourced; `env` naming
   ARM_IP_LIBRARY_PATH or SOCLABS_DMA250_DIR (the registry schema refuses them too, so a registry naming one does
   not load); any path under a `[regen] read_only_roots` entry (symlinks resolved) in the command, the env or the
   work directory. Without the vendor library the generator skips its vendor-rendered parts (bus matrices,
   DMA-250); its own "skipping ..." lines are recorded as `coverage`. The configuration and component must be ids
   and the commit a full sha; `--out` under a read-only root is refused before any directory is made. A run killed
   at budget x 1.5, or one that fails on this host's environment (a missing Python module, `command not found`,
   exit 126/127), is status `not-run`, verdict INFRA: no measurement.
4. **Collect.** Every file under `output` (text, at most 4 MiB each; others counted as `binary`), normalised: the
   sandbox's paths become `$TREE`/`$WORK`, a line holding a date-time stamp is dropped. `flist/*` files are the C7
   file lists, one per variant. sha256 `digest` over the result: two renders of one commit give one digest.
5. **Cache.** `<work_dir>/cache/<configuration>/<component>/<commit>/<root commit[:12]>-<spec digest>-<tree paths
   digest>-<code version>/` (a partial `--at` render is never served as the full B7 move; a render made by other
   render code is a miss); a cached render is used only when its digest still matches its files (`--fresh` ignores
   it; `--out` never reads it). Failed renders are not cached.

### 20.2 The class measurement (`regen.measure_class`, `nanosoc-rm regen class`, job `regen-class`)

The class is `versioning.classify.diff_outputs` over the two renders (docs/versioning.md decision table): a removed
file or a removed/changed port, instance, address or IRQ line is breaking; an added file or shape line feature;
any other text change fix; identical output none. C7: build-affecting when a `flist/*` list differs. A generator
that renders the configuration at `from` and fails for it at `to` is breaking (C5: a consumer that built no longer
builds; `files` and `build_affecting` null); a `to` run killed or failing on the host's environment is INFRA, not
C5 (20.1 step 3). Verdict: PASS (class measured), FAIL (the generator failed at `from`:
no baseline), INFRA (not run here), UNVERIFIED (override not reached). Exit 0, 1, 2, 2; usage 3.

**Observation kind** `regen_class` (event, no snapshot), subject
`regen_class:<component>:<from>:<to>:<configuration>`, fact `regen.schema.json#/$defs/regen_class`. Examples:
`regen.class.example.json`, `observation.regen.example.jsonl` (REAL: multicore-master, 2026-10-14); invalid:
`regen.class-pass-without-class.yaml`, `eligibility.tiers-regen-vendor-env.yaml`.

### 20.3 Wiring into the classifier and the bumps class

- `versioning.consumers.regen_signals(component, base, head, config, regenerator=None)` gives the C3 consumers and
  C7 build signals: a live `Regenerator` when one is passed (`regen.SandboxRegenerator(config)` conforms), else the
  stored `regen_class` measurements of exactly that move (`regen.stored_signals`), else unmeasured, naming the
  `regen class` command that would measure it. A measurement that is not PASS leaves the signal unmeasured with its
  reason; one consumer unmeasured makes the signal unmeasured (C3 complete needs every consumer). Every
  configuration that declares a `regen` entry and pins the component is a consumer: one with no stored
  measurement leaves the stored signals unmeasured, naming the `regen class` command (as the live path, which
  regenerates each).
- `version next` (`versioning.plan`) uses it for the baseline-to-head move; `classify_bump` for a pin move, so the
  bump engine's `change.class` (`class_source: classifier`) carries the measured class, and its sources name each
  stored measurement (`stored regen_class:...`).
- `versioning.classify.consumer_signal`/`build_signal` no longer raise when a live render fails: the consumer is
  unmeasured with the reason, or breaking when only the head fails (C5, as 20.2).
- `nanosoc-rm regen pending [--run]` lists the moves a stored version plan or bump proposal would class that no
  stored measurement covers, per configuration with a `regen` entry pinning the component; `--run` measures them.

`[regen]` service config (all optional): `work_dir` (default `<data_dir>/regen`), `read_only_roots` (list),
`nice` (int, default 15). No page in this wave: the measurements reach the GUI through the version plans and bump
proposals that cite them. Phase 3 surfaces: the `regen` CLI group (`phasegates.LATER_SURFACES`); a scheduler job
named with `regen` is Phase 3 automation (`LATER_AUTOMATION`).

## 21. Publishing to the release store (CR-ART-publish; G8, D12, G3)

The tool has its own Artifactory token (one year, revocable); a person publishes release and tapeout manifests to
`asic-release` with a GUI button or the CLI. This relaxes "the web app is read-only" for exactly this one write
path. Module `nanosoc_rm.publish`.

### 21.1 Records (schemas `release-manifest`, `tapeout-record`)

- `asic-release/<project>/<version>/release-manifest.json` (+ `.sig`): built from a READY local release draft
  (section 14c) with a complete lock: version, tag, full commit of the superproject and of every pin, the
  configuration, the lock's md5 and sha256, the errata revision and digest, the gate verdicts, the raw GDS and
  netlist hashes of a silicon release, referenced stored artefacts (`--artefact ROLE=REPO/PATH`: path, the store's
  sha256 and md5, the raw md5 joined by the lock or by a raw-md5 property, never by a compressed object's checksum)
  and the draft's shared documents uploaded beside it (lock, ERRATA.md/.html, RELEASE_NOTES.md, GATES.md,
  CHECKLIST.md, build_soc.md5sums.txt; never the private archive copy). `<project>` is `[stores.artifactory]
  projects.<component>`, else the component id.
- `.../tapeout-<id>.json` (+ `.sig`): built from `<registry>/tapeouts/<id>.tapeout.yaml` (`$defs/tapeout_draft`,
  the Phase 3 backfill's shape; private): id, chip, foundry and shuttle, PDK and library versions, GDS raw md5 +
  layout hash + the stored object's sha256 (read from the store and checked against the draft and the store's
  `pnr.raw_md5`/`layout.canonical_hash` properties), submission date, foundry verdict, source commit and pins,
  sign-off reports, errata, documents, status draft/submitted/accepted/fabricated. It is filed in its release's
  folder, `<project>/<release version>/`: the draft's `release.project` (when given) must equal its `project`, a
  registered `configuration` must map (through `projects`) to that same project, and `release.manifest_sha256` is
  the sha256 of the release manifest STORED in that folder (read with GET; a draft naming another sha256, or one
  when none is stored, is refused; null when no release manifest is published there yet). The stored manifest
  must name the same project, version and configuration.
- Bytes are canonical JSON (sorted keys, two-space indent, one newline), with no publish time or publisher, so the
  sha256 a person confirms in the preview is the sha256 published.

### 21.2 Signatures

OpenSSH SSHSIG, namespace `nanosoc-rm`, `.sig` beside the record: `ssh-keygen -Y sign -f KEY -n nanosoc-rm FILE`
when `[stores.artifactory] ssh_keygen` (default: `ssh-keygen` on PATH) is OpenSSH 8.1 or later; otherwise, for an
RSA key, the module's own SSHSIG writer (rsa-sha2-512; standard library only), whose output `ssh-keygen -Y verify`
reads. RHEL 8 ships OpenSSH 8.0 (no `-Y`), so the tool host signs with an RSA key. An encrypted key is refused.
Verification reads `allowed_signers` (default: the key's `.pub`) and accepts only a key listed for
`signer_identity` (as `ssh-keygen -Y verify -I <signer_identity>` does), both before upload and on read-back.

### 21.3 Publishing (`publish.service`)

`preview(kind, key)` builds the record, reads the store with GET and HEAD only (each target path: absent, same or
differs; the build record; referenced artefacts) and writes `<data_dir>/publish/previews/<kind>/<key>.json`.
`publish(kind, key, confirm=SHA256, by=LOGIN)`:
1. readiness (url, a 0600 token file, a signing key this host can use, a public key to verify with, the token not
   expired) and `by` a listed publisher that is not a shared login, or the CLI's own `cli:<user>` (the shell user;
   any other `cli:` value is refused). A `by` given with `--by` is recorded with the account that ran the publish
   (`run_by`, `cli:<user>`: the GUI's job runs as the service user);
2. rebuild; refuse unless the sha256 equals `confirm`;
3. HEAD every target: one holding other bytes refuses the whole publish before any upload (never overwrite; OSS
   cannot forbid an overwrite, so the tool does); one holding the same bytes is a retry and is skipped;
4. sign, and verify the signature against the allowed keys BEFORE the first upload (the store is never
   overwritten, so a `.sig` left there must already verify); upload documents, record, signature, each with deploy-time properties (`rm.project`, `rm.repo`,
   `rm.commit`, `rm.dirty`, `rm.pins_md5`, `rm.config`, `rm.role`, `rm.raw_md5`, `rm.confidentiality`,
   `rm.release_version`, `rm.tag`, `rm.tag_commit`, `rm.manifest_sha256`; a tapeout adds `rm.tapeout_id`,
   `rm.subject_md5`) and checksum headers;
5. read back every path's sha256, the record and signature bytes, the signature, the record's property;
6. build-info `rm-release-<project>`/`<version>` or `rm-tapeout-<project>`/`<tapeout id>` (vcs: the superproject and
   every pin) and a status-only promotion RELEASED, or TAPED-OUT for a tapeout at submitted or later;
7. ledger: observation `release_publication` (event, subject `release_publication:<project>:<version>:<name>`), a
   local copy under `<data_dir>/publish/published/`, an `actions` row (`publish_release` / `register_tapeout`,
   refusals too) with the login.

### 21.4 Store client (`publish.client.Store`)

Bearer token only (`[stores.artifactory] token_file`, read per call, never logged; the CR-PR1 pattern, not a fifth
`[credentials]` entry). The base URL must end in `/artifactory` and carry no `user@` credentials (httpx would send
them as Basic auth), and no path has a `..` segment, so no call can reach the Access API, where a Basic-auth
failure locks the admin out. Allow-list checked before any I/O: GET/HEAD under the base; PUT only a new file
directly under `<release_repo>/<project>/<version>/` (never a checksum name) or `api/build` with a build named
`rm-release-<project>` or `rm-tapeout-<project>` (`check()` takes the build name, so only `put_build` reaches it); POST only `api/build/promote/<name>/<number>` for
those names (never a producer's build), with a body of the allow-listed keys `status`, `comment`, `ciUser`,
`timestamp`, `dryRun`, `copy`, `artifacts`, `dependencies`, `failFast` (no `targetRepo`, no `properties`, and
`copy`, `artifacts`, `dependencies` false). No DELETE, PATCH or property set exists.
`refuse_writes_here()` (called by `nanosoc-rm serve`) refuses every PUT and POST in the web process.

### 21.5 CLI, jobs, pages

- `nanosoc-rm publish-release CONFIGURATION/VERSION (--dry-run | --confirm SHA256) [--artefact ROLE=REPO/PATH]...
  [--by LOGIN] [--json]`; `nanosoc-rm register-tapeout ID (--dry-run | --confirm SHA256) [--draft FILE] [--by LOGIN]
  [--json]`. Exit 0 previewed or published, 1 refused, 2 could not answer, 3 usage (also: neither or both modes).
- Job kinds `publish-release`, `register-tapeout` (params: the key, `confirm` or none for a preview, `by`).
- Plug-in `nanosoc_rm.publish.web`, navigation "Publish": GET `/publish/`, `/publish/release/{configuration}/{version}`,
  `/publish/tapeout/{tapeout_id}` (stored facts only: the ledger, the preview file); local-only POST `.../preview`
  and `.../confirm` (a job each). Confirm needs the stored preview's sha256, a preview without problems and the
  ticked box; 403 for a login not in `publishers` or in `shared_logins`; 409 when readiness fails. The button is
  shown disabled with the reasons. In the web process readiness only stats the token and key files
  (`probe_signer=False`): it never parses the private key nor starts ssh-keygen; the job does both. The release draft page links to its Publish page.
- Phase 5 surfaces (`phasegates.LATER_SURFACES`): `/publish`, `publish-release`, `register-tapeout`. A scheduler job
  that reaches `nanosoc_rm.publish` or queues its job kinds is the `soc_release` automation (`AUTOMATION_CODE`).

## 22. The artefact store index (CR-WE-index; B10, G8, D12)

The tool reads what the artefact store (Artifactory, `[stores.artifactory]`) holds and stores it as one fact;
pages show that fact. Read-only: the store client admits GET and HEAD on any path except the Access and
security APIs, and one POST, `api/search/aql` (WRITE_CALLS `read-post`). The allow-list tests the
percent-decoded path and refuses an empty segment, a `;` (Tomcat's `..;`), `.`, `..` and a `%` left after one decode. A PUT is admitted only to the publish
service (`check_request(..., publish_repo=<release repo>)`, inside the release repository); the index never makes
one. Bearer token only (`[stores.artifactory] token_file`); never Basic auth, never a netrc, never a redirect.

### 22.1 Config (`[stores.artifactory]`)

`url` (the API base, `http(s)://host[:port]/artifactory`, no user or token in it), `release_repo` (or the gate-2
`repo`; default `asic-release`), `token_file` (a 0600 file holding one access token; the CR-PR1 pattern, NOT a fifth
`[credentials]` name: AT-A2's credential set is unchanged), `token_expires` (YYYY-MM-DD, shown with the index),
`repos` (the repositories AQL reads; default all), `projects` (`[stores.artifactory.projects] <component> = "<store
project>"`, shared with the publish unit: a person's statement of which store project folder a component's uploads
are in; a configuration's project is its component's; nothing else maps a store path to a configuration). The
index also reads the publish unit's trust keys: `allowed_signers` (else the `.pub` beside `signing_key`),
`signer_identity` (default `nanosoc-rm`), `ssh_keygen` and `shared_logins`. `publishers` is the publish unit's
alone: Artifactory's `created_by` on a published manifest is the tool token's user, never the person, so it is not
compared with it.

### 22.2 One run (`store.index.build_index`, `nanosoc-rm store index [--store] [--no-cache]`, job `store-index`)

Each server feature is probed before it is used; a feature the server lacks is a probe row NOT-RUN with its
reason, and the sections that need it are NOT-RUN too: `system.ping`, `system.version`, `aql.items` (every file
with repo, path, name, size, created, created_by, md5, sha1 and properties), `aql.properties`, `build.list` and
`build.info` (every build record, a number filed twice read once per record with `?started=`; the vcs block is not
AQL-queryable, so it is fetched per build and joined here), `storage.release_repo`, `download.manifest` (each
`*.json` in the release repository, at most 1 MiB, and its `.sig`). Each manifest's `signature`: `verified` (an
SSHSIG, namespace `nanosoc-rm`, over its bytes by an allowed signer; `publish.signing.verify`), `invalid` (a `.sig`
that does not verify), `unverified` (no allowed signers, a signature that could not be read, a format the tool does
not check) or `absent`; `trusted` is `verified` and not uploaded by a `shared_logins` login. A manifest or signature
that could not be read makes the section INFRA. Index verdict: INFRA when the store did not answer, refused the
token or returned a cut answer; UNVERIFIED when the file listing could not run (AQL absent); else PASS.

The fact keeps the rows a page needs: every stream (GDS), source archive, manifest, signature and document as a
row; one row per foundry-return folder; every other file only in the per-repository counts by role. Join keys:
the RAW md5 (`rm.raw_md5`, `submitted.raw_md5`, `pnr.raw_md5`, `file.raw_md5`), never the stored object's checksum
(a `.zst` describes compressed bytes). Git identity is what the store records (`rm.repo`, `rm.commit`,
`release.commit`, any `*.commit`, short ids kept as given); a row without one says "no git identity recorded".
Documents up to 16 MiB that are not marked confidential are downloaded into `<data_dir>/store-cache/<sha1>` and kept
only when their sha1 matches the store's; the GUI serves only those copies (`GET /store/file/{sha1}`).

**Observation kind** `store_index` (event, no snapshot), subject `store_index:artifactory`, fact
`store.schema.json#/$defs/store_index`, sources of kind `artifactory_api` (`METHOD path`, no host, no token).
Examples: `store.index.example.json`, `observation.store.example.jsonl` (synthetic); invalid:
`store.index-pass-without-items.yaml`, `observation.store-index-from-git.yaml`. Exit codes: 0 PASS, 2 UNVERIFIED or
INFRA, 3 no `[stores.artifactory]` or no readable token file.

### 22.3 What pages and commands derive (`store.views`, stored facts only)

- Configuration > Silicon, **Taped out** (a frozen configuration, or any with a tapeout manifest): each tapeout
  manifest naming it (or in its component's store project): foundry, shuttle, date, GDS raw md5 IN STORE / NOT IN
  STORE / MISMATCH (against the stream at the manifest's store path), against the release lock's `outputs.gds.md5`
  MATCH / MISMATCH / NO LOCK, and the commit linked on its host (`hosts.<host>.web`, https only). The streams of the
  mapped store projects follow, each with its git identity or "no git identity recorded". The registry's
  unpublished tapeout drafts naming the configuration (`<registry>/tapeouts/<id>.tapeout.yaml`, a local read) are
  listed under it with a link to `/publish/tapeout/<id>` (a draft that does not meet `$defs/tapeout_draft` says
  why); with drafts the panel shows even without a store or an index (CR-WE-int).
- Configuration > History, **Timeline**: release, candidate and silicon tags (stored `tag_check`), release drafts,
  manifests, and uploads in its component's store project, newest first.
- Component > Versions (`/versions/{component}`), **Tags, commits and chips**: per tag its commit link and the
  silicon configurations (stored trees, or their root) and tapeout manifests that pin it.
- Releases: **Published** and **Store** columns per draft (a manifest for that configuration and version; its GDS
  raw md5 in the store), and the documents with a download link when a verified copy is held.
- Trust: only a trusted manifest is green. Where a verdict would be IN STORE, MATCH or MATCH (lock) for an
  untrusted manifest, the page shows UNVERIFIED (amber) with the reason; the Taped out panel shows each record's
  signature state.
- Inbox cards (category silicon): `store_mismatch` (a manifest names a store path or raw md5 the store does not
  hold, disagrees with the release lock, or is not trusted), `release_not_archived` (a silicon configuration with
  no trusted manifest in the release repository; raised only when the manifests section is PASS),
  `tapeout_unregistered` (streams the store marks shipped that no trusted tapeout manifest of a registered silicon
  configuration covers, one card per store project). `nanosoc-rm store findings` prints them.
- Search (the command bar and `nanosoc-rm store search`): artefact path, name or run tag; a hex prefix of a commit,
  a build revision, a raw md5 or a stored md5.

### 22.4 Pages and schedule

Plug-in `nanosoc_rm.store.web`, navigation "Store": `GET /store/` (`?q=`), `GET /store/file/{sha1}`, and in a local
deployment only `POST /store/index` (queues one `store-index` job; a second press while one is queued or running
reuses it). No GET calls the store or queues work. Scheduler job `store_index` (nightly, v1, enabled): queues one
`store-index` job when `[stores.artifactory]` is configured and none is queued or running; the worker runs it as a
child process.

## 23. Catalogue characterisation (CR-WF-catalogue; A1, B1, H1)

The front page groups the catalogue as a ladder: a **project** (the
top-level repo that builds a chip or a chiplet: "the plate") wraps a **SoC** ("the meat"), which integrates
**subsystems** and **components** ("the potatoes"), built from **arch** tech units and **generators**; **toolkits**,
**flows**, technology **libs**, **infra** and **legacy** sit beside it. This section is the data model and the two
read paths the GUI unit builds on. Nothing here writes the registry. Package `nanosoc_rm.catalogue`.

### 23.1 The model (`catalogue.model`, `Registry.characterisations()`)

Catalogue schema (additive, `schema` stays 1): each component may carry

| field | values | where |
|---|---|---|
| `group` | project, soc, subsystem, component, toolkit, flow, generator, arch, lib, infra, legacy | any entry; `legacy` exactly when `class: legacy` (rule 15) |
| `type` | chip, chiplet (project); soc (soc); subsystem (subsystem) | required on the three ladder groups, refused elsewhere |
| `role` | implementation, forkable, example | required on the ladder groups, refused elsewhere |
| `wraps` | component ids | project (a soc or subsystem) and soc (a subsystem or component) only (rule 15) |
| `guessed` | subset of group, type, role, wraps, inside | fields still a judgement; `inside` = which project or SoC wraps it |
| `question` | text | the open question to the owner; needs `guessed` |

`ignored` entries keep their `reason` (unchanged). Defaults for a catalogue without these fields (every old
catalogue loads unchanged): group from class (`DEFAULT_GROUP`: soc_ref and example -> project, generator ->
generator, arch_tech -> arch, ip_tech -> component, toolkit -> toolkit, infra -> infra, legacy -> legacy), role
from class (`DEFAULT_ROLE`: soc_ref -> implementation, example -> example), type unknown (None) unless the
group fixes it; each field filled this way is listed in `derived`.

```python
@dataclass(frozen=True) class Characterisation:
    id: str; cls: str; group: str; type: str | None; role: str | None
    wraps: tuple[str, ...]; wrapped_by: tuple[str, ...]     # wrapped_by: the entries whose wraps name it
    guessed: tuple[str, ...]; derived: tuple[str, ...]; question: str | None
    top_level: bool                                         # group == "project"
    unconfirmed -> tuple[str, ...]                          # guessed + derived, in GUESSABLE order
    def as_doc(self) -> dict                                # the fields above plus unconfirmed, JSON-ready
Registry.characterisations() -> dict[str, Characterisation]   # catalogue order, wrapped_by filled
model.characterise(component_dict) -> Characterisation        # one entry, wrapped_by empty
model.characterise_all(component_dicts) -> dict[str, Characterisation]
model.socs_without_forkable_project(chars) -> list[str]       # group soc, no `forkable` project in wrapped_by
model.gap_unverified(chars) -> list[str]   # soc_ref entries with a derived group: while non-empty, an empty gap
                                           # list measured nothing (an uncharacterised SoC reads as a project)
model.effective_group(component_dict) -> str | None; model.class_for(group, role=None) -> str
model.class_agrees(group, cls) -> bool                        # cls in GROUP_CLASSES[group]
constants: GROUPS, LADDER_GROUPS, TYPES, ROLES, GUESSABLE, WRAP_TARGETS, DEFAULT_GROUP, DEFAULT_ROLE,
           CLASS_FOR_GROUP, GROUP_CLASSES, GROUP_LABELS, TYPE_LABELS, ROLE_LABELS (display words: "Forkable template", ...)
```

CLI: `nanosoc-rm catalogue show [PATH] [--group G]... [--gaps] [--unconfirmed] [--json]`, one row (or
`as_doc()` plus `gap: bool`) per component. Exit 0; with `--gaps`, 1 when any SoC has no forkable project,
2 (UNVERIFIED, stderr names them) when `gap_unverified` is non-empty. A page shows the gap check as unverified in
that case, never as "no gaps". WRAP_TARGETS: a project wraps soc, subsystem or component (its own custom
peripherals and accelerators); a soc wraps subsystem or component.
Examples: `component-catalogue.example.yaml` (8 entries characterised, the rest derived); invalid
`component-catalogue.type-on-toolkit.yaml`, `.project-without-role.yaml`, `.question-without-guessed.yaml`,
`.soc-typed-chip.yaml`.

### 23.2 The org scan (`catalogue.orgscan.org_scan`, job `org-scan`)

`org_scan(config=None, hosts=None, store=False) -> ScanResult(docs, errors, listed)`: lists the configured GitHub
org and GitLab group (with subgroups) through `mirror.inventory.list_host(..., anonymous=True)`: GET only, every
page, **no credential** even when the service has read tokens (`mirror.layout.api_get(..., anonymous=True)` sends
no Authorization header). Each listed repo the catalogue does not hold (not a component's canonical, copy or
alias, not external, not ignored) becomes a `repo_seen` observation (set kind, scope `repo_seen:<host>`, one
complete snapshot per host per scan; empty when nothing is new, so a repo catalogued or ignored since drops out).
Fact (`observation.schema.json#/$defs/repo_seen`): `repo`, `visibility`, `default_branch`, `created_on` (date or
null), `description` (whitespace-collapsed, at most 300 characters, or null), `mentions_nanosoc` (name or
description matches nano-soc: a hint, not a judgement), `fork` (the listing's own flag: GitHub `fork`, GitLab
`forked_from_project` present), `first_seen` (carried from the current stored fact),
`listing: anonymous`. A host whose listing fails stores nothing; `errors[host]` says why; the other host is still
scanned. Limit: an anonymous listing cannot see a private repo; `nanosoc-rm inventory` (with the read token) stays
the A1 census.

CLI: `nanosoc-rm catalogue org-scan [--host github|gitlab]... [--store] [--json]`; exit 0 nothing new, 1 new repos
seen, 2 a host could not be listed; the last stderr line is the summary (`org-scan: N new repo(s) ...`, with
`could not list <host>` on a failure). Job kind `org-scan` (params `{host?}`; runs
`catalogue org-scan [--host H] --store --json`; verdict PASS when every host was listed, UNVERIFIED otherwise;
result `{new, repos}`). A page reads `db.latest("repo_seen")`; it queues the job only from a POST, never from a
GET. The job name holds no word a later phase's gate governs (phasegates rule 5a). CR-1015-orgscan (David
2026-10-15, "Nightly + Scan button"): the scheduler job `org_scan` (nightly, at or after `schedule.nightly_utc`;
v1, enabled, not a CI job) queues one `org-scan` job with no params unless one is queued or running, and only when a
GitHub org or GitLab group is configured (`orgscan.scanned_hosts`); the Catalogue page's **Scan now** is
`POST /catalogue/scan` (section 25), which queues the same job.
Examples: `observation.catalogue.example.jsonl`; invalid `observation.repo-seen-with-credentials.yaml`.

### 23.3 The catalogue-change preview (`catalogue.preview.preview`)

`preview(registry_or_path, choices) -> Preview(patch, changes, problems, catalogue)`; `Preview.valid` is
`not problems`; `Preview.as_doc()` gives `{patch, changes, problems, valid}`. Like the review basket, the tool
never writes the registry: `patch` is a unified diff of `catalogue.yaml` (`a/`, `b/` prefixes, path relative to
the registry repo's top, so `git apply` works there) that a person reviews and applies by merge request.
`problems` is what the loader would say about the patched catalogue against the registry's other files (schema,
then rules 1 to 15), formatted `file:pointer: message [rule]`; an empty list means the patch loads. Each
component entry is one flow-mapping line and only its mapping is rewritten (comment kept; characterisation fields
right after `class`); an entry written over several lines is a problem, not an edit. `PreviewUsage` (CLI exit 3)
for a choice it cannot read. Choices, applied in order:

```
{"op": "set",     "id": ID, "fields": {group|type|role|wraps|question: VALUE or null}}   # confirms what it sets
{"op": "confirm", "id": ID, "fields": [group|type|role|wraps|inside, ...]}           # drops them from guessed
{"op": "add",     "repo": {"host", "path"}, "group": G, "id"?, "type"?, "role"?, "wraps"?}  # class = class_for
{"op": "ignore",  "repo": {"host", "path"}, "reason": TEXT}                            # "Not nanoSoC"
```

`set` of group soc/subsystem fills the matching type; a group off the ladder drops type, role and their guesses;
off project/soc it drops wraps. A new group that may not carry the entry's class (`GROUP_CLASSES`) sets the
class to `class_for(group, role)` and the change line says `(class OLD -> NEW)`; so moving into or out of
legacy is one choice. `question` goes once `group` is no longer guessed. `confirm` of a derived field
writes the derived value out. CLI: `nanosoc-rm catalogue preview CHOICES|- [--registry PATH] [--out FILE]
[--json]`: the patch on stdout (or the `as_doc()` JSON); exit 0 valid, 1 the patched catalogue would be
refused, 3 a bad choice or an `--out` inside the registry.
## 24. Submodule-tree freshness (CR-WF-fresh; B2, B3, H1, A3)

How far each pin of each registered configuration lags its upstream default branch, when its parent last moved
it, and one verdict per tree, for the projects home page (Home), the freshness grid and the detail panel (David
2026-10-14: "a very clear indication of whether a submodule tree is outdated or not and how long since it's been
updated last"). v1 monitoring, no later-phase automation: the job and route names carry no LATER_AUTOMATION word.

### 24.1 The scan (`nanosoc_rm.freshness.scan`)

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
  shared by several trees is measured once. Four eth/multicore/quickstart trees (140 pins): 4.1 s on the service host.
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

### 24.2 The rule (`nanosoc_rm.freshness.rules`, pure: no git, store or clock)

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

### 24.3 What pages call (`nanosoc_rm.freshness.view`; stored facts only)

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

### 24.4 Job, schedule, CLI, route

- **Job kind** `freshness-scan` (`nanosoc_rm.freshness.jobkind`, in `jobs.KIND_MODULES`): params
  `{configuration: id | null}`; runs `nanosoc-rm freshness scan [CONFIG] --store --json`; outcome PASS, or
  UNVERIFIED when a tree had no stored walk; result `{trees: {id: state}, pins, not_measured}`; ceiling 900 s.
- **Scheduler** job `freshness_scan` (every `poll_minutes`, immediately after `poll`, so after its walks): queues one
  `freshness-scan` job unless one is queued or running. Not a CI job: it runs in a local deployment too.
- **CLI:** `nanosoc-rm freshness scan [CONFIG]... [--store] [--today YYYY-MM-DD] [--json]` (exit 0 answered, 2 a tree
  has no stored walk, 3 unknown configuration or bad date; an outdated tree is not exit 1: the scan measures, it
  does not gate). `nanosoc-rm freshness show [CONFIG] [--tree] [--today] [--json]` prints the stored facts
  re-judged (exit 2 when none is stored).
- **Routes** (plug-in `nanosoc_rm.freshness.web`; no navigation entry, `NAV_LABEL = None`). CR-1015-retire (David
  2026-10-15, "Retire it"): the `/freshness/` page duplicated Home and the Matrix's Freshness tab and is retired.
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

## 25. The front pages: Projects, Matrix, Catalogue, Notifications (CR-WF-gui; H1, A3, A4, A5, PR4)

Requirements: H1, A3, A4, A5, PR4. This section REVERSES
CR-W1's inbox at `/`: `/` is the projects home and the inbox is Notifications behind the bell. Section 13's `GET /`
hooks (AT-H1: `data-configuration`, `data-item`, `data-verdict`, `data-run-t0`) stay on `/`, in the configuration
board closed under the tree. Every page reads stored facts only: a GET never walks, scans, fetches, queues a job or
creates gui.sqlite. Every page keeps one data-age element; a state is always a word beside a shape.

| Route | Shows or does | Module |
|---|---|---|
| `GET /` | the header and toolbar, the project -> configuration -> pin tree, the detail panel, the board (closed). Query (all optional; every row and control is a link carrying it): `v=1` (an explicit view: no default opening), `o` (repeatable; an open row key), `sel` (the selected row key), `f` (a freshness state), `lv` (project, soc, subsystem), `ty` (chip, chiplet), `ro` (implementation, forkable, example), `gap=1` (No forkable project), `filters=1` (the Filters disclosure open), `tl=on\|off` (the timeline for this view only) | `web.home` (`routers.DELEGATES["dashboard"]`) |
| `GET /matrix/` | tab **Freshness** (default): one row per repository, one column per stored tree; query `group` (`all`, project, toolkit, flow, component, generator, arch, lib, infra, legacy, ext), `rows=all` (also rows with no lag), `sort=behind\|age`, `comp` + `cfg` (the cell whose detail line is open). `?tab=compat`: the section 18 configuration list, unchanged; `/matrix/{configuration}` and every other matrix route are unchanged | `matrix.web`, `web.fmatrix` |
| `GET /catalogue/` | the new-repo queue (current `repo_seen` snapshots), "needs your call" (entries with a `question`), every group, the ignored list, "What Save writes" | `catalogue.web` (plug-in) |
| `GET /notifications` | one line per item (`?show=all`: read items too), "notify me about" | `web.notifications` |
| `GET /inbox` | the inbox cards (`web.inbox`, CR-W1, CR-W3) with evidence, snooze and undo; `?cat=` filters by category | `web.inbox` |
| `POST /home/timeline` | field `timeline` (`on`\|`off`), `return` (a `/` or `/?...` URL); saves the login's `timeline` preference; 303 back | `web.home` |
| `POST /notifications/read` | fields `item`, `fp` (the item's fingerprint), `read` (`0` = mark unread); 303 to `/notifications` | `web.notifications` |
| `POST /notifications/read-all` | repeated `item` + `fp` pairs: exactly the items the page showed (400 when none); a later item stays unread | `web.notifications` |
| `POST /notifications/prefs` | repeated `listed` (each preference the form showed) and `on` (the ticked ones); an unticked listed name is saved `off`, a ticked one removed; 400 for a name `uistate.PREF_NAME` refuses | `web.notifications` |
| `POST /catalogue/scan` | **Scan now** (CR-1015-orgscan): no fields but the CSRF token; queues one `org-scan` job (section 23.2; read-only, anonymous listing) as the signed-in login, or finds the one queued or running, and 303s to `/catalogue/?job=N#new`; 400 when no org or group is configured. The page reads the job records only (`catalogue.web.scan_status`: the `?job=N` org-scan job, else the latest; hooks `data-org-scan`, `data-org-scan-job`, `data-job-state`); the button is a `form="catalogue-scan"` button whose form sits outside the patch form | `catalogue.web` |
| `POST /catalogue/patch` | the page's choices (`new.<i>.repo`, `new.<i>.group`, `new.<i>.other`, `new.<i>.type`, `new.<i>.role`, `ask.<id>`, `confirm.<id>`) -> `catalogue.preview` (section 23.3). A new-repo choice applies to the repo its row's hidden `new.<i>.repo` (`<host>:<path>`) names, never to whatever repo now sits at row `i`; a repo no longer in the queue (or a row without the key) is skipped and listed as not used. Every new repo starts on Decide later: the suggested group is a hint (`data-suggest`), so an untouched Save changes nothing; `do=preview` shows the patch, its changes and whether it would load; `do=download` returns `catalogue.patch` (`text/x-diff`; 400 when nothing changes). Writes nothing (no registry, no store, no gui.sqlite) | `catalogue.web` |

The six POSTs are local-only (`web.plugins.local_routes`; `web.routers.WAVE_F_POSTS` lists the core four) and
CSRF-checked by the section 14 guard; a GitHub + CI deployment keeps section 13's two Run routes as its only writes
and its pages show no button for a missing route (the timeline toggle is then a link changing `tl` in the URL).

- **The tree.** Projects are the catalogue's ladder entries (group project, soc, subsystem; `catalogue.model`, from
  the stored `registry_record` component records) plus any configuration root the catalogue does not characterise.
  A configuration row is its `freshness.view.trees` summary (its verdict = worst DIRECT pin; a frozen tree says
  Frozen and "not counted"); a pin row's state is its `rollup` (the worst live state at or beneath it). Siblings
  sort by `home.sibling_rank`: SoC, custom peripherals (a direct component or subsystem of the configuration
  root), subsystems and components, arch tech, generator, libraries and infra, toolkits and flows; then worst state
  first. A project with no measured configuration folds into one "Not measured yet" group (`grp:unk`).
- **Pills** count live configurations by tree verdict and frozen ones as Frozen; the Not measured pill adds the
  projects with no stored scan to the scanned live trees that read Not measured, so every configuration is in one
  pill, and its filter keeps both. CR-1015-unmeasured: a tree reading `current_except` is in its own pill
  ("Current except not measured", `f=current_except`; shown, like Unpushed, only when a tree reads so); its row says
  "Current except N not measured" in amber beside a half-filled dot (`data-state="current_except"`, CSS key `cxn`), and
  a project whose worst live tree reads so says it with N summed over those trees. `data-state` is a
  `freshness.rules.TREE_STATES` value. The Matrix column head prints the tree word; a repository row with a live cell
  nobody could measure says "current except N not measured" (or "not measured") and stays under Lagging only.
- **No forkable project** chip (`gap=1`): counts the SoCs no forkable project wraps
  (`catalogue.model.socs_without_forkable_project`). While `catalogue.model.gap_unverified` is non-empty (a soc_ref
  with no stated group defaults to project, so the check cannot see it) the chip adds "· unverified"
  (`data-gap-unverified`, its title naming those entries): a count of 0 then measured nothing, as `catalogue show
  --gaps` exits 2.
- **Default view** (no `v=1`): the most lagging project with an Outdated or Unpushed live configuration is open,
  with its first live configuration; everything else (frozen trees included) is closed. **Timeline**: a bar from each row's newest bump to today on one axis (`_TL_MONTHS` whole months before
  today's month to `_TL_AFTER` days after today), the today line, and a dashed line at each frozen configuration's
  root commit date (its tapeout).
- **Page hooks** (tests read pages only through these): `data-row` (row key: `p:<project>`, `c:<configuration>`,
  `n:<configuration>:<tree path>`, `grp:unk`) and `data-state` (a `freshness.rules.TREE_STATES` value) on each tree
  row, with `data-subject`/`data-observed-at` when the row is a stored fact; `data-fresh-pill` (state) on each pill;
  `data-timeline` (`on`\|`off`); `data-gap-filter`; `data-bell` (unread count, empty when unknown) on the bell;
  `data-matrix-row` (component), `data-column` (configuration), `data-cell-state` and `data-detail` on the matrix;
  `data-new-repo` (`<host>:<path>`), `data-ask` (component id), `data-group` (group), `data-pending`,
  `data-preview-valid` on the Catalogue page; `data-item` (item id) and `data-read` (`true`\|`false`) on each
  notification.
- **Repository, line and submodule pin** (CR-WG-subrepo-ui; `web.lines`). One vocabulary on Home, the
  configuration pages, the component workspace, the Matrix and search. A configuration row is a **line** of its
  repository: its name is `lines.line(record, root commit)["text"]` ("branch main @ 8e6d043", "tag v1.0.0 @
  ab73e40"; no " @ sha" when no commit is known), its configuration id beside it (`fp-alias`). A pin row is a
  **submodule pin**: "path @ sha", the path relative to the gitlink holding it, the component beside it only when it
  differs from the path's last segment. A repository pinned at N > 1 different commits in one tree carries
  "PINNED ×N" on each of its rows (`data-pinned-again`; `lines.pinned_again`), so two gitlinks of one repo never read
  the same. A branch of a pinned repository prints as `<repo>:<branch>` (`lines.qualified`). Each row kind has an
  inline-SVG icon (`_ui.html` `glyph(kind)`, kinds `lines.KINDS`: repo, branch, tag, pin, again; aria-hidden, hook
  `data-glyph`); "How is this judged?" prints the legend (`data-legend`, `lines.LEGEND`). The Matrix groups the
  columns of one repository under a spanning head (`data-column-repo`), each column head is the line (its id and
  verdict under it), and a repository's cell in its own root's column reads "root @ sha" (`data-cell-root`); the
  Compatibility tab's configuration list has a Line column (`data-ref`). The component Overview lists the
  repository's own lines (`data-lines`, `data-line` = configuration id). The rail groups configurations under their
  repository (`data-rail-repo`). Search names a configuration "line of <repo> · branch main". The word SPLIT is
  retired from the GUI: a pin split reads PINNED ×N (PINNED AGAIN when the walk holds one of the commits), a host
  copy that differs reads "HOSTS DIFFER: <host> <relation> N". Spec codes print as words with the code in a
  tooltip (`web.terms`: `plain` Jinja filter, applied to every `_ui.html` chip; host states such as ON_DEFAULT ->
  "on default branch", tiers T0-T6 -> "resolve check" .. "ASIC flow", requirement IDs dropped from headings into a
  `title`).
- **Notifications.** Items are `inbox.cards` (one per card; kind = the card's category) plus `catalogue:new-repos`
  (kind `catalogue`) when the current `repo_seen` snapshots hold any repo. Read state is the inbox's done state
  (`uistate.set_card` with the item's fingerprint): an item read here is done in the card view, and it comes back
  unread when its facts change. The bell counts unread items of kinds and projects the login has not muted.
  An item's projects (`Item.about`) are those of the configuration or project its id names and of every
  configuration its facts or evidence name (an erratum's exposures, `never_checked:all`); muting projects hides an
  item only when all of its projects are muted, and an item about no project is muted only by its kind.
- **gui.sqlite** (`web.uistate`, `user_version` 2): table `pref (login, name, value, at)`; names `timeline`
  (`on`\|`off`; absent = off), `notify.kind.<kind>` and `notify.project.<id>` (`off` mutes). `prefs()` never
  creates the file; a version-1 file gains the table the next time it is opened, other tables untouched.

## 26. Test capabilities per tree, test-bump, the bump gate and the executor queue (Wave G; D1, D3, E3, PR6)

Requirements: D1, D3, E3, PR6. Agreed: David 2026-10-16 (interactive): evaluation fix wave, decisions 1 to 4:
(1) tier runs execute on an EXECUTOR (a pull agent on the EDA host, sandboxed and unprivileged; GitLab CI later),
never on the service host; (2) each tree declares its test capabilities IN THE REPO, read at the commit under test;
the registry's `tiers.yaml` keeps only host site data; (3) hooks on a submodule pointer change WARN locally and
BLOCK in CI and in the release manager's bump gate; (4) all four streams built now.

### 26.1 `.nanosoc/capabilities.yaml` (schemas/capabilities.schema.json)

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
`capabilities.tidelink.example.yaml` (the evaluation's drafts), `capabilities.wave-g-spelling.example.yaml`.

`capabilities.manifest`: `parse(text)` checks the schema, then: ids unique, every named tool declared, `provides`
never sets TREE/PATH/HOME/TMPDIR/NANOSOC_*, compose paths unique; any problem refuses the WHOLE file (no partial set).
Defaults: `cwd` ".", `gate` block, `when` [on_bump], `budget_s` from `runtime` (S 60, M 900, L 10800, XL 43200; else
900), `runtime` from `budget_s`. `Reader(config).at(component, commit)` reads
`git -C <mirror> cat-file -p <commit>:.nanosoc/capabilities.yaml` (a partial mirror fetches the blob from its
promisor, with the host's read credential), never a working tree: status ok | absent | invalid (with problems; also
when the file names another component) | unreadable (no mirror, the commit not in it). Settled reads (ok, absent,
invalid) are cached per (mirror, commit) across requests: a commit's content never changes.

### 26.2 Site data: registry `tiers.yaml` `hosts` (eligibility#/$defs/tiers_file)

`hosts.<host id>`: `tools` (name -> `version`, `bin`), `licences`, `env` (values of the trees' `env.site` names;
never PATH, HOME, TMPDIR or a tool variable), `needs` (pdk, network, board:<kind>). Host ids, never hostnames.
`Registry.sites()`. The CR-12 `configurations` block stays for `compat --tier` until each tree carries a file.
`capabilities.plan.host_check(cap, tools, site)` lists why a host cannot run a capability: a tool it lacks, a version
under `min` (compared part by part: numbers as numbers), a licence it lacks, a need it cannot meet (`<tool>-seat`
is met by the tool; `regen:`, `after:`, `lease:` and `siblings` are run-time needs, not host ones).

### 26.3 Composition, the bump run set, `nanosoc-rm test-bump`

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
- **Compare** per row: same, better, worse (green -> FAIL), same-red, new-red (no green baseline), new, gone
  (only when After's manifest reads ok; a capability lost to an absent, invalid or unreadable After manifest is
  not-run), not-run. Functional verdict over the block rows: worse FAIL > new-red/not-run UNVERIFIED > same-red WARN > PASS; no
  block row at all is UNVERIFIED; Now and After on different root commits is UNVERIFIED. T0 per side is `hygiene`,
  reported and never hiding the rows (decision 4). Exit 0 PASS or WARN, 1 FAIL, 2 UNVERIFIED, 3 usage.
- The document (schemas/test-bump.schema.json, kind `test_bump`) is the job's result, never a stored fact.

### 26.4 The bump gate (`capabilities.gate`)

`evidence(config, configuration, tree_paths, to, root_commit)`: the newest finished `test-bump` job whose result
tested exactly this move (configuration, root commit = the walk's, moved commit, a superset of the tree paths;
not `--plan`). A PASS document whose job exited non-zero counts as UNVERIFIED (never greener than the exit
code). `bump_proposal.fact.test_bump` records it (bump-proposal schema, optional). Anything but PASS makes
`decision.would` = hold with the second blocker "bump gate: ..." naming the `nanosoc-rm test-bump` command; the E2
ledger then scores the proposal `held`, as for any held proposal.

### 26.5 The executor queue (`jobs`, section 13a)

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

### 26.6 Pages

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

### 26.7 `GET /api/v1/bump-check` (the repositories' CI and pre-push hook; integration review 2026-10-16)

Query `component` (catalogue id of the asking repository), `path` (the gitlink's tree path), `to` (40 hex),
`root` (40 hex: the asking repository's commit BEFORE the move, a CI range's base), optional `from`. Answer 200
`{verdict, url, command, configurations, note}`: for each registered configuration of `component`,
`gate.evidence(config, configuration, [path], to, root)`; a PASS in any one answers PASS with its job page;
otherwise the first non-PASS verdict found, or null with a `note` (none finished, other root commits, no
configuration of that component). `command` is the `nanosoc-rm test-bump CONFIG PATH TO` to run. 400 a malformed
parameter. Who may ask: a signed-in member (session), or a bearer READER token: `[[api.readers]]` in the service
config (`name`, `token_sha256`, optional `revoked`; `nanosoc-rm executor token new NAME --out F --reader`). The
Guard answers a bearer request for this one path before its session check (`capabilities.bumpcheck`); a reader
token opens no other route and an executor token does not open this one (the two digest lists are separate, and
config load refuses a digest in both). Read-only: job records and the registry; nothing runs, queues or fetches.

## 27. The remote tier executor (CR-WG-executor; D1, D4, A2, A6, PR6)

Tier runs need the EDA tools that only the EDA login host has (VCS, Xcelium, Verilator, Genus); the service host has
none. A **pull agent** on the EDA host claims work from the service,
runs it **sandboxed and unprivileged**, and posts the result back; GitLab CI may take its place later. The service
never connects to the agent, never runs a tier command, and never hands the agent a credential.

Package `nanosoc_rm.executor`. Two queues, one transport:

| Queue | What one entry is | Producer | Where it is kept |
|---|---|---|---|
| capability **task** (27.2, 27.3) | one command of one tree at one commit, self-contained (mirror ids, commits, pins, command, budget, verdict rule) | `executor.queue.enqueue` (the capabilities unit's planner, `nanosoc-rm executor enqueue`) | `<data_dir>/executor.sqlite` |
| tier **job** (27.6) | a whole `nanosoc-rm` command of an executor job kind (`test-bump`, `compat --tier`, `matrix cell --tier`; section 26) | the local job queue (`jobs.submit`) | `<data_dir>/jobs.sqlite` (section 13a), plus a lease row in `executor.sqlite` |

### 27.1 Who owns what (the agreement with the capabilities unit, section 26)

- Section 26 (capabilities) owns WHAT runs: the manifests, the plan, the job kinds, the Now/After comparison and the
  bump gate. It marks a job kind as an executor kind; `jobs.claim_for_executor(config, executor, accepts=None)` and
  `jobs.finish_for_executor(config, job_id, executor, rc, stdout, log)` are its seam.
- This section owns HOW it reaches the EDA host and runs there: the executor token, the API, leases and expiry, the
  checkout from the read-only mirror, the sandbox, limits, and the verdict of a capability task.
- `executor.jobbridge` is the only service-side caller of the two job functions. When the job queue lacks them the
  API hands out tasks only (`jobbridge.available()`).
- Config: section 26 reads `[executor] host` and `local_fallback`; this section reads `lease_seconds`,
  `max_attempts` and `[[executor.agents]]`. The keys share the `[executor]` table and the merge must keep both sets in
  `config._EXECUTOR`. A site's host paths and licence values stay in the registry `tiers.yaml` `hosts` (decision 2);
  the agent's own `[sandbox] env`, `path` and `ro_binds` are what that host actually binds.

### 27.2 The task and result contract (`schemas/executor.schema.json`, `executor.task`)

- **task** (`$defs/task`): `subject {configuration|null, component, capability, side?, pair?}`, `mirror` (catalogue
  entry id: `<mirror_root>/<id>.git` on the agent, the section 3 layout), `commit` (40 hex), `pins [{path, mirror,
  commit}]` laid out inside the root, `run {tier t1..t6, command (shell string or argv), cwd?, env?, budget_s,
  verdict {exit = 0, forbid [...], verdicts?}, control?}`, `labels` (every one must be granted to the claiming
  agent), `requested_by`, `reason?`.
  `check_task` adds what a schema cannot say: no command or control sources a `set_env.sh`; `env` sets none of
  `TREE HOME PATH TMPDIR USER LOGNAME SSH_AUTH_SOCK GIT_SSH_COMMAND LD_PRELOAD LD_LIBRARY_PATH` nor any
  `NANOSOC_RM_*`; no tree path twice; no path with a `..` part. Both sides call it: the service before it queues, the
  agent before it runs (a task that should never have been queued is still not run: INFRA).
- **claim** (`$defs/claim`, `type: task`): `{task_id, lease (32 hex), nonce (32 hex), lease_seconds, task}`.
- **result** (`$defs/result`): `{schema, task_id, nonce, executor, sandbox bwrap|fallback|none, started_at,
  finished_at, verdict PASS|FAIL|UNVERIFIED|INFRA|WARN, reason, exit_code, killed, seconds, over_budget, rows
  [verdict_row], log {bytes, sha256, truncated, uploaded}, log_tail, checkout {laid_out, missing}}`.
  `check_result(doc, nonce, task_id)`: every row carries the task's nonce; PASS only with a PASS row and no
  FAIL/UNVERIFIED/INFRA row; finished_at not before started_at; the poster is the agent holding the lease.
- **Verdict of one task** (`agent.judge`): `tier.<tier>` PASS iff the exit code is `verdict.exit` (default 0); a
  run killed (time limit, output limit, cancel, lost lease) is FAIL; each `forbid` string found in the output makes
  `tier.<tier>.forbid` FAIL (exit 0 alone is not trusted; the evaluation's `WRAPPER: SKIPPED`); a declared
  `verdicts` file (removed before the run) adds its rows (`tierrun.parse_verdicts`; missing, a link out of the tree or not a regular file = UNVERIFIED,
  never read: the agent reads it unsandboxed); then the
  `control` runs in the same tree and `tier.<tier>.negctl` is PASS iff it exited non-zero (the gate can fail); a
  control that did not fire turns a PASS into UNVERIFIED. Roll-up FAIL > UNVERIFIED > INFRA > WARN > PASS. Anything
  that stops the command from running (commit not in the mirror, budget over the agent's `max_budget_s`, a bind that
  exposes a protected path, a `ro_binds` tool tree missing on this host, a cancel or lost lease seen at the heartbeat
  just before the run, a refused task) is INFRA with one `tier.<tier>` INFRA row, never FAIL.

### 27.3 The task queue (`executor.queue`, `<data_dir>/executor.sqlite`, 0600)

States `queued -> claimed -> done`; `claimed -> queued` when the lease runs out (up to `max_attempts` claims, default
2), then `lost` (verdict INFRA, "the executor stopped reporting"); `queued -> cancelled`; a claimed task asked to stop
(`cancel`) sees `cancel: true` at its next heartbeat and ends `done` with the agent's result. `claim` takes the oldest
queued task whose labels the agent carries, under `BEGIN IMMEDIATE` (two agents never hold one task). Only the
agent holding the current lease may `heartbeat`, `store_log` (kept at `<data_dir>/executor-logs/<id>.log`, 0600,
at most 4 MiB) or `complete`; anyone else gets `LeaseLost` (409). Expiry runs inside `claim`, `heartbeat` and
`executor list`: no thread, no timer. A row never shows its lease.

### 27.4 The agent (`nanosoc-rm executor run --once|--loop`, on the EDA host)

Config: its own TOML (`$NANOSOC_RM_AGENT_CONFIG`, `executor.agentconfig`), never the service config:
`[agent] name server token_file ca_file? mirror_root scratch_dir labels poll_seconds heartbeat_seconds max_budget_s
max_log_bytes max_upload_bytes max_file_bytes keep_work rm_config? site_host?` and `[sandbox] mode auto|bwrap|fallback,
network, ro_binds, path, env`. `server` is https (http only for 127.0.0.1/localhost) with no user or token in it;
the token file must be 0600 and the agent user's.

One task: (1) `check_task`; budget; (2) `checkout.lay_out`: a fresh repo per checkout whose
`objects/info/alternates` points at the read-only mirror (nothing copied, fetched or written to the mirror); a
missing commit is INFRA ("sync the agent's mirror"); (3) one heartbeat (the checkout may have been long), then
the run in the sandbox below, heartbeating every `heartbeat_seconds`; (4) `judge`; (5) upload the log (head and tail, at most `max_upload_bytes`), then the result;
a 409 drops the result (another claim owns the task). The work dir `<scratch_dir>/task-<id>` (the sandbox's one
writable bind) and the logs dir `<scratch_dir>/task-<id>.logs` (outside every bind, so a command can neither swap a
log for a link the agent would follow nor empty it to hide a `forbid` marker) are removed unless `keep_work`. `run_loop` retries an unreachable service after `poll_seconds` and stops on 401/403/404 (a revoked
agent stops).

**Sandbox** (`executor.sandbox`). Environment built from nothing: `PATH=<[sandbox] path>:/usr/bin:/bin`, HOME and
TMPDIR empty dirs in the work dir, `LANG/LC_ALL=C.UTF-8`, `TREE`, `NANOSOC_RM_TIER`, `NANOSOC_RM_NONCE`,
`NANOSOC_RM_VERDICTS` (when declared), git with no prompt, no system config and ssh with no identity or agent, then
the site env, then the task's env (`${TREE}` and earlier entries expand). Own session and process group, stdin
closed, umask 077, `RLIMIT_CORE` 0, `RLIMIT_FSIZE` `max_file_bytes`; output past `max_log_bytes` kills the run;
the whole group is killed at `budget_s x 1.5`, on cancel and on a lost lease.
- **bwrap** (used when `bwrap --unshare-all` works; `mode = "bwrap"` refuses to start without it): user, pid, ipc, uts
  and cgroup namespaces, the network shared unless `network = false`, `--die-with-parent`, `--new-session`, and an
  EMPTY root holding only `/usr` and `/etc` (read-only), `/proc`, `/dev`, a tmpfs `/tmp`, `ro_binds` (read-only),
  the mirror root (read-only, for the alternates) and the task's work dir (the only writable bind). The agent
  user's home is not mounted: `~/.ssh`, `~/.config` and the executor token do not exist inside.
- **fallback** (no usable bwrap; `mode = "fallback"` or `auto`): the same environment and limits, no filesystem
  isolation. **Residual risk:** the command runs as the agent user and can read what that user can read, the
  executor token file included; with the token it could claim tasks and post results for this agent (it can never
  reach any other route, any store, git host or CI). Mitigation: run the agent as a dedicated user with no ssh keys
  and nothing else in its home, revoke the token on any doubt. `executor check` warns when it would fall back.
- **Exposure check** (`sandbox.exposure`, both modes): no bind (work dir, mirror root, `ro_binds`, a job's binds) may
  contain the token file, the agent user's home, `~/.ssh`, `~/.config` or `~/.gnupg`. A task that would is INFRA,
  not run.

### 27.5 The executor API (`executor.api`; `[executor]` and `[[executor.agents]]` in the service config)

The web app's Guard sends every request under `/executor` to `executor.api.serve` BEFORE its session check, and
the app routes nothing under it: a session never reaches this API and an executor token never reaches any other
route. No `[executor]` table: every path is 404. Any method but POST: 405.

| POST | Body | Answer |
|---|---|---|
| `/executor/v1/claim` | `{labels?}` | 200 `$defs/claim` (`type: task`) or `$defs/job_claim` (`type: job`); 204 nothing to run |
| `/executor/v1/tasks/<id>/heartbeat` | `{lease}` | 200 `{cancel, lease_seconds}` |
| `/executor/v1/tasks/<id>/log` | raw bytes, header `X-Executor-Lease` | 200 `{stored}` |
| `/executor/v1/tasks/<id>/result` | `{lease, result}` | 200 `{state, verdict}` |
| `/executor/v1/jobs/<id>/heartbeat` | `{lease}` | 200 `{cancel, lease_seconds}` |
| `/executor/v1/jobs/<id>/result` | `{lease, exit_code, stdout, log}` | 200 `{state, verdict}` |

401 token missing, unknown or revoked; 404 unknown path or id; 409 not this agent's lease; 413 body over 2 MiB (JSON)
or 4 MiB + 64 KiB (log); 422 a body or result that breaks the contract (the task stays claimed; the agent may retry).
Answers carry `cache-control: no-store`, never a cookie, never the token or the body. A claim's labels are the
intersection of what the agent asks with what its `[[executor.agents]] labels` grant.

**Tokens** (`executor.tokens`): `rmx_` + 64 hex (256 bits). `nanosoc-rm executor token new NAME --out FILE
[--labels a,b]` writes the token to a new 0600 file (never overwrites, never prints it) and prints the
`[[executor.agents]]` entry with its `token_sha256`. The service keeps only the digest and compares constant-time
against every entry. **Revoke:** `revoked = true` on the entry, or delete it; the config is read per request, so the
next call is 401 without a restart. Config checks: `name` a label and never `local` (the service's own worker in
section 26's job queue), `token_sha256` 64 hex and unique, `lease_seconds >= 10`, `max_attempts >= 1`.

**Client** (`executor.client.ExecutorClient`): its one POST call site goes through `check_path`, which admits only
the six paths above; bearer header only, no redirects, `trust_env=False`, TLS verified (`ca_file` or the system
store). WRITE_CALLS effect `executor` (section 17.5).

### 27.6 Tier jobs over the API (`executor.jobbridge`)

An agent whose `[[executor.agents]]` entry grants `jobs` and whose own config names `rm_config` (a nanosoc-rm config
on the EDA host: its own data dir and mirror, anonymous read only, **no credentials**) asks for jobs too. A claim
hands out a capability task first, else `jobs.claim_for_executor(config, agent)`; the bridge records a lease
(`job_leases` in executor.sqlite). The agent runs `<python> -c CLI_BOOT <argv>` (the job queue's own `cli_args`) in
the same sandbox with `NANOSOC_RM_CONFIG=<rm_config>` and `NANOSOC_RM_EXECUTOR_HOST=<site_host>`; read-only binds:
the Python prefix, the nanosoc-rm install, the rm_config directory and its registry checkout; writable: its data dir
(all under the exposure check). stdout (the JSON documents) and the log go back with the exit code, and
`jobs.finish_for_executor` reads them as the local worker would. A job whose agent stops heartbeating is finished
with no exit code (INFRA) by `jobbridge.expire`; so is a running executor job with NO lease row older than one lease
(the claim's two transactions were cut by a service crash). Cancel is the job's
`cancel_by`. Grants: `jobs` claims every tier job kind; `jobs:<kind>` (`jobs:test-bump`, `jobs:compat`,
`jobs:matrix-cell`) only that kind. The tools a job needs are checked on the agent host (`host_check`: a missing
tool is INFRA, so the job reads UNVERIFIED, never PASS). In a bwrap job the agent sets
`NANOSOC_RM_TIER_PIDNS=<bwrap>`: every tier command of the job then runs in its own pid namespace under a nested
bwrap (`tierrun.pidns_wrap`), so it cannot reach the launching `nanosoc-rm`'s stdout through `/proc/<pid>/fd/1` and
forge the job's documents; a namespace that cannot be made is INFRA, never a bare run.

Paired runs: a capability task may carry `subject.side` (`now`|`after`) and `subject.pair`, so a planner can queue
both sides of one bump as tasks and compare the two results (section 26 compares inside one test-bump job today).

### 27.7 CLI

`nanosoc-rm executor run --once|--loop [--agent-config P]` (exit 0 ran or nothing queued; 3 token refused; 2 service
unreachable with --once), `executor check [--agent-config P] [--json]` (sandbox mode, exposure, token file, mirror
root; no network; exit 1 on a problem), `executor token new NAME --out FILE [--labels | --reader]` (`--reader`: a
bump-check reader token and its `[[api.readers]]` entry, section 26.7), `executor enqueue
TASK.json [--json]` (exit 3 when it breaks the contract), `executor list [--state S] [--limit N] [--json]`,
`executor show ID [--json]`, `executor cancel ID`. The install (a systemd --user unit, never started by the tool)
is in DEPLOY.md, "Remote tier executor".
