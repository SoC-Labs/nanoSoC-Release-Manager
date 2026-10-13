# Acceptance tests: scope, web and security

Gate 2 (PR3). One acceptance test per v1 Must requirement in scope A and the web and security part of H:
**A1, A2, A3, A4, A5, A6, A7, H1, H2, H3, H6**.

- Tests: [`tests/acceptance/test_scope_web.py`](../../tests/acceptance/test_scope_web.py). Each AT carries
  `NOT_BUILT` (xfail, `raises=ImportError`, strict): XFAIL until its module exists, red on any other error, red
  as XPASS once it passes (`tests/conftest.py`). The 11 checker-level controls, and a check of the phase-gate
  register, run now as `test_selfcheck_*`.
- Revised 2026-09-30 after the contracts and coverage reviews ([GATE2.md](../GATE2.md) section 4).
- Contracts they test against: [`docs/contracts/`](../contracts/) and [`schemas/`](../../schemas/).
- Requirement text is quoted from [`REQUIREMENTS.md`](../REQUIREMENTS.md) v1.0.

## How these tests work

1. **One checker per AT (PR6).** The test file has one pure function per requirement, `check_<id>(...)`,
   that takes the evidence and returns a list of problems.
   - `test_AT_<ID>` gathers the evidence through the contracted interface and asserts the list is empty.
   - `test_AT_<ID>_negative_control` gives the **same** checker a deliberately broken input and asserts
     it names the problem.
2. **Two levels of negative control.**
   - **Checker level** (`_nc_<id>_checker`): needs no implementation. First it shows the checker passes on
     a consistent input, so it does not simply fail on everything. Then it shows the checker fails on
     the broken one. All 11 run in pytest as `test_selfcheck_<ID>` and pass (see "Negative controls run today").
   - **System level**: the tool itself on a broken input. Runs once the module exists.
