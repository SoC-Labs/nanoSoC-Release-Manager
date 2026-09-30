# nanoSoC Release Manager

Monitors the nanoSoC repos, tests SoC references against submodule versions, and manages errata and releases.

- Requirements: `docs/REQUIREMENTS.md` (v1.0, frozen 2026-09-30); change requests in `docs/CHANGES.md`.
- Gate 2 (contracts and acceptance tests) is ready for approval: start at `docs/GATE2.md`.
- Check the contracts: `python3 schemas/check_examples.py`. Run the tests: `python3 -m pytest tests/acceptance`
  (dependencies in `requirements-dev.txt`; off the service VM add `-m "not live"`).
