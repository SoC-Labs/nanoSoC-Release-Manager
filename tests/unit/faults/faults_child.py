"""A stand-in for `nanosoc-rm` that fails in the ways a real job process can (jobs.Worker(argv_prefix=[python, this
file])). FAULT_MODE (environment):

    sigkill       print a progress line, then kill itself with SIGKILL (the OOM killer, a `kill -9`)
    pass_then_2   print a compat_result whose verdict and run_verdict are PASS, then exit 2 (could not answer)
    pass_then_1   the same document, then exit 1 (a finding)
    half_line     print half a compat_result line (a write cut short: disk full), exit 0
    ok            print the PASS compat_result, exit 0
"""
import json
import os
import signal
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[3] / "schemas" / "examples" / "observation.compat-result.example.jsonl"
mode = os.environ.get("FAULT_MODE", "ok")
print("faults child %s: %s" % (mode, " ".join(sys.argv[1:])), file=sys.stderr, flush=True)


def passing_doc() -> dict:
    doc = json.loads(next(ln for ln in EXAMPLE.read_text().splitlines() if ln.strip().startswith("{")))
    doc["fact"]["verdict"] = "PASS"
    doc["fact"]["run_verdict"] = "PASS"
    return doc


if mode == "sigkill":
    print("working...", file=sys.stderr, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
if mode in ("pass_then_2", "pass_then_1", "ok"):
    print(json.dumps(passing_doc()), flush=True)
    sys.exit({"pass_then_2": 2, "pass_then_1": 1, "ok": 0}[mode])
if mode == "half_line":
    line = json.dumps(passing_doc())
    sys.stdout.write(line[: len(line) // 2])
    sys.stdout.flush()
    sys.exit(0)
sys.exit(9)
