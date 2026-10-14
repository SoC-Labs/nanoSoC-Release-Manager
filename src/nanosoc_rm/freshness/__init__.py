"""Submodule-tree freshness (interfaces.md section 23; CR-WF-fresh, David 2026-10-14).

How far each stored pin lags its upstream default branch, when its parent last moved it, and a tree verdict, for
the projects home page and the freshness grid.

    freshness.scan.scan(...)     the measurement (git reads of the mirrors over the stored walks); a job
    freshness.rules              the rule, pure: judge_pin, judge_tree, age_word
    freshness.view               what pages call: stored facts only, re-judged on the day

The submodules are imported by name (`from nanosoc_rm.freshness import scan, view`); this package re-exports
nothing, so `scan` is always the module.
"""
