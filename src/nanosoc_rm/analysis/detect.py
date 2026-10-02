"""F2: run one detector of a defect on a configuration's built output (interfaces.md section 6a).

    run(defect_id, detector_id, built_output) -> DetectorRun     (= erratum.schema.json#/$defs/detector_run)

A person or a CI job runs it (`nanosoc-rm detect DEFECT DETECTOR --input PATH`) and pastes the result into the
erratum's `exposure.run`. The service never runs it (A4): no schedule or web route calls this module.

**Input.** `built_output` is a generated-RTL archive (any tar), a directory, or one file (a netlist).
`input.md5` is the md5 of the file given; for a directory, the md5 of its md5 manifest: one line
`<md5 of the file>  <path relative to the directory>` per regular file, sorted by that path's bytes,
`\\n`-terminated, byte for byte as GNU `md5sum` prints it (symbolic links are not followed and not listed).
An archive is unpacked into a temporary directory (tar's `data` filter: a leading `/` is stripped, a member or
link that would leave the directory is refused) and judged there.

**Pattern detector** (`pattern`, a defect's netlist or RTL regex): applied by the tool to every regular file of
the input (as text, UTF-8 with replacement). POSIX bracket classes (`[[:alpha:]]` and friends) are translated
to Python's syntax. `fired` when any file matches. `detector_source` is null: the pattern lives in the record.

**Command detector** (`command` + `source`): its code is fetched at its pinned `source` commit from the
component's canonical repo (`hosts.<host>.git`, a depth-1 fetch of that commit into a throwaway repo, through
the one git runner), and the command runs there with `{dir}` replaced by the unpacked input. v1 runs only
`python -m <module> [args]` commands, the form every registered detector uses; anything else is "could not
run". The module runs under `python3` from PATH through a small wrapper that maps its end to an answer:
exit 0 clean, exit 1 fired, any other exit, an uncaught exception or a missing module could not run
(DetectorError, CLI exit 2). So a crash is never read as `fired`.

`forms` = the detector's declared forms when it fired (a detector reports which defect it found, not which of
its forms), [] when clean. `command` = the `nanosoc-rm detect` invocation that reproduces the run.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nanosoc_rm import clock, contracts
from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.analysis._inputs import CannotAnswer, UsageProblem
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import run_git

__all__ = ["DetectorRun", "DetectorError", "run", "input_md5", "md5_manifest", "DETECTOR_TIMEOUT_S"]

DETECTOR_TIMEOUT_S = 900
CONTRACT_FIELDS = ("result", "forms", "input", "detector_source", "command", "observed_at")


class DetectorError(CannotAnswer):
    """The detector could not run, or ended without an answer (CLI exit 2)."""


@dataclass
class DetectorRun:
    """One detector run. The six contract fields are erratum.schema.json#/$defs/detector_run; `log` is the
    detector's own output (not part of the document)."""
    result: Literal["fired", "clean"]
    forms: list[str]
    input: dict
    detector_source: dict | None
    command: str
    observed_at: str
    log: str = field(default="", repr=False, compare=False)

    def as_doc(self) -> dict:
        """The contract document (what goes into an erratum's exposure.run)."""
        d = dataclasses.asdict(self)
        return {k: d[k] for k in CONTRACT_FIELDS}

    def model_dump(self, mode: str = "json", by_alias: bool = True) -> dict:   # the tests' as_doc() helper
        return self.as_doc()


# ---------------------------------------------------------------------------------------------------------
# Input identity
# ---------------------------------------------------------------------------------------------------------
def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_line(md5: str, rel: bytes) -> bytes:
    """One md5sum line. GNU md5sum (coreutils 8) escapes a name holding `\\` or a newline and marks the line
    with a leading backslash; any other byte, UTF-8 or not, is printed as it is."""
    if b"\\" in rel or b"\n" in rel:
        return b"\\" + md5.encode() + b"  " + rel.replace(b"\\", b"\\\\").replace(b"\n", b"\\n") + b"\n"
    return md5.encode() + b"  " + rel + b"\n"


