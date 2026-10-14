"""The toolkit's vendor-collateral scanner as the scanner's second engine (interfaces.md section 11; GATE2 Q4).

The lab's rule set is the ASIC toolkit's `ci/check-vendor-collateral.sh`: foundry file, value and inventory
shapes, known vendor files keyed by sha256, Arm product codes. It is private, so this public package never
carries it. compat-ci carries a pinned copy (the script with its `lib.sh` beside it) and names it with
`--toolkit PATH` or `$RM_SCAN_TOOLKIT`. Without one, only this package's own rules run (`arm.banner`, plus any
private regex rule file, nanosoc_rm.scan.rules).

How it runs (section 11): the toolkit scanner reads git trees.

- A bundle directory becomes a throwaway git repo: one commit over every file (ignored ones too), made under a
  temporary directory that is deleted afterwards, and censused against the native engine's file count. The
  scanner runs on it with `--rev HEAD --fast`.
- A tree T0 fetched (t0.vendor.no-arm-ip and t0.vendor.no-foundry-data, which split its findings by rule family:
  nanosoc_rm.scan.families) is scanned in place with `--rev <commit> --fast`. A tree's own
  `VENDOR_ALLOWLIST` at that commit (the toolkit's project waivers, each with a reason and an owner) is handed to
  the scanner, as the project's own CI would read it from its checkout. A bundle never waives itself: its
  throwaway repo has no checkout, so the scanner finds no project allowlist.

Results come back through the scanner's two machine-readable channels: its findings file
(`VENDOR_CHECK_FINDINGS_OUT`: rule, path, line, why) and its own verdict rows (`CI_VERDICT_DIR`). Only rule,
path and line are kept, so a finding is a location and nothing else. The scanner arms its own rules on invented
specimens and censuses its corpus before it reports (the method this package reuses): its `vendor.arm` row must
be PASS, and an UNVERIFIED row, a timeout, or a non-zero exit without a finding makes the scan unverified.

The scanner runs with an environment built from nothing (PATH, a locale, a private HOME and TMPDIR, an isolated
git config). No credential, no inherited `VC_*` tuning variable and no bypass variable reaches it. It reads the
repo and writes only under its temporary directory. The `git` on its PATH is a shim that runs every call through
the runner's argv guard (nanosoc_rm.gitrun.check_argv) and exits 97 on a refused one, so the scanner can no more
push than the runner can.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nanosoc_rm.gitrun import run_git
from nanosoc_rm.scan.rules import RulesError

__all__ = ["TOOLKIT_ENV", "ARMED_KEY", "Toolkit", "ToolkitRun", "toolkit", "run_toolkit", "scan_dir_toolkit",
           "path_label", "REFUSED_EXIT"]

TOOLKIT_ENV = "RM_SCAN_TOOLKIT"       # path of the pinned check-vendor-collateral.sh (compat-ci); optional
ARMED_KEY = "toolkit"                 # its key in ScanResult.armed
TIMEOUT_S = 900
REFUSED_EXIT = 97                     # the git shim's exit when the runner's argv guard refuses a call
_IDENTITY = {"GIT_AUTHOR_NAME": "nanosoc-rm scan", "GIT_AUTHOR_EMAIL": "scan@nanosoc-rm.invalid",
             "GIT_COMMITTER_NAME": "nanosoc-rm scan", "GIT_COMMITTER_EMAIL": "scan@nanosoc-rm.invalid",
             "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


@dataclass(frozen=True)
class Toolkit:
    """A pinned copy of the toolkit scanner: the script, with lib.sh beside it."""
    script: Path
    sha256: str                       # of the script and lib.sh together: which copy ran

    def describe(self) -> str:
        return "toolkit %s sha256:%s" % (self.script.name, self.sha256[:12])


@dataclass
class ToolkitRun:
    findings: list[dict]              # [{rule, file, line}]
    armed: bool                       # its vendor.arm row was PASS
    why: str = ""                     # why the run cannot vouch for the corpus; "" when it answered
    rows: list[tuple[str, str]] = field(default_factory=list)    # (status, gate) of its verdict rows


def toolkit(path: str | os.PathLike | None = None) -> Toolkit | None:
    """The toolkit scanner named by `path`, else by $RM_SCAN_TOOLKIT; None when neither names one. RulesError
    when the named copy cannot be used (not a file, or no lib.sh beside it)."""
    raw = path if path is not None else (os.environ.get(TOOLKIT_ENV) or None)
    if not raw:
        return None
    script = Path(raw)
    lib = script.parent / "lib.sh"
    if not script.is_file():
        raise RulesError("toolkit scanner %s is not a file" % script)
    if not lib.is_file():
        raise RulesError("toolkit scanner %s has no lib.sh beside it" % script)
    h = hashlib.sha256(script.read_bytes() + b"\0" + lib.read_bytes()).hexdigest()
    return Toolkit(script.resolve(), h)


def path_label(raw: bytes) -> str:
    """A tree path as text: UTF-8, with any byte that is not written `\\xNN`. A label only, never opened."""
    return raw.decode("utf-8", "backslashreplace")


_SHIM = """import os, sys
sys.path.insert(0, %(pkg)r)
from nanosoc_rm.gitrun import GitRefused, check_argv
try:
    check_argv(sys.argv[1:])
