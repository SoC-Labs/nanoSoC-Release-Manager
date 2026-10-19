"""`.nanosoc/capabilities.yaml`: parse, check and normalise one tree's test capabilities (interfaces.md section 26).

    parse(text, *, where) -> dict          the normalised manifest, or ManifestError(problems)
    Reader(config).at(component, commit, host=None) -> Read
                                            the file AT A COMMIT, from the component's mirror; never a working tree

Normalised form (what every other module reads): the draft spelling of the evaluation (`capabilities`, `run`,
`cwd`, `compose`); the wave-G spelling (`tiers`, `command`, `workdir`, `composes`) is accepted and renamed. Each
capability gets its defaults: `cwd` ".", `gate` "block", `when` ["on_bump"], `tools` [], `needs` [],
`affected_by` [], `outputs` [], `verdict` {"exit": 0}, `runtime` from `budget_s` (or `budget_s` from `runtime`),
`require_count` folded into `verdict.require`.

The schema (schemas/capabilities.schema.json) is checked first; then the rules a schema cannot say: ids unique,
every tool a capability names is in `tools`, `provides` never sets a variable the runner owns, compose paths
unique. Any problem refuses the whole file: a tree with an invalid file has NO capabilities (never a partial set).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import yaml

from nanosoc_rm import contracts

__all__ = ["PATH", "SCHEMA", "ManifestError", "parse", "Read", "Reader", "RUNTIME_BUDGET", "runtime_of",
           "RESERVED_ENV"]

PATH = ".nanosoc/capabilities.yaml"
SCHEMA = "capabilities"
ALIASES_TOP = (("tiers", "capabilities"), ("composes", "compose"))
ALIASES_CAP = (("command", "run"), ("workdir", "cwd"))
# runtime class -> the budget used when a capability names only its class (and the class of a budget)
RUNTIME_BUDGET = {"S": 60, "M": 900, "L": 10800, "XL": 43200}
DEFAULT_BUDGET_S = 900
RESERVED_ENV = ("TREE", "NANOSOC_RM_TIER", "NANOSOC_RM_VERDICTS", "NANOSOC_GEN_OK", "PATH", "HOME", "TMPDIR")


class ManifestError(ValueError):
    """The file is not a valid capabilities manifest. `problems` lists every reason."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems[:3]) + (" (and %d more)" % (len(self.problems) - 3)
                                                          if len(self.problems) > 3 else ""))


def runtime_of(budget_s: int) -> str:
    """The runtime class of a budget: S up to 2 min, M up to 15 min, L up to 3 h, XL beyond."""
    if budget_s <= 120:
        return "S"
    if budget_s <= 900:
        return "M"
    if budget_s <= 10800:
        return "L"
    return "XL"


def _when(v) -> list[str]:
    if v is None:
        return ["on_bump"]
    return [v] if isinstance(v, str) else list(v)


def parse(text: str, *, where: str = PATH) -> dict:
    """The normalised manifest (module docstring), or ManifestError with every problem."""
    try:
        doc = contracts.load_yaml(text)
    except yaml.YAMLError as e:
        raise ManifestError(["%s: not YAML: %s" % (where, str(e).splitlines()[0] if str(e) else e)]) from None
    return check(doc, where=where)


def check(doc, *, where: str = PATH) -> dict:
    """parse() for a document already loaded."""
    problems = ["%s: %s" % (where, p) for p in contracts.validate(SCHEMA, doc)]
    if problems:
        raise ManifestError(problems)
    out = copy.deepcopy(doc)
    for alias, name in ALIASES_TOP:
        if alias in out:
            out[name] = out.pop(alias)
    out.setdefault("capabilities", [])
    out.setdefault("compose", [])
    out.setdefault("tools", {})
    env = out.setdefault("env", {})
    env.setdefault("provides", {})
    env.setdefault("site", [])
    env.setdefault("siblings", [])
    for k in env["provides"]:
        if k in RESERVED_ENV:
            problems.append("%s: env.provides may not set %s (the runner sets it)" % (where, k))
    seen: set[str] = set()
    for n, cap in enumerate(out["capabilities"]):
        for alias, name in ALIASES_CAP:
            if alias in cap:
                cap[name] = cap.pop(alias)
        if cap["id"] in seen:
            problems.append("%s: capability id %r is given twice" % (where, cap["id"]))
        seen.add(cap["id"])
        cap.setdefault("cwd", ".")
        cap.setdefault("gate", "block")
        cap["when"] = _when(cap.get("when"))
        for k in ("tools", "needs", "affected_by", "outputs", "inputs"):
            cap.setdefault(k, [])
        cap.setdefault("regenerates", False)
        cap.setdefault("manual", False)
        if "budget_s" not in cap:
            cap["budget_s"] = RUNTIME_BUDGET.get(cap.get("runtime"), DEFAULT_BUDGET_S)
        cap.setdefault("runtime", runtime_of(cap["budget_s"]))
        v = cap.setdefault("verdict", {})
        v.setdefault("exit", 0)
        req = list(v.get("require") or [])
        if "require_count" in v:
            req.append(v.pop("require_count"))
        v["require"] = [{"pattern": r["pattern"], "min": int(r.get("min") or 1)} for r in req]
        v.setdefault("forbid", [])
        for t in cap["tools"]:
            if t not in out["tools"]:
                problems.append("%s: capability %s names tool %r, which `tools` does not declare"
                                % (where, cap["id"], t))
    paths: set[str] = set()
    for c in out["compose"]:
        c["path"] = c["path"].strip("/")
        c["when"] = _when(c.get("when"))
        if c["path"] in paths:
            problems.append("%s: compose names path %s twice" % (where, c["path"]))
        paths.add(c["path"])
    if problems:
        raise ManifestError(problems)
    return out


