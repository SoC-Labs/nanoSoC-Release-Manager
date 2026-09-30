# v1 module interfaces

How the v1 modules fit together, and the exact interface of each: function, CLI command, HTTP endpoint,
inputs, outputs, errors, requirement IDs. Data shapes are the schemas in [`schemas/`](../../schemas/);
this page never redefines them. Index and rules: [README.md](README.md).

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
4. **Read-only (A2, A6, PR8).** v1 makes exactly one kind of write call to anything outside its own disk: the CI pipeline trigger (§10).
   - The mirrors fetch; they never push.
   - No module has a code path that writes to a repo, a fork, a tag or a setting.
5. **Package layout,** mirroring fpgahub (H4, PR9):
   - Python ≥ 3.11, package `nanosoc_rm` under `src/`, click CLI `nanosoc-rm`, hatchling.
   - FastAPI + Jinja2 + htmx, pydantic, httpx, SQLite (WAL).
   - One long-running process, `nanosoc-rm serve`, in one systemd unit. It serves the web pages and runs the schedules (poll every 15 min, nightly T0 trigger, result fetch every 2 min) by calling the same functions as the CLI.

```
src/nanosoc_rm/
  cli.py        config.py      contracts/ (packaged schemas + validate())
  registry/     mirror/        walker/     analysis/ (hosts.py, contains.py, protect.py)
  t0/ (gates.py shared, runner.py runs IN CI only)   ci/ (trigger.py, fetch.py, verify.py)
  scan/         db/            web/        auth/       lock/ (Phase 5)
```

**Data flow:**

```
GitHub + GitLab --(read token, heads+tags only)--> mirror/ --> walker/ --> analysis/ --> db/ --> web/ + CLI
registry repo (protected main) --(read)--> registry/ ------------------------^
web/ Run button, nightly schedule --> ci/trigger --(trigger token, compat-ci only)--> GitLab CI job `t0`
GitLab CI job `t0` (t0/runner, anonymous git only) --> artifacts rm-bundle/
ci/fetch --(read token: job, variables, artifacts)--> ci/verify --> scan/ --> db/
```

## 1. Service config (private; never in this repo)

A TOML file on the service VM, path in `$NANOSOC_RM_CONFIG`. It holds:

| Section | Holds | Requirement |
|---|---|---|
| `hosts.github`, `hosts.gitlab` | web, API and ssh base URLs; the org / group to inventory | A1, B9 |
| `registry` | registry repo, ref (`main`), local checkout path | F1 |
| `ci` | CI project path, trigger ref, job name `t0`, allowed refs (`main`), fetch timeout | A4, D5 |
| `credentials.*` | per credential: token file path, scope as issued, expiry date | H3 (expiry warning 14 days ahead) |
| `stores.*` | `artifactory` URL and repo, `disk` path | D12, G7 |
| `roles` | login → roles; v1 has one role, `member` (view + Run T0) | H2 |
| `schedule` | poll minutes (15), nightly UTC time, fetch minutes | H6, D2 |

v1 credentials, and nothing else (A2, H3):

1. GitHub read-only token (poller).
2. GitLab `read_api` + `read_repository` token (poller, CI fetch).
3. GitLab pipeline trigger token for the compat-ci project.
4. GitHub OAuth app (login).

## 2. Registry loader and validator

Requirements: A1, B1, B2, F1, F2, F5, and PR3 structure. F3 existence is checked by T0 (§9).

```python
def load_registry(path: Path) -> Registry           # raises RegistryError(problems: list[Problem])
@dataclass class Problem: file: str; pointer: str; rule: str; message: str
class Registry:
    def components(self) -> list[Component]
    def resolve_repo(self, repo: Repo) -> tuple[str | None, Literal["canonical","copy","alias","external","unknown"]]
    def configurations(self) -> list[Configuration]
    def defects(self) -> list[Defect];  def errata(self) -> list[Erratum];  def imports(self) -> ImportLedger
def normalise_url(url: str, hosts: HostTable) -> tuple[UrlKind, Repo | None]   # B9; from walk.py norm()
```