def md5_manifest(directory: Path) -> bytes:
    """The md5 manifest of a directory (module docstring), as the bytes md5sum prints: `<md5>  <relative path>`
    per regular file, sorted by the path's bytes (`LC_ALL=C sort`). File names are bytes on Linux, not text."""
    directory = Path(directory)
    files = []
    for root, dirs, names in os.walk(directory, followlinks=False):
        for name in names:
            p = Path(root) / name
            if p.is_file() and not p.is_symlink():
                files.append(p.relative_to(directory))
    files.sort(key=lambda rel: os.fsencode(rel))
    return b"".join(_manifest_line(_md5_file(directory / rel), os.fsencode(rel)) for rel in files)


def input_md5(path: Path) -> str:
    """md5 of a file, or of a directory's md5 manifest (interfaces.md section 6a)."""
    path = Path(path)
    if path.is_dir():
        return hashlib.md5(md5_manifest(path)).hexdigest()
    return _md5_file(path)


def _unpack(path: Path, tmp: Path) -> Path:
    """What the detector judges: the directory itself, an unpacked archive, or the one file."""
    if path.is_dir():
        return path
    if tarfile.is_tarfile(path):
        dest = tmp / "input"
        dest.mkdir()
        try:
            with tarfile.open(path) as tar:
                tar.extractall(dest, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise DetectorError("cannot unpack %s: %s" % (path, e)) from None
        return dest
    if zipfile.is_zipfile(path):
        dest = tmp / "input"
        dest.mkdir()
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(dest)
        except (zipfile.BadZipFile, OSError) as e:
            raise DetectorError("cannot unpack %s: %s" % (path, e)) from None
        return dest
    return path


# ---------------------------------------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------------------------------------
_POSIX_CLASSES = {"alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z", "lower": "a-z",
                  "xdigit": "0-9A-Fa-f", "space": r"\s", "blank": r" \t", "punct": r"!-/:-@\[-`{-~",
                  "word": r"\w"}


def compile_pattern(pattern: str) -> re.Pattern:
    """An extended regex as Python compiles it, with POSIX bracket classes translated."""
    text = re.sub(r"\[:([a-z]+):\]", lambda m: _POSIX_CLASSES.get(m.group(1), m.group(0)), pattern)
    try:
        return re.compile(text, re.MULTILINE)
    except re.error as e:
        raise DetectorError("detector pattern %r does not compile: %s" % (pattern, e)) from None


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    out = []
    for r, _dirs, names in os.walk(root, followlinks=False):
        out += [Path(r) / n for n in names if (Path(r) / n).is_file() and not (Path(r) / n).is_symlink()]
    return sorted(out)


def _pattern_fired(pattern: str, root: Path) -> tuple[bool, str]:
    rx = compile_pattern(pattern)
    files = _files(root)
    if not files:
        raise DetectorError("the input holds no file to scan: a scan of nothing proves nothing")
    hits = []
    for f in files:
        text = f.read_bytes().decode("utf-8", errors="replace")
        m = rx.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            name = os.fsencode(f.relative_to(root) if root.is_dir() else f.name).decode("utf-8", "backslashreplace")
            hits.append("%s:%d" % (name, line))
    log = "pattern %s: %d file(s) scanned, %d matched%s" % (pattern, len(files), len(hits),
                                                            (": " + ", ".join(hits[:20])) if hits else "")
    return bool(hits), log


# ---------------------------------------------------------------------------------------------------------
# Command detectors
# ---------------------------------------------------------------------------------------------------------
# Runs `python -m <module> args` and maps how it ended: 0 clean, 1 fired, anything else could not run (3).
_WRAPPER = r"""
import runpy, sys, traceback
module = sys.argv[1]
sys.argv = [module] + sys.argv[2:]
try:
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    code = 0
except SystemExit as e:
    code = 0 if e.code in (None, 0) else 1 if e.code == 1 else 3
except BaseException:
    traceback.print_exc()
    code = 3
sys.exit(code)
"""


def _fetch_source(cfg: Config, reg: inp.Registry, source: dict, tmp: Path) -> Path:
    comp = reg.component(source["component"])
    repo = comp.canonical or comp.repos[0]
    url = "%s/%s.git" % (cfg.host(repo["host"])["git"].rstrip("/"), repo["path"])
    checkout = tmp / "source"
    steps = [run_git("init", "--quiet", checkout, timeout=60)]
    if steps[-1].ok:
        steps.append(run_git("fetch", "--quiet", "--depth=1", url, source["commit"], cwd=checkout, timeout=600))
    if steps[-1].ok:
        steps.append(run_git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=checkout, timeout=120))
    if steps[-1].ok:
        steps.append(run_git("rev-parse", "HEAD", cwd=checkout, timeout=60))
    last = steps[-1]
    if not last.ok:
        raise DetectorError("cannot fetch the detector's source %s@%s from %s: %s exited %s (%s)" % (
            source["component"], source["commit"][:12], url, last.command, last.returncode,
            " / ".join(last.stderr.strip().splitlines()[-2:])))
    if last.stdout.strip() != source["commit"]:
        raise DetectorError("fetched %s, not the pinned source %s" % (last.stdout.strip(), source["commit"]))
    return checkout


