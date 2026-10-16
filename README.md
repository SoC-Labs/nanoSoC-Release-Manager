# nanoSoC Release Manager

Monitors the nanoSoC repos, tests SoC references against submodule versions, and manages errata and releases.

- Requirements: `docs/REQUIREMENTS.md` (v1.0, frozen 2026-09-30); change requests in `docs/CHANGES.md`.
- Gate 2 (contracts and acceptance tests) was approved on 2026-10-01: `docs/GATE2.md`.
- Check the contracts: `python3 schemas/check_examples.py`. Run the tests: `python3 -m pytest tests/acceptance`
  (dependencies in `requirements-dev.txt`; off the service VM add `-m "not live"`).

## MVP usage (command line, no service VM)

`NANOSOC_RM_CONFIG` names an anonymous config (no tokens): `[service] data_dir`, `[registry] path` (a checkout of
the registry) and `[hosts.*]` (interfaces.md section 17.1). The mirrors come from `nanosoc-rm mirror sync`.

```
# Is this SoC's tree compatible? Walk it, then run the T0 gates locally (anonymous; CR-11, interfaces 9a)
nanosoc-rm compat eth-rc4                     # one line per pin, the gates, the run verdict
# What if one pin moved? Override the gitlink at a tree path (full 40-hex sha; repeatable; never stored)
nanosoc-rm compat eth-rc4 --pin nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=<sha>
nanosoc-rm compat eth-main --json             # one compat_result document
# Then run the SoC's T1 (lint) or T2 (smoke sim) command from the registry's tiers.yaml in a sandbox checkout of
# the same tree (CR-12): budget enforced, killed at 1.5 x, rows tier.t1[.<gate>]; skipped when T0 is FAIL
nanosoc-rm compat eth-main --tier t1
nanosoc-rm compat eth-main --tier t1 --force-tier   # a person's override past a T0 FAIL: UNVERIFIED for release
```

Exit 0 PASS, WAIVED or WARN; 1 FAIL; 2 UNVERIFIED or INFRA; 3 usage (unknown configuration, bad `--pin`, no pin
at that tree path). `t0.runner.hardened` is always NOT-RUN in a local run: a laptop is not the hardened runner.
`--keep-work DIR` keeps the work directory; `--store` records a run without overrides. With `--tier`, the exit code
follows `run_verdict` (the ladder over T0 and the tier); a tier that regenerates `build_soc` (`regenerates: true`)
is refused with exit 3 unless `NANOSOC_GEN_OK=1` is set. No set_env.sh is ever sourced: a tier command gets a clean
environment plus the `env` its tiers.yaml entry declares.

## Test capabilities and "Test this bump" (Wave G)

Each tree declares what it can test in `.nanosoc/capabilities.yaml` at its root (`schemas/capabilities.schema.json`;
drafts for the eth chiplet and TideLink in `schemas/examples/`). The tool reads it at the commit under test from its
mirror; the registry's `tiers.yaml` keeps only per-host site data (`hosts`: tool installs, licences, site paths).

```
nanosoc-rm capabilities validate .nanosoc/capabilities.yaml   # while you write one
nanosoc-rm capabilities show eth-main                         # per tree: capabilities, hosts that can run them, last result
nanosoc-rm test-bump eth-main tidelink v1.0 --plan            # what a move would run (nothing runs)
nanosoc-rm test-bump eth-main tidelink v1.0 --queue           # queue it for an executor (the GUI button does this)
nanosoc-rm test-bump eth-main tidelink v1.0 --host <host-id>  # run it here (on an EDA host): Now vs After
```

Exit 0 PASS or WARN (a gate already red stays red), 1 FAIL (a gate went green to red), 2 UNVERIFIED, 3 usage. T0
runs on both sides and is reported as hygiene. A bump proposal opens a pull request only after a passing test-bump
of exactly that move. Tier jobs never run on the service host: an executor claims them (interfaces.md section 26).

## Web GUI on one host (local sign-in, local jobs)

`nanosoc-rm user add <login> --generate`, put the stanza under `[auth] mode = "local"` in the private config, then
`nanosoc-rm serve --host 127.0.0.1` and reach it through an SSH tunnel. Pages, jobs and set-up: `docs/GUI.md`; as a systemd service: `docs/DEPLOY.md`.

