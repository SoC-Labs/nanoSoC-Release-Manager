"""A stand-in for `nanosoc-rm` in the job runner tests (jobs.Worker(argv_prefix=[python, this file])).

FAKE_MODE (environment): ok (print the contract's example compat_result, exit 0), slow (print a line, then
sleep; also start a grandchild in its own session that sleeps, writing its pid to FAKE_PIDFILE), usage (exit 3
with a message), crash (exit 9), docs (print two mirror_status-like lines, exit 2), tierfail (the example's tier line with T0 PASS
and run_verdict FAIL, exit 1), silent (exit 0, prints nothing), fail (exit 1 with a message).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[3] / "schemas" / "examples" / "observation.compat-result.example.jsonl"
mode = os.environ.get("FAKE_MODE", "ok")
print("fake nanosoc-rm %s" % " ".join(sys.argv[1:]), file=sys.stderr, flush=True)
if "--keep-work" in sys.argv:                 # a tier run: its command's log lands in the work directory
    work = Path(sys.argv[sys.argv.index("--keep-work") + 1])
    work.mkdir(parents=True)
    tier = sys.argv[sys.argv.index("--tier") + 1]
    (work / ("tier-%s.log" % tier)).write_text("tier output line\n")
if mode == "ok":
    line = next(ln for ln in EXAMPLE.read_text().splitlines() if ln.strip().startswith("{"))
    print(json.dumps(json.loads(line)))
    sys.exit(0)
if mode == "tierfail":
    line = [ln for ln in EXAMPLE.read_text().splitlines() if ln.strip().startswith("{")][1]
    doc = json.loads(line)
    doc["fact"]["verdict"], doc["fact"]["run_verdict"] = "PASS", "FAIL"
    print(json.dumps(doc))
    sys.exit(1)
if mode == "silent":
    sys.exit(0)
if mode == "fail":
    print("nanosoc-rm: 2 steps failed", file=sys.stderr)
    sys.exit(1)
if mode == "slow":
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    Path(os.environ["FAKE_PIDFILE"]).write_text(str(kid.pid))
    print("working...", file=sys.stderr, flush=True)
    time.sleep(300)
    sys.exit(0)
if mode == "usage":
    print("nanosoc-rm: no registered configuration 'nope'", file=sys.stderr)
    sys.exit(3)
if mode == "docs":
    for ok in (True, False):
        print(json.dumps({"kind": "mirror_status", "subject": "mirror_status:github:x/%s" % ok, "fact": {"ok": ok}}))
    sys.exit(2)
sys.exit(9)
