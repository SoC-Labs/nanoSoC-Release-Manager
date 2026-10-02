"""T0: the resolve tier (interfaces.md section 9; D1, D2, D6, D11, F3).

- `gates`: the gate table, negative controls and roll-up, shared by the runner and the service;
- `tiers`: the tier table and budgets;
- `runner`: the CI job (`nanosoc-rm t0 run`). It runs in the compat-ci job only; the service never imports it,
  so nothing here imports it either.
"""