except GitRefused as e:
    sys.stderr.write("nanosoc-rm: %%s\\n" %% e)
    sys.exit(%(code)d)
os.execv(%(git)r, [%(git)r] + sys.argv[1:])
"""


def _git_shim(t: Path) -> Path | None:
    """t/bin/git: every git call the toolkit scanner makes passes the runner's own argv guard
    (nanosoc_rm.gitrun.check_argv: no push, send-pack or receive-pack, no exec options, no URL with a password)
    before the real git runs. None when there is no git to wrap."""
    real = shutil.which("git", path=os.environ.get("PATH", "/usr/bin:/bin"))
    if real is None:
        return None
    pkg = str(Path(__file__).resolve().parents[2])
    (t / "bin").mkdir()
    (t / "git-shim.py").write_text(_SHIM % {"pkg": pkg, "git": os.path.realpath(real), "code": REFUSED_EXIT})
    sh = t / "bin" / "git"
    sh.write_text('#!/bin/sh\nexec "%s" -IS "%s" "$@"\n' % (sys.executable, t / "git-shim.py"))
    sh.chmod(0o700)
    return t / "bin"


def _env(t: Path) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C",
           "HOME": str(t / "home"), "TMPDIR": str(t / "tmp"),
           "CI_VERDICT_DIR": str(t / "verdicts"), "VENDOR_CHECK_FINDINGS_OUT": str(t / "findings.tsv"),
           "CI_SUMMARY_FILE": os.devnull, "CI_COLOUR": "0", "NO_COLOR": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for d in ("home", "tmp", "verdicts"):
        (t / d).mkdir()
    return env


def _rows(vdir: Path) -> list[tuple[str, str]]:
    out = []
    for f in sorted(vdir.rglob("verdicts.tsv")):
        for line in f.read_text(errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                out.append((parts[1], parts[2]))
    return out


def _findings(path: Path, prefix: str, name: Callable[[bytes], str] = path_label) -> list[dict]:
    out, seen = [], set()
    if not path.is_file():
        return out
    for line in path.read_bytes().splitlines():
        parts = line.split(b"\t")
        if len(parts) < 3 or not parts[0] or not parts[1]:
            continue
        rule, rel = parts[0].decode("utf-8", "replace"), name(parts[1])
        ln = int(parts[2]) if parts[2].isdigit() and int(parts[2]) > 0 else None   # 0: a file-shaped finding
        rel = "%s/%s" % (prefix, rel) if prefix else rel
        if (rule, rel, ln) not in seen:
            seen.add((rule, rel, ln))
            out.append({"rule": rule, "file": rel, "line": ln})      # never the scanner's `why` column
    return out


PROJECT_ALLOWLIST = "VENDOR_ALLOWLIST"


def run_toolkit(tk: Toolkit, repo: str | os.PathLike, rev: str, prefix: str = "",
                project_allowlist: bool = True, name: Callable[[bytes], str] = path_label) -> ToolkitRun:
    """Run the toolkit scanner on the tree of `rev` in the non-bare repo `repo`. Finding paths are
    `prefix/<path in the tree>`, each named by `name`. With `project_allowlist`, the tree's own VENDOR_ALLOWLIST
    at `rev` applies. The scanner's git is the shim (_git_shim), so its git calls pass the runner's argv guard.
    Never raises for a scanner failure: it comes back as `why`."""
    with tempfile.TemporaryDirectory(prefix="rm-toolkit-") as tmp:
        t = Path(tmp)
        env = _env(t)
        shim = _git_shim(t)
        if shim is None:
            return ToolkitRun([], False, "no git on PATH for the toolkit scanner")
        env["PATH"] = "%s:%s" % (shim, env["PATH"])
        if project_allowlist:
            own = run_git("cat-file", "blob", "%s:%s" % (rev, PROJECT_ALLOWLIST), cwd=repo, isolate=True, text=False)
            if own.ok:
                (t / PROJECT_ALLOWLIST).write_bytes(own.stdout)
                env["VC_PROJECT_ALLOWLIST"] = str(t / PROJECT_ALLOWLIST)
        try:
            p = subprocess.run(["bash", str(tk.script), "--rev", rev, "--fast"], cwd=repo, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return ToolkitRun([], False, "toolkit scanner timed out after %ss" % TIMEOUT_S)
        except OSError as e:
            return ToolkitRun([], False, "toolkit scanner could not start: %s" % (e.strerror or e))
        rows = _rows(t / "verdicts")
        findings = _findings(t / "findings.tsv", prefix, name)
    armed = any(s == "PASS" and g == "vendor.arm" for s, g in rows)
    unverified = sorted({g for s, g in rows if s == "UNVERIFIED"})
    if not armed:
        why = "toolkit scanner did not arm its rules (%s)" % (
            ", ".join(unverified) if unverified else "exit %d, no vendor.arm PASS row" % p.returncode)
    elif unverified:
        why = "toolkit scanner could not verify: %s" % ", ".join(unverified)
    elif p.returncode != 0 and not findings:
        why = "toolkit scanner exited %d without a finding" % p.returncode
    else:
        why = ""
    return ToolkitRun(findings, armed, why, rows)


def scan_dir_toolkit(tk: Toolkit, root: str | os.PathLike, expect_files: int | None = None) -> ToolkitRun:
    """The toolkit scanner over a directory: a throwaway git repo (one commit over the files, in a temporary
    directory deleted afterwards), scanned with --rev HEAD. The directory itself is never written. Every file is
    committed, ignored ones too (`add -f`: a .gitignore in the bundle hides nothing). With `expect_files` (the
    native engine's census), a commit holding any other number of entries (a nested repo becomes one gitlink)
    makes the run unverified. Finding names are the file system's (os.fsdecode), so the caller can delete them."""
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="rm-toolkit-repo-") as tmp:
        repo = Path(tmp) / "repo"
        gd = repo / ".git"
        steps = (("init", "-q", repo),
                 ("--git-dir", gd, "--work-tree", root, "add", "-A", "-f", "--", "."),
                 ("--git-dir", gd, "--work-tree", root, "commit", "-q", "--allow-empty", "-m", "bundle under scan"))
        for argv in steps:
            r = run_git(*argv, cwd=root if argv[0] != "init" else None, env=_IDENTITY, isolate=True, timeout=600)
            if not r.ok:
                return ToolkitRun([], False, "could not build the throwaway repo for the toolkit scanner (%s)"
                                  % " ".join(str(a) for a in argv if a not in (gd, root, repo))[:80])
        if expect_files is not None:
            ls = run_git("--git-dir", gd, "ls-tree", "-r", "-z", "--full-tree", "HEAD", isolate=True, timeout=600,
                         text=False)
            held = len([e for e in ls.stdout.split(b"\0") if e]) if ls.ok else -1
            if held != expect_files:
                return ToolkitRun([], False, "the throwaway repo holds %d of the %d files in the directory (a nested "
                                  "repo or an entry git cannot hold): the toolkit scanner would not see them all"
                                  % (held, expect_files))
        return run_toolkit(tk, repo, "HEAD", project_allowlist=False, name=os.fsdecode)
