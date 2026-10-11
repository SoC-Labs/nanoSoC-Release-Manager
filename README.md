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
```

Exit 0 PASS, WAIVED or WARN; 1 FAIL; 2 UNVERIFIED or INFRA; 3 usage (unknown configuration, bad `--pin`, no pin
at that tree path). `t0.runner.hardened` is always NOT-RUN in a local run: a laptop is not the hardened runner.
`--keep-work DIR` keeps the work directory; `--store` records a run without overrides.

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