- **CLI:** `nanosoc-rm registry validate [PATH] [--json]`. Exit 0 = valid; 1 = problems, printed `file:pointer: message [rule]`.
- **Checks:**
  - every file against its schema;
  - the cross-file rules in README §3;
  - `imports.yaml` items with `disposition: pending` are reported. This is the F5 progress count, not an error.
- **URL normalisation (B9):**
  - https and ssh forms of both hosts;
  - strip `.git` and a trailing `/`;
  - host redirects come from catalogue `aliases`.

  A URL that resolves to no component or external entry gives `unknown`, and the walker raises an alert.
- **Errors:** never partial. A registry with any problem is not loaded; the service keeps serving the last good one and shows the problems.

## 3. Mirror and poller

Requirements: H6, A1 (inventory), B6 (data), B8 (hook).

**Layout:**
- One bare repo per catalogue component or external: `<data>/mirrors/<id>.git`.
- It holds every repo of that component (canonical, copies) under per-repo namespaces: `refs/r/<host>/<urlencoded path>/heads/*` and `.../tags/*`.
- Keeping both hosts in one object store is what makes B6 answerable. GitHub's compare API returns 404 for the other host's head (README §7, fact 4).

**Fetch** (every 15 minutes; explicit refspec, never `--mirror`, so no PR/MR refs):

```
git -C <id>.git fetch --prune --filter=blob:none <https url> \
    +refs/heads/*:refs/r/<key>/heads/*  +refs/tags/*:refs/r/<key>/tags/*
```

- **Mirror config:** `core.logAllRefUpdates=always`, `gc.reflogExpire=never`, `gc.reflogExpireUnreachable=never`, `gc.pruneExpire=never`. Old heads and their objects stay inspectable (B3 REWRITTEN, B8).
- **Private repos:** the read token goes in through a credential helper, never in the URL.
- **Blobs:** `.gitmodules` blobs arrive on demand and are then cached.

```python
def sync_component(component_id: str) -> list[Observation]   # mirror_status per repo, + rewrite_alert (§8)
def inventory() -> list[Observation]                         # repo_inventory for every repo in org + group (A1)
```

- **CLI:**
  - `nanosoc-rm mirror sync [--component ID] [--host H]`
  - `nanosoc-rm mirror status [--json]`: age per repo, H6.
  - `nanosoc-rm inventory [--unclassified] [--json]`: exit 1 if any repo is unclassified, A1.
- **Errors:** a failed fetch is a `mirror_status` with `ok: false` and the error text, and the old refs stay. A mirror is never deleted or re-cloned automatically.
- **Unknown:** total mirror disk size for about 157 repos. Blobless fetches of two small repos took 140 KB and 340 KB. Measure on the first full sync.

## 4. Pin walker

Requirements: B2, B3, B7, B9. Reuses `walk.py`.

```python
def walk(configuration_id: str) -> list[Observation]          # kind pin_state, one per pin
```

