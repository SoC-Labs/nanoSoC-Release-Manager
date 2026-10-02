"""Analysis over the mirrors and the stored trees (interfaces.md sections 5, 6, 6a and 7).

    hosts.compare_hosts(component)          B6  host_compare, one per copy, counted by rev-list in one mirror
    contains.contains(commit, ...)          B4  containment, one per (configuration, host)
    protect.silicon_protection(config)      B5  silicon_protection, pinned host and canonical host
    detect.run(defect, detector, output)    F2  one DetectorRun (erratum exposure.run); never run by the service

Every function returns contract documents (observations validated by nanosoc_rm.contracts) and the CLI
(`nanosoc-rm hosts | contains | silicon-protection | detect`, analysis/commands.py) is a front end over it.

What this package reads, and nothing else (`_inputs.py` is the one place that touches each input):
- the service config (`load_config`, per call);
- the registry (catalogue, eligibility, defects) through `nanosoc_rm.registry.load_registry`, or the raw files
  while that module is not built;
- the latest stored pin_state snapshot of each configuration (`db.latest`): it never re-walks (section 4);
- the component mirrors `<data_dir>/mirrors/<id>.git`, refs `refs/r/<host>/<quote(path)>/{heads,tags}/*`
  (section 3), read through the one git runner.
"""
from nanosoc_rm.analysis._inputs import AnalysisError, Answer, CannotAnswer, UsageProblem

__all__ = ["AnalysisError", "Answer", "CannotAnswer", "UsageProblem"]
