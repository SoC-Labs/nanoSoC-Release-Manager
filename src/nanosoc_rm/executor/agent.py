"""The pull agent: claim one task, run it, report it (interfaces.md section 27.4).

    run_once(cfg, client, *, mode=None) -> dict | None      the posted result, or None when nothing was queued
    run_loop(cfg, client, *, mode=None, stop=None, sleep=time.sleep) -> int   tasks run until `stop()` is true
    execute(cfg, claim, mode, tick=None) -> (result, log_bytes)   one claimed task, no network (the core)
    judge(run, tier, nonce, outcome, log, tree, control=None) -> (verdict, reason, rows)

One task:
  1. Check the task again (task.check_task): a task that breaks the contract is not run (INFRA, with the problems).
     A budget over `max_budget_s`, or a protected path a bind would expose (sandbox.exposure), is INFRA too.
  2. Lay the commit and its pins out from the read-only mirror into `<scratch_dir>/task-<id>/tree`. A missing
     commit is INFRA ("sync the agent's mirror"), never FAIL.
  3. Remove a declared verdicts file the checkout carries, then run the command in the sandbox, heartbeating; the
     service's cancel, a lost lease, the time limit (budget x 1.5) and the output limit stop it.
  4. Judge it: `tier.<tier>` PASS when the exit code is the declared one (default 0); a declared `forbid` string in
     the output adds a FAIL row `tier.<tier>.forbid` (exit 0 alone is not trusted); a declared verdicts file adds
     its rows (missing = UNVERIFIED); a declared control runs next and must exit non-zero (`tier.<tier>.negctl`
     PASS = it fired); a control that did not fire turns a PASS into UNVERIFIED.
  5. Upload the log (its head and tail, at most `max_upload_bytes`), then the result. A lost lease (409) drops the
     result: another claim owns the task now. The work dir is removed unless `keep_work`.

A tier JOB (section 27.6, `type: job`; only when `[agent] rm_config` is set and "jobs" is among the labels) runs
`nanosoc-rm <argv>` with that config (NANOSOC_RM_CONFIG) in the same sandbox, its nanosoc-rm install and config
read-only and its data dir writable; stdout (the JSON documents) and the log go back with the exit code, and the
job queue reads them exactly as the service's own worker would (`jobs.finish_for_executor`).

The agent prints one line per task to stderr (task id, subject, verdict); never the token, never the environment.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from nanosoc_rm import clock
from nanosoc_rm.analysis import tierrun
from nanosoc_rm.executor import checkout, sandbox
from nanosoc_rm.executor import task as T
from nanosoc_rm.executor.agentconfig import AgentConfig
from nanosoc_rm.executor.client import ApiError, ExecutorClient, LeaseGone

__all__ = ["run_once", "run_loop", "run_job", "execute", "judge", "upload_bytes", "asked_labels", "LOG_TAIL"]

LOG_TAIL = 20


def _row(gate: str, status: str, detail: str, nonce: str) -> dict:
    return {"utc": clock.iso(), "status": status, "gate": gate, "detail": tierrun.G.clean_detail(detail)[:1000],
            "nonce": nonce}


def _tail(log: Path) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [tierrun.G.clean_detail(x)[:300] for x in lines[-LOG_TAIL:]]


def upload_bytes(log: Path, limit: int) -> bytes:
    """The whole log when it fits, else its head and tail with a marker between."""
    try:
        data = log.read_bytes()
    except OSError:
        return b""
    if len(data) <= limit:
        return data
    marker = b"\n[... nanosoc-rm executor: %d bytes cut ...]\n" % (len(data) - limit)
    half = max(0, (limit - len(marker)) // 2)
    return data[:half] + marker + data[len(data) - half:]


def _forbidden(log: Path, forbid: list[str]) -> list[str]:
    if not forbid:
        return []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return list(forbid)
    return [f for f in forbid if f in text]


def judge(run: dict, tier: str, nonce: str, outcome: sandbox.Outcome, log: Path, tree: Path,
          control: sandbox.Outcome | None = None) -> tuple[str, str | None, list[dict]]:
    want = int((run.get("verdict") or {}).get("exit", 0))
    head = "tier.%s" % tier
    if not outcome.started:
        rows = [_row(head, "FAIL", "did not start: %s" % outcome.start_error, nonce)]
    elif outcome.killed:
        rows = [_row(head, "FAIL", outcome.stop_reason or "killed", nonce)]
    else:
        rc = outcome.returncode
        rows = [_row(head, "PASS" if rc == want else "FAIL",
                     "exit %s in %.1f s (want %d)" % (rc, outcome.seconds, want), nonce)]
    forbid = list((run.get("verdict") or {}).get("forbid") or [])
    if forbid and outcome.started:
        hit = _forbidden(log, forbid)
        rows.append(_row(head + ".forbid", "FAIL" if hit else "PASS",
                         ("the output carries %s: exit %d alone is not trusted" % (", ".join(map(repr, hit)), want))
                         if hit else "none of %d forbidden marker(s) in the output" % len(forbid), nonce))
    vfile = (run.get("verdict") or {}).get("verdicts")
    if vfile and outcome.started:
        rows += tierrun.parse_verdicts(tree / vfile, tier, nonce)
    verdict = tierrun.tier_verdict(rows)
    if control is not None:
        fired = control.started and not control.killed and control.returncode not in (None, 0)
        rows.append(_row(head + ".negctl", "PASS" if fired else "FAIL",
                         "control exited %s: the gate can fail" % control.returncode if fired else
                         "the negative control did not fail (%s): this gate cannot tell a broken input"
                         % (control.stop_reason or control.start_error or "exit %s" % control.returncode), nonce))
        if not fired and verdict == "PASS":
            verdict = "UNVERIFIED"
    reason = None
    if verdict != "PASS":
        bad = [r for r in rows if r["status"] not in ("PASS", "WARN", "SKIP")]
        reason = "; ".join("%s: %s" % (r["gate"], r["detail"]) for r in bad)[:1000] or None
    return verdict, reason, rows


def _doc(cfg: AgentConfig, claim: dict, mode: str, started: str, **kw) -> dict:
    d = {"schema": 1, "task_id": claim["task_id"], "nonce": claim["nonce"], "executor": cfg.name, "sandbox": mode,
         "started_at": started, "finished_at": clock.iso(), "verdict": "INFRA", "reason": None, "exit_code": None,
         "killed": False, "seconds": None, "over_budget": False, "rows": [],
         "log": {"bytes": 0, "sha256": None, "truncated": False, "uploaded": False}, "log_tail": [],
         "checkout": {"laid_out": [], "missing": []}}
    d.update(kw)
    if d["verdict"] == "INFRA" and not d["rows"]:
        tier = (((claim.get("task") or {}).get("run") or {}).get("tier")) if isinstance(claim.get("task"), dict) \
            else None
        tier = tier if tier in ("t1", "t2", "t3", "t4", "t5", "t6") else "t1"
        d["rows"] = [_row("tier.%s" % tier, "INFRA", d["reason"] or "not run", claim["nonce"])]
    d["reason"] = d["reason"][:1000] if d["reason"] else d["reason"]
    return d


def execute(cfg: AgentConfig, claim: dict, mode: str, tick: Callable[[], str | None] | None = None,
            work: Path | None = None) -> tuple[dict, Path | None]:
    """Run one claimed task in its own work dir. (result document, the kept log or None)."""
    started = clock.iso()
    task, nonce = claim.get("task"), claim["nonce"]
    probs = T.check_task(task)
    if probs:
        return _doc(cfg, claim, mode, started, reason="the agent refused the task: " + "; ".join(probs)), None
    run, tier = task["run"], task["run"]["tier"]
    budget = int(run["budget_s"])
    if budget > cfg.max_budget_s:
        return _doc(cfg, claim, mode, started, reason="budget %d s is over this agent's max_budget_s %d s: not run"
                    % (budget, cfg.max_budget_s)), None
    work = Path(work) if work is not None else Path(cfg.scratch_dir) / ("task-%d" % claim["task_id"])
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    exposed = sandbox.exposure(cfg, work)
    if exposed:
        return _doc(cfg, claim, mode, started, reason="not run: " + "; ".join(exposed)), None
    absent = [str(b) for b in cfg.ro_binds if not Path(b).exists()]
    if absent:
        return _doc(cfg, claim, mode, started, reason="tool tree(s) not on this host: %s; not run"
                    % ", ".join(absent)), None
    tree = work / "tree"
    layout = checkout.lay_out(cfg.mirror_root, task, tree)
    co = {"laid_out": layout.laid_out, "missing": layout.missing}
    if layout.missing:
        return _doc(cfg, claim, mode, started, checkout=co,
                    reason="the checkout lacks %s: not run" % "; ".join(layout.missing)), None
    cwd = (tree / (run.get("cwd") or ".")).resolve()
    if not cwd.is_dir() or not (cwd == tree.resolve() or tree.resolve() in cwd.parents):
        return _doc(cfg, claim, mode, started, checkout=co, reason="cwd %s is not a directory of the tree"
                    % run.get("cwd")), None
    stop = tick() if tick is not None else None        # the checkout may have taken a while: still ours?
    if stop:
        return _doc(cfg, claim, mode, started, checkout=co, reason="not run: %s" % stop), None
    vfile = (run.get("verdict") or {}).get("verdicts")
    if vfile and ((tree / vfile).is_file() or (tree / vfile).is_symlink()):
        (tree / vfile).unlink()             # a committed or stale file is never this run's evidence
    try:
        env = sandbox.clean_env(cfg, run, tree, work, tier, nonce)
    except ValueError as e:
        return _doc(cfg, claim, mode, started, checkout=co, reason="not run: %s" % e), None
    limits = sandbox.Limits(kill_after_s=budget * sandbox.KILL_FACTOR, max_log_bytes=cfg.max_log_bytes,
                            max_file_bytes=cfg.max_file_bytes)
    log = work / ("tier-%s.log" % tier)
    out = sandbox.run(sandbox.argv_for(mode, cfg, run["command"], tree, work, cwd), env, cwd, log, limits,
                      tick=tick, tick_every_s=cfg.heartbeat_seconds)
    control = None
    if run.get("control") and out.started and not out.killed:
        clog = work / ("control-%s.log" % tier)
        control = sandbox.run(sandbox.argv_for(mode, cfg, run["control"], tree, work, cwd), env, cwd, clog, limits,
                              tick=tick, tick_every_s=cfg.heartbeat_seconds)
    verdict, reason, rows = judge(run, tier, nonce, out, log, tree, control)
    try:
        sha = hashlib.sha256(log.read_bytes()).hexdigest()
    except OSError:
        sha = None
    doc = _doc(cfg, claim, mode, started, verdict=verdict, reason=reason, rows=rows, checkout=co,
               exit_code=out.returncode, killed=out.killed, seconds=out.seconds, over_budget=out.seconds > budget,
               log={"bytes": out.log_bytes, "sha256": sha, "truncated": out.truncated, "uploaded": False},
               log_tail=_tail(log))
    return doc, log


def _say(msg: str) -> None:
    print("nanosoc-rm executor: %s" % msg, file=sys.stderr, flush=True)


def asked_labels(cfg: AgentConfig) -> list[str]:
    """What the agent asks for: its labels, without `jobs` when it has no nanosoc-rm config to run a job with."""
    from nanosoc_rm.executor.jobbridge import JOB_LABEL
    return [x for x in cfg.labels if x != JOB_LABEL or cfg.rm_config is not None]


def _job_binds(cfg: AgentConfig) -> tuple[list[Path], list[Path], str | None]:
    """(read-only, writable, problem) for a job: the Python and nanosoc-rm install and the config's directory and
    registry checkout read-only, its data dir writable."""
    import nanosoc_rm
    from nanosoc_rm.config import ConfigError, load_config
    try:
        rm = load_config(cfg.rm_config)
    except ConfigError as e:
        return [], [], "the agent's rm_config does not load: %s" % str(e)[:300]
    ro = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(nanosoc_rm.__file__).resolve().parents[1],
          Path(cfg.rm_config).resolve().parent}
    reg = (rm.get("registry") or {}).get("path")
    if reg:
        ro.add(Path(reg).resolve())
    ro = sorted(p for p in ro if not any(p != q and q in p.parents for q in ro) and str(p) != "/usr")
    return [p for p in ro if not str(p).startswith("/usr/")], [Path(rm.data_dir).resolve()], None


def run_job(cfg: AgentConfig, client: ExecutorClient, claim: dict, mode: str) -> dict | None:
    """One tier job: `nanosoc-rm <argv>` in the sandbox; its exit code, stdout and log go back to the job queue."""
    from nanosoc_rm.jobs import CLI_BOOT
    jid, lease = claim["job_id"], claim["lease"]
    _say("job %d claimed: %s (%s sandbox)" % (jid, claim.get("kind"), mode))
    work = Path(cfg.scratch_dir) / ("job-%d" % jid)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    lost = {"gone": False}

    def tick() -> str | None:
        try:
            hb = client.job_heartbeat(jid, lease)
        except LeaseGone:
            lost["gone"] = True
            return "the service no longer leases this job to this agent"
        except ApiError:
            return None
        return "cancelled by the service" if hb.get("cancel") else None
    try:
        problem = None if cfg.rm_config is not None else "this agent has no rm_config: it cannot run a job"
        ro, rw = [], []
        if problem is None:
            ro, rw, problem = _job_binds(cfg)
        if problem is None:
            exposed = sandbox.exposure(cfg, work, extra=ro + rw)
            problem = ("not run: " + "; ".join(exposed)) if exposed else None
        if problem is None:
            absent = [str(b) for b in list(cfg.ro_binds) + rw if not Path(b).exists()]
            problem = ("not run: not on this host: %s" % ", ".join(absent)) if absent else None
        rc, stdout, log_text = None, "", ""
        if problem is not None:
            log_text = "[executor %s] %s\n" % (cfg.name, problem)
        else:
            env = sandbox.clean_env(cfg, {}, work, work, "job", "0" * 32)
            for k in ("TREE", "NANOSOC_RM_TIER", "NANOSOC_RM_NONCE"):
                env.pop(k, None)
            env["NANOSOC_RM_CONFIG"] = str(cfg.rm_config)
            if cfg.site_host:
                env["NANOSOC_RM_EXECUTOR_HOST"] = cfg.site_host
            argv = [sys.executable, "-c", CLI_BOOT] + list(claim["argv"])
            limits = sandbox.Limits(kill_after_s=cfg.max_budget_s, max_log_bytes=cfg.max_log_bytes,
                                    max_file_bytes=cfg.max_file_bytes)
            out = sandbox.run(sandbox.argv_for(mode, cfg, argv, work, work, work, ro=ro, rw=rw), env, work,
                              work / "job.log", limits, tick=tick, tick_every_s=cfg.heartbeat_seconds,
                              stdout_to=work / "stdout.jsonl")
            rc = out.returncode
            stdout = (work / "stdout.jsonl").read_text(encoding="utf-8", errors="replace") \
                if (work / "stdout.jsonl").is_file() else ""
            log_text = upload_bytes(work / "job.log", cfg.max_upload_bytes).decode("utf-8", "replace")
            if out.stop_reason or not out.started:
                log_text += "\n[executor %s] %s\n" % (cfg.name, out.stop_reason or out.start_error)
        if lost["gone"]:
            _say("job %d: lease lost; result dropped" % jid)
            return None
        try:
            done = client.finish_job(jid, lease, rc, stdout, log_text)
        except LeaseGone:
            _say("job %d: lease lost; result dropped" % jid)
            return None
        _say("job %d: %s %s" % (jid, done.get("state"), done.get("verdict") or ""))
        return {"job_id": jid, "exit_code": rc, **done}
    finally:
        if not cfg.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def run_once(cfg: AgentConfig, client: ExecutorClient, *, mode: str | None = None) -> dict | None:
    mode = mode or sandbox.mode_for(cfg)
    claim = client.claim(asked_labels(cfg))
    if claim is None:
        return None
    if claim.get("type") == "job":
        return run_job(cfg, client, claim, mode)
    tid, lease = claim["task_id"], claim["lease"]
    subj = (claim.get("task") or {}).get("subject") or {}
    _say("task %d claimed: %s %s (%s sandbox)" % (tid, subj.get("component"), subj.get("capability"), mode))
    lost = {"gone": False}

    def tick() -> str | None:
        try:
            hb = client.heartbeat(tid, lease)
        except LeaseGone:
            lost["gone"] = True
            return "the service no longer leases this task to this agent"
        except ApiError:
            return None                      # a missed heartbeat; the lease has room for more
        return "cancelled by the service" if hb.get("cancel") else None

    work = Path(cfg.scratch_dir) / ("task-%d" % tid)
    try:
        result, log = execute(cfg, claim, mode, tick=tick, work=work)
        if lost["gone"]:
            _say("task %d: lease lost; result dropped" % tid)
            return None
        if log is not None:
            try:
                client.upload_log(tid, lease, upload_bytes(log, cfg.max_upload_bytes))
                result["log"]["uploaded"] = True
            except LeaseGone:
                _say("task %d: lease lost; result dropped" % tid)
                return None
            except ApiError as e:
                _say("task %d: log not uploaded (%s)" % (tid, e))
        result["finished_at"] = max(result["finished_at"], clock.iso())
        try:
            client.post_result(tid, lease, result)
        except LeaseGone:
            _say("task %d: lease lost; result dropped" % tid)
            return None
        _say("task %d: %s" % (tid, result["verdict"]))
        return result
    finally:
        if not cfg.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def run_loop(cfg: AgentConfig, client: ExecutorClient, *, mode: str | None = None,
             stop: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Claim and run until `stop()` (or SIGTERM through the CLI). An unreachable service is retried after
    `poll_seconds`; a refused token (401) ends the loop: a revoked agent stops."""
    mode = mode or sandbox.mode_for(cfg)
    n = 0
    while not (stop and stop()):
        try:
            done = run_once(cfg, client, mode=mode)
        except ApiError as e:
            if e.status in (401, 403, 404):
                _say("stopping: %s" % e)
                raise
            _say("service not reachable (%s); retrying in %d s" % (e, cfg.poll_seconds))
            done = None
        if done is not None:
            n += 1
            continue
        sleep(cfg.poll_seconds)
    return n
