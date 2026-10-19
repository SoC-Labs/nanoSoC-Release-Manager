"""The pull agent's own config (interfaces.md section 27.4): a TOML file on the EDA host, `$NANOSOC_RM_AGENT_CONFIG`.

It is NOT the service config: the agent host holds no service secret, no store and no registry checkout.

    [agent]
    name = "eda-1"                         # the [[executor.agents]] name on the service
    server = "https://<host>:8771"         # the release manager; http only for 127.0.0.1/localhost
    token_file = "~/.config/nanosoc-rm-executor/token"   # 0600; never inside a sandbox bind
    ca_file = "/path/ca.pem"               # optional: the service's CA (self-signed TLS)
    mirror_root = "/srv/nanosoc-mirrors"   # read-only replica of <data_dir>/mirrors (<entry>.git)
    scratch_dir = "/scratch/nanosoc-exec"  # one work dir per task, removed after the upload
    labels = ["verilator", "vcs"]          # what this agent asks for (the service grants at most its own list)
    poll_seconds = 30
    heartbeat_seconds = 30
    max_budget_s = 28800                   # a task asking more is not run (INFRA)
    max_log_bytes = 8388608                # output past this kills the run (FAIL: output limit)
    max_upload_bytes = 2097152             # how much of the log is uploaded (the head and the tail)
    max_file_bytes = 17179869184           # RLIMIT_FSIZE for the command
    keep_work = false                      # keep the task's work dir (debugging)
    rm_config = "/srv/nanosoc-rm-agent/rm.toml"   # optional: a nanosoc-rm config ON THIS HOST for tier JOBS
                                           # (section 27.6): anonymous only, no credentials; add "jobs" to labels
    site_host = "eda-1"                    # optional: the registry tiers.yaml host id ($NANOSOC_RM_EXECUTOR_HOST)

    [sandbox]
    mode = "auto"                          # auto | bwrap | fallback; auto = bwrap when usable, else fallback
    network = true                         # licence servers need it; false adds --unshare-net
    ro_binds = ["/eda", "/opt/tools"]      # tool trees, read-only inside the sandbox
    path = ["/eda/synopsys/vcs/bin"]       # prepended to PATH=/usr/bin:/bin inside
    [sandbox.env]                          # site values (licence servers, tool homes); the task's env wins
    LM_LICENSE_FILE = "27000@<licence-host>"
"""
from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm.config import ConfigError

__all__ = ["AgentConfig", "load_agent_config", "read_token", "ENV_VAR", "MODES"]

ENV_VAR = "NANOSOC_RM_AGENT_CONFIG"
MODES = ("auto", "bwrap", "fallback")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}\Z")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}\Z")
_LOCAL = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?(/|\Z)")
_AGENT_KEYS = {"name": str, "server": str, "token_file": str, "ca_file": str, "mirror_root": str, "scratch_dir": str,
               "labels": list, "poll_seconds": int, "heartbeat_seconds": int, "max_budget_s": int,
               "max_log_bytes": int, "max_upload_bytes": int, "max_file_bytes": int, "keep_work": bool,
               "rm_config": str, "site_host": str}
_SANDBOX_KEYS = {"mode": str, "network": bool, "ro_binds": list, "path": list, "env": dict}


@dataclass
class AgentConfig:
    path: Path
    name: str
    server: str
    token_file: Path
    mirror_root: Path
    scratch_dir: Path
    ca_file: Path | None = None
    labels: list[str] = field(default_factory=list)
    poll_seconds: int = 30
    heartbeat_seconds: int = 30
    max_budget_s: int = 8 * 3600
    max_log_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 2 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 ** 3
    keep_work: bool = False
    rm_config: Path | None = None
    site_host: str | None = None
    mode: str = "auto"
    network: bool = True
    ro_binds: list[Path] = field(default_factory=list)
    path_prepend: list[str] = field(default_factory=list)
    site_env: dict[str, str] = field(default_factory=dict)


