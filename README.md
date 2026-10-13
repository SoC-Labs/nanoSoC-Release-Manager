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

## Web GUI on one host (local sign-in, local jobs)

`nanosoc-rm user add <login> --generate`, put the stanza under `[auth] mode = "local"` in the private config, then
`nanosoc-rm serve --host 127.0.0.1` and reach it through an SSH tunnel. Pages, jobs and set-up: `docs/GUI.md`.

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
