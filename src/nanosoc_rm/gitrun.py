"""THE one git runner (interfaces.md sections 0.4, 3, 17.5; A2, A6, PR8).

Every git call of the package goes through `run_git`. It:

1. **Refuses writes before git starts.** `check_argv` allows only read subcommands and the local forms the
   service needs in its own repos (init, fetch, clone, checkout, add and commit in a throwaway scan repo, local
   config). It refuses `push`, `send-pack` and `receive-pack` (the contract's guard), tag and branch creation or
   deletion, every `remote` change, `config` writes to remotes, URL rewrites, credentials, aliases or hooks,
   reflog expiry, `update-ref`, `submodule foreach`, `--upload-pack`/`--receive-pack`/`--exec`, `--exec-path`,
   `ext::`/`fd::` transports, a URL carrying a password, and any subcommand it does not know. A refusal raises
   GitRefused (a PermissionError).
2. **Never prompts.** `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, no askpass helper, ssh `BatchMode=yes`,
   stdin closed. A credential git would have asked for is a failed call, not a hang.
3. **Records the exact argv.** `GitResult.command` is the argv as one shell-quoted string, and
   `GitResult.source()` is the observation `source` ({kind: git, command}) for it (A3). Credentials must never
   be in an argv (interfaces.md section 0.3): a URL with a password is refused, and tokens reach git through a
   credential helper that reads them from the environment, which is not recorded.

Environment (interfaces.md section 3): the service's git calls inherit the process environment, so tests can
route fetches with `GIT_CONFIG_GLOBAL` and `url.insteadOf`. `isolate=True` (the T0 runner) adds
`GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`. Inherited `GIT_DIR`-style variables are dropped so a
call acts only on the repo its argv names (`-C`, `--git-dir`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = ["GitRefused", "GitError", "GitResult", "check_argv", "check_credentialed", "hardened_env", "run_git",
           "GIT_WRITES", "DEFAULT_TIMEOUT_S", "CREDENTIALED_MARKER", "CREDENTIALED_READS"]

GIT_WRITES = ("push", "send-pack", "receive-pack")        # interfaces.md section 17.5: the guard's minimum
DEFAULT_TIMEOUT_S = 900                                   # the T0 kill limit; callers pass less where they can
# An environment carrying this variable holds a private-read credential (mirror/private_read.py). With
# it, run_git allows only these subcommands, on top of every other refusal.
CREDENTIALED_MARKER = "NANOSOC_RM_PRIVATE_READ"
CREDENTIALED_READS = ("fetch", "ls-remote")


class GitRefused(PermissionError):
    """run_git refused an argv: it could write outside the service's own repos, or it is not on the allowlist."""


class GitError(RuntimeError):
    """A git call that ran and failed (GitResult.check())."""

    def __init__(self, result: "GitResult"):
        self.result = result
        tail = (result.stderr or "").strip().splitlines()[-3:]
        super().__init__("%s: exit %s%s" % (result.command, result.returncode, (": " + " / ".join(tail)) if tail else ""))


@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]           # exactly what ran, starting with "git"
    returncode: int                 # -1 when the call timed out
    stdout: str | bytes
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def command(self) -> str:
        """The exact argv, shell-quoted: what an observation's sources[].command records (A3)."""
        return shlex.join(self.argv)

    def source(self) -> dict:
        """The observation source for this call (defs.schema.json#/$defs/source)."""
        return {"kind": "git", "command": self.command}

    def check(self) -> "GitResult":
        """self if the call succeeded; GitError otherwise."""
        if not self.ok:
            raise GitError(self)
        return self


# ---------------------------------------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------------------------------------
# Global options git accepts before the subcommand. Value-taking ones are listed with their value.
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_GLOBAL_FLAGS = {"--bare", "--no-pager", "-P", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
                 "--icase-pathspecs", "--no-replace-objects", "--no-optional-locks", "--no-lazy-fetch"}

# Subcommands that only read, or write only inside the repo the service owns (a mirror, a scratch clone).
_READ = {"ls-remote", "rev-list", "merge-base", "cat-file", "for-each-ref", "rev-parse", "ls-tree", "show", "log",
         "show-ref", "name-rev", "describe", "diff", "diff-tree", "ls-files", "grep", "count-objects", "fsck",
         "var", "version", "check-ref-format", "blame", "shortlog", "cherry", "archive", "verify-commit",
         "verify-tag", "check-ignore", "check-attr", "show-branch", "hash-object"}
