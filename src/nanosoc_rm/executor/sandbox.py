"""Run one command sandboxed and unprivileged, with limits (interfaces.md section 27.4 step 3).

    mode_for(cfg) -> "bwrap" | "fallback"         what this agent will use (ConfigError: mode=bwrap, not usable)
    bwrap_usable(bwrap=BWRAP) -> bool             a real probe: unshare-all and run /bin/true
    exposure(cfg, work) -> list[str]              protected paths a bind would expose (must be empty to run)
    clean_env(cfg, run, tree, work, tier, nonce) -> dict
    argv_for(mode, cfg, command, tree, work, cwd) -> list[str]
    run(argv, env, cwd, log, limits, tick=None) -> Outcome

bwrap (preferred): a new user, pid, ipc, uts and cgroup namespace (`--unshare-all`; `--share-net` unless
`network = false`), an EMPTY root: only /usr and /etc (read-only), /proc, /dev, a tmpfs /tmp, the configured tool
trees (read-only), the mirror root (read-only, for the checkouts' alternates) and the task's work dir (read-write:
the tree, HOME and TMPDIR live there). The agent user's home is not mounted, so ~/.ssh, ~/.config and the executor
token are not there at all. `--die-with-parent` and `--new-session` (no TIOCSTI into the agent's terminal).

fallback (no usable bwrap): the same clean environment (env is built from nothing: no SSH_AUTH_SOCK, no token,
HOME an empty dir in the work dir, PATH /usr/bin:/bin plus the configured tool dirs, ssh with no identity), its own
session and process group, umask 077, RLIMIT_CORE 0 and RLIMIT_FSIZE. RESIDUAL RISK: the command still runs as the
agent user and can read whatever that user can read, the token file included. Run the agent as a dedicated user
with no ssh keys and nothing else in its home (docs/DEPLOY.md, "Remote tier executor").

Both modes: stdin closed; stdout and stderr go through the agent, which keeps at most `max_log_bytes` and kills
the run past it; the whole process group is killed at `budget_s * KILL_FACTOR`, when the service asks it to stop,
or when the agent loses its lease; nothing of the run outlives it.

Every exec of this module is the subprocess.Popen in `run()` and the fixed probe in `bwrap_usable()` (WRITE_CALLS
`local`: this file imports no HTTP client).
"""
from __future__ import annotations

import os
import pwd
import resource
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Callable, Mapping

from nanosoc_rm.config import ConfigError
from nanosoc_rm.executor.agentconfig import AgentConfig

__all__ = ["BWRAP", "KILL_FACTOR", "Outcome", "Limits", "mode_for", "bwrap_usable", "exposure", "clean_env",
           "argv_for", "run", "real_home"]

BWRAP = "/usr/bin/bwrap"
KILL_FACTOR = 1.5
_GRACE_S = 10
_SSH = ("ssh -F /dev/null -o BatchMode=yes -o IdentityAgent=none -o IdentitiesOnly=yes -o IdentityFile=/dev/null")


@dataclass
class Limits:
    kill_after_s: float
    max_log_bytes: int
    max_file_bytes: int


@dataclass
class Outcome:
    returncode: int | None
    seconds: float
    killed: bool = False
    stop_reason: str | None = None          # why the agent stopped it (timeout, output limit, cancel, lease)
    log_bytes: int = 0
    truncated: bool = False
    started: bool = True
    start_error: str | None = None


def bwrap_usable(bwrap: str = BWRAP) -> bool:
    if not os.access(bwrap, os.X_OK):
        return False
    try:
        p = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-all",
                            "--die-with-parent", "/bin/true"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=20, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def mode_for(cfg: AgentConfig, probe: Callable[[], bool] = bwrap_usable) -> str:
    if cfg.mode == "fallback":
        return "fallback"
    if probe():
        return "bwrap"
    if cfg.mode == "bwrap":
        raise ConfigError("%s: [sandbox] mode = \"bwrap\" but %s cannot create a sandbox on this host"
                          % (cfg.path, BWRAP))
    return "fallback"


def real_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return Path(os.environ.get("HOME", "/nonexistent"))


def _within(p: Path, d: Path) -> bool:
    p, d = Path(os.path.realpath(p)), Path(os.path.realpath(d))
    return p == d or d in p.parents


def exposure(cfg: AgentConfig, work: Path, home: Path | None = None, extra=()) -> list[str]:
    """The protected paths (the token file, the agent user's ~/.ssh and ~/.config, the home itself) that a sandbox
    bind would put inside the sandbox. A bind INSIDE the home (a scratch dir there) is fine; a bind that contains
    one of these is not, in either mode (in fallback the same layout keeps the tree away from them)."""
    home = home or real_home()
    protected = [cfg.token_file, home / ".ssh", home / ".config", home / ".gnupg", home]
    binds = [Path(work), cfg.mirror_root] + list(cfg.ro_binds) + [Path(x) for x in extra]
    out = []
    for b in binds:
        for p in protected:
            if _within(p, b):
                out.append("%s would be visible inside the sandbox through %s" % (p, b))
    return sorted(set(out))


