"""The regenerator (interfaces.md section 20; C3 3/3 and C7, Phase 3, built per CR-15).

    render(configuration, component, commit, *, config) -> Render         one sandbox regeneration (sandbox.py)
    measure_class(configuration, component, frm, to, *, config, store) -> regen_class obs  (measure.measure)
    stored_signals(component, frm, to, *, config) -> (consumers, build) signals | None         the C3 classifier
    SandboxRegenerator(config)                                            versioning.classify.Regenerator
    pending(config) -> [move]                                             what plans and bumps still need

A configuration opts in with a `regen` entry in the registry's tiers.yaml (eligibility#/$defs/regen_command).
Nothing here is scheduled; long work runs as the `regen-class` local job or on the command line.
"""
from nanosoc_rm.regen.measure import measure as measure_class
from nanosoc_rm.regen.measure import pending, stored, stored_signals
from nanosoc_rm.regen.sandbox import Render, RegenerationError, SandboxRegenerator, render, tree_paths
from nanosoc_rm.regen.spec import RegenUsage

__all__ = ["measure_class", "pending", "stored", "stored_signals", "Render", "RegenerationError", "SandboxRegenerator",
           "render", "tree_paths", "RegenUsage"]
