"""The local job runner (CR-17; interfaces.md section 13a).

With no `[ci] project` in the service config, Run executes `compat` in the service's own worker instead of
triggering CI. The same worker runs `mirror sync` and `walk` for the Mirrors and Configuration pages.

    submit(config, kind, params, by) -> job     queue a job (params checked first); kinds in KINDS
    cancel(config, job_id, by) -> job           a queued job is cancelled; a running one is stopped
    get(config, job_id), jobs(config, ...)      job records (dicts)
    log_text(config, job_id) -> str             the tail of the job's log, plus its tier log while one runs
    recover(config) -> [job ids]                at start: a job left running is marked INFRA with the reason
    Worker(config_path).start() / .stop() / .step()

One job at a time, oldest first, one worker per data directory (a lock file). Each job is a child process of
this package's own CLI (`nanosoc-rm compat CONFIG --json ...`) in its own session, stderr to
`<data_dir>/jobs/<id>/log.txt`. So the service never imports the gates or the tier runner, a crash in a job
never takes the service down, and a cancel stops the whole process tree: a tier command starts its own process
group, so the worker collects the groups of every descendant (from /proc) while the job runs and stops them all.

Budgets are the CLI's: T0 over 120 s is a warning, a tier is killed at 1.5 x its budget. The worker adds only a
ceiling (JOB_LIMIT_S, plus the tier's kill limit) so nothing runs forever; past it the job is INFRA.

Results. A compat job without pin overrides runs with `--store`: its walk and its compat_result (mode `local`) are
stored exactly as the CLI stores them, so the dashboard and configuration pages show them. A what-if (with
`--pin`) is never a stored fact (section 9a); its compat_result stays in the job record. Submitting a compat job is
logged in `actions` as `run_t0` with the login (H2).

States: queued, running, done (the command answered: its verdict is set), failed (a usage error, exit 3),
cancelled, infra (no answer: a crash, the ceiling, or a service restart while it ran).
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from nanosoc_rm import clock
from nanosoc_rm.config import Config, load_config

__all__ = ["KINDS", "STATES", "FINAL", "JobUsage", "submit", "cancel", "get", "jobs", "log_text", "recover",
           "Worker", "cli_args", "command_text", "JOB_LIMIT_S", "CLI_BOOT", "is_local_ci", "JobKind",
           "KIND_MODULES", "register_kind", "registered", "all_kinds", "job_dir"]

# Job kinds: three built in (KINDS); every other kind is a JobKind that a package registers with register_kind()
# when one of KIND_MODULES is imported. ONE seam for every plug-in kind (Wave C, CR-WC-seams): the matrix cell,
# bumps, quality, release and versioning kinds all register the same way. The worker imports KIND_MODULES once,
# on first use of a kind it does not know, so a fresh worker process knows them all.
KINDS = ("compat", "mirror-sync", "walk")
KIND_MODULES = ("nanosoc_rm.matrix.jobkind", "nanosoc_rm.bumps.jobkind", "nanosoc_rm.quality.jobkinds",
                "nanosoc_rm.releases.jobkinds", "nanosoc_rm.versioning.jobkinds",
                "nanosoc_rm.regen.jobkind", "nanosoc_rm.store.jobkind")
_KINDS_LOADED = False
STATES = ("queued", "running", "done", "failed", "cancelled", "infra")
FINAL = ("done", "failed", "cancelled", "infra")
JOB_LIMIT_S = 1800                    # ceiling for a job's T0 and walk, plus the tier kill limit when a tier runs
LOG_TAIL_BYTES = 64 * 1024
CLI_BOOT = "import sys; sys.argv[0] = 'nanosoc-rm'; from nanosoc_rm.cli import main; main()"
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}$")
_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL,
    by TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, pid INTEGER, exit_code INTEGER,
    verdict TEXT, reason TEXT, result TEXT, cancel_by TEXT, command TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state);
"""


class JobUsage(ValueError):
    """A job request that cannot run: unknown kind, a bad id, a malformed --pin. Nothing is queued."""