## The front page: projects, freshness, catalogue, notifications (CR-WF-gui)

`/` is the projects home: one tree of projects, their configurations and their pins, each row with its freshness
state as a word beside a shape and the days since the pin last moved, and a detail panel on the right. Freshness
pills and Filters (Type, Builds a, Role, No forkable project) narrow it; Timeline (off by default, saved per login)
adds a bar per row from its last bump to today. The inbox is now **Notifications** behind the bell
(`/notifications`; the cards stay at `/inbox`). `/matrix/` opens on the freshness matrix, with the compatibility
matrix as its second tab. `/catalogue/` characterises repos: the new-repo queue from the org scan, the open
questions, every group, and a downloadable `catalogue.yaml` patch (the tool never writes the registry). Pages read
stored facts only. Page map: `docs/GUI.md`; contracts: interfaces.md section 25.

## Submodule-tree freshness (CR-WF-fresh)

How far each pin lags its upstream default branch, and how long since its parent last moved it. Read-only: the last
stored walk plus the mirrors; nothing is re-walked or fetched.

```
nanosoc-rm freshness scan --store              # every registered configuration; the scheduler runs it after each poll
nanosoc-rm freshness show                      # one line per tree: verdict, lagging pins, days since the newest bump
nanosoc-rm freshness show eth-main --tree      # every pin, depth first, with the worst state beneath it
```

Per pin: Outdated (10+ commits behind, or behind and not bumped for 90 days), Behind (1 to 9), Current, Unpushed
(no host holds the commit), Not measured (with the reason). A tree's verdict is its worst direct pin, except that
a tree with a direct pin nobody could measure is never Current: "Current except N not measured" when nothing is
worse; a frozen silicon tree says Frozen and what it would be, and is never counted. Pages: Home (`/`) and the
Matrix's Freshness tab (the older `/freshness/` page is retired and redirects to Home). Details: interfaces.md
section 24.

## Code and documentation quality (CR-14, Q1-Q6)

Read-only reports on any catalogued repo at any ref, or a local checkout; every fix is a patch you review and
apply with `git apply`. No config is needed with `--path`.

```
nanosoc-rm quality density  --path ~/work/my-repo          # comment ratio per language, directory, file; outliers
nanosoc-rm quality comments --path ~/work/my-repo --patch fix.diff   # findings + mechanical edits as a patch
nanosoc-rm quality readme   --path ~/work/my-repo --patch layout.diff  # stale paths, make targets, submodules, flows
nanosoc-rm quality docs     --path ~/work/my-repo          # conciseness score per Markdown file and section
nanosoc-rm quality all tidelink --ref main --store          # a mirrored component; stored per commit for trends
nanosoc-rm quality trend density tidelink                   # one point per stored commit
```

Thresholds: the registry's optional `quality.yaml` (example: `schemas/examples/eligibility.quality.example.yaml`).
Local checkouts the GUI offers: `[quality] paths = { name = "/path/to/checkout" }` in the service config. The
pages live under `/quality/` (overview, per-repo density, findings with a patch download, README check with the
generated layout section, docs ranking, trend). `docs --rewrite` asks Claude for shorter sections only when
`ANTHROPIC_API_KEY` or `[quality] api_key_file` is set (it sends those sections to the Anthropic API); without a
key it makes no network call. Details: `docs/contracts/interfaces.md` section 14a.

## Compatibility matrix (D3; built per CR-15, Phase 2 automation not enabled)

```
nanosoc-rm matrix eth-rc4 --component tidelink --component nanosoc_gen     # the grid from stored results
nanosoc-rm matrix eth-rc4 --component tidelink --run --wait                # queue its cells as local jobs, run them
nanosoc-rm matrix cell eth-rc4 --column head --component nanosoc_gen       # one cell in the foreground
nanosoc-rm matrix quarantine eth-rc4                                       # D8: FLAKY twice in 14 days
nanosoc-rm matrix eth-main --tiers t1 --component tidelink                 # add a "+ t1" (lint) cell per cell
nanosoc-rm matrix cell eth-main --column pinned --tier t1 [--force-tier]   # T0, then tiers.yaml t1, in a checkout
```

