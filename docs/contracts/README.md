# Gate 2 contracts

The data formats and module interfaces for the nanoSoC release manager, fixed before any
implementation (PR3). Builders code against these; reviewers and the coverage checker test against them
(PR5). After David approves gate 2, a contract changes only through a change request (PR2).

- **Formats:** JSON Schema draft 2020-12, in [`schemas/`](../../schemas/).
- **Interfaces:** [`interfaces.md`](interfaces.md): modules, functions, CLI commands, HTTP endpoints, the T0 job.
- **Examples:** [`schemas/examples/`](../../schemas/examples/): one or more valid instances per schema, built
  from real rc4 data where possible, plus deliberately broken ones in `examples/invalid/`.

Check everything with:

```
python3 schemas/check_examples.py
```

It exits 0 only if every schema is valid 2020-12, every example validates, and every invalid example
fails for the reason written on its first line (the schemas' own negative control, PR6). Result on
2026-09-30: **47 ok, 0 failed** (jsonschema 4.19.1, PyYAML 6.0.1).

## 1. Index

| Contract | File (where it lives at run time) | Purpose | Owner module | Requirement IDs | Phase |
|---|---|---|---|---|---|
| [defs](../../schemas/defs.schema.json) | none (shared types) | SHA, host key, repo, component id, timestamps, `source`/`observed` (A3), pin states (B3), gate id | all | A1, A3, B3, B4, C1, C2, D6, F3 | v1 |
| [component-catalogue](../../schemas/component-catalogue.schema.json) | registry `catalogue.yaml` | Every repo, grouped into components; class; one canonical host; copies; aliases; known rewrites; third-party repos | registry | A1, B1, B3, B6, B8, B9 | v1 |
| [eligibility](../../schemas/eligibility.schema.json) | registry `eligibility.yaml` | Registered configurations (branch or tag), frozen and silicon flags, upgrade path; Phase 4 auto-merge scope | registry | B2, B5, D2, E3, E5, H1 | v1 (auto_merge: 4) |
| [defect](../../schemas/defect.schema.json) | registry `defects/SL-DEF-NNNN.yaml` | Root cause per component: origin, forms, trigger, detectors, fixes, effects, bench pinned by commit | registry | F1, F2, F3, F6, F7 | v1 |
| [erratum](../../schemas/erratum.schema.json) | registry `errata/<configuration>/ERR-NNN.yaml` | Effect on one configuration: forms, exposure and how it was proven, category, workaround, status, doc revision | registry | F1, F2, F3, F4 | v1 |
| [import-ledger](../../schemas/import-ledger.schema.json) | registry `imports.yaml` | Every issue in the existing registers and what became of it: record, dismissed (with reason) or pending | registry | F3, F5 | v1 |
| [pin-state](../../schemas/pin-state.schema.json) | walker output (JSONL), DB | One pin of a configuration's tree with its state on each host | walker | A3, B2, B3, B7, B9 | v1 |
| [observation](../../schemas/observation.schema.json) | DB, JSON API, CLI `--json` | Envelope of every fact shown (source + observed_at) and the facts: host compare, containment, silicon protection, rewrite alert, mirror status, repo inventory, T0 result | each producer; db | A1, A2, A3, B4, B5, B6, B8, D2, D5, D6, D7, D11, H1, H6 | v1 |
| [result-bundle](../../schemas/result-bundle.schema.json) | CI artifact `rm-bundle/provenance.json` + `rm-bundle/verdicts.tsv` | What a CI job ran and what it found; the verdicts.tsv row contract reused from the toolkit, plus a run nonce | T0 runner (writes); ci fetch/verify (reads) | A4, D2, D4, D5, D6, D7, D11, PR9 | v1 (host, ip_hash: 2) |
| [component-manifest](../../schemas/component-manifest.schema.json) | `nanosoc-component.yaml` in each repo | ID, class, version, canonical URL, provides, requires, re-exports, variants, tests with controls | registry (reads), T0 (validates) | C1, C6, C7, C11, D1, D6, E1, E3, I2 | 3 (validated by T0 from v1) |
| [release-lock](../../schemas/release-lock.schema.json) | release artefact `release.lock.yaml` | Every nested pin with host and state; generated-RTL archive; GDS and netlist hashes; errata and ledger refs | lock writer | A3, B7, C10, G2, G3, G7 | 5 |

The registry layout the loader expects:

```
catalogue.yaml
eligibility.yaml
defects/SL-DEF-NNNN.yaml              file name = id
errata/<configuration>/ERR-NNN.yaml   directory = configuration id; the record's id is <PREFIX>-ERR-NNN
imports.yaml                          F5 import ledger
```

## 2. Versioning rule

1. Every top-level document carries `schema: <int>`. Each schema file pins it with `const`.
2. Readers accept only the `schema` values they know, and refuse the rest with a clear error. They never guess.
3. **Bump the number** when a change can make a reader misread or reject a document it used to accept:
   a field removed or renamed, a type or meaning changed, a constraint tightened, a new required field.
   Ship a one-shot migration script with the bump.
4. **Keep the number** when adding an optional field or an enum value. Writers and readers are one package,
   so they move together. The tool release that adds the field is class `feature` (C2). A bump is `breaking`.
5. The registry's CI and the compat-ci harness each pin the tool version they validate with, so a schema
   change reaches them only when they move that pin.
6. Schema `$id`s are names (`https://soclabs.org/nanosoc-rm/schemas/...`), not download locations.

Formats the tool REUSES keep their owners' rules: `verdicts.tsv` columns 1 to 4 are exactly the toolkit's
`ci/lib.sh` `_ci_record` output, and `ci/collect-results.py` still parses a 5-column row (it ignores column 5).

## 3. Rules the loader enforces across files

JSON Schema checks one document. The registry loader (interfaces.md §2) also refuses:

1. A component id used twice, or a repo that belongs to two components (as canonical, copy, alias or external). (A1)
2. A configuration whose component is missing or is not class `soc_ref` or `example`. (B2)
3. A `commit_ref`, `regression_test`, workaround or detector source naming an unknown component. (F3)
4. An erratum whose configuration or defect does not exist, or whose `forms_present` / `effects_present`
   / `exposure.detector` ids are not in the defect. (F1, F2)
5. A defect whose effects or detectors name a form it does not define. (F1)
6. `status: permanent` on an erratum whose configuration has no `silicon` block. (F1, A6)
7. A file name or directory that disagrees with the id inside it.
8. A duplicate key in any YAML file. YAML is read with timestamps left as strings.

Rules 1 to 8 need no network. Whether each cited commit exists is T0's job (gate `t0.registry.refs-resolve`, F3).

## 4. Observation subjects

The latest observation per `subject` is the current value (A3). Keys:

| kind | subject |
|---|---|
| pin_state | `pin_state:<configuration>:<first tree path>` |
| host_compare | `host_compare:<component>:<copy host>:<copy path>` |
| containment | `containment:<component>:<commit>:<configuration>:<host>` |
| silicon_protection | `silicon_protection:<configuration>:<component>:<commit>` |
| rewrite_alert | `rewrite_alert:<host>:<path>:<branch>:<old head>` |
| mirror_status | `mirror_status:<host>:<path>` |
| repo_inventory | `repo_inventory:<host>:<path>` |
| t0_result | `t0_result:<configuration or @registry>:<nonce>` |

`observed_at` on an envelope is the **oldest** time of anything inside it, so a page's data age
(now minus the oldest `observed_at` it shows) is never flattering.

## 5. Pin states (B3)

Exactly one state per pin per host. First match wins:

| # | State | Rule | Colour |
|---|---|---|---|
| 1 | LOCAL_URL | the URL is a local path | red |
| 2 | NO_GITLINK | .gitmodules names the path, but the tree holds no gitlink there | red |
| 3 | UNREADABLE | the repo itself cannot be read with the poller's credentials | red |
| 4 | TAGGED | a tag other than `keep/*` points exactly at the commit | green |
| 5 | ON_DEFAULT | reachable from the default branch; `lag` = commits the pin lacks | green, amber as lag grows |
| 6 | PROTECTED_BY_TAG | not on the default branch, reachable from at least one tag | grey-green |
| 7 | BRANCH_ONLY | reachable only from non-default branches | amber; red in a silicon or frozen tree |
| 8 | REWRITTEN | not reachable now, but was on the default branch before a rewrite (mirror reflog, or `known_rewrites`) | red |
| 9 | PR_ONLY | not reachable from heads or tags; the host has it through a PR/MR ref or its fork network | red |
| 10 | ORPHAN | the host has the object, and no ref of any kind reaches it | red |
| 11 | MISSING | the host does not have the object, after proving the repo is readable and the parent's entry is a gitlink | red |

- **SSH-only** is `url_kind: ssh` on the pin, shown as a badge beside the state.
- **Local path** is state LOCAL_URL.
- The `.gitmodules` `branch=` value is recorded in `branch_field` and never used.
- **Unknown URL (B9)** is `child.catalogued_as: unknown`, and raises an alert.

"Reachable" always means from a branch or tag on **that** host. PR/MR refs are never mirrored.

## 6. Verdicts (D5, D6, D7)

| verdicts.tsv row status | Gate verdict in the service |
|---|---|
| PASS, and row `<gate>.negctl` is PASS (the control fired) | PASS |
| PASS, and the `.negctl` row is FAIL or missing | UNVERIFIED (D6) |
| FAIL | FAIL |
| UNVERIFIED, or WARN on a declared gate | UNVERIFIED |
| INFRA | INFRA (never FAIL, D7) |
| SKIP | NOT-RUN; the detail must say why |
| no row, or two rows, for a declared gate | UNVERIFIED |

- **Run verdict:** FAIL, else UNVERIFIED, else INFRA, else PASS. NOT-RUN gates are counted and shown beside the verdict; they do not set it.
- **Rejected bundle (D5)** or **refused bundle (D11):** the run's verdict is UNVERIFIED and the reason is shown.
- **No bundle:** INFRA when the CI API reports an infrastructure cause, else UNVERIFIED.

## 7. Examples and what is real

| Example | Real (observed 2026-09-30, read-only) | Illustrative |
|---|---|---|
| `component-catalogue.example.yaml` | all repo paths, copies, the slcorem0p_tech alias, externals, the legacy repo | every `class`; canonical host of ahb_qspi and asic-flow |
| `eligibility.example.yaml` | eth-rc4 commit and tags | the choice of configurations; eth-main's upgrade_from |
| `defect.SL-DEF-0001.example.yaml` | every commit (full ids resolved), fix order on nanosoc_gen main, hsel_lint usage | effect-to-port mapping and netlist pattern (from the proposal, not re-checked) |
| `erratum.ETH-R0P0-ERR-001.example.yaml` | ancestry evidence (merge_base), 57d2c76 in rc4, 7a87e20's branch | doc_revision; category left null |
| `pin-state.example.jsonl` | 3 rc4 pins, each host's state, with the commands that produced them | none |
| `observation.example.jsonl` | 3 host compares, 1 containment, 11 silicon-protection rows, mirror status, inventory | the rewrite alert (synthetic fixture repo) and the rejected T0 run |
| `release-lock.eth-rc4.example.yaml` | all 44 pins with state, lag and tags; P1 archive; GDS and netlist md5 | errata, gate_ledger, generated_by.version |
| `result-bundle.provenance.example.json` + `.verdicts.example.tsv` | commits, the ssh URLs, and the ahb_qspi reachability gap they report | run ids, times, harness SHA; no T0 job has run yet |
| `component-manifest.example.yaml` | canonical URL | everything else; no repo has a manifest yet |
| `import-ledger.example.yaml` | source documents, their commit, item headings | none (dispositions left `pending`) |

**Hostname placeholder.** Where a real pinned URL names the lab's GitLab host, the examples write
`<gitlab-host>`. The real hostname lives only in the private service config (interfaces.md §1).

### Facts found while building the fixtures

These affect acceptance tests:

1. **rc4 → TideLink `5e8bdb5` is TAGGED.** It is `v0.90` exactly, 5 behind GitHub main. The proposal listed it as ON_DEFAULT. The same commit is MISSING on the GitLab copy `soclabs/tidelink`.
2. **rc4 has 6 split components, not 4.** Pinned at more than one commit:
   - nanosoc_arch_tech, nanosoc_gen, socdebug_tech, hostio4 (the proposal's 4);
   - tidelink-gpio-phy: `6ee8418` and `8c560c5`;
   - asic-flow: `69500ef` and `46c88d3` on GitLab, and `3824aaa` on GitHub.
3. **All 11 branch-only rc4 commits are now PROTECTED_BY_TAG.** Multicore's branch `fix/dma250-hsel-guard` disappeared between 09-28 and 09-30. The keep tag now covers `f60449b`.
4. **GitHub's compare API returns 404 when the other host's head is absent.** That says nothing about divergence. The real answers came from one object store holding both hosts' refs:
   - AHB-QSPI: GitHub +1, GitLab +5.
   - ASIC-Flow: GitHub +4, GitLab +1.
   - The mirror design follows from this (interfaces.md §3).
5. **The SL-DEF-0001 bench `6aafc94` exists only in a local checkout.** Recording it makes `t0.registry.refs-resolve` fail until it is pushed: F3 working on real data.
6. **rc4's pin set does not fit one canonical host for two components:**
   - ahb_qspi `e908ac8` exists only on GitLab;
   - asic-flow `3824aaa` exists only on GitHub.

   Whichever host is canonical for ahb_qspi, D2's "reachable on its canonical host" check will judge rc4 by that host's copy.

## 8. Reuse (PR9)

| Existing asset | Where it is used |
|---|---|
| `walk.py` prototype | Pin walker (interfaces.md §4): URL normalisation, `.gitmodules` parsing, gitlink type check, dedupe by (repo, commit), recursion. Changed to keep full SHAs and to read mirrors before APIs. rc4's 44-pin output is the B2 golden. |
| toolkit `ci/lib.sh` `_ci_record` | verdicts.tsv columns 1 to 4 and the gate-id convention, unchanged; column 5 (nonce) added. |
| toolkit `ci/collect-results.py` | The row parser and the roll-up doctrine (an unread report is never a pass). |
| toolkit `ci/check-pins.sh` | T0 reachability method: `merge-base --is-ancestor` against fetched refs, never `ls-remote` tips; `--offline` asymmetry (stale cache: PASS sound, not-found UNVERIFIED). |
| toolkit `ci/check-vendor-collateral.sh` | Bundle and tree scanner (D11, D2 no-Arm-IP). Its method is kept: arm every rule on a specimen first, census the corpus (empty = UNVERIFIED), allowlist with owner. |
| toolkit `ci/check-vacuous-gates.sh` | The same method is applied to the tool's own gates: every T0 gate has a control. |
| fpgahub stack | Layout and dependencies: Python 3.11, click CLI, FastAPI + Jinja2 + htmx, pydantic, httpx, itsdangerous sessions, hatchling, one systemd unit (H4). |
| Artifactory | Store `artifactory` in `store_ref` (D12, G7). The P1 rc4 archive is already there. |
| compute's pin checker (`NanoSoC-Compute-Chiplet/.github/workflows/submodule-pins.yml`) | Gate `t0.pins.fetchable`: its probe (`git fetch --depth=1 <url> <sha>` into an empty repo, no credentials, `GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) and its failure classes: not-our-ref and auth are FAIL, unreachable host is INFRA. Extended from direct pins to the whole recursive tree. |
