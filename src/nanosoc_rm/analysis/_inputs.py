"""Everything the analysis functions read, in one place (the integrator's wiring point).

Inputs and the contract each follows:

- **Registry** (interfaces.md section 2, README section 1 layout): `catalogue.yaml`, `eligibility.yaml`,
  `defects/<id>.yaml` under `[registry] path`. Each file is read strictly (contracts.load_yaml) and checked
  against its schema. When `nanosoc_rm.registry` is built, `load_registry(path)` is ALSO called first, as the
  gate: a registry with any cross-file problem is not used (section 2: "never partial"). While that module is
  not built, only the per-file schema check applies. That fallback is the one place this package stands in
  for another module; delete it once the registry module is merged.
- **Stored trees** (section 4 "Storage"): the latest complete pin_state snapshot of a configuration, through
  `db.latest`. A configuration that was never walked has no snapshot, and the analysis cannot answer for it
  (CannotAnswer, exit 2). It never re-walks.
- **Mirrors** (section 3): `<data_dir>/mirrors/<component>.git`; one namespace per repo,
  `refs/r/<host>/<quote(path, safe="")>/{heads,tags}/*`. Read with the one git runner, `-C <mirror>`.
- **Mirror status** (section 3): the latest stored `mirror_status` of a repo (ok, last_success_at).
- **Default branch of a repo on its host.** The mirror module records it at each sync, as the host advertised
  it, in the mirror's own git config: `nanosoc-rm.<key>.defaultbranch` (mirror/layout.py; not yet in the
  section 3 text, raised as a CR candidate). `default_branch()` reads it there (through
  `nanosoc_rm.mirror.layout.default_branch` once that module is merged), then falls back to the newest stored
  pin_state host state for that repo. It never asks a host: the analysis reads stored data only, so a private
  repo needs no credential here and the name always matches the refs the mirror holds.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.gitrun import GitResult, run_git

__all__ = ["AnalysisError", "UsageProblem", "CannotAnswer", "Answer", "Component", "config_of", "registry", "Registry",
           "stored_tree", "repo_key", "ref_prefix", "mirror_path", "git", "mirror_status", "default_branch",
           "new_snapshot", "store_snapshot", "oldest", "GIT_TIMEOUT_S"]

GIT_TIMEOUT_S = 300


class AnalysisError(Exception):
    """Base of the errors an analysis function raises instead of returning documents."""

    exit_code = 2


class UsageProblem(AnalysisError, LookupError):
    """The question cannot be asked: unknown component, configuration, defect or detector; a SHA that resolves
    to nothing or to several commits. CLI exit 3 (interfaces.md section 0.2)."""

    exit_code = 3


class CannotAnswer(AnalysisError):
    """The question is valid but the data to answer it is missing: no stored tree, no mirror, an invalid
    registry, a detector that could not run. CLI exit 2."""

    exit_code = 2


class Answer(list):
    """The observations a query produced (a plain list of documents to every caller that wants only rows), plus
    `unanswered`: one sentence per part of the question the stored data could not answer (a configuration never
    walked, a pin whose protection cannot be judged). Rows for the parts that could be answered are still
    returned, so one gap never hides the rest; the CLI prints them and exits 2 (could not answer)."""

    def __init__(self, docs=(), unanswered=()):
        super().__init__(docs)
        self.unanswered: list[str] = list(unanswered)


def config_of(config: Config | str | Path | None) -> Config:
    """The service config: a loaded Config, a path to one, or None ($NANOSOC_RM_CONFIG, read now)."""
    return config if isinstance(config, Config) else load_config(config)


# ---------------------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Component:
    """A catalogue component or external, reduced to what the analysis needs."""
    id: str
    canonical: dict | None                 # the canonical repo; None for an external
    repos: tuple[dict, ...]                # every repo of it: canonical first, then copies (or the external's)
    external: bool = False

    @property
    def copies(self) -> tuple[dict, ...]:
        return self.repos[1:] if not self.external else ()

    def hosts(self) -> list[str]:
        """Hosts where the component has a repo, canonical host first."""
        out: list[str] = []
        for r in self.repos:
            if r["host"] not in out:
                out.append(r["host"])
        return out

    def repos_on(self, host: str) -> list[dict]:
        return [r for r in self.repos if r["host"] == host]


@dataclass
class Registry:
    path: Path
    catalogue: dict
    eligibility: dict
    _defects: dict[str, dict] = field(default_factory=dict)

    def component(self, component_id: str) -> Component:
        for c in self.catalogue["components"]:
            if c["id"] == component_id:
                return Component(c["id"], c["canonical"], (c["canonical"], *c.get("copies", [])))
        for e in self.catalogue.get("external", []):
            if e["id"] == component_id:
                return Component(e["id"], None, (e["repo"],), external=True)
        raise UsageProblem("unknown component %r: not in the catalogue (%s/catalogue.yaml)" % (component_id, self.path))

    def components(self) -> list[Component]:
        return [self.component(c["id"]) for c in self.catalogue["components"]]

    def configurations(self) -> list[dict]:
        return list(self.eligibility.get("configurations", []))

    def configuration(self, configuration_id: str) -> dict:
        for c in self.configurations():
            if c["id"] == configuration_id:
                return c
        raise UsageProblem("unknown configuration %r: not in %s/eligibility.yaml" % (configuration_id, self.path))

    def defect(self, defect_id: str) -> dict:
        if defect_id not in self._defects:
            f = self.path / "defects" / ("%s.yaml" % defect_id)
            if not f.is_file():
                raise UsageProblem("unknown defect %r: no %s" % (defect_id, f))
            doc = _read(f, "defect")
            if doc.get("id") != defect_id:
                raise CannotAnswer("%s holds defect %r, not %r (README section 3 rule 7)" % (f, doc.get("id"), defect_id))
            self._defects[defect_id] = doc
        return self._defects[defect_id]


def _read(path: Path, schema: str) -> Any:
    try:
        doc = contracts.load_yaml(path.read_text())
    except OSError as e:
        raise CannotAnswer("registry file %s is unreadable (%s)" % (path, e.strerror)) from None
    except Exception as e:                                  # yaml.YAMLError: duplicate key, syntax
        raise CannotAnswer("registry file %s is not valid YAML: %s" % (path, e)) from None
    problems = contracts.validate(schema, doc)
    if problems:
        raise CannotAnswer("registry file %s breaks %s.schema.json: %s" % (path, schema, "; ".join(problems[:5])))
    return doc


def _registry_gate(path: Path) -> None:
    """load_registry(path) when the registry module is built: any cross-file problem stops the analysis."""
    try:
        from nanosoc_rm.registry import RegistryError, load_registry
    except ModuleNotFoundError as e:
        if e.name != "nanosoc_rm.registry":
            raise                                         # a built module that cannot import: never hidden
        return                                            # not built yet: the per-file schema checks apply
    try:
        load_registry(path)
    except RegistryError as e:
        problems = getattr(e, "problems", []) or []
        shown = "; ".join("%s:%s: %s" % (p.file, p.pointer, p.message) for p in problems[:5])
        raise CannotAnswer("the registry at %s does not load (%d problem(s)): %s" % (path, len(problems), shown)) \
            from None


def registry(cfg: Config) -> Registry:
    """The registry named by `[registry] path`, read now."""
    path = Path(cfg.require("registry", "path"))
    if not path.is_dir():
        raise CannotAnswer("registry path %s is not a directory" % path)
    _registry_gate(path)
    return Registry(path, _read(path / "catalogue.yaml", "component-catalogue"),
                    _read(path / "eligibility.yaml", "eligibility"))


# ---------------------------------------------------------------------------------------------------------
# Stored trees
# ---------------------------------------------------------------------------------------------------------
def stored_tree(configuration: str, cfg: Config) -> list[dict]:
    """The pin_state observations of the latest complete walk of `configuration`. CannotAnswer when it was
    never walked (an empty walk is a complete, empty snapshot, and gives [])."""
    scope = "pin_state:%s" % configuration
    with db.connect(cfg) as con:
        done = con.execute("SELECT 1 FROM snapshots WHERE scope = ? AND completed_at IS NOT NULL LIMIT 1",
                           (scope,)).fetchone()
    if done is None:
        raise CannotAnswer("configuration %s has no stored tree: run `nanosoc-rm walk %s` first (interfaces.md "
                           "section 4: analysis reads the latest stored walk, never re-walks)" % (configuration, configuration))
    return db.latest("pin_state", scope + ":", config=cfg)


# ---------------------------------------------------------------------------------------------------------
# Mirrors
# ---------------------------------------------------------------------------------------------------------
def repo_key(repo: dict) -> str:
    """The namespace key of a repo in its component mirror: `<host>/<quote(path, safe="")>` (section 3)."""
    return "%s/%s" % (repo["host"], urllib.parse.quote(repo["path"], safe=""))


def ref_prefix(repo: dict) -> str:
    return "refs/r/" + repo_key(repo)


def mirror_path(cfg: Config, component_id: str) -> Path:
    return cfg.mirrors_dir / ("%s.git" % component_id)


def git(mirror: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> GitResult:
    """One read in a mirror, as `git -C <mirror> ...` (the form the contract examples record)."""
    return run_git(*args, cwd=mirror, timeout=timeout)


def mirror_status(cfg: Config, repo: dict) -> dict | None:
    """The latest stored mirror_status observation of exactly this repo, or None."""
    subject = "mirror_status:%s:%s" % (repo["host"], repo["path"])
    docs = [d for d in db.latest("mirror_status", subject, config=cfg) if d["subject"] == subject]
    return docs[-1] if docs else None


DEFAULT_BRANCH_KEY = "nanosoc-rm.%s.defaultbranch"           # mirror/layout.py's config key, `%s` = repo_key


def _recorded_default(mirror: Path, key: str) -> tuple[str | None, dict]:
    """(the default branch the mirror's last sync recorded, the read that found it)."""
    r = git(mirror, "config", "--get", DEFAULT_BRANCH_KEY % key, timeout=60)
    try:                                                    # the mirror module, once merged, owns the layout
        from nanosoc_rm.mirror.layout import default_branch as layout_default_branch
    except ModuleNotFoundError as e:
        if not (e.name or "").startswith("nanosoc_rm.mirror"):
            raise
        return (r.stdout.strip() or None) if r.ok else None, r.source()
    return layout_default_branch(mirror, key), r.source()


def default_branch(cfg: Config, mirror: Path, repo: dict) -> tuple[str | None, list[dict]]:
    """(default branch name or None, the sources that decided it). See the module docstring for the order."""
    key = repo_key(repo)
    tried: list[dict] = []
    if (mirror / "HEAD").is_file():
        name, src = _recorded_default(mirror, key)
        if name:
            return name, [src]
        tried.append(src)
    best = None
    for doc in db.latest("pin_state", config=cfg):
        for state in doc["fact"]["hosts"].values():
            if state["repo"] == repo and state["default_branch"]:
                if best is None or clock.parse(state["observed_at"]) > clock.parse(best[1]["observed_at"]):
                    best = (doc, state)
    if best is not None:
        return best[1]["default_branch"], [{"kind": "derived", "command": "default branch of %s:%s from stored %s "
                                            "(host state observed %s)" % (repo["host"], repo["path"], best[0]["subject"],
                                                                          best[1]["observed_at"])}]
    return None, tried


# ---------------------------------------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------------------------------------
def new_snapshot(scope: str) -> dict:
    """A snapshot {id, scope} for one producer run; the id is unique to the microsecond."""
    now = dt.datetime.now(dt.timezone.utc)
    return {"id": "%s@%sZ" % (scope, now.strftime("%Y-%m-%dT%H:%M:%S.%f")), "scope": scope}


def store_snapshot(scope: str, docs: list[dict], cfg: Config) -> None:
    """db.put_snapshot, except for one case. An EMPTY run gets the id `<scope>@<now to the second>` from the store,
    so a second empty run in the same second (two viewers of the same page) is refused as a duplicate id. When
    the scope's newest complete snapshot is itself empty, the store already says exactly what this run found
    (no rows, as of this second), so nothing is lost by not storing it again. Every other refusal is raised."""
    try:
        db.put_snapshot(scope, docs, config=cfg)
    except db.ObservationRefused:
        if docs:
            raise
        with db.connect(cfg) as con:
            newest = con.execute("SELECT id FROM snapshots WHERE scope = ? AND completed_at IS NOT NULL "
                                 "ORDER BY completed_at DESC LIMIT 1", (scope,)).fetchone()
            rows = con.execute("SELECT COUNT(*) FROM observations WHERE snapshot_id = ?",
                               (newest[0],)).fetchone()[0] if newest else None
        if rows != 0:
            raise


def oldest(*times: str | None) -> str:
    """The oldest of the given contract timestamps and now (A3: an envelope is never fresher than its data)."""
    best = clock.now()
    for t in times:
        if t:
            p = clock.parse(t)
            if p < best:
                best = p
    return clock.iso(best)