@dataclasses.dataclass(frozen=True)
class JobKind:
    """One registered job kind.

    params(p) -> dict     the checked parameters; ValueError (or JobUsage) when the CLI would exit 3
    args(p) -> [str]      the `nanosoc-rm` argv the job runs (`--json` is added when missing)
    outcome(docs, last_line) -> fields   optional: the job's state, verdict, reason and result from the documents
                          the child printed (exit 0, 1 or 2); default: the exit code's verdict and a document count
    limit_s(config, job) -> float        optional: the worker's ceiling for this kind (default JOB_LIMIT_S)
    action                optional: an `actions` entry logged with the login on submit (H2), about the job's
                          `configuration` parameter
    """
    name: str
    params: Callable[[dict], dict]
    args: Callable[[dict], list[str]]
    outcome: Callable[[list[dict], str], dict] | None = None
    limit_s: Callable[[Config, dict], float] | None = None
    action: str | None = None


_REGISTERED: dict[str, JobKind] = {}


def register_kind(name: str, params: Callable[[dict], dict], args: Callable[[dict], list[str]], *,
                  outcome: Callable[[list[dict], str], dict] | None = None,
                  limit_s: Callable[[Config, dict], float] | None = None, action: str | None = None) -> JobKind:
    """Add a job kind. A built-in kind cannot be replaced, nor can another module's kind; the same module
    registering its kind again (a re-import) replaces its own entry."""
    if name in KINDS:
        raise ValueError("job kind %r is built in" % name)
    if not isinstance(name, str) or not _ID.match(name):
        raise ValueError("%r is not a job kind name" % (name,))
    old = _REGISTERED.get(name)
    if old is not None and getattr(old.params, "__module__", None) != getattr(params, "__module__", None):
        raise ValueError("job kind %r is already registered by %s" % (name, old.params.__module__))
    k = JobKind(name, params, args, outcome, limit_s, action)
    _REGISTERED[name] = k
    return k


def _load_kinds() -> None:
    """Import KIND_MODULES once. A module that is not built here is skipped; one that fails to import is an
    error, never hidden."""
    global _KINDS_LOADED
    if _KINDS_LOADED:
        return
    import importlib
    for mod in KIND_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name != mod and not mod.startswith(str(e.name) + "."):
                raise
    _KINDS_LOADED = True


def registered(name: str) -> JobKind | None:
    """The registered kind `name` (None for a built-in or an unknown kind)."""
    if name in KINDS:
        return None
    if name not in _REGISTERED:
        _load_kinds()
    return _REGISTERED.get(name)


def all_kinds() -> tuple[str, ...]:
    _load_kinds()
    return KINDS + tuple(sorted(_REGISTERED))


def is_local_ci(config: Config) -> bool:
    """CR-17: no CI project configured, so Run executes locally."""
    return not (config.get("ci") or {}).get("project")


# ---------------------------------------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------------------------------------
def _db_path(config: Config) -> Path:
    return config.data_dir / "jobs.sqlite"


def job_dir(config: Config, job_id: int) -> Path:
    return config.data_dir / "jobs" / str(int(job_id))


@contextlib.contextmanager
def _connect(config: Config) -> Iterator[sqlite3.Connection]:
    path = _db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_DDL)
        yield con
    finally:
        con.close()


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def _update(config: Config, job_id: int, **fields: Any) -> None:
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"], separators=(",", ":"))
    cols = ", ".join("%s = ?" % k for k in fields)
    with _connect(config) as con:
        con.execute("UPDATE jobs SET %s WHERE id = ?" % cols, (*fields.values(), job_id))


def get(config: Config, job_id: int) -> dict | None:
    if not 0 <= int(job_id) < 2 ** 63:          # beyond SQLite INTEGER: no such job (not an OverflowError)
        return None
    with _connect(config) as con:
        return _row(con.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone())


def jobs(config: Config, *, limit: int = 50, kind: str | None = None, configuration: str | None = None,
         states: Sequence[str] | None = None) -> list[dict]:
    """Job records, newest first."""
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if states:
        where.append("state IN (%s)" % ",".join("?" * len(states)))
        args += list(states)
    with _connect(config) as con:
        rows = [_row(r) for r in con.execute("SELECT * FROM jobs %s ORDER BY id DESC" % (
            ("WHERE " + " AND ".join(where)) if where else ""), args)]
    if configuration:
        rows = [r for r in rows if r["params"].get("configuration") == configuration]
    return rows[:limit]


