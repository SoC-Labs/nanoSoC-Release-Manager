"""The regeneration command of a configuration and the host settings that bound it (interfaces.md section 20).

    spec_for(configuration, config) -> dict          the registry tiers.yaml `regen` entry, checked (RegenUsage)
    settings(config) -> Settings                     the service config's optional [regen] table
    check(spec, configuration, settings) -> dict     the refusals, before any work starts

The spec is a `tier_command` (CR-12) plus `output`, the tree-relative directory the command writes (build_soc), and
optional `include`/`exclude` globs over that directory. It runs exactly as a T1/T2 command does (clean
environment, PATH=/usr/bin:/bin, HOME and TMPDIR in the sandbox, nothing inherited), with these additions:

- PYTHONDONTWRITEBYTECODE=1 is always set: a generator never leaves .pyc files in a tree it reads.
- The vendor-library variables (VENDOR_ENV: ARM_IP_LIBRARY_PATH, SOCLABS_DMA250_DIR) are refused in `env`. With
  them unset the generator skips the vendor-rendered parts (the bus matrices, the DMA-250) and says so in its log;
  the measurement records those lines under `coverage`. A vendor renderer runs with its working directory inside
  the vendor library and writes there, so the tool never points it at one.
- Any path under a `[regen] read_only_roots` entry (the lab's shared IP library, say), in the command, the env or
  the work directory, is refused: nothing the tool runs may be handed a path in a read-only tree.
- The command runs under `nice -n <[regen] nice>` (default 15): a regeneration is background work.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm.analysis import tierrun

__all__ = ["RegenUsage", "Settings", "settings", "spec_for", "check", "VENDOR_ENV", "DEFAULT_NICE", "DEFAULT_EXCLUDE"]

VENDOR_ENV = ("ARM_IP_LIBRARY_PATH", "SOCLABS_DMA250_DIR")
DEFAULT_NICE = 15
DEFAULT_EXCLUDE = ("**/*.log", "**/logs/**", "**/__pycache__/**", "**/*.pyc")
_REL = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$)).+\Z")


class RegenUsage(ValueError):
    """A regeneration the tool will not run as asked (no spec, a vendor variable, a read-only path). Exit 3."""
    exit_code = 3


@dataclass
class Settings:
    work_dir: Path
    read_only_roots: list[Path] = field(default_factory=list)
    nice: int = DEFAULT_NICE

    def under_read_only(self, path: str | Path) -> Path | None:
        """The read-only root `path` lies under (resolved, symlinks followed), or None."""
        p = Path(os.path.realpath(str(path)))
        for r in self.read_only_roots:
            rr = Path(os.path.realpath(str(r)))
            if p == rr or rr in p.parents:
                return r
        return None


def settings(config) -> Settings:
    """[regen] work_dir (default <data_dir>/regen), read_only_roots, nice."""
    t = (config or {}).get("regen") or {}
    work = Path(t["work_dir"]) if t.get("work_dir") else Path(config.data_dir) / "regen"
    nice = t.get("nice", DEFAULT_NICE)
    return Settings(work_dir=work, read_only_roots=[Path(x) for x in t.get("read_only_roots") or []],
                    nice=int(nice))


def spec_for(configuration: str, config) -> dict:
    """The configuration's `regen` entry in the registry's tiers.yaml. RegenUsage when there is none."""
    from nanosoc_rm.ci import configs
    spec = configs.tiers(configuration, config).get("regen")
    if not spec:
        raise RegenUsage("the registry's tiers.yaml declares no regen command for %s" % configuration)
    return dict(spec)


def _paths_in(text: str) -> list[str]:
    return re.findall(r"(/[^\s:;'\"=]+)", text or "")


def check(spec: dict, configuration: str, st: Settings) -> dict:
    """The spec with the tool's additions, or RegenUsage. Run before any fetch or checkout."""
    out = dict(spec)
    cmd = out.get("command")
    parts = [cmd] if isinstance(cmd, str) else list(cmd or [])
    if not parts or any(not isinstance(p, str) or not p for p in parts):
        raise RegenUsage("%s regen: command must be a non-empty string or argv list" % configuration)
    if any(tierrun.SOURCES_SET_ENV.search(p) for p in parts):
        raise RegenUsage("%s regen: the command sources a set_env.sh; a regeneration never does (declare `env`)"
                         % configuration)
    env = dict(out.get("env") or {})
    vendor = sorted(k for k in env if k in VENDOR_ENV)
    if vendor:
        raise RegenUsage("%s regen: env may not set %s: a vendor renderer writes inside the vendor library, so the "
                         "tool never points one at it (the vendor-rendered parts are recorded as not regenerated)"
                         % (configuration, ", ".join(vendor)))
    bad = sorted(k for k in env if k in tierrun.RESERVED_ENV)
    if bad:
        raise RegenUsage("%s regen: env may not set %s (the tool sets it)" % (configuration, ", ".join(bad)))
    output = out.get("output")
    if not isinstance(output, str) or not _REL.match(output):
        raise RegenUsage("%s regen: output must be a tree-relative directory (no absolute path, no ..)"
                         % configuration)
    for text in parts + ["%s=%s" % kv for kv in env.items()]:
        for p in _paths_in(text):
            root = st.under_read_only(p)
            if root is not None:
                raise RegenUsage("%s regen: %r names a path under the read-only root %s" % (configuration, text[:80],
                                                                                           root))
    root = st.under_read_only(st.work_dir)
    if root is not None:
        raise RegenUsage("[regen] work_dir %s is under the read-only root %s" % (st.work_dir, root))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out["env"] = env
    out["regenerates"] = True
    if st.nice:
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        out["command"] = ["nice", "-n", str(st.nice)] + argv
    out["declared_command"] = tierrun.command_text(cmd)
    return out