_LOCAL = {"init", "fetch", "clone", "checkout", "switch", "add", "commit"}
_RESTRICTED = {"tag", "branch", "remote", "reflog", "symbolic-ref", "config", "submodule"}

# Options that run a program or point git at another one: refused in every subcommand.
_EXEC_OPTS = ("--upload-pack", "--receive-pack", "--exec")
# Config keys that redirect URLs, store credentials, rename subcommands, run hooks or change a remote:
# refused as `-c`/`--config` values that the call could act on, and as `git config` writes.
_FORBIDDEN_CONFIG_WRITE = re.compile(r"^(remote\.|url\.|credential\.|alias\.|core\.hookspath$|core\.askpass$|"
                                     r"core\.sshcommand$|core\.gitproxy$|protocol\.|include\.|includeif\.)", re.I)
# `-c` on the command line may set a credential helper (the read token comes in that way, never in the URL)
# and may set ssh options, but may not alias a subcommand, add hooks, or open dangerous transports.
_FORBIDDEN_CONFIG_ARG = re.compile(r"^(alias\.|core\.hookspath$|protocol\.|include\.|includeif\.|"
                                   r"remote\.[^=]*\.(pushurl|push|receivepack|uploadpack)$|url\..*\.pushinsteadof$)",
                                   re.I)
_URL_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]*:[^/@\s]*@", re.I)


def _refuse(why: str, args: Sequence[str]) -> GitRefused:
    return GitRefused("git %s refused: %s" % (shlex.join(list(args)), why))


def _split_options(args: Sequence[str], value_opts: set[str]) -> tuple[list[str], list[str]]:
    """(options, positionals) of a subcommand's arguments; a value-taking option in `value_opts` given as two
    arguments takes the next one with it. Everything after `--` is positional."""
    opts, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("-") and a != "-":
            opts.append(a)
            if a in value_opts and i + 1 < len(args):
                i += 1
        else:
            pos.append(a)
        i += 1
    return opts, pos


def _has(opts: list[str], *names: str) -> bool:
    for o in opts:
        for n in names:
            if o == n or (n.startswith("--") and o.startswith(n + "=")):
                return True
            if not n.startswith("--") and len(n) == 2 and o.startswith("-") and not o.startswith("--") \
                    and n[1] in o[1:] and o[1:].isalpha():
                return True                       # a short flag bundled with others, e.g. -fd
    return False


_TAG_VALUE = {"--contains", "--no-contains", "--points-at", "--merged", "--no-merged", "--sort", "--format",
              "-n", "--column"}
_TAG_WRITE = ("-d", "--delete", "-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-f", "--force",
              "-m", "--message", "-F", "--file", "-e", "--edit", "--cleanup", "--create-reflog", "--trailer")
_BRANCH_VALUE = {"--contains", "--no-contains", "--points-at", "--merged", "--no-merged", "--sort", "--format",
                 "--column", "--abbrev"}
_BRANCH_WRITE = ("-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force",
                 "-u", "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track",
                 "--no-track", "--create-reflog", "--recurse-submodules")
_LISTING = ("-l", "--list", "--contains", "--no-contains", "--points-at", "--merged", "--no-merged",
            "--show-current", "-r", "--remotes", "-a", "--all", "-v", "-vv", "--verbose")
_CONFIG_READ = ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool",
                "-l", "--list")
_CONFIG_WRITE_FLAGS = ("--add", "--replace-all", "--unset", "--unset-all", "--rename-section", "--remove-section",
                       "-e", "--edit")
_CONFIG_SCOPE = ("--global", "--system", "--worktree", "-f", "--file", "--blob")


def _check_subcommand(sub: str, rest: list[str], argv: Sequence[str]) -> None:
    if sub in GIT_WRITES:
        raise _refuse("%s writes to another repository (interfaces.md section 17.5)" % sub, argv)
    _check_any(rest, argv)
    if sub in ("clone", "ls-remote") and "-u" in rest:
        raise _refuse("-u names an upload-pack program", argv)
    if sub == "clone":
        _check_clone(rest, argv)
    if sub in _READ or sub in _LOCAL:
        return
    if sub not in _RESTRICTED:
        raise _refuse("subcommand %r is not on the runner's read/local allowlist" % sub, argv)
    _RESTRICTED_CHECKS[sub](rest, argv)


