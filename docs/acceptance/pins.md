# Acceptance tests: pins, trees and hosts

Gate 2 (PR3). One acceptance test (AT) per v1 Must requirement in this area: **B1, B2, B3, B4, B5, B6, B8**.
Each AT has a negative control (PR6).

- **Requirements:** [`REQUIREMENTS.md`](../REQUIREMENTS.md) v1.0, frozen 2026-09-30.
- **Contracts exercised:** [`contracts/README.md`](../contracts/README.md), [`contracts/interfaces.md`](../contracts/interfaces.md), [`schemas/`](../../schemas/).
- **Test code:** [`tests/acceptance/test_pins.py`](../../tests/acceptance/test_pins.py). Every AT carries `NOT_BUILT` (xfail, `raises=ImportError`, strict): XFAIL until its module exists, red on any other error, red as XPASS once it passes, so its builder removes the marker (`tests/conftest.py`).
- **Status:** for David's gate-2 review. Written 2026-09-30, revised the same day after the contracts and coverage reviews ([GATE2.md](../GATE2.md) section 4). Every real fixture value was re-derived the same evening by a second method (read-only), see [Real facts](#real-facts-the-fixtures-use).

## How to read this page

1. **Two tests per AT.** Both call the same checker, `check_<ID>()`, which returns a list of problems.
   - `test_AT_<ID>` runs the tool on the real (or hermetic) input and asserts the list is empty.
   - `test_AT_<ID>_negative_control` runs the tool on a deliberately broken input and asserts the checker reports it. If the checker passes there, the AT does not count (PR6).