@dataclass
class Read:
    """One tree's manifest at one commit. status: ok | absent (no file at that commit) | invalid (the file does not
    check: `problems`) | unreadable (no mirror, the commit is not in it, or git failed: `problems`)."""
    component: str | None
    commit: str | None
    status: str
    doc: dict | None = None
    problems: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def capabilities(self) -> list[dict]:
        return list((self.doc or {}).get("capabilities") or [])

    def as_dict(self) -> dict:
        return {"component": self.component, "commit": self.commit, "status": self.status,
                "problems": list(self.problems), "sources": list(self.sources)}


# (mirror, commit) -> Read, across requests: what a commit holds never changes, so an ok, absent or invalid read
# is settled; an unreadable one (no mirror, the commit not fetched yet) is read again next time.
_SETTLED: dict[tuple, "Read"] = {}
_SETTLED_MAX = 4096


def no_fetch_env(environ=None) -> dict[str, str]:
    """Environment additions that stop git reaching any remote: lazy (promisor) fetching off, and every transport
    refused (`protocol.allow=never`, for a git that ignores GIT_NO_LAZY_FETCH). No credential helper is set."""
    import os
    env = os.environ if environ is None else environ
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    return {"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(n + 1),
            "GIT_CONFIG_KEY_%d" % n: "protocol.allow", "GIT_CONFIG_VALUE_%d" % n: "never"}


class Reader:
    """Reads manifests from the component mirrors (interfaces.md section 3), once per (component, commit).

    `git -C <mirror> cat-file -p <commit>:.nanosoc/capabilities.yaml` (the `git show` of the file at the commit):
    the file as the commit holds it, so a capability is always the one that ships with the code under test. A
    working tree is never read.

    `fetch=False` (every GET page): the mirrors are partial clones, so reading a blob
    they lack makes git fetch it from the host, with the read credential when one is configured. A page must never
    reach the network: it reads anonymously with lazy fetching off (`NO_FETCH_ENV`), and a blob the mirror lacks
    reads `unreadable` ("not fetched yet") until a command or a job reads it with `fetch=True`."""

    def __init__(self, config, *, fetch: bool = True):
        self.config = config
        self.fetch = fetch
        self._cache: dict[tuple, Read] = {}

    def at(self, component: str | None, commit: str | None, host: str | None = None) -> Read:
        key = (component, commit)
        if key not in self._cache:
            from nanosoc_rm.mirror import layout
            gkey = (str(layout.mirror_path(self.config, component)), commit) if component and commit else None
            hit = _SETTLED.get(gkey) if gkey else None
            if hit is None:
                hit = self._read(component, commit, host)
                if gkey and hit.status in ("ok", "absent", "invalid"):    # a commit's content never changes
                    if len(_SETTLED) >= _SETTLED_MAX:
                        _SETTLED.clear()
                    _SETTLED[gkey] = hit
            self._cache[key] = hit
        return self._cache[key]

    def _read(self, component: str | None, commit: str | None, host: str | None) -> Read:
        from nanosoc_rm.mirror import layout
        if not component or not commit:
            return Read(component, commit, "unreadable", problems=["no catalogued component or commit at this pin"])
        mirror = layout.mirror_path(self.config, component)
        if not (mirror / "HEAD").is_file():
            return Read(component, commit, "unreadable",
                        problems=["no mirror of %s: run `nanosoc-rm mirror sync --component %s`" % (component, component)])
        if not self.fetch:
            env = no_fetch_env()
        else:
            try:
                env = layout.git_env(self.config, host) if host else None
            except Exception:                                      # noqa: BLE001 - anonymous then
                env = None
        present = layout.run_in(mirror, "cat-file", "-e", "%s^{commit}" % commit, env=env, timeout=60)
        if not present.ok:
            return Read(component, commit, "unreadable", sources=[present.source()],
                        problems=["commit %s is not in the %s mirror (sync it, or the pin is unpushed)"
                                  % (commit[:12], component)])
        has = layout.run_in(mirror, "ls-tree", commit, "--", PATH, env=env, timeout=60)
        if not has.ok:
            return Read(component, commit, "unreadable", sources=[has.source()],
                        problems=["cannot read the tree of %s at %s: %s" % (component, commit[:12],
                                                                            has.stderr.strip()[-200:])])
        if not has.stdout.strip():
            return Read(component, commit, "absent", sources=[has.source()])
        blob = layout.run_in(mirror, "cat-file", "-p", "%s:%s" % (commit, PATH), env=env, timeout=60)
        srcs = [has.source(), blob.source()]
        if not blob.ok:
            return Read(component, commit, "unreadable", sources=srcs,
                        problems=["cannot read %s of %s at %s: %s" % (PATH, component, commit[:12],
                                                                      blob.stderr.strip()[-200:])])
        try:
            doc = parse(blob.stdout if isinstance(blob.stdout, str) else blob.stdout.decode("utf-8", "replace"),
                        where="%s@%s:%s" % (component, commit[:12], PATH))
        except ManifestError as e:
            return Read(component, commit, "invalid", problems=e.problems, sources=srcs)
        if doc["component"] != component:
            return Read(component, commit, "invalid", sources=srcs,
                        problems=["%s@%s:%s names component %r, but this repo is %r in the catalogue"
                                  % (component, commit[:12], PATH, doc["component"], component)])
        return Read(component, commit, "ok", doc=doc, sources=srcs)
