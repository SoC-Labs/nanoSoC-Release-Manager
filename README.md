# nanoSoC Release Manager

A web tool and command line that watch the nanoSoC repositories and say what needs doing: whether a SoC's
submodules still fit together, which pins lag their upstream, which branches can be deleted, whether taped-out
silicon is still protected, and where comments and READMEs need tidying. It manages errata, release drafts and
signed release records. It only reads repositories: it never pushes, tags or deletes, and when something should
change it gives a person the command or the patch.

## Documentation

| If you want to | Read |
|---|---|
| use it for the first time | [Getting started](docs/getting-started.md) (day one, 20 minutes) |
| do a task: check compatibility, bump, delete branches, release; what each page is for | [User guide](docs/user-guide.md) |
| know what a word or a verdict means | [Concepts](docs/concepts.md) (the glossary) |
| look up a command | [CLI reference](docs/cli.md) (generated from `--help`) |
| install or operate the service | [Admin guide](docs/DEPLOY.md) |
| see what changed | [CHANGELOG.md](CHANGELOG.md) |
| change the tool | [Development](#development), then the [contracts](docs/contracts/) |

The project record: [requirements](docs/REQUIREMENTS.md) (v1.0, frozen 2026-09-30), [change requests](docs/CHANGES.md),
[version policy](docs/versioning.md), [phase gates](docs/phase-gates.yaml), the [gate 2 review](docs/GATE2.md),
[acceptance specs](docs/acceptance/) and [history](docs/archive/history.md) (finished plans, old upgrade notes, [interface rationale](docs/archive/interfaces-history.md)).

## Quick start (command line)

```
scripts/dev-env.sh && export PATH=$PWD/.venv/bin:$PATH
export NANOSOC_RM_CONFIG=/path/to/config.toml      # template: docs/user-guide.md section 1
nanosoc-rm mirror sync --component nanosoc-ethernet-chiplet
nanosoc-rm compat eth-main                         # is this SoC's tree compatible?
nanosoc-rm branches tidelink --all --delete-list   # which branches can go?
```

## Development

Python 3.11 or later (the system `python3` may be older; the script finds `/usr/bin/python3.11`).

```
scripts/dev-env.sh                                     # creates .venv, pip install -e '.[dev]'; idempotent
.venv/bin/python -m pytest -m "not live" -n 6 tests            # about 8 minutes; the venv bin/ goes on PATH itself
python3 schemas/check_examples.py                      # the contracts' own check
.venv/bin/python -m pyflakes src tests scripts
.venv/bin/python scripts/gen_cli_docs.py               # after changing any command's help
```

- `-m "not live"` deselects the tests that need the lab (the service config, the registry checkout, the network).
  An acceptance test whose module is not built yet reports XFAIL; remove its `@NOT_BUILT` marker when it passes.
- Package layout: [docs/contracts/interfaces.md](docs/contracts/interfaces.md) section 0, item 5. Shared by every
  module: `config`, `contracts` (schemas, `validate()`, `WRITE_CALLS`), `gitrun` (the one git runner, which
  refuses writes), `db` (the store), `cli` (the `nanosoc-rm` group).
- A module adds CLI commands without editing `cli.py`: put `register(main)` in `nanosoc_rm/<module>/commands.py`
  and import helpers from `nanosoc_rm.cliutil`. Then regenerate `docs/cli.md`; `tests/unit/test_cli_docs.py`
  fails until you do.
- Every git call goes through `nanosoc_rm.gitrun.run_git`. Any other process start (the executor's sandbox,
  the tier runner) is listed in `WRITE_CALLS` with its guard. A new call that
  could write outside the service's own disk needs a `WRITE_CALLS` entry (`tests/unit/test_write_calls.py`).
- Commits name the requirement ids they implement in a `Requirements:` trailer; contract changes get a row in
  `docs/CHANGES.md`; user-visible changes get an entry in `CHANGELOG.md` under a class.