# ---------------------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------------------
def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise JobUsage("%r is not a %s id" % (str(value)[:60], what))
    return value


def _params(kind: str, params: dict) -> dict:
    """The job's parameters, checked; JobUsage when the CLI would exit 3 on them."""
    k = None if kind in KINDS else registered(kind)
    if k is not None:
        try:
            return k.params(dict(params))
        except ValueError as e:
            raise JobUsage(str(e)) from None
    if kind == "compat":
        from nanosoc_rm.analysis.compat import CompatUsage, parse_pins
        cfg = _check_id(params.get("configuration"), "configuration")
        pins = [p for p in (params.get("pins") or []) if str(p).strip()]
        try:
            parse_pins(pins)
        except CompatUsage as e:
            raise JobUsage(str(e)) from None
        tier = params.get("tier") or None
        if tier not in (None, "t1", "t2"):
            raise JobUsage("tier must be t1 or t2")
        force = bool(params.get("force_tier"))
        if force and not tier:
            raise JobUsage("--force-tier needs a tier")
        return {"configuration": cfg, "pins": [str(p).strip() for p in pins], "tier": tier, "force_tier": force}
    if kind == "mirror-sync":
        comp = params.get("component") or None
        return {"component": _check_id(comp, "component") if comp else None}
    if kind == "walk":
        return {"configuration": _check_id(params.get("configuration"), "configuration")}
    raise JobUsage("unknown job kind %r (known: %s)" % (kind, ", ".join(all_kinds())))


def cli_args(kind: str, params: dict, work: Path | None = None) -> list[str]:
    """The `nanosoc-rm` arguments a job runs (`--keep-work` only when `work` is given)."""
    k = None if kind in KINDS else registered(kind)
    if k is not None:
        args = list(k.args(params))
        return args if "--json" in args else args + ["--json"]
    if kind == "compat":
        args = ["compat", params["configuration"]]
        for p in params["pins"]:
            args += ["--pin", p]
        if not params["pins"]:
            args.append("--store")                 # a real run is a fact; a what-if never is (section 9a)
        if params.get("tier"):
            args += ["--tier", params["tier"]] + (["--force-tier"] if params.get("force_tier") else [])
        if work is not None:
            args += ["--keep-work", str(work)]
        return args + ["--json"]
    if kind == "mirror-sync":
        return ["mirror", "sync"] + (["--component", params["component"]] if params.get("component") else []) + ["--json"]
    if kind == "walk":
        return ["walk", params["configuration"], "--json"]
    raise JobUsage("unknown job kind %r" % kind)


def command_text(kind: str, params: dict) -> str:
    """The command as a person would type it (what the page shows)."""
    return "nanosoc-rm " + shlex.join([a for a in cli_args(kind, params) if a != "--json"])


def submit(config: Config, kind: str, params: dict, by: str) -> dict:
    """Queue a job. JobUsage for a request the CLI would refuse. A compat job logs `run_t0` (H2)."""
    if not by:
        raise JobUsage("a job needs the login that asked for it")
    p = _params(kind, dict(params or {}))
    with _connect(config) as con:
        cur = con.execute("INSERT INTO jobs (kind, params, state, by, created_at, command) VALUES (?, ?, 'queued', ?, ?, ?)",
                          (kind, json.dumps(p, sort_keys=True), by, clock.iso(), command_text(kind, p)))
        job_id = cur.lastrowid
    k = None if kind in KINDS else registered(kind)
    action = "run_t0" if kind == "compat" else (k.action if k is not None else None)
    if action:
        from nanosoc_rm import db
        db.log_action(by, action, p["configuration"], "local job %d" % job_id + (
            "" if kind == "compat" else " (%s)" % command_text(kind, p)), config=config)
    return get(config, job_id)