def clean_env(cfg: AgentConfig, run: Mapping, tree: Path, work: Path, tier: str, nonce: str) -> dict[str, str]:
    """The command's whole environment, built from nothing (never os.environ)."""
    home, tmp = work / "home", work / "tmp"
    for d in (home, tmp):
        d.mkdir(parents=True, exist_ok=True)
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = ""
    env = {"PATH": ":".join(list(cfg.path_prepend) + ["/usr/bin", "/bin"]), "HOME": str(home), "TMPDIR": str(tmp),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TREE": str(tree), "NANOSOC_RM_TIER": tier,
           "NANOSOC_RM_NONCE": nonce, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_SSH_COMMAND": _SSH}
    if user:
        env.update(USER=user, LOGNAME=user)
    verdicts = (run.get("verdict") or {}).get("verdicts")
    if verdicts:
        env["NANOSOC_RM_VERDICTS"] = str(tree / verdicts)
    env.update(cfg.site_env)
    for k, v in (run.get("env") or {}).items():
        try:
            env[k] = Template(str(v)).substitute(env)
        except (KeyError, ValueError) as e:
            raise ValueError("env %s=%r names %s, which is not set before it" % (k, v, e)) from None
    return env


def _system_binds() -> list[str]:
    out = ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(d):
            out += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            out += ["--ro-bind", d, d]
    return out


def argv_for(mode: str, cfg: AgentConfig, command, tree: Path, work: Path, cwd: Path, *, ro=(), rw=()) -> list[str]:
    """The argv that runs `command` in `mode`. `ro`/`rw`: extra read-only / writable binds (a job's nanosoc-rm
    install, config and data dir, section 27.6); each must pass exposure() like the rest."""
    cmd = ["/bin/bash", "--noprofile", "--norc", "-c", command] if isinstance(command, str) else list(command)
    if mode == "fallback":
        return cmd
    if mode != "bwrap":
        raise ValueError(mode)
    argv = [BWRAP, "--die-with-parent", "--new-session", "--unshare-all"]
    if cfg.network:
        argv.append("--share-net")
    argv += _system_binds()
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for b in cfg.ro_binds:
        argv += ["--ro-bind", str(b), str(b)]          # the agent checked each exists (else INFRA, not run)
    argv += ["--ro-bind", str(cfg.mirror_root), str(cfg.mirror_root)]
    for b in ro:
        argv += ["--ro-bind", str(b), str(b)]
    for b in rw:
        argv += ["--bind", str(b), str(b)]
    argv += ["--bind", str(work), str(work), "--chdir", str(cwd)]
    return argv + cmd


def _limits(max_file_bytes: int) -> Callable[[], None]:
    def pre() -> None:
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    return pre


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(argv: list[str], env: Mapping[str, str], cwd: Path, log: Path, limits: Limits,
        tick: Callable[[], str | None] | None = None, tick_every_s: float = 30.0,
        stdout_to: Path | None = None) -> Outcome:
    """Run `argv` to completion or until stopped. `tick()` is called about every `tick_every_s` (the heartbeat);
    a non-None answer is a reason to stop the run now. stdout and stderr both go to `log`, unless `stdout_to` is
    given (a job: its stdout is the JSON documents), which then gets stdout under the same byte limit."""
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if stdout_to is not None else subprocess.STDOUT,
                                start_new_session=True, preexec_fn=_limits(limits.max_file_bytes), close_fds=True)
    except OSError as e:
        return Outcome(None, 0.0, started=False, start_error=e.strerror or str(e))
    state = {"bytes": 0, "over": False}

    def pump(stream, path: Path, count: bool) -> None:
        kept = 0
        with open(path, "wb") as out:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    break
                room = limits.max_log_bytes - kept
                if room > 0:
                    out.write(chunk[:room])
                    kept += min(len(chunk), room)
                    if count:
                        state["bytes"] = kept
                if len(chunk) > room and not state["over"]:
                    state["over"] = True
                    _killpg(proc.pid, signal.SIGKILL)
    if stdout_to is None:
        readers = [threading.Thread(target=pump, args=(proc.stdout, log, True), daemon=True)]
    else:
        readers = [threading.Thread(target=pump, args=(proc.stderr, log, True), daemon=True),
                   threading.Thread(target=pump, args=(proc.stdout, stdout_to, False), daemon=True)]
    for r in readers:
        r.start()
    stop, next_tick = None, time.monotonic() + tick_every_s
    while True:
        try:
            proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if state["over"]:
            stop = stop or "output passed the %d-byte limit" % limits.max_log_bytes
        elif now - t0 > limits.kill_after_s:
            stop = "killed after %.1f s: past the %g s limit" % (now - t0, limits.kill_after_s)
        elif tick is not None and now >= next_tick:
            next_tick = now + tick_every_s
            stop = tick()
        if stop:
            _killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=_GRACE_S)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            break
    _killpg(proc.pid, signal.SIGKILL)                  # nothing of the run outlives it
    for r in readers:
        r.join(timeout=30)
    if state["over"] and not stop:
        stop = "output passed the %d-byte limit" % limits.max_log_bytes
    return Outcome(None if stop else proc.returncode, round(time.monotonic() - t0, 3), killed=bool(stop),
                   stop_reason=stop, log_bytes=state["bytes"], truncated=state["over"])