3. **Modes.**

   | Mode | Needs | ATs |
   |---|---|---|
   | offline | git and Python ≥ 3.11. Local bare repos stand in for the git hosts (interfaces.md §8). Local HTTP servers (`FakeHost`) stand in for the GitHub and GitLab APIs. `$NANOSOC_RM_CONFIG` points at a throwaway config for the test. | A5, A6, A7, every system-level negative control |
   | live | On the service VM: `NANOSOC_RM_CONFIG` (the private service config) and `NANOSOC_RM_TEST_MEMBER` (a SoC-Labs member's GitHub login). Marked `live`. | A1, A2, A3, H3, H6, and the membership probe in H2 |
   | live + Run | As live. The test also starts one real compat-ci pipeline. | A4, H1 |

   - A live test without its inputs **fails**. It never skips and never passes, as in `test_ci_errata.py`.
     Off the VM, deselect with `-m "not live"`.
   - **PR8:** a live + Run test makes the tool's one write, a pipeline trigger in compat-ci (A2). While v1 is
     built, a person runs those tests (the gate-3 demo), not an agent.
4. **Names beyond the schemas.** The test file keeps each in one table, `ASSUMED`, and every one is now
   fixed in `interfaces.md` section 17 (one decision for all three AT files). A change goes through that
   section and this table, not the tests.
5. **Page hooks.** Pages are read only through the `data-*` attributes of `interfaces.md` section 13 (the
   merged list; gap G4). This file uses:

   | Attribute | On | Meaning |
   |---|---|---|
   | `data-age-seconds`, `data-oldest-observed-at` | one element per page | A3 data age: request time minus the oldest `observed_at` shown (README §4) |
   | `data-subject`, `data-observed-at` | each fact | the observation subject (README §4) and its time |
   | `data-source-for="<subject>"` | an `<a>` per fact | link to the page that shows that fact's `sources[].command` (gap G5) |
   | `data-configuration` | a dashboard row | configuration id |
   | `data-item` | a dashboard cell | `t0-result`, `red-pins`, `open-errata`, `host-drift`, `runner-status`, `data-age` (H1) |
   | `data-run-t0` | a `<form>` | the Run T0 button: POST to `/configurations/{id}/runs` with a CSRF field |

## Index

| AT | One-line check | Mode | Negative control |
|---|---|---|---|
| [AT-A1](#at-a1) | Each host's inventory count equals that host's own repo total from a separate endpoint (57 on GitHub, 101 on GitLab on 2026-09-30). Every repo is classified with its catalogue role. Each component has exactly one canonical repo. `inventory --unclassified` prints nothing. | live | The legacy entry is removed from the catalogue, so that repo is flagged and the command exits 1. A GitLab listing cut off at 100 is caught. |
| [AT-A2](#at-a2) | The write-call census of the package finds exactly one write, POST `trigger/pipeline`, and finds it. The config holds exactly the 4 v1 credentials, read-only except the CI-scoped trigger. GitLab's own answer matches the poller token's record. Every manual Run has one `actions` row. | live | A planted `git push`, a planted `POST`, a second write, scope `api`, and a manual run with its `actions` row deleted are each caught. |
| [AT-A3](#at-a3) | On every page: exactly one data age, measured from the oldest fact shown. Every fact links to a page showing its exact commands. Every fact's time matches the store. Every API line validates (including `configurations` and `alerts`, now `registry_record` and `alert` observations). `db.put` refuses a fact without `observed_at`. | live | A page with a fact's link removed, no data age, or an age measured from the newest fact is caught. A row stored without sources is caught. |
| [AT-A4](#at-a4) | Pressing Run on eth-rc4 creates a trigger pipeline in compat-ci with one `t0` job. Its `RM_REQUEST` carries the run's nonce and rc4's commit. The fetched result carries that job's id and a verdict. No T0 code runs in the service. | live + Run | A trigger answered 401 is recorded as exactly one `no-bundle` run with no pipeline and a reason naming 401, and is caught. An `RM_REQUEST` with a foreign nonce is rejected and caught. |
| [AT-A5](#at-a5) | Every route in the app's own route table answers 403 to a signed-in non-member, and 401 or the login redirect to an anonymous user, with no rc4 data in the body. A member is never refused. Only `/auth/*` and static files are open, and static files carry no data. | offline | An app whose auth trusts a non-member is caught. A route added after construction is still swept. |
| [AT-A6](#at-a6) | No call site in the package writes to a user's fork or touches a silicon release (checked in every phase, so a Phase 4 branch write to an org repo is not an A6 finding). The schemas cannot express yanking or un-freezing a silicon configuration. A full CLI cycle over a tree that pins a user's fork makes no push, no write request, and leaves every repo's refs unchanged. | offline | A planted write to `repos/alice/ip`, a push to an unresolvable URL and a planted `silicon/*` tag delete are caught. The git runner refuses a push. A yanked or un-frozen silicon entry is rejected. |
| [AT-A7](#at-a7) | Phases 2 to 5 each have a written entry gate whose every criterion has a check. No commit made before CR-15 that changes `src/` names a later-phase requirement before that phase's gate was checked. No later-phase automation is enabled before its gate (CR-15: building is allowed). | offline (reads the tool repo) | A pre-CR-15 D3 implementation commit before the Phase 2 gate is caught; a contracts-only commit naming G3 is not. An enabled `nightly_matrix` before the Phase 2 gate is caught; a `/releases` route and a `lock` command are not. |
| [AT-H1](#at-h1) | On live data, the dashboard has all six items and one Run T0 button for every registered configuration, with counts equal to the CLI's. Each v1 page shows exactly the facts its CLI command gives (44 pins, 11 covered pinned-host silicon rows plus the canonical-host rows, 036d381 → `no`). Pressing Run shows the pending run and disables the button. | live + Run | A missing runner-status cell, a missing configuration, a Run form without CSRF, a wrong count, and a missing or invented fact are each caught, on fixture pages and on the real offline dashboard. |
| [AT-H2](#at-h2) | GitHub sign-in asks for `read:org` only and checks `state`. Only `active` SoC-Labs members get a session: a non-member and a pending invitee get 403 everywhere. Roles come from the config. The user's token is kept nowhere. A Run is logged. The fake's membership answers match GitHub's real ones. | offline + live probe | A non-member who is let in is caught. A token left in the session cookie is caught. |
| [AT-H3](#at-h3) | The credentials check reports the 4 v1 credentials (`credential_status` observations), each with its recorded scope inside its allowed scope. The poller token's live scopes and expiry match the record. The GitHub token is not a classic token. The OAuth request asks for `read:org` only. A credential that could not be checked is never `ok`. A token expiring within 14 days raises an alert. No Phase 4 credential exists before the Phase 4 gate. | live | Recorded `api`, live scopes that differ from the record, an unchecked `ok`, a missed expiry alert and a `bump_writer` credential are each caught. |
| [AT-H6](#at-h6) | Every catalogued repo has a mirror no older than 20 minutes in two snapshots 16 minutes apart, and each one advanced. The `mirror status` table prints each repo's age. Mirrors hold only `refs/r/<host>/<repo>/{heads,tags}/*`. A fresh sync equals `ls-remote` heads and tags. | live | `git clone --mirror` (picks up `refs/pull/*`) and the first draft's §3 fetch without `--no-tags` (auto-follows `refs/tags/*`, G19, now fixed) are caught. A stale mirror and one that did not advance are caught. |

## Real data used

Observed 2026-09-30 with read-only calls, and re-checked the same evening. Contract examples are cited by file.

- **rc4:** `eth-rc4` = SoC-Labs/NanoSoC-Ethernet-Chiplet `ab73e40924317e45e7b3b941f52a7ed35f20832e`, tags `v1.0.0` and `silicon/eth-chiplet-r0p0`.
  - 44 pins, 5 levels deep ([`release-lock.eth-rc4.example.yaml`](../../schemas/examples/release-lock.eth-rc4.example.yaml)).
  - On its pinned host, every pin is TAGGED (1), ON_DEFAULT (32) or PROTECTED_BY_TAG (11), so rc4 has **0 red pins** today.
  - 2 pins use ssh URLs: `tidelink` and `ASIC/asic-toolkit`.
- **Keep tags:** 11 silicon-protection rows on the pinned host, all covered by `keep/eth-rc4/*`, all observed at 17:31:59Z; plus 2 canonical-host rows (arch_tech `ecf76aa` and ahb_qspi `e908ac8` on GitHub), covered by the 2026-10-01 keep tags, observed 2026-10-02 (CR-2) ([`observation.example.jsonl`](../../schemas/examples/observation.example.jsonl)).
- **Host drift in rc4's tree:** tidelink's GitHub copy is 298 commits ahead of GitLab. ahb_qspi and asic-flow are DIVERGED.
- **Containment:** `036d381` (SL-DEF-0001 form A fix) gives `no` for eth-rc4 on gitlab.
- **Registry records:** SL-DEF-0001, ETH-R0P0-ERR-001 (`status: permanent`, exposure `probably-exposed` by ancestry), and the import ledger ([`schemas/examples/`](../../schemas/examples/)).
- **Observation times** in the examples span 17:31:59Z (silicon rows) to 18:05:04Z (inventory), so a page that measured its age from the newest fact would hide 33 minutes.
- **Repo totals:**
  - GitHub SoC-Labs: 57 repos (41 public, 16 private). `GET orgs/SoC-Labs` gives `public_repos + total_private_repos` = 57.
  - GitLab soclabs with subgroups: 101 projects (58 public, 33 private, 10 internal). `X-Total: 101`, so the listing is 2 pages at `per_page=100`. 8 of them are forks inside the group.
  - Total: 158, the "~157" of A1. Some are not nanoSoC-related (other SoC families, demos, `.github`): the catalogue lists those as `ignored` with a reason (question 6 in GATE2.md).
- **Refs that are neither heads nor tags** (from `git ls-remote`):
  - SoC-Labs/NanoSoC-Ethernet-Chiplet advertises 5 `refs/pull/*/head`.
  - SoC-Labs/AHB-QSPI advertises 2 `refs/environments/production/deployments/*`.
- **Membership:**
  - `GET orgs/SoC-Labs/members/octocat` returns 404.
  - For a real member it returns 204. Which login that is comes from `NANOSOC_RM_TEST_MEMBER`: individual memberships are not written here.
  - `GET user/memberships/orgs/SoC-Labs` gives `{"state": "active", ...}` for a member.
- **Token introspection:**
  - The lab GitLab answers `GET personal_access_tokens/self` with `scopes`, `expires_at`, `active` and `revoked` (among other fields), so H3 can check the poller token live.
  - A GitHub classic token reports its scopes in the `X-OAuth-Scopes` header.
  - A GitHub fine-grained token reports no permissions about itself (see "Untestable").
- **git 2.43 tag auto-follow:** a fetch with the §3 refspecs and no `--no-tags` also writes `refs/tags/<name>` outside the per-repo namespace. Reproduced on scratch repos (G19).

---

## AT-A1

**Requirement (A1, Must, v1).** Track every nanoSoC-related repo on GitHub (SoC-Labs) and GitLab (soclabs) as a catalogue of components, each with exactly one canonical host.
*Done when:* All ~157 repos appear, each classified; any repo without a class is flagged.

**Given**
- The live GitHub org `SoC-Labs` and GitLab group `soclabs` (with subgroups), read with the poller's read-only tokens.
- The registry's `catalogue.yaml` at `main`.

**When**
- `nanosoc-rm inventory --json` runs.
- `nanosoc-rm inventory --unclassified --json` runs.

**Then**
1. For each host, the number of `repo_inventory` observations equals the host's own total, read from a **different** endpoint:
   - GitHub: `GET orgs/SoC-Labs` → `public_repos + total_private_repos`;
   - GitLab: the `X-Total` header of `GET groups/soclabs/projects?include_subgroups=true`.

   No repo appears twice.
2. Every observation validates against `observation.schema.json`.
3. No row has role `unclassified`. Each row names a catalogue entry whose `class` is one of the 8 B1 classes, and its role (canonical, copy, external or ignored) matches the catalogue.
4. No repo belongs to two catalogue entries. Every component has exactly one `canonical` row, and that repo is found on its host.
5. `inventory --unclassified --json` prints nothing and exits 0.

**Fixture**
- Live hosts, compared with the totals above.
- Until the full catalogue is written, the registry holds the gate-2 example: 27 components and 3 externals. Against that catalogue this test must fail, listing about 130 unclassified repos. That is the done-when's "flagged", seen on real data.

**Exercises**
- interfaces.md §3 `inventory()`, CLI `inventory` (exit 1 if any repo is unclassified);
- §2 `load_registry` and README §3 rule 1;
- schemas `component-catalogue` and `observation#/$defs/repo_inventory`.

**Negative control**
- *Checker:* a consistent synthetic inventory of 57 + 101 repos (the example catalogue padded with legacy entries) passes. Then each of these is caught:
  - one repo marked `unclassified`;
  - one GitLab row dropped (the page-boundary bug: 100 rows, host says 101);
  - soclabs/tidelink also listed as another component's canonical;
  - a copy reported as `canonical`.
- *System, offline:* fake GitHub and GitLab APIs serve 57 and 101 repos, paged as the real hosts page them. The catalogue has `secworks-sha256-project` removed. Then:
  - that repo, and only it, comes back `unclassified`;
  - `inventory --unclassified` exits 1 and names it;
  - `check_a1` reports it;
  - the tool asked the fake GitLab for page 2.

**Why it cannot pass vacuously**
- The count is compared with the host's own total from a separate endpoint, not with the tool's own listing. An empty or first-page-only inventory fails, and GitLab's 101 forces a second page.
- Roles are compared with the catalogue, so a tool that labels every repo `canonical` fails on the four real GitLab copies.
- If the token cannot see `total_private_repos`, the check reports that instead of passing.

## AT-A2

**Requirement (A2, Must, v1).** v1 never pushes, tags, merges or changes settings in any repo. Its only credential beyond read access is a trigger token that can start jobs in the CI project and nothing else.
*Done when:* The service holds read-only repo credentials plus one trigger token scoped to nanosoc-compat-ci; every Run is logged with who pressed it.

**Given**
- The deployed package source.
- The declared write allowlist (gap G10).
- The service config's `credentials.*`.
- The `runs` and `actions` tables, after at least one manual Run (AT-A4 or the H1 demo).

**When** the test:
1. censuses the package (AST) for every call that can write outside the service's disk:
   - HTTP `post`, `put`, `patch` and `delete` method calls;
   - `request`, `stream` or `build_request` with a method other than GET or HEAD;
   - urllib `Request` with a `method` other than GET or HEAD, or with a body;
   - `git push`, `send-pack` and `receive-pack`;
   - any `gh` or `glab` process;
   - any argv or method it cannot resolve;

   A method call on a module of this package (such as `db.put`) is internal and skipped: that module's own file is censused. An object imported from the package is not skipped.
2. reads the credentials and asks GitLab `GET personal_access_tokens/self` with the poller's GitLab token;
3. reads `runs` and `actions`.

**Then**
1. Every census entry is covered by an allowlist entry of the same file, kind and method. The allowlist has exactly one `write`: POST `projects/:id/trigger/pipeline`. The census finds it. The other entries are:
   - `read-post`: the OAuth code exchange (`login/oauth/access_token`), a POST that writes nothing;
   - `local`: a call the census mistakes for HTTP, allowed only in a file that imports no HTTP library;
   - `git-runner`: the one dynamic git runner, with a guard that refuses git writes.
2. The config holds exactly 4 credentials (interfaces.md §1). Their recorded scopes:

   | Credential | Recorded scope must be within |
   |---|---|
   | GitLab read | `read_api`, `read_repository` |
   | GitHub read | `contents:read`, `metadata:read`, `members:read` |
   | OAuth | `read:org` |
   | Trigger | `trigger`, and its project is `ci.project` |
3. GitLab's live answer for the poller token equals the recorded scopes, is within them, and is `active`.
4. Every `runs` row with trigger `manual` has exactly one `actions` row with the same login and configuration, within 5 s of it. Every scheduled run says `schedule`. No run has an empty `triggered_by`.

**Fixture**
- Live config and database.
- The row shape is the contract example `t0_result` (`trigger: manual`, `triggered_by`).

**Exercises**
- interfaces.md §0.4, §1 and §10 (Trigger steps 3 and 4);
- §12 `runs` and `actions`;
- `observation#/$defs/t0_result/run`.

**Negative control**
- *Checker:* a package shaped like §0.5 passes: the trigger, the OAuth exchange, a db module whose `put` and `delete` are not HTTP, a caller of `db.put` in a file that imports httpx, and one git runner. Each of these is then caught:
  - planted `client.post("repos/…/git/refs")` on an object imported from the package;
  - planted `subprocess.run(["git", "push", …])`;
  - planted `request("DELETE", "…/git/refs/tags/silicon/…")`;
  - an argv the census cannot resolve;
  - an empty census (the census is blind);
  - a second `write` entry, and a `read-post` to `graphql`;
  - a `local` claim in a file that imports httpx;
  - GitLab read scope `api`, and a trigger scoped to another project;
  - a manual run without its `actions` row.
- *System, offline:* the offline service performs one Run. Its `actions` row is then deleted from a copy of the database, and `check_a2` names that run.

**Why it cannot pass vacuously**
- The census must find the trigger call itself. A census that sees nothing (wrong path, broken parser) fails.
- An unresolvable call counts as a finding, never as a pass.
- At least one manual run must exist.
- Scopes come from GitLab, not only from the config.

## AT-A3

**Requirement (A3, Must, v1).** Every fact the tool shows carries where it came from and when it was observed.
*Done when:* Each page shows the data age; each fact links to the command or API call that produced it.

**Given**
- The live service, in-process, signed in as a member.
- The `observations` table (latest row per subject = the current value, README §4).

**When** every GET route in the app's route table that returns HTML is fetched. Path parameters come from live data: eth-rc4, tidelink, 036d381 with nanosoc_gen, SL-DEF-0001, the latest run nonce, and a real subject.

**Then**, for each page:
1. There is exactly one data-age element. Its `data-oldest-observed-at` is the oldest `data-observed-at` among the facts shown. Its `data-age-seconds` is the request time minus that, within 5 s.
2. The page shows at least one fact. Every fact has a `data-source-for` link. Following the link returns 200, and the body contains every `sources[].command` of the stored observation, verbatim.
3. Every fact's `data-observed-at` equals the stored observation's `observed_at`.
4. At least 9 HTML pages were checked (the v1 set of interfaces.md §13 plus the provenance page).
5. Every line of every `/api/v1/*` response validates against `observation.schema.json` (G20 resolved: `configurations` returns `registry_record` and `alerts` returns `alert` observations).
6. `db.put` refuses `schemas/examples/invalid/observation.without-observed-at.yaml`.

**Fixture**
- Live data.
- The invalid example is the contract's own golden (interfaces.md §16).

**Exercises**
- The observation envelope (`observed_at`, `sources`);
- §12 `put` and `data_age`;
- §13 routes and the JSON API;
- README §4.

**Negative control**
- *Checker:* a page built from the 11 real silicon-protection observations passes. Then each of these is caught:
  - one source link removed;
  - the data-age element removed;
  - the age measured from the newest fact instead of the oldest (one row re-stamped 2026-10-03T00:00:01Z, after every row; CR-2);
  - a stored fact with no sources;
  - an empty page.
- *System, offline:* a row with `sources: []` is inserted into `observations` directly, bypassing `put`. `/silicon-protection` still shows the 11 real rows, and either omits the bad row or `check_a3_page` reports it. Separately, `db.put` refuses the invalid example.

**Why it cannot pass vacuously**
- A page with no facts fails.
- Links are followed, and the exact command strings must appear.
- Times are compared with the store, and the age must be measured from the **oldest** fact. The real examples span 17:31:59Z to 18:05:04Z, so an age measured from the newest fact is 33 minutes too young and fails.

## AT-A4

**Requirement (A4, Must, v1).** The tool never runs tests itself. It starts CI jobs, on a schedule and when a member presses Run in the web interface, and then fetches their results.
*Done when:* Pressing Run on a configuration starts its T0 job in the CI project, and the result appears in the tool.

**Given**
- The live service, in-process, signed in as a member.
- eth-rc4 registered at `ab73e40`, with no run pending.
- compat-ci has job `t0` on `main`.
- A PATH shim logs every git argv.

**When**
1. The Run T0 form is read from `/configurations/eth-rc4` and posted with its CSRF field.
2. `nanosoc-rm t0 fetch` runs every 2 minutes until the run leaves `pending`, for at most 45 minutes.

**Then**
1. The response shows fact `t0_result:eth-rc4:<nonce>`.
2. `t0 runs eth-rc4 --json` shows that nonce as `pending`, `manual`, by the member, with a `pipeline_id`.
3. The CI API itself, read independently, shows:
   - the pipeline in compat-ci has `source: trigger` and the configured ref;
   - it has exactly one job named `t0`;
   - its `RM_REQUEST` variable has the run's nonce, `configuration: eth-rc4` and `commit: ab73e40…`.
4. The final `t0_result` carries that job's id and a non-null verdict. If accepted, its gate ids are exactly the configuration gates in `nanosoc_rm.t0.gates`.
   - Today's data gives raw verdicts FAIL on `t0.pins.fetchable` and `t0.urls.no-ssh-public` (interfaces.md §9).
     With rc4's `t0_waivers` registered (GATE2 Q1) those gates show WAIVED and keep `raw_verdict`; the run is FAIL
     or WAIVED, never PASS. AT-D2 checks the waivers item by item.
5. `/runs/<nonce>` shows the run.
6. The service ran no tests: `nanosoc_rm.t0.runner` was never imported in the service process, and the git log has no `clone` and no `fetch --depth=1` (T0's probe).

**Fixture**
- Live: real rc4 and a real pipeline (PR8: a person runs it).
- Expected gate rows: `result-bundle.verdicts.example.tsv`.

**Exercises** interfaces.md §9 (job contract), §10 (trigger, fetch, verify), §13 POST `/configurations/{id}/runs`, and `t0_result`.

**Negative control**
- *Checker:* a consistent run and pipeline pass. Then each of these is caught:
  - `pipeline_id: null`;
  - an `RM_REQUEST` whose nonce is not the run's;
  - two `t0` jobs;
  - a result still `pending`;
  - a result carrying another job's id.
- *System, offline* (local estate and fake GitLab):
  - The trigger answers 401. The tool records exactly one `t0_result`: `acceptance: no-bundle`, `pipeline_id: null`, verdict UNVERIFIED or INFRA, reason naming 401 (interfaces.md section 10, Trigger step 5). A tool that swallowed the 401 and stored a pending run, or nothing, fails. `check_a4` reports "no pipeline for this nonce". No run is `accepted`, and the runner module stays unimported.
  - The pipeline's variables carry a foreign nonce. The fetched run is `rejected` at verify step 2 with `RM_REQUEST` in its reason, and `check_a4` names the mismatch.

**Why it cannot pass vacuously**
- The link between the button and the result is proven through the CI API's own record (nonce in `RM_REQUEST`, job id), not through the tool's database alone. A stale earlier result, or a result made locally, fails.

## AT-A5

**Requirement (A5, Must, v1).** The tool is private to SoC Labs members.
*Done when:* Every page requires a SoC-Labs member login.

**Given**
- The offline service: the example registry, the example observations seeded, and a fake GitHub.
- Three identities:
  - `alice`: active member;
  - `bob`: 404, a non-member;
  - `anonymous`: no session.

**When** the test enumerates **the app's own route table**, fills path parameters from the rc4 fixtures, and requests every route as each identity. alice and bob sign in through the fake OAuth flow first.
- alice makes GET requests only.
- bob and anonymous use every method.
- Signed-in clients do not request `/auth/*`, so a logout route cannot end their session mid-sweep.

**Then**
1. Every route in interfaces.md §13 is in the table.
2. alice is never refused (no 401, 403 or 5xx on any GET), and gets 200 on `/`, `/silicon-protection`, `/defects` and `/api/v1/configurations`.
3. bob gets 403 on every route outside `/auth/*`, including `/api/v1/*` and POST.
4. anonymous gets 401, 403, or a redirect to the login path, on every route outside `/auth/*`.
5. No response to bob or anonymous, on any route including `/auth/*`, contains rc4 data: its commit, `eth-rc4`, `SL-DEF-0001`, `ETH-R0P0-ERR-001` or `keep/eth-rc4`.
6. The only mounted static directory is `/static`. No static file is a template or holds any of those strings.

**Fixture**
- Synthetic identities: a person's GitHub login cannot be scripted, and no spare non-member account is known.
- The data behind the pages is the real rc4 examples.

**Exercises** interfaces.md §13 routes and §14 auth.

**Negative control**
- *Checker:* a clean sweep passes. Then each of these is caught:
  - a route open to bob that leaks the rc4 commit;
  - a sweep with no member requests;
  - a contracted route missing from the table.
- *System 1:* the app is built with an auth that trusts bob (`FakeMembership(login="bob")`). The sweep reports "non-member bob got 200 on GET /". This fires whatever the auth design.
- *System 2 (coverage):* a raw route is appended to the app after construction, outside any FastAPI dependency. The sweep still requests it, because it reads the route table. If it answers bob with 200, the checker names it.

**Why it cannot pass vacuously**
- The sweep is driven by the route table, so a new route cannot escape it.
- alice must open the fixed pages, so a service that refuses everyone fails.
- Bodies are scanned for real data, so a 403 page that leaks data fails.

## AT-A6

**Requirement (A6, Must, All).** The tool never writes to users' forks and never withdraws a silicon release.
*Done when:* No code path writes to a fork or yanks a silicon release.

**Given**
- The package source, all phases.
- The schemas.
- An offline estate:
  - a superproject on "github" that pins `soclabs/ip` at a tag, and a user's fork `alice/ip` at a fork-only commit;
  - an MR ref on `ip` and a PR ref on the superproject.

**When**
1. The census from AT-A2 runs over the package.
2. The real eth-rc4 eligibility entry is validated as it is, with `yanked: true` added, and with `frozen: false`.
3. The full v1 CLI cycle runs over the estate with the git shim:
   - `mirror sync`, `inventory`, `walk`, `hosts`, `silicon-protection`, `contains`;
   - `t0 trigger`, `t0 fetch`.

**Then**
1. No census entry, allowlisted or not (except the contracted CI trigger), targets any of these (coverage review Low 3: A6 holds in every phase, so it looks only at forks and silicon):
   - a fork endpoint (`/fork`, `/forks`);
   - a GitHub repo outside `SoC-Labs`, or a GitLab project outside `soclabs` (a user's fork);
   - anything naming `silicon/`, or "yank";
   - a `git push` or `send-pack` to a target that cannot be resolved statically.
2. Every allowlisted `write` and `git-runner` entry declares its guard. In v1 the only write is the CI trigger, which targets the CI project, not a repo.
3. The real eth-rc4 entry is valid, and both variants are rejected by `eligibility.schema.json`.
4. The git log holds no `push`, `send-pack` or `receive-pack`. The fake hosts received no request except GETs and the one trigger POST. The cycle did work: at least one `fetch`, and at least one GET per host.
5. The refs of `ip`, the fork and the superproject are exactly as before the cycle.

**Fixture**
- Real: the eth-rc4 eligibility entry and the schemas.
- Synthetic: the fork estate, because a scratch fork on a real host would need a person to create it (PR8).

**Exercises** interfaces.md §0.4, the `eligibility` and `erratum` schemas (`status: permanent`), and README §3 rule 6.

**Negative control**
- *Checker:* the §0.5-shaped package passes. Then each of these is caught:
  - a planted `git push <fork url> HEAD`;
  - a planted `POST repos/alice/ip/git/refs` (a user's fork);
  - a planted `DELETE repos/…/git/refs/tags/silicon/eth-chiplet-r0p0`;
  - a write entry without a guard.

  A planted write to an org repo's refs is not an A6 finding (A2 catches it in v1).

  The planted git runner refuses `push` at run time. The two eligibility variants fail validation against the real schema today.
- *System:* the same checker over a copy of the real package with the planted module added names that module.

**Why it cannot pass vacuously**
- Patterns are matched over every census entry, including allowlisted ones.
- The runtime cycle must show that it fetched and read. A cycle that did nothing fails.

## AT-A7

**Requirement (A7, Must, All).** Capabilities beyond v1 (test matrix, versioning, bump bot, releases, forks) each start only when their entry gate is met.
*Done when:* Each phase's entry gate is written down and checked before it starts.

**Given**
- The phase-gate register [`docs/phase-gates.yaml`](../phase-gates.yaml) (G12 resolved; format in interfaces.md section 17.6). It holds a written, checkable entry gate for Phases 2 to 5, all `not-met`.
- The requirement-to-phase map parsed from REQUIREMENTS.md.
- The tool repo's git log with each commit's changed files: under PR7, commits name requirement IDs.
- The routes of the offline app and the CLI's commands.

**When** `check_a7` runs over them.

**Then**
1. Phases 2, 3, 4 and 5 each have an entry gate with at least one criterion, and every criterion has a `check`.
2. A phase marked `gate-met` or `started` has `checked.at` and `checked.by`.
3. The first commit that changes `src/` and names a requirement of a later phase is later than that phase's `checked.at`. Commits that change only contracts, schemas, docs or tests do not count: PR3 writes later phases' formats before any code (interfaces.md §15).
4. Before a phase's gate is met, none of its automation is enabled (none is a job the scheduler runs). CR-15
   (2026-10-12) separates build from enable: a phase's routes and commands may exist (built and used locally),
   and an implementation commit made from 2026-10-12 on that names its requirements is building, not starting
   it; rule 3 still holds for every earlier commit. The register's per-phase `automation` names what the gate
   governs (default below):

   | Phase | Automation the gate governs | Surfaces (may be built; CR-15) |
   |---|---|---|
   | 2 | `nightly_matrix` | `/matrix`, `matrix` |
   | 3 | `tool_release` | `release`, `changelog` |
   | 4 | `bump_bot` | `/bumps`, `bump` |
   | 5 | `soc_release` | `/releases`, `/trains`, `/forks`, `lock`, `forks` |

**Fixture**
- Real: REQUIREMENTS.md, the tool repo's log, and the proposal's §9 entry gates. For example, Phase 2: generator deterministic, runner ephemeral with a fresh HOME, CPU-budgeted host.
- The register exists (`test_selfcheck_phase_gate_register` checks its shape and that its capabilities are exactly the later-phase requirement IDs). No phase has met its gate, which is correct before v1 is built.

**Exercises** PR7 (commit IDs), interfaces.md §15 (formats fixed now for later phases), and §13.

**Negative control**
- *Checker:* in a scratch git repo:
  - a commit "feat(matrix): D3 …" that adds `src/matrix.py` before the Phase 2 gate was checked is caught;
  - a commit naming G3 and C11 that changes only `schemas/` is not;
  - the same repo with the Phase 2 gate checked before the D3 commit passes.
- An enabled `nightly_matrix` while Phase 2 is not met is caught. A route list with `/releases` and a command list with `lock` are not (CR-15), nor is a D3 commit made after 2026-10-12. A register missing Phase 2 is caught.

**Why it cannot pass vacuously**
- A missing register fails.
- Each later phase must have at least one criterion with a check.
- The commit rule reads real history, file by file.

## AT-H1

**Requirement (H1, Must, v1).** v1 pages: a dashboard (per SoC: T0 result, red pins, open errata, host drift, runner status, data age), repo detail, contains-commit, silicon protection, and defects and errata. The dashboard and each configuration have a Run T0 button. Later phases add the matrix (with Run for any cell), bump queue, releases and trains, and forks.
*Done when:* Each v1 page is demonstrated on live data, including a manual Run.

**Given**
- The live service, in-process, signed in as a member.
- The configurations registered in the live registry, with no eth-rc4 run pending.

**When** these pages are fetched, and eth-rc4's dashboard Run form is posted:
- `/`, and `/configurations/<id>` for every configuration;
- `/components/tidelink`;
- `/contains?sha=036d381&component=nanosoc_gen`;
- `/silicon-protection`;
- `/defects`, `/defects/SL-DEF-0001`, `/errata/eth-rc4`.

**Then**
1. Every configuration has a dashboard row with all six items and exactly one Run T0 form: a POST to `/configurations/<id>/runs` with a CSRF field.
   - `red-pins` equals the count of pins red on their pinned host in `pins <id> --json` (BRANCH_ONLY counts as red in a frozen or silicon tree). For rc4 that is 0 today.
   - `open-errata` equals the registry's errata with status `open` or `permanent`. For rc4 that is 1: ETH-R0P0-ERR-001.
   - `host-drift` names every component in the tree whose copy is not SAME in `hosts --json` (G22). For rc4 that includes tidelink, ahb_qspi and asic-flow.
2. Each configuration page has one working Run T0 form.
3. Each page's facts include every subject its CLI command gives, and no extra facts of the same kind:
   - `pins eth-rc4`: 44;
   - `hosts tidelink`;
   - `contains 036d381`: `no` on gitlab;
   - `silicon-protection`: eth-rc4's 11 pinned-host rows, all covered, plus its canonical-host rows (interfaces.md section 7).
4. The defects and errata pages show SL-DEF-0001 and ETH-R0P0-ERR-001.
5. After the Run:
   - the response shows `t0_result:eth-rc4:<nonce>`;
   - the dashboard's eth-rc4 Run form is disabled, or gone, while that run is pending (interfaces.md §10).

**Fixture** Live. The expected values are the contract goldens (interfaces.md §16).

**Exercises** interfaces.md §13 (pages) and PR4 (two front ends over one function), through the CLI parity.

**Negative control**
- *Checker:* a fixture dashboard with all hooks passes. Then each of these is caught:
  - the runner-status cell removed;
  - one configuration's row removed;
  - a Run form without CSRF;
  - a red-pins cell showing 3 where the CLI gives 0;
  - a silicon page that lacks one of the 11 CLI subjects;
  - a silicon page with a 12th, invented subject.
- *System, offline:* the real offline dashboard passes for eth-rc4 and the estate configuration. Asked for a configuration the registry does not hold, the checker reports all six items and the Run button missing.

**Why it cannot pass vacuously**
- Every page is compared with the CLI's output for the same question, so a static or empty page fails.
- The CLI side must itself be non-empty.
- Each count is checked against the CLI or the registry, not merely for presence.

## AT-H2

**Requirement (H2, Must, v1).** Sign in with GitHub, limited to SoC-Labs members. Roles come from the service's own configuration, and every write action is logged.
*Done when:* A non-member cannot open any page.

**Given**
- The offline service, with a fake GitHub for OAuth and the membership API.
- Identities:
  - `alice`: `active`;
  - `bob`: 404;
  - `carol`: `pending`, invited but not joined.

**When**
1. Each identity signs in through `/auth/login` → the fake authorize URL → `/auth/callback`.
2. Each sweeps every GET route (as in AT-A5).
3. A callback with a forged `state` is attempted.
4. alice presses Run on the estate configuration.
5. The config is rebuilt with an empty role table, and alice signs in and requests `/` again.
6. Live probe: `is_member()` runs with the service's real read token for `NANOSOC_RM_TEST_MEMBER` and for `octocat`.

**Then**
1. The authorize redirect carries `scope=read:org`, the configured client id, and a `state`. The forged state is refused (400, 401 or 403).
2. Only alice gets a session and opens pages. bob and carol get 403 at the callback and on every route.
3. alice's GitHub token (`fake-token-alice`) appears in neither the decoded session cookie nor the database file.
4. alice's Run writes an `actions` row: login `alice`, action `run_t0`.
5. With an empty role table, alice gets 403: roles come from the config, not from org membership alone.
6. The live probe gives True for the member and False for octocat. The fake's answers therefore match GitHub's real ones (fixture realism).

**Fixture**
- Synthetic identities, plus two real logins for the realism probe.
- The OAuth consent page is not scripted (see "Untestable").

**Exercises** interfaces.md §14 and §12 `actions`.

**Negative control**
- *Checker:* a clean result set passes. Then each of these is caught:
  - bob opening a page;
  - carol given a session;
  - a Run with no `actions` row;
  - the token inside the cookie text;
  - an authorize URL asking for `repo`.
- *System:* the fake answers `active` for bob while the test's own table says non-member. The service lets him in, and `check_h2` reports both the session and the opened pages. This shows the test catches a service that trusts a membership answer it should not have been given.

**Why it cannot pass vacuously**
- alice must get in, so a service that refuses everyone fails.
- The pending state is a real GitHub answer that a naive "200 means member" check lets through.
- Without the live probe the test fails, so a fake that drifts from GitHub cannot carry it.

## AT-H3

**Requirement (H3, Must, All).** Split the credentials: the poller is read-only; the CI trigger is scoped to the CI project; the bump writer exists only on non-silicon repos; the bot's tag credential covers tool repos only; final tags of breaking tool releases and of every SoC release are made by people. Warn before any token expires.
*Done when:* Each credential's scope is recorded and checked.

**Given**
- The live config.
- The credentials check command `nanosoc-rm credentials check --json` (G9 resolved, interfaces.md section 17.3): one `credential_status` observation per credential, fact fields `name`, `recorded_scope`, `live_scope`, `expires`, `project`, `verdict` ok|finding|unverified, `reason`.
- `docs/phase-gates.yaml`: whether Phase 4's gate is met (H3 holds in every phase).
- The `alerts` table.

**When**
1. The check runs.
2. Independently, the test reads GitLab `GET personal_access_tokens/self` with the poller token.
3. It reads GitHub `GET user` with the GitHub read token, looking at the `X-OAuth-Scopes` and `github-authentication-token-expiration` headers.
4. It reads the Location of `/auth/login`.

**Then**
1. The rows are exactly `github_read`, `gitlab_read`, `ci_trigger` and `github_oauth`. No `bump_writer` or `bot_tag` credential exists before the Phase 4 gate is met; after it they may, and get their own scope checks in Phase 4 (coverage review Low 3).
2. Every row has a recorded scope within its allowed set (as in AT-A2) and a recorded expiry. The OAuth app needs none.
3. `gitlab_read`: `live_scope` equals GitLab's own answer and the recorded scope. The recorded expiry equals `expires_at`.
4. `github_read` is not a classic token (no `X-OAuth-Scopes` header). Its expiry header, if any, equals the recorded date.
5. `github_oauth`: the authorize URL asks for `read:org` only.
6. No row is `ok` without a `live_scope`. A credential the host cannot describe is `unverified` with a reason.
7. Every credential expiring within 14 days has an uncleared expiry alert.
8. `ci_trigger` is recorded against the configured `ci.project`.

**Fixture** Live tokens (read-only calls). The GitLab answer's field names were checked on 2026-09-30.

**Exercises** interfaces.md §1 (`credentials.*`, 14-day warning) and §14 (OAuth scope).

**Negative control**
- *Checker:* a clean report passes. Then each of these is caught:
  - recorded `api`;
  - GitLab reporting `write_repository` where the record says `read_repository`;
  - `ok` with no live scope;
  - a classic-token header;
  - a token 10 days from expiry with no alert (and the same token with the alert passes);
  - a `bump_writer` row before Phase 4 (and the same row passes once the Phase 4 gate is met).
- *System, offline:* the fake GitLab answers `scopes: ["api"]` while the config records `read_api`. The command exits 1, and its `gitlab_read` row shows the live scope that differs.

**Why it cannot pass vacuously**
- Scopes are compared with the host's own answer where the host gives one.
- A credential that could not be checked cannot show `ok`.
- The credential set must be exactly the 4 v1 credentials.

## AT-H6

**Requirement (H6, Must, v1).** Keep local mirrors of every repo's branches and tags (not pull-request refs), refreshed every 15 minutes. v1 polls; webhooks come later.
*Done when:* The mirrors refresh on schedule and report their age.

**Given**
- The live service with its schedule running.
- The catalogue.

**When**
1. `nanosoc-rm mirror status --json` is read twice, 16 minutes apart, then the table form (no `--json`) once.
2. For the two repos with foreign refs, `mirror sync --component <id>` runs. The mirror's refs are then compared with anonymous `git ls-remote --heads --tags` against the configured host.

**Then**
1. Every catalogued repo (canonical, copies and externals) has a `mirror_status` in both snapshots. Each is `ok`, with age (`now - last_success_at`) at most 20 minutes: the 15-minute poll plus 5 minutes for one sweep.
2. Every repo's `last_success_at` advanced between the snapshots, and the table prints an `age` column (seconds) for every repo, within 2 minutes of now − `last_success_at` (the done-when's "report their age", coverage review Low 2).
3. Every mirror ref matches `refs/r/<github|gitlab>/<urlencoded path>/(heads|tags)/*`. There is no `refs/tags/*` outside that namespace (G19).
   - NanoSoC-Ethernet-Chiplet's mirror has no `pull/` ref, although the host advertises 5.
   - AHB-QSPI's mirror has no `environments/` ref, although the host advertises 2.
4. After the sync, the mirror's heads and tags equal the host's `ls-remote`.

**Fixture** Live. The real foreign refs are listed under "Real data used".

**Exercises** interfaces.md §3 (layout, refspec, `mirror status`, age), `observation#/$defs/mirror_status`, and README §5 ("PR/MR refs are never mirrored").

**Negative control**
- *Checker:* a local fixture repo carries `refs/pull/1/head` and `refs/merge-requests/1/head`.
  - `git clone --mirror` of it: `check_h6_refs` names both refs.
  - The first draft's §3 fetch, without `--no-tags`: `check_h6_refs` names the auto-followed `refs/tags/v0.1.0`.
  - The §3 refspec with `--no-tags`: it passes, and the MR-only commit is absent from the mirror.
  - Status docs 40 minutes old, one with `ok: false`, one repo missing, and a repo that did not advance are each caught.
- *System, offline:* the estate is synced, then its host directory is removed and synced again. The status shows `ok: false` with the old `last_success_at`. 16 minutes later the check reports it stale.

**Why it cannot pass vacuously**
- Both snapshots must cover every catalogued repo, and each must advance.
- The real repos with foreign refs are checked by name, so a mirror that simply fetched nothing would fail the `ls-remote` equality.

---

## Negative controls run today

Each `_nc_<id>_checker` now runs in pytest as `test_selfcheck_<ID>`. On 2026-09-30, after the revision,
`python3 -m pytest --strict-markers tests/acceptance/test_scope_web.py` gave **12 passed, 22 xfailed** under
Python 3.8 (pytest 8.3.5, jsonschema 4.19.1) and 3.11 (pytest 9.1.1, jsonschema 4.26.0), git 2.43.7. All 11
controls fired, and each first passed on its consistent input: A1, A2, A3, A4, A5, A6, A7, H1, H2, H3, H6. The
12th, `test_selfcheck_phase_gate_register`, checks the A7 register.

- The offline fixtures were also exercised without the package: the estate builds, the fixture registry validates against the catalogue and eligibility schemas, the TOML config parses, the fake GitLab pages 101 projects into 2 pages, and the fake CI's bundle validates against result-bundle v1 with 5-field, nonce-stamped rows.
- The system-level controls need the implementation.
- The 22 ATs report XFAIL (`not implemented: gate 2`) until the package exists.

## Contract gaps

**Every gap below was resolved in the gate-2 revision (2026-09-30), G17 in part;** the last column says where. `docs/contracts/interfaces.md` section 17.7 has the same index. The text of each gap is kept as written, so the review trail stays readable.

These are things a test needs that the contracts do not fix. The test file assumes the name shown, in its `ASSUMED` table. "Sibling" means another gate-2 AT file.

| Gap | What is missing | Assumed here | ATs | Resolution (2026-09-30 revision) |
|---|---|---|---|---|
| G1 | **Entry points.** No app factory, test auth seam or config loader is named. The `db` functions (`put`, `latest`, `data_age`) take no handle, so how they find the database is undefined. The three AT files assumed different seams: `test_ci_errata.py` uses `create_app(config_path, auth=FakeMembership(login=))`, and this file now uses the same. | `nanosoc_rm.web:create_app(config_path, auth=None)`, `nanosoc_rm.auth:FakeMembership(login=)`, `nanosoc_rm.config:load_config(path)`; `db.*` find the database through `$NANOSOC_RM_CONFIG`, read per call (as `test_pins.py` assumes) | A3, A4, A5, H1, H2, H3 | Resolved: interfaces.md 17.2 (`create_app(config_path, auth=None)`, `FakeMembership`, `load_config`; db finds the database through the config) |
| G2 | **Config keys.** interfaces.md §1 names sections, not keys. The three AT files disagree: data directory `[service] data_dir` (`test_ci_errata.py`, and now this file) against `[data] dir` (`test_pins.py`); registry checkout `registry.path` (all three now). The role table's shape conflicts: §1 says "login → roles" while §14 says "every member has `member`". | `[service] data_dir`; `registry.path`; `hosts.<h>.{web, api, git, org or group}`; `credentials.<name>.{token_file, scope, expires, project, client_id}`; `roles.default` for every member plus optional per-login entries; `session.secret_file` | all | Resolved: interfaces.md 17.1 (`[service] data_dir`; `roles.default` plus per-login entries) |
| G3 | **Auth surface.** Missing: login, callback and logout paths; the membership re-check function; what an anonymous user gets (redirect or 401); the CSRF field name. §14's "403 on every route" for a non-member needs a signed-in-but-refused session state after the callback; otherwise a refused user is simply anonymous and gets the login redirect. | `/auth/login`, `/auth/callback`, everything auth under `/auth`; `/static`; `nanosoc_rm.auth:is_member(config, login)`; `csrf_token`; a refused session answering 403 | A4, A5, H1, H2 | Resolved: interfaces.md section 14 (paths, refused session gets 403, `is_member`, `csrf_token`) |
| G4 | **Page markup.** Routes are fixed, markup is not. Without hooks, A3 and H1 could only grep text. `test_ci_errata.py` proposes overlapping hooks (`data-configuration`, `data-age-seconds`, `data-verdict`); the two sets should be merged into one list. | The `data-*` hooks above | A3, H1 | Resolved: interfaces.md section 13 (one merged hook list) |
| G5 | **Provenance route.** §13 says each fact links to its command, but no route shows an observation's sources. | `GET /facts/{subject}` (HTML) | A3 | Resolved: interfaces.md section 13 (`GET /facts/{subject}`) |
| G6 | **Registry records as facts.** Defects, errata and eligibility are shown on pages but have no observation kind, no defined `observed_at` (registry commit time or last pull time?), and no JSON API route or CLI read command. The last conflicts with PR4 (CLI first). | Subjects `registry:<file>`; the link must show the `git -C <registry> show <commit>:<file>` command | A3, H1 | Resolved: observation `registry_record`; interfaces.md section 2 |
| G7 | **Runner status** (an H1 dashboard item) has no observation kind, so it has no source or age. `test_ci_errata.py` G7 proposes a `runner_settings` kind. | Shown as a fact like the others | A3, H1 | Resolved: observation `runner_settings`; interfaces.md section 13 |
| G8 | **Trigger failure.** What `trigger()` records when the trigger POST fails (401, 404, 5xx) is undefined. A `pending` run with `pipeline_id: null` would keep the Run button disabled forever (one pending run per configuration). `test_ci_errata.py` G2 proposes `acceptance: no-bundle` with a reason; this file's control accepts that. | The check requires only that no result appears without a pipeline | A4 | Resolved: interfaces.md section 10 Trigger step 5 (`no-bundle`, `pipeline_id: null`, button enabled again) |
| G9 | **Credential check.** H3 says "recorded and checked", but no function or command does the check. The `alerts.kind` for expiry is also missing. | `nanosoc-rm credentials check [--json]`, rows as in AT-H3, exit 0/1/2; alert kind contains `expir` | H3, A2 | Resolved: interfaces.md 17.3 (`credentials check`); observation `credential_status`; alert `token_expiry` |
| G10 | **Write allowlist.** A2 and A6 need a declared list of outbound calls, so the census has something to equal. A plain "one entry, the trigger" list cannot work: the OAuth code exchange is also a POST (§14), the store's own `put` or `delete` looks like HTTP to a static census, and the one git runner has a dynamic argv. | `nanosoc_rm.contracts:WRITE_CALLS`, entries `{file, kind, method, target, effect, guard, reason}`, effect one of `write` (exactly one: the trigger), `read-post` (OAuth exchange only), `local` (only in a file that imports no HTTP library), `git-runner` (at most one, with a guard) | A2, A6 | Resolved: interfaces.md 17.5 (`WRITE_CALLS`) |
| G11 | **Action log vocabulary.** The `actions.action` value for Run and the `target` format are not fixed. The CLI `t0 trigger --by LOGIN` lets a shell user write any login into `runs.triggered_by`. It is undefined whether a CLI trigger is `manual` and whether it writes an `actions` row. | `run_t0`; target contains the configuration id | A2, H2 | Resolved: interfaces.md section 10 CLI (`--by` required, `manual`, `actions` row) and section 14 |
| G12 | **Phase-gate register.** A7 has no artefact: interfaces.md §16 says "process, not an interface". PR7's commit convention (trailer name) is also not fixed. Separately, proposal §1 item 7 (a v1 lock and ERRATA script) conflicts with REQUIREMENTS G3/F4 (Phase 5) and interfaces.md §15. Under A7, a v1 `lock` command needs a CR. | `docs/phase-gates.yaml` with `phases[] {phase, capabilities, entry_gate[] {criterion, check}, status, checked {at, by, evidence}}`; IDs read anywhere in the message of a commit that changes `src/` | A7 | Resolved: interfaces.md 17.6, `docs/phase-gates.yaml` |
| G13 | **Scheduler seam.** There is no deterministic scheduler step, such as a tick function with an injected time. Without one, schedule tests wait on the wall clock (16 minutes in AT-H6). | None; AT-H6 waits | H6 | Resolved: interfaces.md section 12a (`schedule.tick(now)`); AT-H6 still waits 16 minutes on the live schedule, by design |
| G14 | **Mirror naming is inconsistent.** interfaces.md §3 says `refs/r/<host>/<urlencoded path>/heads/*` in `<component id>.git`. The `mirror_status` source in `observation.example.jsonl` shows `refs/hosts/<host>/heads/*` in `mirrors/ahb_qspi.git`. | §3 | H6, A3 | Resolved: interfaces.md section 3; examples fixed |
| G15 | **Mirror coverage.** §3 says one mirror per catalogue component or external. H6 says "every repo". It is undefined whether legacy repos and unclassified repos are mirrored. | Canonical, copies and externals of the catalogue | H6, A1 | Resolved: interfaces.md section 3 (which repos) |
| G16 | **Observation history retention** is unstated. If old `mirror_status` rows are kept, H6's cadence can be proven from data instead of by waiting. | Not relied on | H6 | Resolved: interfaces.md section 12 (history kept in v1) |
| G17 | **Silicon rules in the loader.** Rule 6 refuses `permanent` on a non-silicon configuration. Nothing refuses the converse: a silicon configuration's erratum marked `fixed`. Nothing flags a registry change that deletes a silicon configuration from `eligibility.yaml`, which silently drops its silicon-protection rows. | Not tested; question for A6 and B5 | A6 | Resolved: README section 3 rule 10 (no `fixed` on silicon); deleting a silicon configuration: a code-owner approval on `eligibility.yaml` (GATE2 Q12, decided 2026-10-01; a registry setting, not a tool rule) |
| G18 | **Test infrastructure.** The markers `requirement` and `live` must be registered in `tests/conftest.py` or in pyproject `markers` (`test_ci_errata.py` G13 says the same). That file is outside this writer's paths. Dev dependencies: pytest, jsonschema ≥ 4.18, PyYAML, fastapi (TestClient), httpx. | None | all | Resolved: interfaces.md 17.4, `tests/conftest.py`, `requirements-dev.txt` |
| G19 | **The §3 mirror fetch has no `--no-tags`.** git auto-follows tags even with explicit refspecs, so every fetch also writes `refs/tags/<name>` outside the per-repo namespace. In a store holding both hosts, the first host fetched owns the bare tag names. Reproduced with git 2.43.7 on scratch repos. | Proposed fix: add `--no-tags` to the §3 command. AT-H6 fails an implementation that copies §3 literally. | H6, B3 (TAGGED reads tags) | Resolved: interfaces.md section 3 (`--no-tags`) |
| G20 | **Two JSON API routes return no observation kind.** `/api/v1/configurations` ("list, with the latest T0 result") and `/api/v1/alerts` (expiry and unknown-URL alerts have no kind) cannot validate as observations, yet A3 needs every fact to carry a source and a time. | The test validates every line strictly, so these two fail until a kind or an explicit exemption is contracted | A3 | Resolved: observation `registry_record` and `alert`; interfaces.md section 13 JSON API |
| G21 | **Live-test doctrine differs.** `test_ci_errata.py` and this file fail a live test without its inputs. `test_pins.py` skips it (`NANOSOC_RM_TEST_LIVE_CONFIG`). The env var names differ too. | Fail, with `NANOSOC_RM_CONFIG` and `NANOSOC_RM_TEST_MEMBER` | all live ATs | Resolved: interfaces.md 17.4 (a live test without its inputs fails) |
| G22 | **"Host drift" is not defined exactly.** §13 says "DIVERGED or lags". It is unclear whether BEHIND (the copy has commits the canonical lacks) and UNRELATED count. | Any relation except SAME and UNKNOWN | H1 | Resolved: interfaces.md section 13 (host drift) |

## Untestable, or testable only in part

1. **H2, the GitHub consent page.** It cannot be driven headless without storing a person's GitHub password and 2FA. The test covers everything from the callback on, against a fake GitHub whose answers are checked against the real API for two real logins. A person signs in with a browser at the gate-3 demo.
2. **H3, GitHub read token permissions.** A fine-grained token (or GitHub App token) cannot report its own permissions. Proving it read-only live would need a write attempt, which PR8 forbids.
   - The check proves it is not a classic token, compares its expiry, and otherwise reports `unverified` with a reason.
   - A person records the scope as issued.
3. **H3, the trigger token.** A GitLab pipeline trigger token belongs to one project by construction. Listing it (`GET projects/:id/triggers`) needs a role the read token should not have. It is recorded, checked against `ci.project`, and reported `unverified` otherwise.
4. **H3, Phase 4 credentials** (bump writer, bot tag). They do not exist in v1. The test asserts only that they are absent. Their scope checks come with Phase 4.
5. **A7 is a process requirement.** The test checks that each gate is written down, checked before the phase's code starts, and enforced in the surfaces. It cannot check that a criterion was truly met: a person signs.
6. **A6 and A2, "no code path".** The census reads Python call sites statically.
   - A dynamic URL or argv counts as a finding unless it is the single declared git runner, whose guard is proven at run time (AT-A6) and by the runtime git log.
   - Code outside the package (shell, CI YAML) is not censused. The compat-ci job scrubs every CI token and password from its environment before its first git call (interfaces.md §9; `t0.runner.hardened` checks it), except one read of the private registry by the `@registry` job (question 3 in GATE2.md).
   - A call through `getattr` or `eval` is invisible to any static census.
7. **A4, the expected rc4 gate results.** GATE2 Q1 (decided 2026-10-01) adds waivers, so the test asserts the raw FAIL gates and accepts FAIL or WAIVED for the run. The exact waived roll-up is AT-D2's and AT-D6's to prove.
8. **A4, unknown:** whether the poller's GitLab token, at its project role, may read `GET pipelines/:id/variables`, and whether a new compat-ci project accepts pipeline variables from the trigger's owner. interfaces.md §10 verify step 2 depends on both, and names a missing variable in its reason (question 10 in GATE2.md).
9. **A1, unknown:** whether GitHub returns `total_private_repos` to the poller's token. It is returned only to an org member with sufficient access. If it is absent, the check reports that, and a person supplies the total.
10. **H1, "demonstrated".** A demonstration is a person watching. The test automates the page checks and the Run. The demonstration itself is gate 3.