def cancel(config: Config, job_id: int, by: str) -> dict | None:
    """Cancel a queued job now; ask the worker to stop a running one. A finished job is unchanged."""
    with _connect(config) as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT state FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if r is not None and r["state"] == "queued":
            con.execute("UPDATE jobs SET state = 'cancelled', cancel_by = ?, finished_at = ?, reason = ? WHERE id = ?",
                        (by, clock.iso(), "cancelled by %s before it started" % by, int(job_id)))
        elif r is not None and r["state"] == "running":
            con.execute("UPDATE jobs SET cancel_by = ? WHERE id = ?", (by, int(job_id)))
        con.execute("COMMIT")
    return get(config, job_id)


def queue_position(config: Config, job_id: int) -> int | None:
    """1 for the next job to run; None when the job is not queued."""
    with _connect(config) as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM jobs WHERE state = 'queued' ORDER BY id")]
    return ids.index(int(job_id)) + 1 if int(job_id) in ids else None


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return ("[... %d bytes not shown]\n" % (size - limit) + text.split("\n", 1)[-1]) if size > limit else text


def log_text(config: Config, job_id: int) -> str:
    """The job's log tail, then the tier command's log tail (live from the work directory while the job runs,
    from the copy the worker keeps once it has finished)."""
    d = job_dir(config, job_id)
    text = _tail(d / "log.txt")
    logs = sorted((d / "work").glob("tier-*.log")) if (d / "work").is_dir() else sorted(d.glob("tier-*.log"))
    for tier_log in logs:
        text += "\n--- %s (tier command output) ---\n%s" % (tier_log.name, _tail(tier_log))
    return text


# ---------------------------------------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------------------------------------
def _children() -> dict[int, list[int]]:
    """{ppid: [pid]} of every process this user can see (Linux /proc)."""
    out: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        rest = stat[stat.rfind(")") + 2:].split()
        if len(rest) > 1:
            out.setdefault(int(rest[1]), []).append(int(entry))
    return out