Each cell is a local T0 compat run with one component moved to that column's commit: a FAIL re-runs once (FLAKY when
green the second time), host failures are INFRA, a run that read other commits than it asked for is UNVERIFIED,
and every cell is compared with the same night's pinned run. The nightly matrix is not scheduled: it waits for the
Phase 2 entry gate. A t1 cell runs the registry's tiers.yaml t1 command after T0; a host without the tool says
INFRA "tool not on this host" and runs nothing; a pin the command moved in its checkout makes the cell UNVERIFIED;
a tier that declares `ip_files` gets an IP hash. Contract: `docs/contracts/interfaces.md` section 18; pages under
`/matrix/`.

## Versions, change classes and tool releases (Phase 3, built per CR-15)

Read-only: lint a classed changelog, compute the next version, check tag names, propose a manifest, draft a tool
release for a person. Nothing is tagged, pushed or opened; yanks and fixed-in updates are registry patches.

```
nanosoc-rm changelog lint nanosoc_gen          # C2: every entry carries breaking/feature/fix/none
nanosoc-rm version next nanosoc_gen            # C1, C3: next version from the class (consumer regeneration: unmeasured)
nanosoc-rm version tags tidelink               # C10: tag names, grandfathered tags, moved release tags
nanosoc-rm manifest init tidelink --patch m.diff
nanosoc-rm version draft nanosoc_gen           # G1: <data_dir>/drafts/nanosoc_gen/<version>/ (README, patch, release.json)
```

Format, decision table and approval matrix: `docs/versioning.md`. Pages: `/versions/`.

## Regenerating generated RTL to class a change (C3, C7; built per CR-15)

```
nanosoc-rm regen run multicore-master --component nanosoc_gen --commit <sha> --out /tmp/r   # one sandbox render
nanosoc-rm regen class multicore-master --component nanosoc_gen --from <sha> --to <sha> --store   # none/fix/feature/breaking
nanosoc-rm regen pending --run           # measure every move a stored version plan or bump proposal needs
```

A configuration opts in with a `regen` entry in the registry's `tiers.yaml` (command, `output`). The tree is
checked out anonymously in a sandbox with the component moved, the command runs with a clean environment (no
set_env.sh, PYTHONDONTWRITEBYTECODE=1, under nice), and the vendor-library variables are never set: the vendor
renderers are skipped and listed as coverage. `[regen] read_only_roots` refuses any path under a shared library
tree. `version next` and the bump engine read the stored class. Details: interfaces.md section 20.

## Catalogue characterisation (CR-WF-catalogue)

Every catalogue component has a group (project, soc, subsystem, component, toolkit, flow, generator, arch, lib,
infra, legacy); projects, SoCs and subsystems also a type (chip, chiplet, soc, subsystem) and a role
(implementation, forkable, example). A catalogue without these fields still loads: they are derived from `class`.

```
nanosoc-rm catalogue show --gaps            # SoCs that no forkable project wraps (exit 1 when any, 2 unverified)
nanosoc-rm catalogue show --unconfirmed     # entries with a guessed or derived field, and the open question
nanosoc-rm catalogue org-scan --store       # anonymous org/group listing: repos not in the catalogue (repo_seen)
nanosoc-rm catalogue preview choices.json --out cat.patch   # then, in the registry: git apply cat.patch
```

The tool never writes the registry: `preview` turns choices (set, confirm, add, ignore) into a patch and says
whether the patched catalogue would load. Details: interfaces.md section 23.

## Bump proposals, shadow mode (Phase 4 E1-E7, built per CR-15, not enabled)

What the bump bot WOULD do, for a person to do: the tool never pushes, writes a branch or opens a pull request.

```
nanosoc-rm walk eth-main && nanosoc-rm bumps eth-main --store   # releases newer than each pin, deepest first
nanosoc-rm bumps show <id>                                       # changelog, class, defects, matrix cell, blockers
nanosoc-rm bumps patch <id> --out <id>.diff                      # then, in the consumer: git apply --index <id>.diff
nanosoc-rm bumps mark <id> done --by <login>                     # a person applied it
nanosoc-rm bumps ledger                                          # E2: agreement with what people pinned
```

Frozen and silicon configurations are never proposed; generated RTL is not regenerated; the schedule ships
disabled until the Phase 4 gate. Pages under `/bumps/`. Details: `docs/contracts/interfaces.md` section 19.

## Releases, errata documents and forks (local drafts; CR-R1)

