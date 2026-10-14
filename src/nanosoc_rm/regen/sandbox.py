"""Regenerate a configuration's generated output with one component moved to a commit, in a sandbox (section 20).

    tree_paths(configuration, component, config) -> [tree path]      where the stored walk pins the component
    render(configuration, component, commit, *, config, ...) -> Render
    SandboxRegenerator(config)                                       the C3/C7 `Regenerator` (versioning.classify)

A render:

1. **Request.** The configuration's commit is resolved once (`ci.trigger.build_request`: a frozen configuration's
   recorded commit, else its ref in the mirror); every tree path where the stored walk pins the component (a B7
   split moves together) is overridden to `commit` (`pin_overrides`, CR-11). Two renders of one measurement share
   the request's root commit, so only the component differs between them.
2. **Tree.** The overridden tree is walked anonymously in a fresh work directory (`t0.gates.Context.for_local`:
   empty HOME, no credential, no user git config) and the spec's `pins` are checked out (`tierrun.materialise`),
   each override checked to have been reached. A tree that cannot be laid out, or an override the walk did not
   reach, is not run: status `not-run`, the measurement INFRA (or UNVERIFIED for an unreached override).
3. **Run.** The spec's command (spec.py) through `tierrun.run_tier`: clean environment, budget, kill at 1.5 x,
   stdin closed, its own process group. The `output` directory is deleted first, so a committed or stale tree is
   never read as this run's output. A non-zero exit is status `failed`.
4. **Collect.** Every file under `output` (include/exclude globs; text only, at most MAX_FILE_BYTES each) is read
   and normalised: the sandbox's absolute paths become `$TREE` and `$WORK`, and a line holding a date-time stamp
   (`// Generated: 2026-10-14 04:37:45`) is dropped, so two renders of one commit are identical. The file lists of
   `output/flist/*` (one per variant) feed C7. The log's "skipping" and "not set" warning lines become `coverage`.

Renders are cached under `<[regen] work_dir>/cache/<configuration>/<component>/<commit>/<key>/` (key: root commit
and spec digest), as normalised text plus `render.json`. A cached render is identical to a fresh one by
construction; `fresh=True` ignores the cache.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import secrets
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from nanosoc_rm.regen import spec as S

__all__ = ["Render", "RegenerationError", "render", "tree_paths", "normalise", "SandboxRegenerator", "MAX_FILE_BYTES",
           "MAX_FILES", "STAMP"]

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 5000
STAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")
_ERROR = re.compile(r"\b(error|fatal|traceback|unrecognized)\b", re.I)
_COVERAGE = re.compile(r"(skipping|skipped|not set|not installed|not found)", re.I)
_CACHE_VERSION = 1


class RegenerationError(RuntimeError):
    """A render that produced no output to diff. `status` is not-run or failed; `verdict` INFRA, UNVERIFIED or FAIL."""

    def __init__(self, reason: str, status: str = "not-run", verdict: str = "INFRA", render: "Render | None" = None):
        super().__init__(reason)
        self.reason, self.status, self.verdict, self.render = reason, status, verdict, render


@dataclass
class Render:
    configuration: str
    component: str
    commit: str
    root_commit: str | None
    tree_paths: list[str]
    status: str                                  # ok | failed | not-run
    verdict: str                                 # PASS | FAIL | INFRA | UNVERIFIED
    reason: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    flists: dict[str, list[str]] = field(default_factory=dict)
    binary: list[str] = field(default_factory=list)     # files left out of the diff (binary or too large), by name
    coverage: list[str] = field(default_factory=list)
    seconds: float | None = None
    exit_code: int | None = None
    log_tail: list[str] = field(default_factory=list)
    cached: bool = False
    digest: str | None = None
    sources: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        """The `render` block of a regen_class fact (regen.schema.json#/$defs/render)."""
        return {"commit": self.commit, "status": self.status, "verdict": self.verdict, "reason": self.reason,
                "files": len(self.outputs), "binary": len(self.binary), "digest": self.digest,
                "seconds": self.seconds, "exit_code": self.exit_code, "cached": self.cached,
                "coverage": self.coverage[:20], "log_tail": self.log_tail[-10:]}


def _digest(outputs: dict[str, str], binary: list[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(outputs):
        h.update(p.encode() + b"\0" + hashlib.sha256(outputs[p].encode()).digest())
    for p in sorted(binary):
        h.update(b"bin\0" + p.encode())
    return h.hexdigest()


def normalise(text: str, tree: Path | None, work: Path | None) -> str:
    """Sandbox paths to `$TREE`/`$WORK`; lines with a date-time stamp dropped (module docstring step 4)."""
    for p, name in ((tree, "$TREE"), (work, "$WORK")):
        if p is not None:
            for form in {str(p), str(p.resolve())}:
                text = text.replace(form, name)
    return "\n".join(x for x in text.split("\n") if not STAMP.search(x))


def _wanted(rel: str, include, exclude) -> bool:
    if include and not any(fnmatch.fnmatch(rel, g) for g in include):
        return False
    return not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in exclude)


def collect(out_dir: Path, tree: Path | None, work: Path | None, include=None, exclude=S.DEFAULT_EXCLUDE):
    """({rel: normalised text}, {variant: [entries]}, [binary rel]) of everything under `out_dir`."""
    outputs, flists, binary = {}, {}, []
    files = sorted(p for p in Path(out_dir).rglob("*") if p.is_file() and not p.is_symlink())
    for p in files[:MAX_FILES]:
        rel = p.relative_to(out_dir).as_posix()
        if not _wanted(rel, include, exclude):
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            binary.append(rel)
            continue
        if len(raw) > MAX_FILE_BYTES or b"\0" in raw[:8192]:
            binary.append(rel)
            continue
        text = normalise(raw.decode("utf-8", errors="replace"), tree, work)
        outputs[rel] = text
        if rel.startswith("flist/"):
            flists[rel[len("flist/"):]] = [x.strip() for x in text.splitlines()
                                           if x.strip() and not x.strip().startswith(("#", "//"))]
    return outputs, flists, binary


def tree_paths(configuration: str, component: str, config) -> list[str]:
    """Every tree path where the configuration's stored walk pins `component` (B7 splits move together)."""
    from nanosoc_rm import db
    out = set()
    for d in db.latest("pin_state", "pin_state:%s:" % configuration, config=config):
        f = d["fact"]
        if f["configuration"] == configuration and (f.get("child") or {}).get("component") == component:
            out.update(f["tree_paths"])
    return sorted(out)


def _spec_digest(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _cache_dir(st: S.Settings, configuration, component, commit, root_commit, spec) -> Path:
    key = "%s-%s" % ((root_commit or "none")[:12], _spec_digest(spec))
    return st.work_dir / "cache" / configuration / component / commit / key


def _load_cached(d: Path) -> Render | None:
    meta = d / "render.json"
    try:
        doc = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    if doc.get("cache_version") != _CACHE_VERSION:
        return None
    doc.pop("cache_version", None)
    outputs = {}
    for rel in doc.pop("files", []):
        try:
            outputs[rel] = (d / "files" / rel).read_text(encoding="utf-8")
        except OSError:
            return None
    r = Render(**doc)
    r.outputs = outputs
    r.cached = True
    return r if r.digest == _digest(outputs, r.binary) else None


def _save_cached(d: Path, r: Render) -> None:
    tmp = d.with_name(d.name + ".tmp-" + secrets.token_hex(4))
    try:
        for rel, text in r.outputs.items():
            p = tmp / "files" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        doc = asdict(r)
        doc.pop("outputs")
        doc["files"] = sorted(r.outputs)
        doc["cached"] = False
        doc["cache_version"] = _CACHE_VERSION
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "render.json").write_text(json.dumps(doc, sort_keys=True))
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def request_for(configuration: str, config, nonce: str | None = None) -> tuple[dict, list[dict]]:
    """The RM_REQUEST a render walks (its commit is the root commit of the measurement)."""
    from nanosoc_rm import db
    from nanosoc_rm.ci.trigger import build_request, canonical_urls_from
    req, sources = build_request(configuration, nonce or secrets.token_hex(16), config)
    request = {k: v for k, v in req.items() if k not in ("schema", "nonce")}
    docs = db.latest("pin_state", "pin_state:%s:" % configuration, config=config)
    request["canonical_urls"] = dict(request.get("canonical_urls") or {}, **canonical_urls_from(config, docs))
    return request, sources


def render(configuration: str, component: str, commit: str, *, config, request: dict | None = None,
           paths: list[str] | None = None, url_map=None, fresh: bool = False, keep_work: Path | None = None) -> Render:
    """One render (module docstring). RegenUsage for a spec or tree path the tool refuses; otherwise a Render
    whose status says whether it produced output (never raises for a failed run)."""
    from nanosoc_rm.analysis import tierrun
    from nanosoc_rm.analysis.compat import _host_bases
    from nanosoc_rm.t0 import gates as G
    from nanosoc_rm.t0.gitwork import iter_pins

    st = S.settings(config)
    spec = S.check(S.spec_for(configuration, config), configuration, st)
    paths = sorted(paths) if paths else tree_paths(configuration, component, config)
    if not paths:
        raise S.RegenUsage("the stored walk of %s pins no %s: run `nanosoc-rm walk %s` first, or name the tree "
                           "path(s) with --at" % (configuration, component, configuration))
    if request is None:
        request, req_sources = request_for(configuration, config)
    else:
        req_sources = []
    root_commit = request.get("commit")
    cdir = _cache_dir(st, configuration, component, commit, root_commit, spec)
    if not fresh and keep_work is None:
        got = _load_cached(cdir)
        if got is not None:
            return got
    base = Render(configuration, component, commit, root_commit, list(paths), "not-run", "INFRA",
                  sources=[{"kind": "derived", "command": "nanosoc-rm regen run %s --component %s --commit %s"
                            % (configuration, component, commit)}] + list(req_sources))
    st.work_dir.mkdir(parents=True, exist_ok=True)
    if keep_work is not None:
        work = Path(keep_work).absolute()
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="render-", dir=str(st.work_dir)))
    if st.under_read_only(work) is not None:
        raise S.RegenUsage("the sandbox %s is under a read-only root" % work)
    nonce = secrets.token_hex(16)
    try:
        req = dict(request, pin_overrides={p: commit for p in paths})
        ctx = G.Context.for_local(nonce=nonce, workdir=work / "run", request=req,
                                  registry_dir=Path(config.require("registry", "path")),
                                  host_bases=_host_bases(config), url_map=url_map)
        root = ctx.walk()
        if root is None:
            base.reason = "no tree to check out: the configuration's commit is not resolved"
            return base
        reached = {p.tree_path for p in iter_pins(root) if p.override_of is not None}
        unreached = [p for p in paths if p not in reached]
        if unreached:
            base.verdict = "UNVERIFIED"
            base.reason = ("the anonymous walk did not reach %s (above it a tree no anonymous fetch reads): not "
                           "regenerated" % ", ".join(unreached))
            return base
        tree = work / "tree"
        layout = tierrun.materialise(ctx.git, root, tree, spec.get("pins"))
        out_dir = tree / spec["output"]
        if out_dir.exists() and not layout.missing:
            shutil.rmtree(out_dir)              # a committed or stale output is never read as this run's
        doc = tierrun.run_tier(spec, "regen", tree, work, nonce, layout=layout)
        base.seconds = doc.get("seconds")
        base.exit_code = doc.get("exit_code")
        base.log_tail = [normalise(x, tree, work) for x in doc.get("log_tail") or []]
        try:
            log = (work / "tier-regen.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        base.coverage = sorted({normalise(" ".join(x.split()), tree, work)[:200]
                                for x in log.splitlines() if _COVERAGE.search(x)})[:40]
        if doc.get("seconds") is None:          # not run: a pin not laid out, a tool not on this host, no cwd
            base.reason = doc.get("reason") or "the generator did not run"
            return base
        if doc["verdict"] != "PASS":
            base.status, base.verdict = "failed", "FAIL"
            said = next((x for x in base.log_tail if _ERROR.search(x) and not x.startswith("make")), None) or \
                next((x for x in reversed(base.log_tail) if _ERROR.search(x)), None)
            base.reason = "the generator failed at %s: %s%s" % (commit[:12], doc.get("reason") or "",
                                                                "; %s" % said[:300] if said else "")
            return base
        if not out_dir.is_dir():
            base.status, base.verdict = "failed", "FAIL"
            base.reason = "the command exited 0 but wrote no %s" % spec["output"]
            return base
        base.outputs, base.flists, base.binary = collect(out_dir, tree, work, spec.get("include"),
                                                         tuple(spec.get("exclude") or ()) + S.DEFAULT_EXCLUDE)
        base.status, base.verdict, base.reason = "ok", "PASS", None
        base.digest = _digest(base.outputs, base.binary)
        if keep_work is None:
            _save_cached(cdir, base)
        return base
    finally:
        if keep_work is None:
            shutil.rmtree(work, ignore_errors=True)