def _check_any(rest: list[str], argv: Sequence[str]) -> None:
    """Refused in every subcommand: an option that runs a program, a transport that runs a command, a password in
    a URL."""
    for a in rest:
        if a.startswith(_EXEC_OPTS):
            raise _refuse("%s runs another program" % a.split("=")[0], argv)
        if a.startswith(("ext::", "fd::")):
            raise _refuse("transport %s runs a command" % a.split("::")[0], argv)
        if _URL_PASSWORD.match(a):
            raise _refuse("a URL carries a password; credentials never go in an argv", argv)


_CLONE_VALUE = {"-c", "--config", "-o", "--origin", "-b", "--branch", "--depth", "--filter", "--reference",
                "--separate-git-dir", "-j", "--jobs", "--template", "--server-option", "--shallow-since",
                "--shallow-exclude"}


def _check_clone(rest: list[str], argv: Sequence[str]) -> None:
    opts, _ = _split_options(rest, _CLONE_VALUE)
    for i, a in enumerate(rest):
        val = None
        if a in ("-c", "--config") and i + 1 < len(rest):
            val = rest[i + 1]
        elif a.startswith("--config="):
            val = a.split("=", 1)[1]
        if val is not None and _FORBIDDEN_CONFIG_ARG.match(val.split("=", 1)[0]):
            raise _refuse("clone config %s" % val.split("=", 1)[0], argv)
    if _has(opts, "--template"):
        raise _refuse("--template installs hooks", argv)


def _check_tag(rest: list[str], argv: Sequence[str]) -> None:
    opts, pos = _split_options(rest, _TAG_VALUE)
    if _has(opts, *_TAG_WRITE):
        raise _refuse("tag creation or deletion", argv)
    if pos and not _has(opts, "-l", "--list"):
        raise _refuse("tag creation (a tag name without --list)", argv)


def _check_branch(rest: list[str], argv: Sequence[str]) -> None:
    opts, pos = _split_options(rest, _BRANCH_VALUE)
    if _has(opts, *_BRANCH_WRITE):
        raise _refuse("branch creation, deletion or change", argv)
    if pos and not _has(opts, "-l", "--list"):
        raise _refuse("branch creation (a branch name without --list)", argv)


def _check_remote(rest: list[str], argv: Sequence[str]) -> None:
    _, pos = _split_options(rest, set())
    if pos and pos[0] not in ("show", "get-url"):
        raise _refuse("remote %s changes a remote" % pos[0], argv)


def _check_reflog(rest: list[str], argv: Sequence[str]) -> None:
    _, pos = _split_options(rest, set())
    if pos and pos[0] in ("expire", "delete", "drop"):
        raise _refuse("reflog %s discards history (B3 REWRITTEN and B8 read it)" % pos[0], argv)


def _check_symbolic_ref(rest: list[str], argv: Sequence[str]) -> None:
    opts, pos = _split_options(rest, {"-m"})
    if _has(opts, "-d", "--delete") or len(pos) > 1:
        raise _refuse("symbolic-ref change", argv)


def _check_submodule(rest: list[str], argv: Sequence[str]) -> None:
    opts, pos = _split_options(rest, {"--reference", "--depth", "-j", "--jobs", "--filter"})
    allowed = ("update", "init", "sync", "status", "summary")
    if pos and pos[0] not in allowed:
        raise _refuse("submodule %s is not one of %s" % (pos[0], ", ".join(allowed)), argv)
    if pos and pos[0] == "update" and _has(opts, "--merge", "--rebase"):
        raise _refuse("submodule update --merge/--rebase", argv)


def _check_config(rest: list[str], argv: Sequence[str]) -> None:
    opts, pos = _split_options(rest, {"-f", "--file", "--blob", "--type", "--default", "--comment"})
    if _has(opts, *_CONFIG_SCOPE):
        raise _refuse("config outside the repo's own local file", argv)
    reading = _has(opts, *_CONFIG_READ) or (len(pos) == 1 and not _has(opts, *_CONFIG_WRITE_FLAGS))
    if reading:
        return
    key = pos[0] if pos else ""
    if _has(opts, "--rename-section", "--remove-section") or _FORBIDDEN_CONFIG_WRITE.match(key):
        raise _refuse("config write to %r (remotes, URL rewrites, credentials, aliases and hooks are never "
                      "written)" % (key or rest), argv)


_RESTRICTED_CHECKS = {"tag": _check_tag, "branch": _check_branch, "remote": _check_remote, "reflog": _check_reflog,
                      "symbolic-ref": _check_symbolic_ref, "submodule": _check_submodule, "config": _check_config}
assert set(_RESTRICTED_CHECKS) == _RESTRICTED