def _command_fired(cfg: Config, reg: inp.Registry, det: dict, root: Path, tmp: Path) -> tuple[bool, str]:
    argv = [a.replace("{dir}", str(root)) for a in shlex.split(det["command"])]
    if len(argv) < 3 or argv[0] not in ("python", "python3") or argv[1] != "-m":
        raise DetectorError("detector %s runs %r: v1 runs only `python -m <module> [args]` detectors"
                            % (det["id"], det["command"]))
    checkout = _fetch_source(cfg, reg, det["source"], tmp)
    module, args = argv[2], argv[3:]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(["python3", "-c", _WRAPPER, module, *args], cwd=checkout, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=DETECTOR_TIMEOUT_S)
    except FileNotFoundError:
        raise DetectorError("python3 is not on PATH: the detector cannot run") from None
    except subprocess.TimeoutExpired:
        raise DetectorError("detector %s ran past %d s" % (det["id"], DETECTOR_TIMEOUT_S)) from None
    log = (proc.stdout + proc.stderr).strip()
    if proc.returncode not in (0, 1):
        raise DetectorError("detector %s ended without an answer (exit %s): %s" % (
            det["id"], proc.returncode, " / ".join(log.splitlines()[-3:]) or "no output"))
    return proc.returncode == 1, log


# ---------------------------------------------------------------------------------------------------------
def run(defect_id: str, detector_id: str, built_output: Path, *, config: Config | str | Path | None = None
        ) -> DetectorRun:
    """Run detector `detector_id` of defect `defect_id` on `built_output`. UsageProblem for an unknown defect,
    detector or input path; DetectorError when it could not run or gave no answer."""
    cfg = inp.config_of(config)
    reg = inp.registry(cfg)
    defect = reg.defect(defect_id)
    det = next((d for d in defect.get("detectors", []) if d["id"] == detector_id), None)
    if det is None:
        raise UsageProblem("defect %s has no detector %s (it has: %s)" % (
            defect_id, detector_id, ", ".join(d["id"] for d in defect.get("detectors", [])) or "none"))
    path = Path(built_output)
    if not path.exists():
        raise UsageProblem("input %s does not exist" % path)
    observed_at = clock.iso()
    md5 = input_md5(path)
    with tempfile.TemporaryDirectory(prefix="nanosoc-rm-detect-") as tmp_name:
        tmp = Path(tmp_name)
        root = _unpack(path, tmp)
        if "pattern" in det:
            fired, log = _pattern_fired(det["pattern"], root)
            source = None
        else:
            fired, log = _command_fired(cfg, reg, det, root, tmp)
            source = {"component": det["source"]["component"], "commit": det["source"]["commit"]}
    result = DetectorRun(result="fired" if fired else "clean", forms=list(det["forms"]) if fired else [],
                         input={"path": str(path), "md5": md5}, detector_source=source,
                         command=shlex.join(["nanosoc-rm", "detect", defect_id, detector_id, "--input", str(path)]),
                         observed_at=observed_at, log=log)
    problems = contracts.validate_detector_run(result.as_doc())
    if problems:
        raise DetectorError("the run breaks erratum.schema.json#/$defs/detector_run: %s" % "; ".join(problems))
    return result