2. **Self-checks run now.** `test_selfcheck_*` test the test file, not the tool. They need only git and Python.
   - Each checker passes on a consistent real input and fails on the broken one.
   - The hermetic fixtures really hold the states they claim. A raw-git oracle, which shares no code with the tool, computes them.
   - Results: [Negative controls run today](#negative-controls-run-today).
3. **Where each test runs.**

   | Mode | Needs | Used by |
   |---|---|---|
   | `hermetic` | git and Python ≥ 3.11. Local bare repos stand in for both hosts (git `url.insteadOf`). A local HTTP server (`FakeApi`) answers the host API calls the walker makes. | B1 (control), B3 (table), B8 |
   | `live` | `NANOSOC_RM_CONFIG`: the private service config, for its read tokens and host table. The tests sync into their own data dir, or into `NANOSOC_RM_TEST_DATA` to reuse one between runs. They never use the service's data dir. `NANOSOC_RM_REGISTRY` for B1. Marked `@pytest.mark.live`. | B1, B2, B3, B4, B5, B6 |
   | `shadow` | A per-test copy of the live data dir. Its mirrors borrow the live objects through `objects/info/alternates`, and its refs are edited locally to build broken inputs. It is never synced, because a sync would fetch the refs back. Nothing on a host changes (A2, PR8). | the B3 to B6 controls, and the B6 replay |

   - A live test without its inputs **fails**; it never skips and never passes (interfaces.md section 17.4). Off the VM, deselect with `-m "not live"`.
   - AT-B3 and its control run their hermetic half first, so that half is exercised wherever the tests run.
4. **Real or synthetic.** Values marked OBSERVED were read on 2026-09-30 with read-only git and API calls. A SYNTHETIC input is used only where no real tree holds the case (PR_ONLY, ORPHAN, REWRITTEN, UNREADABLE, LOCAL_URL and NO_GITLINK do not occur in rc4 or multicore).
5. **Names beyond the schemas** are fixed in `interfaces.md` section 17. Each lives in one helper (`rm()`, `as_doc()`, `service_config()`, `ref_prefix()`, `pin_level_state()`). The gaps the first draft logged, and where each was resolved, are under [Contract gaps](#contract-gaps).
6. **Hostnames.** The lab GitLab host is written `<gitlab-host>`, as in the contracts. The hermetic fixtures use `gitlab.example.invalid`.

## Index

| AT | Requirement | What the test checks | Runs (test / control) | Negative control |
|---|---|---|---|---|
| [AT-B1](#at-b1) | B1 classes | The registry loads; every component has exactly one of the 8 classes; a legacy entry is listed; no configuration names a legacy component | live (the real registry) / hermetic | 5 registries, each with one defect: no class, two classes, unknown class, one id with two classes, a configuration on the legacy component |
| [AT-B2](#at-b2) | B2 nested tree | `walk eth-rc4` gives exactly rc4's 44 pin keys, with child commit, repo, component, tree path and depth; depths 5/9/17/12/1 | live / live | eth main's real tree (43 pins, 13 shared) is reported as not rc4's; a golden missing one pin is reported |
| [AT-B3](#at-b3) | B3 pin states | rc4's 44 + 7 and multicore's 35 + 4 host states match the hand-checked goldens; `branch=` is recorded and ignored; the hermetic table yields all 11 states where placed | live + hermetic / shadow + hermetic | `keep/eth-rc4/nanosoc_gen` deleted: `d04de2e` turns BRANCH_ONLY; tag `v1.0.0` deleted: the TAGGED pin turns ON_DEFAULT |
| [AT-B4](#at-b4) | B4 contains commit X | `036d381` resolves to its full SHA; per host, eth-rc4 answers `no` with both generator pins proven; the other configurations match a raw-git oracle; `yes`, `partial`, `unknown` and `absent` also occur on real data | live / shadow | `d04de2e` still an object but reachable from no ref: the answer must be `unknown`, never `no` |
| [AT-B5](#at-b5) | B5 silicon protection | eth-rc4 lists the 11 branch-only commits on their pinned host (3 GitHub, 8 GitLab), each covered by its keep tag, and the canonical-host rows: arch_tech `ecf76aa` and ahb_qspi `e908ac8` on GitHub, covered since 2026-10-01; on the live registry (DEC6) also the 3 R1 rows, uncovered (CR-2) | live / shadow | `keep/eth-rc4/tidechart` deleted: tidechart `4b4b898` joins the uncovered list |
| [AT-B6](#at-b6) | B6 host drift | On the 2026-09-30 heads: TideLink AHEAD 298/0, AHB-QSPI DIVERGED 1/5, ASIC-Flow DIVERGED 4/1, counted by rev-list in one mirror; DIVERGED exits 1; today's answers equal raw git | live + shadow / shadow | AHB-QSPI's copy moved to GitHub's head reads SAME and fails; ASIC-Flow with its GitLab refs gone reads UNKNOWN |
| [AT-B8](#at-b8) | B8 rewrite alert | A force-push on a test repo's default branch raises exactly one alert naming old and new head, 3 lost commits and the lost registered pin | hermetic / hermetic | a fast-forward and a force-push of a non-default branch: no alert, so the expected-alert check fails |

## Real facts the fixtures use

OBSERVED 2026-09-30, read-only. The architect's goldens came from the host APIs and walk.py. They were then re-derived by a second method: raw git in fresh blobless mirrors holding heads and tags only, with the README §5 precedence. The three MISSING states were confirmed by the read APIs.

| Fact | Value | Used by |
|---|---|---|
| rc4 superproject | SoC-Labs/NanoSoC-Ethernet-Chiplet `ab73e40924317e45e7b3b941f52a7ed35f20832e`. Tags `v1.0.0`, `silicon/eth-chiplet-r0p0` and `candidate/rc4-20260829` all peel to it. | all |
| rc4 tree | 44 pins, one root. Depths: 1 → 5, 2 → 9, 3 → 17, 4 → 12, 5 → 1. No parent commit appears at two tree paths, so no pin has two `tree_paths`. | B2 |
| rc4 pinned-host states | 32 ON_DEFAULT, 11 PROTECTED_BY_TAG, 1 TAGGED. All 44 states and lags re-derived: 44 agree. URLs: 42 https, 2 ssh (`tidelink`, `ASIC/asic-toolkit`). | B3 |
| rc4 `branch=` | 5 entries. Three say `main` for commits not on main, all PROTECTED_BY_TAG: `tidelink/deps/tidelink-phy`, `ASIC/asic-toolkit`, `.../ethernet-mac-ahb`. The others: `.../ethernet-subsystem-ahb/nanosoc_arch_tech` `main` (ON_DEFAULT) and `ASIC/asic-flows` `lpddr4-pll` (ON_DEFAULT, GitHub's default branch). | B3 |
| TideLink `5e8bdb5` | exactly `v0.90` on GitHub, 5 behind main, so TAGGED. MISSING on the GitLab copy (API 404). GitLab main `9092300` is an ancestor of it, 293 behind. | B3, B4, B6 |
| multicore-master | soclabs/nanosoc-multicore-system `master` = `127e3d230cba6d8c0760685413d91755d02be786`, unchanged when re-checked that evening. 35 pins, depth ≤ 4, all ON_DEFAULT on their pinned host; all 35 re-derived. One ssh URL: `nanosoc_arch_tech/rtl/coresight_soc400_tech` (SoC-Labs/SoC-Labs-SoC400-Tech). Two `branch=main`, both on main. | B3 |
| eth main | `8e6d0434fc8331191607ad5b2af73646e995c892` (2026-08-07, still the head). 43 pins; 13 of its pin keys are also rc4's. | B2 control |
| nanosoc_gen ancestry | `b0a6069` (SL-DEF-0001 origin) → … → `1d7772d` → `d9973b3` → `c2ca528` → `036d381` (fix, form A) → `adbb9ab` (main). `d04de2e` branches off `d9973b3`. Checked with `merge-base --is-ancestor`. | B4 |
| rc4 generators | `d04de2e` (`nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen`) and `1d7772d` (`.../ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen`). Neither contains `036d381`; both contain `b0a6069`; only `d04de2e` contains `d9973b3`. | B4 |
| 036d381 elsewhere | eth main pins `d9973b3` and `1d7772d`; multicore master pins `c2ca528` and `1d7772d`. Neither contains the fix. Compute (not registered) pinned `b8ec29b` on 2026-09-28, which does not contain it either. nanosoc_gen has no GitHub repo. | B4 |
| keep tags | 11 `keep/eth-rc4/*` tags, each peeling to its commit (`git ls-remote`): 3 on GitHub, 8 on GitLab. `keep/eth-rc4/tidelink-phy` is named by submodule path, not component id: it covers tidelink-gpio-phy `8c560c5`. | B5 |
| host heads | TideLink GitHub main `6c4632a` / GitLab main `9092300`: 298 / 0. AHB-QSPI GitHub master `71f8d1c` / GitLab master `1e5117e`: 1 / 5. ASIC-Flow GitHub `lpddr4-pll` (its default) `3824aaa` / GitLab main `1f99de2`: 4 / 1. Counted with `rev-list` in one object store holding both hosts' refs. | B6 |
| drift while checking | nanosoc_gen main moved 2 commits (to `5d6e2a5`) between two checks about 10 minutes apart. The `1d7772d` lag went 39 → 41. Live lags only grow, so the live ATs compare lag ≥ golden. | B3 |

**Second-host states** (the B3 golden's other half). Each is a component with a copy:

| Tree path | Commit | Pinned host | Other host |
|---|---|---|---|
| `tidelink` | `5e8bdb5` | GitHub (canonical): TAGGED `v0.90`, lag 5 | GitLab `soclabs/tidelink`: MISSING |
| `nanosoc-multicore-system/nanosoc_arch_tech` | `ecf76aa` | GitLab copy: PROTECTED_BY_TAG | GitHub (canonical): BRANCH_ONLY. The keep tag is on the GitLab copy only. |
| `nanosoc-multicore-system/ethernet-subsystem-ahb/nanosoc_arch_tech` | `06b4d24` | GitLab: ON_DEFAULT, lag 62 | GitHub: ON_DEFAULT, lag 73 |
| `nanosoc-multicore-system/ahb_qspi` | `e908ac8` | GitLab: ON_DEFAULT, lag 3 | GitHub: MISSING (API 422) |
| `ASIC/asic-flows` | `3824aaa` | GitHub: ON_DEFAULT, lag 0 | GitLab: MISSING (API 404) |
| `tidelink/deps/axi-chiplet-controller/asic_flow` | `69500ef` | GitLab: ON_DEFAULT, lag 2 | GitHub: ON_DEFAULT, lag 5 |
| `nanosoc-multicore-system/syn/asic/asic_flow` | `46c88d3` | GitLab: ON_DEFAULT, lag 1 | GitHub: ON_DEFAULT, lag 4 |

multicore's four: `nanosoc_arch_tech` `490d6d0` GitHub ON_DEFAULT 47; `ethernet-subsystem-ahb/nanosoc_arch_tech` `06b4d24` GitHub ON_DEFAULT 73; `ahb_qspi` `a0d3d51` GitHub ON_DEFAULT 1; `syn/asic/asic_flow` `46c88d3` GitHub ON_DEFAULT 4.

## Fixtures

1. **Live registry.** The example catalogue and eligibility, plus:
   - two test-only frozen configurations, so the goldens have fixed trees: `eth-main-20260930` at `8e6d043` and `multicore-master-20260930` at `127e3d2`;
   - component `soc400-tech`, which multicore pins and the example catalogue lacks.

   These exist only in the test registry, never in the real one.
2. **Hermetic state table** (`build_state_table`). One SoC commit with 15 pins, one per README §5 state:
   - TAGGED, ON_DEFAULT, PROTECTED_BY_TAG (by a `keep/*` tag, which must not count as TAGGED), BRANCH_ONLY with `branch = main` (the trap);
   - REWRITTEN twice: once through the mirror reflog after a real force-push, and once through catalogue `known_rewrites`;
   - PR_ONLY (a commit only on `refs/pull/7/head`), ORPHAN (an object on the host with no ref), MISSING, UNREADABLE (a catalogued repo the host lacks);
   - LOCAL_URL, NO_GITLINK twice (a directory, and nothing at the path);
   - an ssh URL (the badge);
   - `dual`: a two-host component whose GitLab copy lags one commit, so the pinned commit is in the shared mirror but MISSING on GitLab.
3. **Hermetic rewrite fixture** (`build_rewrite_fixture`). GitLab-side repo `soclabs/rm-rewrite-fixture`, main = A-B-C-D-E. A frozen configuration `rm-fixture` pins D at `deps/rewrite`.

---

<a id="at-b1"></a>
## AT-B1 · Every component has exactly one class

**Requirement B1 (Must, v1).** "Classify each component as SoC reference, example project, generator, arch_tech, IP tech, toolkit, infrastructure or legacy. Legacy repos are listed but never tested." Done when: every catalogue entry has one class.

**Given**
- The real registry, named by `NANOSOC_RM_REGISTRY`. Without it the AT fails: the example catalogue's classes are judgement, so passing on them would prove nothing about the registry (coverage review, Medium 5).
- The negative control uses the examples: 27 components (including the legacy `secworks-sha256-project`), 3 externals, 3 ignored repos, 3 configurations.

**When**
1. `load_registry(path)` (interfaces §2).
2. `nanosoc-rm registry validate PATH --json`.

**Then**
1. The registry loads with no problems, and the CLI exits 0.
2. The number of loaded components equals the number of `catalogue.yaml` entries, and no id appears twice.
3. Every component's `class` is exactly one of `soc_ref, example, generator, arch_tech, ip_tech, toolkit, infra, legacy`.
4. At least one `legacy` entry is loaded and listed.
5. No configuration names a legacy component. Only configurations are tested (T0 runs per configuration), so legacy is never tested.

**Fixture:** REAL repo paths, copies and aliases. Every `class` is JUDGEMENT, for the registry owner to confirm.

**Exercises:** component-catalogue schema `class` (required, enum); README §3 rules 1 (an id used twice) and 2 (a configuration's component must be `soc_ref` or `example`); interfaces §2 `load_registry`, `RegistryError.problems`, `registry validate`.

**Negative control.** Five registries, each the example with one defect. `check_B1` must report every one:
1. `tidechart` with no class;
2. `class: [ip_tech, legacy]`;
3. `class: library`;
4. `tidechart` twice, the second as `toolkit`;
5. a configuration `secworks-sha256` on the legacy component.

The schema alone rejects 1 to 3. Cases 4 and 5 are schema-valid, so only the loader's rules 1 and 2 catch them (self-check `test_selfcheck_B1_schema`).

**Cannot pass vacuously because**
- The loaded count must equal the file's entry count, so an empty or partial load fails.
- A registry with no legacy entry fails, so "legacy is listed" is always exercised.
- Each case must be refused by the loader itself (`RegistryError`) with the specific problem: a schema problem at tidechart's `class` for 1 to 3, a `rule-1` problem for 4, a `rule-2` problem for 5. `check_B1` also re-checks classes and legacy use itself, so its own failure alone does not prove the loader enforces anything (CR-4).

**Open:** the real registry does not exist yet, so AT-B1 fails until it does. Classifying all ~158 repos is A1's done-when (AT-A1 in `scope_web.md`).

---

<a id="at-b2"></a>
## AT-B2 · rc4's tree is exactly its 44 pins

**Requirement B2 (Must, v1).** "For each registered SoC configuration (a SoC repo at a branch or tag, including silicon references like rc4), resolve the full nested submodule tree from that commit's own .gitmodules and pinned commits." Done when: rc4's tree of 44 pins is reproduced exactly.

**Given**
- eth-rc4 registered frozen at `ab73e40` (tag `v1.0.0`).
- Every catalogue component and external mirrored read-only (live).

**When**
1. `walk("eth-rc4")` (interfaces §4).
2. `nanosoc-rm walk eth-rc4 --json`.

**Then**
1. Exactly 44 `pin_state` observations, each valid against `pin-state.schema.json`.
2. Their keys (parent host, parent path, parent commit, path) equal the 44 in `release-lock.eth-rc4.example.yaml`, with none missing, extra or repeated.
3. For each pin, these fields equal the golden: child commit, child repo (after alias normalisation), child component, tree paths, depth, and `entry_type: commit`.
4. The depth histogram is 1 → 5, 2 → 9, 3 → 17, 4 → 12, 5 → 1.
5. Every depth-1 pin hangs off `ab73e40`.
6. The CLI prints the same tree and exits 0 or 1.

**Fixture:** REAL: all 44 pins, from the lock example (walk.py output with full SHAs, PR9).

**Exercises:** interfaces §4 `walk` and its B2 golden; `pin-state.schema.json` `parent`, `child`, `tree_paths`, `depth`, `entry_type`; README §5 alias normalisation (`slcorem0p_tech`).

**Negative control**
1. A different real tree: eth main frozen at `8e6d043` (43 pins, 13 shared with rc4). `check_B2` against rc4's golden reports missing pins.
2. The true rc4 walk against a golden missing one pin: `check_B2` reports an extra pin.

Self-check: 44 synthesised correct documents pass. Dropping one, repeating one, moving TideLink's commit to `9092300`, or hanging the depth-1 pins off eth main's commit each fail.

**Cannot pass vacuously because**
- The key sets must match both ways, so an empty, partial or padded walk fails.
- The per-pin fields are compared, so a walk with the right keys and wrong commits fails.

**Open**
- No rc4 pin has two tree paths, so the `tree_paths` merge is not exercised on real data.
- Which repo counts as a pin's parent is not written down (G8).

---

<a id="at-b3"></a>
## AT-B3 · Every pin has a state on each host

**Requirement B3 (Must, v1).** "Give every pin a state on each host: tagged, on the default branch (and how far behind), branch-only, protected by a tag, pull-request-only, orphaned, missing, rewritten, SSH-only, or a local path. Never trust the .gitmodules branch= field." Done when: the states match a hand check on rc4 and multicore.

**Given**
- Live: eth-rc4, and `multicore-master-20260930` (frozen at `127e3d2`), mirrored from both hosts.
- Hermetic: the state table (Fixtures, item 2), synced once, then `fx-rewritten` force-pushed and synced again.

**When:** `walk(CONFIG)` and `nanosoc-rm pins CONFIG --json`.

**Then**
1. **rc4, pinned host:** all 44 states match the golden: 32 ON_DEFAULT, 11 PROTECTED_BY_TAG, TideLink TAGGED `v0.90`.
   - Each lag is ≥ the golden's (the hosts move).
   - Each golden tag is present.
2. **rc4, second host:** the 7 states in the table above: TideLink MISSING on GitLab, arch_tech `ecf76aa` BRANCH_ONLY on GitHub, and so on.
3. **URL kind and `branch=`:**
   - `url_kind: ssh` on `tidelink` and `ASIC/asic-toolkit`; SSH is a badge, not a state.
   - `branch_field` is recorded exactly as written for the 5 entries, and null elsewhere.
   - The 3 `branch = main` pins that are not on main still read PROTECTED_BY_TAG.
4. **multicore:** the 35 pinned-host states and lags (lag ≥ golden), the ssh badge on `coresight_soc400_tech`, and the 4 second-host states.
5. Every pin with hosts has exactly one host marked `pinned`.
6. `pins eth-rc4` exits 0: nothing is red on a pinned host.
7. **Hermetic table:** each of the 11 states appears exactly where the fixture put it, with its lag, tags and branches. In particular:
   - a `keep/*` tag does not make a pin TAGGED;
   - `dual` is ON_DEFAULT on GitHub and MISSING on GitLab, although its object is in the shared mirror.

   `pins fx-states` exits 1.
8. **Hand check:** `HAND_CHECK` in the test file names the person who checked each golden, and the date. The AT fails while it is empty.

**Fixture**
- REAL: rc4 and multicore, both re-derived by a second method on 2026-09-30.
- SYNTHETIC: the hermetic table, for the six states no real tree holds.

**Exercises:** README §5 (precedence, colours, SSH badge, `branch_field`); `pin-state.schema.json` `hosts`, `lag`, `tags`, `branches`, `pinned`; interfaces §3 (mirror layout, kept reflogs) and §4 step 4 (API fallback: GitHub `commits/{sha}` and `/pulls`, GitLab `repository/commits/{sha}` and `/merge_requests`); catalogue `known_rewrites`.

**Negative control**
1. **Shadow:** `keep/eth-rc4/nanosoc_gen` is deleted from the real mirror's refs. `d04de2e` is then reachable only from `wip/uncommitted-2026-08-18`, and must read BRANCH_ONLY. `check_B3` fails at `nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen@gitlab`.
2. **Hermetic:** tag `v1.0.0` is deleted on the host and re-synced. The `tagged` pin must read ON_DEFAULT, and `check_B3` fails at `tagged@github`.

Self-checks:
- The oracle reproduces the whole table, and control 2.
- Synthesised rc4 documents pass, and each of these mutations fails: the keep tag lost; `branch=` trusted; a second host dropped; a lag shrunk; the SSH badge lost.

**Cannot pass vacuously because**
- Every golden tree path must be present, and every walked path must be in the golden.
- Each host's state is compared, not just the pinned one.
- The hermetic table must yield all 11 states.
- The hand-check record starts empty, so the AT fails until a person signs it.

**Open**
- The goldens are dated 2026-09-30. If a branch is merged to main, or a keep tag is removed, the live AT fails, and a person re-checks the golden (see [Untestable](#untestable-or-only-partly-testable)).
- G4, G6 and G9 are resolved: the pin-level `fact.state`, per-host reachability in a shared mirror, and `branch_field` null when absent.

---

<a id="at-b4"></a>
## AT-B4 · Which configurations contain commit X

**Requirement B4 (Must, v1).** "Answer 'which configurations contain commit X?' on each host, counting only commits reachable from branches and tags, and only after proving both commits exist." Done when: querying 036d381 lists the configurations that contain the cpu_ss_hsel fix, per host.

**Given**
- eth-rc4, eth-main and multicore-master walked.
- nanosoc_gen mirrored. It is GitLab only.

**When**
1. `contains("036d381", component="nanosoc_gen", configurations=[all three])` (interfaces §6).
2. Three more real queries:
   - `b0a6069` (the defect's origin) on eth-rc4;
   - `d9973b3` on eth-rc4;
   - TideLink's GitLab head `9092300` on eth-rc4 and multicore-master.
3. `nanosoc-rm contains … --json`.

**Then**
1. The prefix resolves to `036d3811e630c89b2344b56aa617b40a99cd652b`.
2. There is exactly one `gitlab` row per configuration. There is no `github` row: the component has no GitHub repo.
3. eth-rc4 answers `no`. Both generator pins, `d04de2e` and `1d7772d`, are `contains: false`, with merge-base proven.
4. eth-main and multicore-master equal a raw-git oracle over the same mirror (`merge-base --is-ancestor`, and only after both commits are proven reachable from a branch or tag). Today both are `no`. The oracle row must hold at least one pin (eth-main pins `d9973b3` and `1d7772d`; multicore `c2ca528` and `1d7772d`), so an empty walk cannot make `absent` equal `absent`.
5. Each decided row carries the proof of existence of both commits in `sources` (A3).
6. The per-host list of configurations containing the fix is printed. Today it is empty on GitLab, and there is no GitHub row.
7. **The other answers occur on real data:**
   - `b0a6069` → `yes`: both pins contain it.
   - `d9973b3` → `partial`: `d04de2e` yes, `1d7772d` no. rc4 is a split tree (B7).
   - `9092300` → eth-rc4 GitHub `yes`; eth-rc4 GitLab `unknown`, because `5e8bdb5` is not on GitLab; multicore-master `absent` on both hosts.
8. The CLI exits 0 for `036d381` (answered, even though the answer is `no`) and 2 for `9092300` (an `unknown`).

**Fixture:** REAL: every commit and every answer above, from `merge-base` in a fresh nanosoc_gen mirror.

**Exercises:** interfaces §6 steps 1 to 4 and exit codes; the observation `containment` fact (`yes / partial / no / absent / unknown`, and the rule that an undecided pin needs a `reason`).

**Negative control**
- **Shadow:** `d04de2e` is still an object in the mirror, but its branch and keep tag are deleted.
  - The same question must answer `unknown` with a reason, never `no` (the schema now refuses `no` over an undecided pin; `observation.containment-no-with-undecided-pin.yaml`).
  - The AT's expectation (`no`, both pins proven) therefore fails.
- A commit that exists nowhere (`0000000`) exits 3.

Self-check:
- The real containment example passes.
- Each of these fails:
  - the contract's own invalid example (undecided, with no reason);
  - `no` claimed while a pin is undecided;
  - `no` with the `d04de2e` proof removed from `sources`.

**Cannot pass vacuously because**
- A tool that always answers `no` fails the `yes`, `partial`, `unknown` and `absent` queries.
- A tool that answers `no` without proof fails the control.
- Each answer must agree with its own pins.

**Open**
- The done-when's list is empty today: no registered configuration carries the fix. The AT proves that the listing works with real `yes` and `partial` answers. It cannot show a real configuration containing `036d381` until one is registered, such as compute or QuickStart (questions 8 and DEC3).
- G10 is resolved: one row per (configuration, host where the component has a repo), `absent` included.

---

<a id="at-b5"></a>
## AT-B5 · rc4's silicon-protection list

**Requirement B5 (Must, v1).** "Show a silicon-protection list: every commit in a silicon or frozen tree that its component's default branch cannot reach, and whether a protective tag covers it." Done when: the list for rc4 shows the 11 commits and their coverage.

**Given**
- eth-rc4 (frozen, silicon eth-chiplet r0p0) walked.
- The 11 `keep/eth-rc4/*` tags on the hosts.

**When**
1. `silicon_protection("eth-rc4")` (interfaces §7).
2. `nanosoc-rm silicon-protection eth-rc4 [--uncovered] --json`.

**Then** (interfaces.md section 7: each pin is judged on its pinned host and on its component's canonical host; question 9 in GATE2.md)
1. Exactly the golden rows (CR-2: 13 on the example catalogue; 16 on the live registry, whose DEC6 catalogue makes ethernet-mac-ahb, hostio4 and socdebug_tech canonical on GitHub).
   - **11 on the pinned host** (the done-when's 11 commits): tidechart, axi-chiplet-controller, tidelink-gpio-phy (×2), ethernet-mac-ahb, ethernet-subsystem-ahb, hostio4 `51348de`, nanosoc_gen `d04de2e`, nanosoc_arch_tech `ecf76aa` (GitLab copy), nanosoc-multicore-system `f60449b`, asic-toolkit. 3 are on GitHub and 8 on GitLab. Each is `covered: true`, by at least its golden keep tag.
   - **On the canonical host, where it differs:** nanosoc_arch_tech `ecf76aa` and ahb_qspi `e908ac8` on GitHub, PROTECTED_BY_TAG by `keep/eth-rc4/nanosoc_arch_tech` and `keep/eth-rc4/ahb_qspi` (David's 2026-10-01 tags), both `covered: true`. On the live registry also ethernet-mac-ahb `44ff8b7` (MISSING), hostio4 `51348de` (BRANCH_ONLY) and socdebug_tech `50359ee` (MISSING) on GitHub, `covered: false` until decision R1 is carried out; the GitLab rows of the first two are then `pinned` only.
   - None is on its default branch, and each has the golden tree paths and host roles.
2. The pinned-host rows are exactly the pins whose pinned host's default branch cannot reach them, which cross-checks the B3 states.
3. eth-main and multicore-master are neither frozen nor silicon, so they give no rows.
4. `--uncovered` prints exactly the uncovered golden rows and exits 1 while there is one (the 3 R1 rows on the live registry), else 0. The full list passes the same check.

**Fixture:** REAL: all 13 rows from `observation.example.jsonl`. Each keep tag was confirmed with `git ls-remote` on 2026-09-30; the two canonical-host rows were re-derived on 2026-10-10 (CR-2) from `git ls-remote` of the GitHub keep tags. The 3 R1 rows (`RC4_DEC6_ROWS` in the test file) come from anonymous fetches ("not our ref") and `ls-remote` on 2026-10-10, and are added only when the live catalogue makes those components canonical on GitHub. They depend on the catalogue's canonical host for ahb_qspi (question 2).

**Exercises:** interfaces §7; the observation `silicon_protection` fact (`covered` ⇔ `covering_tags` non-empty); eligibility `frozen` and `silicon`.

**Negative control**
- **Shadow:** `keep/eth-rc4/tidechart` is deleted.
  - `check_B5` fails on tidechart `4b4b898@gitlab`.
  - `--uncovered` exits 1 and prints the baseline uncovered rows (the R1 rows on the live registry) plus exactly one more, tidechart on GitLab.

Self-check: the 13 real rows pass. Each of these fails: tidechart uncovered; a row missing; a row on its default branch; the list judged on the pinned host only (2 rows missing); a canonical-host row that lost its tag; the 13 rows against the DEC6 golden (3 rows missing).

**Cannot pass vacuously because**
- The row set must match both ways, so an empty list fails.
- Coverage is checked row by row.
- The control flips exactly one tag, and exactly that row must turn uncovered.

**Open**
- Coverage is judged by containment, not by tag name. `keep/eth-rc4/tidelink-phy` covers component `tidelink-gpio-phy`, because a split component needs one tag per commit. Existing keep tags are grandfathered (interfaces.md section 7; C10 is Phase 3).
- The 2 canonical-host rows are real findings today. Closing them needs writes only David can make (PR8): see questions 2 and 9 in GATE2.md.

---

<a id="at-b6"></a>
## AT-B6 · The copies on the two hosts are compared

**Requirement B6 (Must, v1).** "Compare a component's copies on the two hosts and show same, ahead by n, or diverged; diverged shows red." Done when: TideLink shows GitHub ahead; AHB-QSPI and ASIC-Flow show diverged.

**Given**
- **Replay (shadow):** the three components' default branches set to the heads observed on 2026-09-30. Every head object is present in the mirror.
- **Live:** today's mirrors.

**When**
1. `compare_hosts(component)` (interfaces §5).
2. `nanosoc-rm hosts [COMPONENT] --json`.

**Then**
1. **Replay:**
   - TideLink AHEAD, GitHub 298 and GitLab 0;
   - AHB-QSPI DIVERGED, 1 and 5;
   - ASIC-Flow DIVERGED, 4 and 1.
2. Every count comes from `rev-list` in the component's mirror. A count taken from one host's compare API is refused (README §7, fact 4).
3. Diverged is red: `hosts --json` exits 1, and `hosts tidelink --json` exits 0.
4. **Live:** every catalogue component with a copy has exactly one row per copy. Each row's heads, counts and relation equal raw git in the same mirror. Today's relations are not hard-coded, because the hosts move. The replay is the done-when, on fixed heads.

**Fixture:** REAL: the six heads and three count pairs, re-counted on 2026-09-30 in fresh two-host mirrors.

**Exercises:** interfaces §3 (one object store per component, per-repo ref namespaces) and §5; the observation `host_compare` fact (`AHEAD` means the copy has 0 of its own; `DIVERGED` means ≥ 1 on each side).

**Negative control (shadow)**
1. AHB-QSPI's GitLab `master` is set to GitHub's head, as if the copy had been synced. It reads SAME, so the DIVERGED expectation fails.
2. Every ASIC-Flow GitLab ref is deleted. That side cannot be read, so the relation must be UNKNOWN, never SAME or AHEAD.

Self-check:
- The real TideLink example row was counted by GitHub's compare API, and the checker refuses it.
- With `rev-list` sources, all three rows pass.
- AHB-QSPI set to SAME fails, and so does a missing row.

**Cannot pass vacuously because**
- The counts are exact on fixed heads.
- The source rule rejects API-derived counts.
- The live part compares the tool with raw git row by row.

**Open**
- G5 is resolved: an UNKNOWN row carries null counts and may carry a null head. ASIC-Flow's rows now also carry `default_branch_mismatch: true` (GitHub `lpddr4-pll` against GitLab `main`).
- The red colour on the web page is H1's (`scope_web.md`). Here, red is the CLI exit 1.

---

<a id="at-b8"></a>
## AT-B8 · A rewritten default branch raises an alert

**Requirement B8 (Must, v1).** "Detect when a default branch's history is rewritten (a force-push) and raise an alert." Done when: a simulated rewrite on a test repo raises the alert.

**Given**
- A local bare repo standing in for GitLab's `soclabs/rm-rewrite-fixture`, with main = A-B-C-D-E.
- It is mirrored once.
- The frozen configuration `rm-fixture`, which pins D, is walked.

**When**
1. main is force-pushed to A-B-F, which drops C, D and E.
2. The poller syncs: `sync_component` (interfaces §3 and §8).

**Then**
1. Exactly one `rewrite_alert`:
   - `old_head` E, `new_head` F;
   - `lost_commits` 3;
   - `pinned_lost` `[{rm-fixture, deps/rewrite, D}]`;
   - subject `rewrite_alert:gitlab:soclabs/rm-rewrite-fixture:main:<E>`.
2. It is in `db.latest("rewrite_alert")`, the dashboard banner's source.
3. A quiet re-sync raises no second alert and does not clear the first.
4. The lost pin D now reads REWRITTEN (B3, from the mirror's reflog).
5. `detect_rewrite` returns an alert for a non-descendant (E → F) and `None` for a descendant (B → E).

**Fixture:** SYNTHETIC by design: the done-when asks for a test repo. It is a real force-push through git, into a real bare repo.

**Exercises:** interfaces §8 (`merge-base --is-ancestor`, `rev-list --count <old> --not --glob=refs/r/<key>/*`, `pinned_lost`); §3 mirror config (`core.logAllRefUpdates=always`, reflogs never expire); the observation `rewrite_alert` fact and its README §4 subject; §12 `latest`.

**Negative control (hermetic)**
1. main moves by a fast-forward, E → G.
2. The non-default branch `feat/side` is force-pushed.

Neither is a rewrite of a default branch, so the check that expects one alert fails on both. The tool must raise no alert for either (no false alarm).

Self-check: the oracle shows that the rewrite loses exactly C, D and E, including the pinned D, and that the fast-forward loses nothing.

**Cannot pass vacuously because**
- It needs exactly one alert, with exact fields.
- The control proves that no alert makes the check fail, and that the tool stays quiet on a fast-forward.

**Open**
- The done-when on a hosted scratch repo needs a person to create the repo and force-push it (PR8). The hermetic AT is the automated part.
- G12 is resolved: `db.alerts()`, `nanosoc-rm alerts clear SUBJECT --by LOGIN` (interfaces.md section 8).

---

## Negative controls run today

`python3 -m pytest --strict-markers tests/acceptance/test_pins.py` on 2026-09-30, after the revision, gave **9 passed, 14 xfailed**. It ran under both Python 3.8 (pytest 8.3.5, jsonschema 4.19.1) and Python 3.11 (pytest 9.1.1, jsonschema 4.26.0). The 14 xfailed are the 7 ATs and their 7 controls, which wait for the tool.

| Self-check | Consistent input passes | Broken inputs that fail |
|---|---|---|
| `test_selfcheck_B1_schema` | the example catalogue | no class, two classes, unknown class (by the schema); duplicate id and legacy configuration shown schema-valid, so only the loader catches them |
| `test_selfcheck_B2_checker` | 44 synthesised rc4 pins | one dropped, one repeated, a child commit moved, depth-1 pins under the wrong root |
| `test_selfcheck_B3_checker` | 44 pins + 7 second hosts | keep tag lost, `branch=` trusted, second host dropped, lag shrunk, SSH badge lost |
| `test_selfcheck_B3_fixture` | the oracle's walk of the hermetic table: all 11 states where `expected` says | tag `v1.0.0` deleted on the host: `tagged@github` is ON_DEFAULT |
| `test_selfcheck_B4_checker` | the real `036d381` / eth-rc4 containment row | contract invalid example; `no` over an undecided pin; missing existence proof |
| `test_selfcheck_B5_checker` | the 13 real rows | tidechart uncovered; a row missing; a row on its default branch; pinned-host judging only (2 rows missing); a canonical row claiming coverage |
| `test_selfcheck_B6_checker` | the 3 real rows (rev-list sources, as the contract examples now record them) | the TideLink row with GitHub's compare API as its source; AHB-QSPI SAME; a row missing |
| `test_selfcheck_B8_checker` | the example alert | no alert; two alerts; wrong lost count; an alert where none should fire |
| `test_selfcheck_B8_fixture` | the oracle on the real force-push: 3 lost, D pinned | a fast-forward: no alert, so the check fails |

The system-level controls (the tool on the broken input) need the implementation.

## Contract gaps

**Every gap below was resolved in the gate-2 revision (2026-09-30);** the last column says where. `docs/contracts/interfaces.md` section 17.7 has the same index. The text of each gap is kept as written, so the review trail stays readable.

These are needed by the tests above and not in the gate-2 contracts. Each is a proposal. None has been added to a contract. G1, G2 and G14 overlap `ci_errata.md` G4, G13 and G14 and `scope_web.md`'s ASSUMED table: one decision should serve all three.

| # | Gap | Needed by | Proposal | Resolution (2026-09-30 revision) |
|---|---|---|---|---|
| G1 | **Service config keys are unnamed.** interfaces §1 names the sections, not the keys. The data dir has no key. It is not said that clone URLs are built from `hosts.<key>.web`, or API calls from `hosts.<key>.api`. GitLab `projects/{id}` could be a number or an encoded path. | all | `[registry] path, ref`; `[data] dir`; `[hosts.<key>] web, api, ssh, org\|group`; GitLab projects addressed by URL-encoded path, as the contract examples do. | Resolved: interfaces.md 17.1 (`[service] data_dir`; `hosts.<key>.{web, api, ssh}` plus optional `git`; GitLab projects by encoded path) |
| G2 | **Module paths and return types.** §0's layout does not say which module holds each function, or whether an Observation is a dict or a model. | all | `nanosoc_rm.registry.{load_registry, RegistryError}`, `walker.walk`, `analysis.hosts.compare_hosts`, `analysis.contains.contains`, `analysis.protect.silicon_protection`, `mirror.{sync_component, detect_rewrite}`, `db.latest`. Observations are schema documents: dicts, or pydantic models dumped with `model_dump(mode="json", by_alias=True)`. | Resolved: interfaces.md 17.2 |
| G3 | **When `$NANOSOC_RM_CONFIG` is read.** Tests switch between several hermetic environments in one process. | all | It is read per call, never cached at import; or an explicit `config.load(path)` is passed down. | Resolved: interfaces.md section 1 (read per call) |
| G4 | **LOCAL_URL and NO_GITLINK have no state field.** `hosts` is empty for them, so no field carries the state, and README §5 lists them as states. "Exit 1 if any pin is red on its pinned host" is undefined for a pin with no host. | B3 | Add an optional pin-level `fact.state` (`LOCAL_URL`, `NO_GITLINK` or null). Count both as red for the `pins`/`walk` exit code. | Resolved: pin-state `fact.state`; interfaces.md section 4 step 2 |
| G5 | **An UNKNOWN host compare cannot be written.** `branch_head.head` is a required SHA, but an unreadable side has no head. "Unreadable" is also undefined for B6: a failed `mirror_status`, or no default-branch ref in the mirror? | B6 | Allow `head: null` (and `default_branch: null`) when `relation` is UNKNOWN. Define unreadable as the latest `mirror_status` not being ok, or the side's default-branch ref being absent. | Resolved: interfaces.md section 5; observation `branch_head` nullable, UNKNOWN counts null |
| G6 | **API fallback in a shared object store.** §4 step 4 says "objects absent from the mirror fall back to the API". The mirror holds both hosts' objects, so an object can be present and still absent on one host. Real cases: TideLink `5e8bdb5` (not on GitLab), AHB-QSPI `e908ac8` (not on GitHub), ASIC-Flow `3824aaa` (not on GitLab). The same applies to REWRITTEN: the reflog must be the host's own namespace. | B3, B4 | Reword step 4: "a commit reachable from no head or tag **of that host** is decided by that host's reflog, `known_rewrites`, then that host's API". | Resolved: interfaces.md section 4 step 4, README section 5 (per-host reachability) |
| G7 | **Mirror ref namespace.** "urlencoded path" is not defined (is `/` encoded?). `detect_rewrite`'s `key` has no stated format. The examples use `refs/hosts/<host>/heads/*`, which disagrees with interfaces §3's `refs/r/<host>/<path>/…`. The shadow controls edit these refs directly. | B3–B6, B8 | `refs/r/<host>/<quote(path, safe="")>/{heads,tags}/*`; `key` = `<host>/<quote(path, safe="")>`; fix the example source commands. | Resolved: interfaces.md section 3 (`quote(path, safe="")`, key format); examples fixed |
| G8 | **A pin's parent repo.** pin-state does not say whether `parent.repo` is the repo the parent was pinned by, or the component's canonical repo. The B2 key includes it. | B2 | The repo named by the parent's own pinned URL, after alias normalisation (for example the GitLab copy of nanosoc_arch_tech), as the rc4 lock records it. | Resolved: interfaces.md section 4 step 3 |
| G9 | **`branch_field` when there is no `branch=`.** walk.py writes `""`; the schema allows null. | B3 | null when the key is absent; `""` never written. | Resolved: pin-state `branch_field` (null, never empty) |
| G10 | **Containment row set.** It is not said whether an `absent` configuration gets a row per host, or which configurations `configurations=None` covers. | B4 | One row per (configuration, host where the component has a repo), `absent` included; `None` means every registered configuration. | Resolved: interfaces.md section 6 step 4 |
| G11 | **Silicon protection outside frozen trees, and keep-tag names.** It is not said what `silicon_protection("eth-main")` returns. C10's `keep/<config>/<component>` cannot name two tags for a split component (rc4 has `keep/eth-rc4/tidelink-phy`). | B5, C10 | Return `[]` for a configuration that is neither frozen nor silicon. Judge coverage by containment only. C10 (Phase 3) grandfathers existing keep tags, or allows `keep/<config>/<submodule path>`. | Resolved: interfaces.md section 7 (`[]` for non-frozen; coverage by containment; keep tags grandfathered) |
| G12 | **Alert lifecycle.** The `alerts` table has `cleared_at`/`cleared_by`, but no function reads open alerts or clears one. It is not said that `sync_component` stores what it returns. `pinned_lost` needs the registered trees, and nothing says where it reads them. | B8 | `db.alerts(open_only=True)`; `nanosoc-rm alerts clear SUBJECT --by LOGIN` (logged in `actions`); `sync_component` puts every observation it returns; `pinned_lost` reads the latest stored `pin_state` of every configuration. | Resolved: interfaces.md sections 8 and 12 (`db.alerts`, `alerts clear`, sync stores what it returns) |
| G13 | **A frozen configuration on a branch ref.** The schema allows it, but walker step 1 defines an alert only for a tag that stops peeling. The test registry uses two frozen branch snapshots. | B2, B3 | A frozen configuration always uses `commit`; its `ref` is informational, and a moved branch raises no alert. | Resolved: interfaces.md section 4 step 1, eligibility `commit` |
| G14 | **Test infrastructure.** The markers `requirement` and `live` are unregistered, so pytest warns. The lab inputs differ between the three AT files. | all | `tests/conftest.py` registers both markers. The lab inputs are `NANOSOC_RM_CONFIG` (all files), `NANOSOC_RM_REGISTRY`, and `NANOSOC_RM_TEST_DATA` (a test-owned data dir). | Resolved: interfaces.md 17.4, `tests/conftest.py` |
| G15 | **The service's git environment.** The hermetic tests route the tool's git through `GIT_CONFIG_GLOBAL` (`url.insteadOf`). | B3, B8 | The service's git subprocesses inherit the process environment. Only the T0 runner forces `GIT_CONFIG_GLOBAL=/dev/null`. | Resolved: interfaces.md section 3 (git environment) |
| G16 | **Does `walk()` store its tree?** `contains()` and `silicon_protection()` need the walked tree. It is not said whether they re-walk or read the DB. | B4, B5, B8 | `walk()` puts its observations; the analysis functions read the latest stored `pin_state`. | Resolved: interfaces.md section 4 (storage) |

## Untestable, or only partly testable

| Item | Why |
|---|---|
| B3 "a hand check" | A person must do it. The AT fails until `HAND_CHECK` names who checked each golden and when. The agent re-derivation agreed on all 44 + 7 rc4 and 35 + 4 multicore states; two multicore lags had grown by 2 during the check. That is agent evidence, not the hand check. |
| B3/B5 goldens over time | They are live and dated 2026-09-30. A legitimate change (a `wip/…` branch merged to main, a keep tag removed) fails the AT until a person re-checks the golden. B6 avoids this by replaying fixed heads. B3 and B5 cannot, because they depend on every ref of every repo. |
| B3 PR_ONLY, ORPHAN, REWRITTEN, UNREADABLE, LOCAL_URL, NO_GITLINK on real data | No rc4 or multicore pin is in these states. They are tested on the synthetic hermetic table only. B8's "why" (the ASIC-Toolkit re-root left 3 pinned commits unreachable) would give a real REWRITTEN case through `known_rewrites`, but those 3 SHAs are not recorded in any source checked for this page. Unknown. |
| B4 "lists the configurations that contain the fix" | The list is empty on today's data. No registered configuration carries `036d381`, and compute's 2026-09-28 pin does not either. The AT proves that the listing gives real `yes` and `partial` answers (`b0a6069`, `d9973b3`). A non-empty real list waits on registering compute or QuickStart (DEC3, question 8). |
| B6 "diverged shows red" | Colour is a web property, covered by AT-H1 in `scope_web.md`. Here red means the CLI exits 1. |
| B8 on a hosted repo | Creating and force-pushing a scratch repo on either host is a write (PR8). The automated AT uses a local bare repo, which is what the done-when asks. A live demo needs a person. |
| B1 on the real registry | The registry does not exist yet, so AT-B1 fails until `NANOSOC_RM_REGISTRY` points at it. Its control runs on the example catalogue today. |