def check_argv(args: Sequence[str]) -> None:
    """Raise GitRefused unless `args` (the argv after "git") is a read or a local write the service may make.
    Pure: runs nothing."""
    args = [str(a) for a in args]
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--config-env"):
            raise _refuse("--config-env reads config from the environment", args)
        if a in _GLOBAL_WITH_VALUE:
            if i + 1 >= len(args):
                raise _refuse("%s needs a value" % a, args)
            if a == "-c" and _FORBIDDEN_CONFIG_ARG.match(args[i + 1].split("=", 1)[0]):
                raise _refuse("-c %s" % args[i + 1].split("=", 1)[0], args)
            i += 2
            continue
        if any(a.startswith(o + "=") for o in _GLOBAL_WITH_VALUE if o.startswith("--")):
            i += 1
            continue
        if a in _GLOBAL_FLAGS:
            i += 1
            continue
        if a.startswith("--exec-path") or a in ("-p", "--paginate"):
            raise _refuse("global option %s" % a, args)
        if a.startswith("-"):
            raise _refuse("unknown global option %s" % a, args)
        _check_subcommand(a, args[i + 1:], args)
        return
    raise _refuse("no subcommand", args)


def check_credentialed(args: Sequence[str]) -> None:
    """Raise GitRefused unless `args` is a `fetch` or `ls-remote` (no submodule recursion), the only
    calls a private-read credential may reach. Called by run_git when the environment carries the marker."""
    args = [str(a) for a in args]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        # No -c or --config-env: a config setting could turn on submodule recursion or change the transport
        # (core.sshCommand) under the credential. The private-read path passes its config through the env.
        if args[i] == "-c" or args[i].startswith(("-c", "--config-env")):
            raise _refuse("a private-read credential takes no -c or --config-env", args)
        i += 2 if args[i] in _GLOBAL_WITH_VALUE else 1
    sub = args[i] if i < len(args) else ""
    if sub not in CREDENTIALED_READS:
        raise _refuse("a private-read credential is only for %s, not %r" % (" and ".join(CREDENTIALED_READS),
                                                                             sub), args)
    # Every --recurse-submodules* option (including --recurse-submodules-default) is refused unless its value is
    # an explicit "no" (any case); --no-recurse-submodules is allowed.
    if any(a.startswith("--recurse-submodules") and a.partition("=")[2].lower() != "no" for a in args[i + 1:]):
        raise _refuse("a private-read credential never follows submodules", args)


# ---------------------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------------------
_DROPPED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EXEC_PATH",
            "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_EXTERNAL_DIFF")


def hardened_env(extra: Mapping[str, str] | None = None, *, isolate: bool = False) -> dict[str, str]:
    """The environment run_git gives git: the process environment, never prompting, never paging, acting
    only on the repo named in the argv. `extra` is applied last (it may set GIT_SSH_COMMAND or a credential
    helper's variable). isolate=True also ignores every global and system git config (the T0 runner)."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_PAGER="cat", LC_ALL="C", LANGUAGE="C")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    if isolate:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env.update(extra or {})
    return env


def run_git(*args: str | os.PathLike, cwd: str | os.PathLike | None = None, env: Mapping[str, str] | None = None,
            input: str | bytes | None = None, timeout: float | None = DEFAULT_TIMEOUT_S, isolate: bool = False,
            text: bool = True) -> GitResult:
    """Run `git <args>` after check_argv. `cwd` is passed to git as a leading `-C <cwd>`, so the recorded
    argv is the whole command. Never raises for a failed call (see GitResult.ok/.check()); raises GitRefused
    for a refused argv. A timeout gives returncode -1 and timed_out=True."""
    argv = ([] if cwd is None else ["-C", os.fspath(cwd)]) + [os.fspath(a) for a in args]
    check_argv(argv)
    if (env and env.get(CREDENTIALED_MARKER)) or os.environ.get(CREDENTIALED_MARKER):
        check_credentialed(argv)
    stdin = subprocess.DEVNULL if input is None else None
    try:
        p = subprocess.run(["git", *argv], input=input, stdin=stdin, capture_output=True, text=text,
                           env=hardened_env(env, isolate=isolate), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if e.stdout is not None else ("" if text else b"")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return GitResult(("git", *argv), -1, out, err + "\ntimed out after %ss" % timeout, timed_out=True)
    err = p.stderr if isinstance(p.stderr, str) else p.stderr.decode(errors="replace")
    return GitResult(("git", *argv), p.returncode, p.stdout, err)