def _tree(pid: int) -> list[int]:
    """pid and its descendants."""
    kids, out, todo = _children(), [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def _groups(pids: Sequence[int]) -> set[int]:
    own = os.getpgrp()
    out = set()
    for p in pids:
        try:
            g = os.getpgid(p)
        except OSError:
            continue
        if g != own and g > 1:
            out.add(g)
    return out


def _signal_groups(groups: set[int], sig: int) -> None:
    for g in groups:
        try:
            os.killpg(g, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _group_alive(g: int) -> bool:
    try:
        os.killpg(g, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_groups(groups: set[int], grace_s: float = 5.0) -> None:
    """SIGTERM every group, then SIGKILL what is left after `grace_s`."""
    _signal_groups(groups, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(_group_alive(g) for g in groups):
        time.sleep(0.1)
    _signal_groups({g for g in groups if _group_alive(g)}, signal.SIGKILL)


def _is_job_process(pid: int, job_id: int) -> bool:
    """Is `pid` still this job's process? The worker starts every job with NANOSOC_RM_JOB=<id> in its
    environment, so a recycled pid is never mistaken for it."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            return ("NANOSOC_RM_JOB=%d" % job_id).encode() in fh.read().split(b"\0")
    except OSError:
        return False


def recover(config: Config) -> list[int]:
    """Service start: every job still marked running belongs to a service that is gone. Its process (if still
    alive) is stopped and the job becomes INFRA with the reason. Returns the job ids."""
    out = []
    for job in jobs(config, limit=10 ** 6, states=("running",)):
        pid = job.get("pid")
        alive = bool(pid) and _is_job_process(pid, job["id"])
        if alive:
            _stop_groups(_groups(_tree(pid)) | {pid})
        reason = ("the service restarted while this job ran; %s, so the job has no result. Run it again."
                  % ("its process (pid %d) was stopped" % pid if alive else "its process had already ended"))
        _update(config, job["id"], state="infra", verdict="INFRA", reason=reason, finished_at=clock.iso())
        _append_log(config, job["id"], "\n[worker] %s\n" % reason)
        out.append(job["id"])
    return out


def _append_log(config: Config, job_id: int, text: str) -> None:
    d = job_dir(config, job_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "log.txt", "a", encoding="utf-8") as fh:
        fh.write(text)


def _limit_s(config: Config, job: dict) -> float:
    k = None if job["kind"] in KINDS else registered(job["kind"])
    if k is not None and k.limit_s is not None:
        return float(k.limit_s(config, job))
    limit = float(JOB_LIMIT_S)
    tier = job["params"].get("tier")
    if job["kind"] == "compat" and tier:
        try:
            from nanosoc_rm.ci import configs
            spec = configs.tiers(job["params"]["configuration"], config).get(tier) or {}
            limit += float(spec.get("budget_s") or 0) * 1.5
        except Exception:                                         # noqa: BLE001 - unreadable: the CLI will say why
            limit += 4 * 3600
    return limit


def _summary(kind: str, docs: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    for d in docs:
        kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
    out: dict[str, Any] = {"documents": len(docs), "kinds": kinds}
    if kind == "mirror-sync":
        out["failed"] = sorted(d["subject"] for d in docs if d.get("kind") == "mirror_status" and not d["fact"]["ok"])
    return out


def _outcome(job: dict, rc: int | None, stdout: str, log_tail: str) -> dict:
    """The job's final fields from the child's exit code and output (exit codes: interfaces.md section 0.2)."""
    last = (log_tail.strip().splitlines() or [""])[-1][:500]
    docs = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                docs.append(json.loads(line))
            except ValueError:
                pass
    if rc == 3:
        return {"state": "failed", "verdict": None, "reason": last or "usage error (exit 3)"}
    if rc not in (0, 1, 2):
        return {"state": "infra", "verdict": "INFRA",
                "reason": "the job process ended with %s%s" % ("signal %d" % -rc if rc is not None and rc < 0
                                                               else "exit %s" % rc, ": " + last if last else "")}
    k = None if job["kind"] in KINDS else registered(job["kind"])
    if k is not None and k.outcome is not None:
        return k.outcome(docs, last)
    if job["kind"] == "compat":
        doc = next((d for d in reversed(docs) if d.get("kind") == "compat_result"), None)
        if doc is None:
            return {"state": "done", "verdict": "UNVERIFIED", "reason": last or "compat printed no result"}
        f = doc["fact"]
        return {"state": "done", "verdict": f.get("run_verdict") or f["verdict"], "reason": f.get("reason"),
                "result": doc}
    verdict = {0: "PASS", 1: "FAIL", 2: "UNVERIFIED"}[rc]
    return {"state": "done", "verdict": verdict, "reason": None if rc == 0 else last or None,
            "result": _summary(job["kind"], docs)}


class Worker:
    """Runs queued jobs one at a time (module docstring). `argv_prefix` is the test seam: the command that stands
    for `nanosoc-rm` (default: this Python running the package's CLI)."""

    def __init__(self, config_path: str | os.PathLike | None = None, *, argv_prefix: Sequence[str] | None = None,
                 poll_s: float = 1.0, log: Callable[[str], None] | None = None):
        self.config_path = None if config_path is None else os.fspath(config_path)
        self.argv_prefix = list(argv_prefix) if argv_prefix else [sys.executable, "-c", CLI_BOOT]
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        self._log = log or (lambda m: print("nanosoc-rm worker: %s" % m, file=sys.stderr))

    def config(self) -> Config:
        return load_config(self.config_path)

    # -- lifecycle --------------------------------------------------------------------------------------------
    def acquire(self, config: Config) -> bool:
        """The one-worker-per-data-dir lock. False when another worker holds it."""
        if self._lock_fd is not None:
            return True
        config.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(config.data_dir / "jobs.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def start(self) -> "Worker":
        config = self.config()
        if not self.acquire(config):
            self._log("another worker holds %s: this process runs no jobs" % (config.data_dir / "jobs.lock"))
            return self
        for job_id in recover(config):
            self._log("job %d was running when the service stopped: marked INFRA" % job_id)
        self._thread = threading.Thread(target=self._loop, name="rm-jobs", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 20.0) -> None:
        """Stop the loop; a running job is stopped and marked INFRA ("the service stopped"). Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.step()
            except Exception as e:                                # noqa: BLE001 - the next job still runs
                self._log("a job step failed: %s: %s" % (type(e).__name__, e))
                ran = None
            if ran is None:
                self._stop.wait(self.poll_s)

    # -- one job ----------------------------------------------------------------------------------------------
    def _claim(self, config: Config) -> dict | None:
        with _connect(config) as con:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT id FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
            if r is not None:
                con.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?", (clock.iso(), r["id"]))
            con.execute("COMMIT")
        return get(config, r["id"]) if r is not None else None

    def step(self) -> int | None:
        """Run the oldest queued job to its end. Returns its id, or None when nothing was queued."""
        config = self.config()
        job = self._claim(config)
        if job is None:
            return None
        try:
            fields = self._run(config, job)
        except Exception as e:                                    # noqa: BLE001 - recorded on the job, never lost
            fields = {"state": "infra", "verdict": "INFRA", "reason": "the worker could not run the job: %s: %s"
                      % (type(e).__name__, str(e)[:300])}
        fields["finished_at"] = clock.iso()
        _update(config, job["id"], **fields)
        _append_log(config, job["id"], "[worker] %s %s at %s%s\n" % (
            fields["state"], fields.get("verdict") or "", fields["finished_at"],
            ": " + fields["reason"] if fields.get("reason") else ""))
        return job["id"]

    def _run(self, config: Config, job: dict) -> dict:
        d = job_dir(config, job["id"])
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        # A tier job keeps its work directory while it runs, so the page can show the tier log as it grows; the
        # worker removes it afterwards (a tier checkout can be a gigabyte) and keeps only the tier log.
        work = d / "work" if job["kind"] == "compat" and job["params"].get("tier") else None
        argv = self.argv_prefix + cli_args(job["kind"], job["params"], work)
        env = dict(os.environ, NANOSOC_RM_CONFIG=str(config.path), PYTHONUNBUFFERED="1",
                   NANOSOC_RM_JOB=str(job["id"]))
        limit = _limit_s(config, job)
        _append_log(config, job["id"], "[worker] job %d (%s) for %s, started %s\n$ %s\n" % (
            job["id"], job["kind"], job["by"], job["started_at"], job["command"]))
        t0 = time.monotonic()
        stopped: dict | None = None
        groups: set[int] = set()
        with open(d / "stdout.jsonl", "wb") as out, open(d / "log.txt", "ab") as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=str(d),
                                    start_new_session=True)
            _update(config, job["id"], pid=proc.pid)
            groups.add(proc.pid)
            last_scan = 0.0
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if now - last_scan >= 2.0:
                    groups |= _groups(_tree(proc.pid))
                    last_scan = now
                current = get(config, job["id"]) or {}
                if current.get("cancel_by"):
                    stopped = {"state": "cancelled", "verdict": None,
                               "reason": "cancelled by %s after %.0f s" % (current["cancel_by"], now - t0)}
                elif now - t0 > limit:
                    stopped = {"state": "infra", "verdict": "INFRA",
                               "reason": "stopped at the worker's ceiling of %.0f s" % limit}
                elif self._stop.is_set():
                    stopped = {"state": "infra", "verdict": "INFRA",
                               "reason": "the service stopped while this job ran. Run it again."}
                if stopped is not None:
                    groups |= _groups(_tree(proc.pid))
                    _stop_groups(groups)
                    rc = proc.wait()
                    break
        _signal_groups({g for g in groups if _group_alive(g)}, signal.SIGKILL)    # nothing outlives the job
        if work is not None and work.is_dir():
            for tier_log in work.glob("tier-*.log"):
                shutil.copyfile(tier_log, d / tier_log.name)
            shutil.rmtree(work, ignore_errors=True)
        if stopped is not None:
            return dict(stopped, exit_code=rc)
        fields = _outcome(job, rc, (d / "stdout.jsonl").read_text(encoding="utf-8", errors="replace"),
                          _tail(d / "log.txt", 4096))
        fields["exit_code"] = rc
        return fields