class SandboxRegenerator:
    """The measured C3/C7 mode (versioning.classify.Regenerator): regenerate each consumer configuration that
    declares a `regen` command and pins the component, in a sandbox. Renders of one consumer share one request
    (one root commit) for as long as this object lives."""
    name = "sandbox"

    def __init__(self, config, *, url_map=None, consumers: list[str] | None = None):
        self.config, self.url_map, self._only = config, url_map, consumers
        self._requests: dict[str, dict] = {}
        self.renders: list[Render] = []

    def available(self) -> tuple[bool, str]:
        if not self.consumers_all():
            return False, ("class unmeasured: no configuration declares a regen command in the registry's "
                           "tiers.yaml (interfaces.md section 20)")
        return True, ""

    def consumers_all(self) -> list[str]:
        from nanosoc_rm.ci import configs
        try:
            ids = [c["id"] for c in configs.configurations(self.config)]
        except Exception:                                              # noqa: BLE001 - no registry: no consumer
            return []
        out = []
        for c in ids:
            try:
                if configs.tiers(c, self.config).get("regen"):
                    out.append(c)
            except Exception:                                          # noqa: BLE001
                continue
        return [c for c in out if self._only is None or c in self._only]

    def consumers(self, component: str) -> list[str]:
        return [c for c in self.consumers_all() if tree_paths(c, component, self.config)]

    def regenerate(self, consumer: str, component: str, commit: str):
        from nanosoc_rm.versioning.classify import RegenerationResult
        if consumer not in self._requests:
            self._requests[consumer] = request_for(consumer, self.config)[0]
        r = render(consumer, component, commit, config=self.config, request=self._requests[consumer],
                   url_map=self.url_map)
        self.renders.append(r)
        if r.status != "ok":
            raise RegenerationError(r.reason or "not regenerated", r.status, r.verdict, r)
        return RegenerationResult(consumer, r.outputs, r.flists, r.sources)