Built under CR-15: the Phase 5 gate still governs publishing. The tool never tags, pushes or uploads; every such
step is a command in the draft's CHECKLIST.md for a person.

```
nanosoc-rm release draft eth-rc4 1.0.0 --archive build_soc.tar.gz --gds chip.gds --netlist chip.pnr.v \
    --waiver '*=taped out before the release scheme'      # folder in <data_dir>/releases/eth-rc4/1.0.0/
nanosoc-rm release draft quickstart-main 0.2.0             # no archive: release.lock.draft.yaml lists what is pending
nanosoc-rm release verify eth-rc4/1.0.0 --against <dir holding build_soc/>   # re-check against the mirrors now
nanosoc-rm release list
nanosoc-rm forks --store                                   # public forks of the example projects (anonymous)
```

A draft holds the release lock (every pin with its host, state and protection), ERRATA.md/.html with its
revision (F4), release notes, CHECKLIST.md (tag commands, approvals), GATES.md (the gate ledger to sign) and the
generated-RTL md5 manifest (G7). Exit 0 PASS/WARN/WAIVED, 1 FAIL, 2 UNVERIFIED (gate evidence or a person step
still open), 3 usage. The pages are under `/releases/`; details: `docs/contracts/interfaces.md` section 14c.

## Artefact store index (Wave E; B10, G8)

Read-only: what Artifactory holds, indexed once a night and on demand, shown on `/store/` and the configuration,
version and release pages. Config `[stores.artifactory]` (docs/DEPLOY.md); contract: interfaces.md section 22.

```
nanosoc-rm store index --store     # GET, HEAD and one AQL search; probes each feature; stores the store_index fact
nanosoc-rm store search 1a2b3c4d   # a stored artefact by path, name, run tag, commit or md5 prefix
nanosoc-rm store findings          # mismatches, silicon not archived, shipped streams no configuration covers
```

## Publishing release and tapeout records (CR-ART-publish; G8, D12)

A person's confirmed step, never scheduled: a signed record goes to the release store
(`asic-release/<project>/<version>/`), never over an existing file, then is read back and verified.

```
nanosoc-rm publish-release eth-rc4/1.0.0 --dry-run           # preview: the record, its sha256, every target path
nanosoc-rm publish-release eth-rc4/1.0.0 --confirm <sha256>  # publish exactly what the preview showed
nanosoc-rm register-tapeout <tapeout id> --dry-run           # from <registry>/tapeouts/<id>.tapeout.yaml
```

The GUI's Publish page (`/publish/`) does the same as jobs, for a login listed in `[stores.artifactory]
publishers`. Set-up (token, signing key): `docs/DEPLOY.md`; contract: `docs/contracts/interfaces.md` section 21.

## Development

Python 3.11 or later (the system `python3` may be older; the script below finds `/usr/bin/python3.11`).

```
scripts/dev-env.sh                                     # creates .venv, pip install -e '.[dev]'; idempotent
PATH=$PWD/.venv/bin:$PATH python -m pytest -m "not live" tests
python3 schemas/check_examples.py                      # the contracts' own check
.venv/bin/python -m pyflakes src tests
```

- `-m "not live"` deselects the tests that need the lab (the service VM's config, the registry checkout, the
  network). A live test without its inputs fails; it never skips.
- An acceptance test whose module is not built yet reports XFAIL. When it passes, remove its `@NOT_BUILT`
  marker (strict xfail turns a pass red until you do).
- Package layout: `docs/contracts/interfaces.md` section 0.5. Shared by every module: `config` (the service
  config, read per call), `contracts` (the packaged schemas, `validate()`, `WRITE_CALLS`), `gitrun` (the one git
  runner, which refuses writes), `db` (the store), `cli` (the `nanosoc-rm` group).
- A module adds CLI commands without editing `cli.py`: put `register(main)` in `nanosoc_rm/<module>/commands.py`
  (or in `nanosoc_rm/<module>.py`) and import the helpers from `nanosoc_rm.cliutil`. Exit codes: 0 answered,
  1 finding or FAIL, 2 could not answer, 3 usage or config error.
- Every git call goes through `nanosoc_rm.gitrun.run_git`; no other module starts a subprocess. Any new call
  that could write outside the service's own disk needs a `WRITE_CALLS` entry (`tests/unit/test_write_calls.py`
  fails until it has one).