def _abs(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def _check(where: str, table, keys: dict, problems: list[str]) -> dict:
    if not isinstance(table, dict):
        problems.append("[%s] must be a table" % where)
        return {}
    for k, v in table.items():
        if k not in keys:
            problems.append("[%s] unknown key %r" % (where, k))
        elif not isinstance(v, keys[k]) or (keys[k] is int and isinstance(v, bool)):
            problems.append("[%s] %s must be %s" % (where, k, keys[k].__name__))
    return table


_POSITIVE = ("poll_seconds", "heartbeat_seconds", "max_budget_s", "max_log_bytes", "max_upload_bytes",
             "max_file_bytes")


def _agent_problems(a: dict) -> list[str]:
    problems = []
    for k in ("name", "server", "token_file", "mirror_root", "scratch_dir"):
        if not isinstance(a.get(k), str) or not a.get(k):
            problems.append("[agent] %s is required" % k)
    if isinstance(a.get("site_host"), str) and not _NAME.match(a["site_host"]):
        problems.append("[agent] site_host must be a host id (lower case letters, digits, _ . -)")
    if isinstance(a.get("name"), str) and a["name"] and not _NAME.match(a["name"]):
        problems.append("[agent] name must be lower case letters, digits, _ . -")
    server = a.get("server")
    if isinstance(server, str) and server and not (server.startswith("https://") or _LOCAL.match(server)):
        problems.append("[agent] server must be https:// (http only for 127.0.0.1 or localhost)")
    if isinstance(server, str) and "@" in server.split("//", 1)[-1].split("/", 1)[0]:
        problems.append("[agent] server must carry no user or token (token_file is the only credential)")
    if isinstance(a.get("labels"), list) and not all(_label_ok(x) for x in a["labels"]):
        problems.append("[agent] labels must be label names")
    for k in _POSITIVE:
        if isinstance(a.get(k), int) and a[k] < 1:
            problems.append("[agent] %s must be >= 1" % k)
    return problems


def _label_ok(x) -> bool:
    return isinstance(x, str) and bool(_NAME.match(x) or (x.startswith("jobs:") and _NAME.match(x[5:])))


def _sandbox_problems(s: dict, mode) -> list[str]:
    problems = []
    if mode not in MODES:
        problems.append("[sandbox] mode must be one of %s" % ", ".join(MODES))
    for k in ("ro_binds", "path"):
        if isinstance(s.get(k), list) and not all(isinstance(x, str) and x.startswith("/") for x in s[k]):
            problems.append("[sandbox] %s must be absolute paths" % k)
    env = s.get("env", {})
    if isinstance(env, dict):
        for k, v in env.items():
            if not _ENV_NAME.match(k) or not isinstance(v, str):
                problems.append("[sandbox.env] %s must be an UPPER_CASE name with a string value" % k[:40])
    return problems


def load_agent_config(path: str | os.PathLike | None = None) -> AgentConfig:
    if path is None:
        path = os.environ.get(ENV_VAR)
        if not path:
            raise ConfigError("no agent config: set %s or pass --agent-config" % ENV_VAR)
    p = Path(path).expanduser()
    try:
        doc = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError("%s: cannot read the agent config (%s)" % (p, e.strerror)) from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError("%s: not valid TOML: %s" % (p, e)) from None
    problems: list[str] = []
    for k in doc:
        if k not in ("agent", "sandbox"):
            problems.append("unknown section [%s]" % k)
    a = _check("agent", doc.get("agent", {}), _AGENT_KEYS, problems)
    s = _check("sandbox", doc.get("sandbox", {}), _SANDBOX_KEYS, problems)
    problems += _agent_problems(a)
    mode = s.get("mode", "auto")
    problems += _sandbox_problems(s, mode)
    env = s.get("env", {})
    if problems:
        raise ConfigError("%s: invalid agent config:\n  %s" % (p, "\n  ".join(problems)))
    base = p.resolve().parent
    cfg = AgentConfig(path=p, name=a["name"], server=a["server"].rstrip("/"),
                      token_file=_abs(base, a["token_file"]), mirror_root=_abs(base, a["mirror_root"]),
                      scratch_dir=_abs(base, a["scratch_dir"]),
                      ca_file=_abs(base, a["ca_file"]) if a.get("ca_file") else None,
                      rm_config=_abs(base, a["rm_config"]) if a.get("rm_config") else None,
                      site_host=a.get("site_host") or None,
                      labels=list(a.get("labels", [])), mode=mode, network=bool(s.get("network", True)),
                      ro_binds=[Path(x) for x in s.get("ro_binds", [])], path_prepend=list(s.get("path", [])),
                      site_env=dict(env))
    for k in _POSITIVE + ("keep_work",):
        if k in a:
            setattr(cfg, k, a[k])
    return cfg


def read_token(cfg: AgentConfig) -> str:
    """The executor token. Refused when the file is readable by group or others, or is not the agent user's."""
    try:
        st = os.stat(cfg.token_file)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError("%s: the executor token file must be mode 0600 (chmod 600)" % cfg.path)
        if st.st_uid != os.getuid():
            raise ConfigError("%s: the executor token file is not owned by this user" % cfg.path)
        value = Path(cfg.token_file).read_text(encoding="ascii").strip()
    except OSError as e:
        raise ConfigError("%s: the executor token file is unreadable (%s)" % (cfg.path, e.strerror)) from None
    except UnicodeDecodeError:
        raise ConfigError("%s: the executor token file is not a token" % cfg.path) from None
    from nanosoc_rm.executor.tokens import TOKEN_RE
    if not TOKEN_RE.match(value):
        raise ConfigError("%s: the executor token file does not hold an rmx_ token" % cfg.path)
    return value