**Algorithm** (walk.py's, reading mirrors first):
1. Resolve the configuration ref to a commit on its canonical host. A frozen configuration uses its recorded `commit`; if its tag no longer peels to that commit, raise an alert.
2. At each (repo, commit):
   - read `.gitmodules` with `git cat-file -p <commit>:.gitmodules`;
   - for each entry, read `git ls-tree <commit> -- <path>` and record `entry_type`. Anything but a gitlink is state NO_GITLINK and is kept, never dropped.
3. Normalise the URL (§2). Recurse into (child repo, child commit) once, deduplicated by (repo, commit), with depth ≤ 10. Record every tree path at which the pin occurs.
4. **Per host** holding a copy of the child (canonical, copies), apply the precedence in README §5:
   - branches containing the commit: `git for-each-ref --contains <c> refs/r/<key>/heads`;
   - tags: same for `tags`, and `--points-at <c>` for TAGGED;
   - lag: `git rev-list --count <c>..refs/r/<key>/heads/<default>`;
   - REWRITTEN: the default branch's reflog in the mirror, or catalogue `known_rewrites`;
   - objects absent from the mirror fall back to the API: GitHub `GET repos/{r}/commits/{sha}` and `.../pulls`; GitLab `GET projects/{id}/repository/commits/{sha}` and `.../merge_requests`. This separates PR_ONLY, ORPHAN and MISSING.
5. `split` = the child component appears at more than one commit in this tree (B7). `effective` stays `unknown` until Phase 2 captures file sets.

- **CLI:**
  - `nanosoc-rm walk CONFIG [--json]`
  - `nanosoc-rm pins CONFIG [--state S] [--host H]`

  Exit 1 if any pin is red on its pinned host.
- **Errors:** unknown configuration → exit 3. An unreadable repo is state UNREADABLE, not an exception.
- **Golden (B2):** `walk eth-rc4` gives exactly the 44 (parent repo, parent commit, path) keys in [`release-lock.eth-rc4.example.yaml`](../../schemas/examples/release-lock.eth-rc4.example.yaml). Maximum depth 5.

## 5. Host comparator

Requirement: B6.

```python
def compare_hosts(component_id: str) -> list[Observation]     # kind host_compare, one per copy
```

- **Counts,** in the component's mirror:
  - `canonical_only = rev-list --count copy..canonical`
  - `copy_only = rev-list --count canonical..copy`
- **Relation:**
  - no merge base → UNRELATED;
  - either side unreadable → UNKNOWN.
- **CLI:** `nanosoc-rm hosts [COMPONENT] [--json]`. Exit 1 if any relation is DIVERGED (red, B6).
- **Golden:** as observed on 2026-09-30 in `observation.example.jsonl`:
  - tidelink: AHEAD 298;
  - ahb_qspi: DIVERGED 1/5;
  - asic-flow: DIVERGED 4/1.

## 6. Containment query

Requirement: B4.

```python
def contains(commit: str, component: str | None = None,
             configurations: list[str] | None = None) -> list[Observation]   # kind containment
```

1. Resolve the prefix to one full SHA in the mirrors. If `component` is not given, search every mirror. Ambiguous or not found → exit 3, listing the candidates.
2. For each host where the component has a repo, prove X exists there:
   `git for-each-ref --count=1 --contains X refs/r/<key>/heads refs/r/<key>/tags`
   An object that is present but not reachable from a head or tag counts as not there.
3. For each configuration, and each pin of that component in its walked tree (all split pins):
   - prove the pin commit P exists on the host the same way;
   - then `git merge-base --is-ancestor X P`;
   - if either proof fails, `contains: null` with the reason. It is never `false`.
4. The configuration's answer: `yes`, `partial`, `no`, `absent` or `unknown`, as the schema defines.

- **CLI:** `nanosoc-rm contains SHA [--component ID] [--config ID] [--json]`. Exit 0 = answered (including `no`), 2 = any `unknown`.
- **Golden:** `contains 036d381 --component nanosoc_gen --config eth-rc4` gives `no` on gitlab (both generator pins, merge-base verified). The component has no GitHub repo, so there is no github row.

## 7. Silicon-protection list

Requirement: B5.

```python
def silicon_protection(configuration: str | None = None) -> list[Observation]   # kind silicon_protection
```

- **Rows:** every pin in a `frozen` or `silicon` configuration whose commit the default branch cannot reach on the pinned host.
- **Coverage:** `covering_tags` are the tags on that host containing the commit.
- **CLI:** `nanosoc-rm silicon-protection [CONFIG] [--uncovered] [--json]`. Exit 1 if any row is uncovered.
- **Golden:** eth-rc4 gives 11 rows, all covered by `keep/eth-rc4/*` (in `observation.example.jsonl`).

## 8. Rewrite detector

Requirement: B8. Runs inside `sync_component` for each repo's default branch.

```python
def detect_rewrite(mirror: Path, key: str, branch: str, old: str, new: str) -> Observation | None
```

- **Fires when** `git merge-base --is-ancestor <old> <new>` is false.
- **`lost_commits`** = `git rev-list --count <old> --not --glob=refs/r/<key>/*`.
- **`pinned_lost`** = registered pins whose commit is among them.
- **Output:** a `rewrite_alert` observation, shown as a dashboard banner until someone clears it. Alert delivery beyond the dashboard is open (question 7 in the gate-2 summary).
- **Test:** a local bare repo stands in for a host; force-push, sync, expect one alert. Done-when on live data needs a scratch repo that a person creates (PR8).

## 9. T0: definition and runner

Requirements: D2, D6, D10, D11, C11, F3, D7 (probe).

T0 runs **only** in the GitLab CI job `t0` of the compat-ci project (A4). The service never runs it.

### Job contract (compat-ci `.gitlab-ci.yml`)

```yaml
t0:
  rules: [{if: '$CI_PIPELINE_SOURCE == "trigger" && $CI_COMMIT_REF_PROTECTED == "true"'}]
  tags: [<hardened-runner tag>]            # D10
  timeout: 15m
  script: [nanosoc-rm t0 run --out rm-bundle]
  artifacts: {when: always, paths: [rm-bundle/], expire_in: 90 days}   # D12 log retention
```

**Input:** one pipeline variable, `RM_REQUEST`:
- compact JSON `{schema:1, nonce, configuration, repo, commit, upgrade_from, canonical_urls}` (result-bundle `request` plus `nonce`);
- the tool package version is pinned by the compat-ci commit, which is the harness.

**Environment:**
- fresh `HOME` and `TMPDIR`;
- `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`;
- `GIT_TERMINAL_PROMPT=0`;
- ssh with `BatchMode=yes` and no agent;
- no credentials of any kind;
- needs git and Python ≥ 3.11 only.

**Output:** `rm-bundle/provenance.json`, `rm-bundle/verdicts.tsv` (5-column rows, every row stamped with the nonce) and `rm-bundle/logs/`.

1. The runner scans `rm-bundle/` itself (§11) and records the result in `provenance.scan`.
2. It captures `git submodule status --recursive` **after** the gates.
3. Exit code: 0 all PASS or SKIP, 1 any FAIL or UNVERIFIED, 2 INFRA. The service ignores the exit code and reads the bundle.

### Gates

Each gate lives in `nanosoc_rm/t0/gates.py` as a table shared by the runner and the service's roll-up. Each has a
check and a negative control: the same code run on a deliberately broken input in the same job (D6, PR6).
The control's row is `<gate>.negctl`, PASS when it fired.

| Gate id | Check | Negative control (must go red) | Req |
|---|---|---|---|
| `t0.infra.probe` | anonymous `git ls-remote` of one known public repo per host succeeds; if not, host-dependent gates record INFRA, not FAIL | the probe against a repo path that does not exist | D7 |
| `t0.runner.hardened` | `CI_COMMIT_REF_PROTECTED=true`; `HOME` and `TMPDIR` empty at start; job user equals the configured service account | the same check on a synthetic environment with an unprotected ref | D10 |
| `t0.pins.fetchable` | walk the tree by probing every pin as compute's `submodule-pins.yml` does: `git fetch --depth=1 <url as pinned> <sha>` into an empty repo, then read `.gitmodules` and recurse. Not-our-ref and auth are FAIL. Unreachable host is INFRA. Pins below an unfetched parent are listed as not reached | a synthetic pin at a nonexistent commit of a real public repo | D2 |
| `t0.pins.reachable` | for every pin: fetch its canonical repo's heads and tags anonymously (blobless), then `merge-base --is-ancestor`, the method of the toolkit's `check-pins.sh`. The canonical URL comes from `canonical_urls`, else the pinned URL with ssh rewritten to https. A canonical repo not readable anonymously makes that pin UNVERIFIED | a synthetic commit absent from the fetched refs | D2 |
| `t0.urls.no-ssh-public` | no public parent (one that is fetchable anonymously) pins by an ssh URL (`git@…`, `ssh://…`) | a synthetic public parent with a `git@` URL | D2 |
| `t0.manifest.valid` | every `nanosoc-component.yaml` at every pinned commit validates against component-manifest v1. None anywhere → SKIP | the example manifest with `version: "1.0"` | D2, C11 |
| `t0.clone.upgrade-path` | if `upgrade_from`: anonymous recursive clone of it, check out `commit`, `git submodule update --init --recursive` **without** sync must resolve every pin; then `git submodule sync --recursive` and update again. No `upgrade_from` → SKIP | a synthetic two-commit repo pair where the second commit moves a submodule URL to a path that does not exist | D2 |
| `t0.vendor.no-arm-ip` | scan every fetched tree at its pinned commit with the scanner (§11). A tree that was not fetched is not scanned, so the gate is UNVERIFIED | a scratch copy with a planted Arm banner file | D2, D11 |

**Registry job** (`configuration: "@registry"`, `repo` = the registry repo, `commit` = its `main` head):

| Gate id | Check | Negative control | Req |
|---|---|---|---|
| `t0.registry.schema` | the registry loads with no problem (§2) | an invalid example from `schemas/examples/invalid/` | D2, PR3 |
| `t0.registry.refs-resolve` | every `commit_ref`, bench, detector source, workaround and import-source commit is reachable from a branch or tag on its component's canonical host (anonymous fetch; UNVERIFIED for a private canonical repo) | a synthetic record citing a commit that does not exist | D2, F3 |

The registry is private, so this job needs read access to it from compat-ci (question 3 in the gate-2 summary).

**Expected on real data today:**
- eth-rc4 T0 is red on three gates:
  - `t0.pins.fetchable`: TideLink and ASIC-Toolkit use ssh URLs;
  - `t0.urls.no-ssh-public`: same two URLs;
  - `t0.pins.reachable`: ahb_qspi `e908ac8`, if GitHub is canonical.
- The registry job is red on `t0.registry.refs-resolve`: bench `6aafc94` is not pushed.

`result-bundle.verdicts.example.tsv` shows the rc4 rows.

## 10. CI trigger, fetch and verify

Requirements: A2, A4, D2, D5, D7, H2.

```python
def trigger(configuration: str, by: str, kind: Literal["schedule","manual"]) -> Observation   # t0_result, pending
def fetch_pending() -> list[Observation]                                                    # t0_result, final
def verify(run: RunRecord, pipeline: dict, job: dict, variables: dict, bundle: Path) -> Verdict
```

- **CLI:**
  - `nanosoc-rm t0 trigger CONFIG|--all [--by LOGIN]`
  - `nanosoc-rm t0 fetch`
  - `nanosoc-rm t0 runs [CONFIG] [--json]`
- **Schedule (D2):** `serve` calls `trigger(all, by="schedule")` nightly. The Run button calls `trigger(id, by=<login>, kind="manual")` (A4, H1).

### Trigger

1. Build `RM_REQUEST`:
   - the commit resolved from the mirror;
   - `canonical_urls` from the walk;
   - `nonce = secrets.token_hex(16)`.
2. Store the `RunRecord`: nonce, request bytes, configuration, commit, trigger, who, when.
3. `POST projects/:id/trigger/pipeline` with `ref=<ci.ref>` and `variables[RM_REQUEST]`. This is the only write call in v1 (A2).
4. Record `pipeline_id`. A manual run also writes an `actions` row (H2).

At most one pending run per configuration: the Run button is disabled while one is pending.

### Fetch

Every 2 minutes, for each pending run:
- `GET projects/:id/pipelines/:pid`
- `GET projects/:id/pipelines/:pid/jobs`
- `GET projects/:id/pipelines/:pid/variables`
- `GET projects/:id/jobs/:job_id/artifacts`: unzip `rm-bundle/` into a quarantine directory.

A run still unfinished after the timeout (2 h) is `no-bundle` / INFRA.

### Verify (D5)

Checks run in this order. The first failure makes the run `rejected`, with the check named in `reason`.

1. The API says: the project is the configured CI project, the ref is in `ci.allowed_refs`, the job name is `t0`, and the pipeline id matches the run.
2. The API's `RM_REQUEST` variable is byte-equal to the stored request.
3. `provenance.json` validates against result-bundle v1.
4. `run_nonce` equals the run's nonce, and `request` equals the stored request.
   - `ci.pipeline_id` and `ci.job_id` equal the API's values.
   - `ci.harness_sha` equals the API's pipeline SHA.
5. `captured.superproject_commit` equals `request.commit`. This is the D5 done-when.
6. `verdicts.tsv`:
   - at least one row, 5 fields each, each row valid;
   - every nonce is the run's nonce;
   - every timestamp lies inside the job's API start/finish ±60 s.
7. The service's own scan (§11): a finding, or a scan that could not run, makes the run `refused`. The bundle is deleted from quarantine, and only the finding locations are kept.

Then:
- **Roll-up:** as in README §6, against the gate table in `gates.py`.
- **Storage:** the accepted bundle goes to the `ci` store in v1, and to `artifactory` from Phase 2 (D12).

**No bundle:** the job's `failure_reason` decides.
- `runner_system_failure`, `stuck_or_timeout_failure`, `job_execution_timeout`, `scheduler_failure`, `api_failure`, or exit 137 → INFRA.
- Anything else → UNVERIFIED.

## 11. Bundle and tree scanner

Requirements: D11, D2. Reuses the toolkit's `check-vendor-collateral.sh`.

```python
def scan_dir(path: Path) -> ScanResult     # verdict: clean | finding | unverified; findings: [{rule, file, line}]
```

- **CLI:** `nanosoc-rm scan bundle DIR [--json]`. Exit 0 clean, 1 finding, 2 unverified.
- **Findings** report locations and rule ids, never matched text: the log of a scan is itself a copy.
- **Method (reused):**
  1. Arm every rule against an invented specimen, and a counter-specimen for the near miss, before scanning.
  2. Census the corpus: zero files scanned is `unverified`.
  3. Allowlist entries each carry a reason and an owner.
- **Rules:**
  - the toolkit scanner's rule set: foundry file, value and inventory shapes, and Arm product codes;
  - one new rule, `arm.banner`, for the Arm confidential-and-proprietary file header. The toolkit scanner has no such rule. D11's done-when plants exactly this banner.
- **How it runs:** the toolkit scanner reads git trees. For a bundle directory, the tool builds a throwaway git repo over the extracted files and runs the scanner with `--rev HEAD`.

  Whether to call a pinned toolkit copy or carry the rules in this repo is question 5 in the gate-2 summary.

## 12. Store

Requirement: A3.

SQLite in WAL mode at `<data>/rm.sqlite`:

| Table | Columns | Rebuildable? |
|---|---|---|
| `observations` | kind, subject, observed_at, doc JSON | yes, from mirrors, registry and CI |
| `runs` | nonce, configuration, commit, request, trigger, triggered_by, triggered_at, pipeline_id, job_id, state | **no**: back it up |
| `actions` | at, login, action, target, result | **no**: back it up (H2) |
| `alerts` | kind, subject, raised_at, cleared_at, cleared_by | yes |

```python
def put(obs: Observation) -> None           # validates against observation.schema.json before insert
def latest(kind: str, subject_prefix: str = "") -> list[Observation]
def data_age(obs: list[Observation]) -> timedelta   # now - min(observed_at)
```

## 13. Web views and JSON API

Requirements: H1, A3, A4, A5.

Every route is behind auth (§14). Every page prints its data age and links each fact to its `sources[].command` (A3).

| Route | Shows | Function |
|---|---|---|
| `GET /` dashboard | per configuration: last T0 verdict, its age and not-run count; red pins; open errata (status `open` or `permanent`); host drift (components in the tree whose copy is DIVERGED or lags); runner status; data age; **Run T0** | `latest()` over t0_result, pin_state, host_compare |
| `GET /configurations/{id}` | pin table (tree path, component, commit, state per host, lag, tags, split); T0 history; **Run T0** | §4, §10 |
| `GET /components/{id}` | repo detail: tags per host, host comparison, consumers and their lag, defects citing it | §3, §5 |
| `GET /contains?sha=&component=` | containment per configuration and host, with proof commands | §6 |
| `GET /silicon-protection` | §7 rows by configuration, uncovered first | §7 |
| `GET /defects`, `/defects/{id}`, `/errata/{configuration}` | records, exposure level and method (amber for probably-exposed, F2) | §2 |
| `GET /runs/{nonce}` | a T0 run: acceptance, reason, gates, controls | §10 |
| `POST /configurations/{id}/runs` | Run T0 (htmx form with CSRF token); returns the pending run fragment | `trigger(id, login, "manual")` |

**Runner status (H1)** is read from the latest compat-ci job's `runner` object and the age of any pending job. No runner-admin API is needed.

**JSON API:** `GET /api/v1/...`, same auth, returns observations as JSON lines:

| Route | Returns |
|---|---|
| `configurations` | list, with the latest T0 result |
| `configurations/{id}/pins` | pin_state |
| `components/{id}/hosts` | host_compare |
| `contains/{sha}?component=` | containment |
| `silicon-protection?configuration=` | silicon_protection |
| `runs?configuration=` | t0_result |
| `mirrors` | mirror_status |
| `inventory?unclassified=1` | repo_inventory |
| `alerts` | alerts |

`POST /api/v1/configurations/{id}/runs` is the same action as the button.

Registry records are served as they are, with `sources: [{kind: registry, command: "git -C <registry> show <commit>:<file>"}]`.

## 14. Auth and roles

Requirements: A5, H2.

- **Login:** a GitHub OAuth app with scope `read:org`. On callback, `GET /user/memberships/orgs/SoC-Labs` must be `active`. The user's token is then discarded.
- **Session:** a signed cookie (itsdangerous, as in fpgahub), valid 8 h.
- **Re-check:** membership is re-checked at most hourly with the service's read token: `GET /orgs/SoC-Labs/members/{login}` must return 204.
- **Non-members:** 403 on every route, including `/api` (A5, H2 done-when). Static assets carry no data.
- **Roles:** from the service config (H2). v1: every member has `member`, which allows viewing and Run T0.
- **Logging:** every write action (in v1, only Run T0) is logged in `actions` with the login (A2, H2).

## 15. Formats fixed now for later phases

| Phase | What | Contract |
|---|---|---|
| 2 | D4 capture | `captured.pins`, `host`, `ip_hash` in result-bundle |
| 2 | Matrix and flaky handling (D3, D8) | FLAKY / XFAIL / XPASS gate verdicts |
| 3 | Manifests | T0 validates component-manifest (C11) |
| 4 | Bump bot (E3, E5) | `auto_merge` in eligibility; `workarounds` in manifests |
| 5 | Lock writer (G3, G7) | `nanosoc-rm lock CONFIG --out release.lock.yaml` emits release-lock, built from §4 plus a release spec (archive refs, output hashes) |

## 16. v1 Must requirements → interface → golden

For the coverage checker (PR5):

| Req | Interface | Golden or fixture |
|---|---|---|
| A1 | §3 `inventory`, §2 catalogue | no repo_inventory with role `unclassified`, once the full catalogue is written |
| A2 | §0.4, §10 trigger | the only write call is `trigger/pipeline`; `actions` row per manual Run |
| A3 | observation envelope; §12 `put` validates it | `observation.without-observed-at.yaml` is rejected |
| A4 | §9, §10 | a Run T0 produces a pending, then a final, t0_result |
| A5, H2 | §14 | a non-member gets 403 on `/` and `/api/v1/configurations` |
| A6 | §0.4 | no write path exists; silicon errata are `permanent`, never yanked |
| A7 | phase entry gates (process) | not an interface |
| B1 | catalogue `class` | `component-catalogue.no-class.yaml` is rejected |
| B2 | §4 | the 44 pin keys of `release-lock.eth-rc4.example.yaml` |
| B3 | §4, README §5 | the 3 pins in `pin-state.example.jsonl`, plus a hand check of multicore-master |
| B4 | §6 | 036d381 → eth-rc4 `no` on gitlab |
| B5 | §7 | 11 rows, all covered |
| B6 | §5 | tidelink AHEAD 298; ahb_qspi and asic-flow DIVERGED |
| B8 | §8 | local fixture force-push → one rewrite_alert |
| D1 | §9 T0 command and 15-minute budget | T1–T6 later |
| D2 | §9 gates | expected rc4 rows in `result-bundle.verdicts.example.tsv` |
| D5 | §10 verify | a bundle whose `captured.superproject_commit` differs is `rejected` |
| D6 | `.negctl` rows, README §6 | `observation.pass-without-control.yaml` is rejected |
| D10 | gate `t0.runner.hardened` | its control on an unprotected ref |
| D11 | §11 | a planted Arm banner makes the bundle `refused` |
| F1 | defect and erratum | SL-DEF-0001 example, plus errata for eth (example), compute and QuickStart (need their configurations) |
| F2 | erratum `exposure` | `erratum.ancestry-claims-exposed.yaml` is rejected |
| F3 | gate `t0.registry.refs-resolve` | SL-DEF-0001 fails while 6aafc94 is unpushed |
| F5 | import ledger | no `pending` items |
| H1 | §13 | each v1 page on live data, including one manual Run |
| H3 | §1 credentials table | four credentials, each with its scope and expiry recorded |
| H6 | §3 | mirror_status age under 15 minutes |
