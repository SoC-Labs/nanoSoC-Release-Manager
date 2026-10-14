"""The registry loader and validator (interfaces.md section 2; README sections 1 and 3).

    load_registry(path) -> Registry          raises RegistryError(problems) on ANY problem: never partial

The registry is a directory (a checkout of the registry repo's main) laid out as README section 1 says:

    catalogue.yaml                         required   component-catalogue
    eligibility.yaml                       required   eligibility
    defects/SL-DEF-NNNN.yaml               optional   defect
    errata/<configuration>/ERR-NNN.yaml    optional   erratum
    imports.yaml                           optional   import-ledger (F5)
    tiers.yaml                             optional   eligibility#/$defs/tiers_file (CR-12: local T1/T2 commands)

Every file is read with the strict YAML reader (timestamps stay strings, a duplicate key is refused: rule 8),
must carry a `schema` version this reader knows (README section 2 rule 2), and is validated against its
schema; then the cross-file rules 1 to 15 run (registry/rules.py). Problems are collected from every file and
raised together, so one `registry validate` shows all of them.

Records come back as schema documents (plain dicts, deep copies: a caller cannot change the loaded registry).

Every regular file under defects/ and errata/ is accounted for: a file the loader would not read as a record
(another suffix, `.YAML`, a README, an editor's swap file) is a rule-7 problem, never skipped in silence.

`Registry.commit` is the registry commit the files were read at: HEAD of the git checkout, when the directory
is one, nothing under it differs from HEAD (tracked changes, staged changes, untracked files), and every file
read is in HEAD's tree with the same blob (so an ignored file, or an edit hidden from `git diff`, cannot pass).
A directory that is not a git checkout, or a dirty one, loads the same way with `commit = None`, because no
`git show <commit>:<file>` would reproduce what was read; such a registry cannot produce registry_record facts
(records.py). `Registry.path` is absolute, so that command does not depend on the caller's cwd.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

from nanosoc_rm import contracts, gitrun
from nanosoc_rm.registry import rules
from nanosoc_rm.registry.urls import Repo

__all__ = ["Problem", "RegistryError", "Registry", "load_registry", "SCHEMA_OF", "KNOWN_SCHEMA_VERSION", "Role"]

Role = Literal["canonical", "copy", "alias", "external", "ignored", "unknown"]

KNOWN_SCHEMA_VERSION = 1
SCHEMA_OF = {"catalogue.yaml": "component-catalogue", "eligibility.yaml": "eligibility",
             "imports.yaml": "import-ledger", "defects": "defect", "errata": "erratum", "tiers.yaml": "eligibility"}
POINTER_OF = {"tiers.yaml": "/$defs/tiers_file"}         # a file validated against one $defs entry
_YAML_SUFFIXES = (".yaml", ".yml")


@dataclass
class Problem:
    """One reason the registry does not load. `file` is relative to the registry root (posix), `pointer` a JSON
    pointer into it ("" for the whole file), `rule` the README section 3 rule (`rule-1`..`rule-13`), `schema`,
    `version`, `yaml` or `layout`."""
    file: str
    pointer: str
    rule: str
    message: str

    def __str__(self) -> str:
        return "%s:%s: %s [%s]" % (self.file, self.pointer, self.message, self.rule)

    def as_doc(self) -> dict:
        return asdict(self)


class RegistryError(Exception):
    """The registry has at least one problem, so it is not loaded at all (interfaces.md section 2: never
    partial). `problems` lists every one."""

    def __init__(self, problems: Iterable[Problem]):
        self.problems = list(problems)
        n = len(self.problems)
        head = "; ".join(str(p) for p in self.problems[:3])
        super().__init__("registry refused: %d problem%s: %s%s" % (n, "" if n == 1 else "s", head,
                                                                     " ..." if n > 3 else ""))


def _pointer(path: Iterable[Any]) -> str:
    return "".join("/" + rules.escape(t) for t in path)


def _read(root: Path, rel: str, schema: str, problems: list[Problem], pointer: str | None = None) -> Any:
    """The document in `rel`, or None (with its problems recorded) when it cannot be used at all."""
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        problems.append(Problem(rel, "", "layout", "cannot read the file: %s" % (getattr(e, "strerror", None) or e)))
        return None
    try:
        doc = contracts.load_yaml(text)
    except yaml.YAMLError as e:
        dup = "duplicate key" in str(e)
        problems.append(Problem(rel, "", "rule-8" if dup else "yaml",
                                ("duplicate key: %s" if dup else "not valid YAML: %s") % " ".join(str(e).split())))
        return None
    if not isinstance(doc, dict):
        problems.append(Problem(rel, "", "schema", "the document is not a mapping"))
        return None
    if doc.get("schema") != KNOWN_SCHEMA_VERSION:
        problems.append(Problem(rel, "/schema", "version",
                                "schema version %r is not one this reader knows (%d); it is refused, never guessed at "
                                "(README section 2)" % (doc.get("schema"), KNOWN_SCHEMA_VERSION)))
        return None
    for e in sorted(contracts.validator(schema, pointer).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        problems.append(Problem(rel, _pointer(e.absolute_path), "schema", e.message))
    return doc


def _record_files(root: Path, top: str) -> tuple[list[str], list[str]]:
    """(the YAML files under `top`, every other regular file under it), registry-relative and sorted."""
    d = root / top
    if not d.is_dir():
        return [], []
    found = sorted(p.relative_to(root).as_posix() for p in d.rglob("*") if p.is_file())
    return ([f for f in found if Path(f).suffix in _YAML_SUFFIXES],
            [f for f in found if Path(f).suffix not in _YAML_SUFFIXES])


def _read_files_match_head(root: Path, read: Iterable[str]) -> bool:
    """Every file in `read` (registry-relative) is in HEAD's tree under `root`, and its bytes hash to that
    blob. `git diff` and `ls-files --others --exclude-standard` cannot see an ignored file or an edit to an
    assume-unchanged or skip-worktree file; this sees both."""
    read = sorted(read)
    if not read:
        return True
    tree = gitrun.run_git("ls-tree", "-r", "-z", "HEAD", "--", ".", cwd=root, timeout=120)
    if not tree.ok:
        return False
    blobs = {}
    for entry in tree.stdout.split("\0"):
        if "\t" in entry:
            meta, name = entry.split("\t", 1)
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                blobs[name] = parts[2]
    if any(f not in blobs for f in read):
        return False
    hashed = gitrun.run_git("hash-object", "--", *read, cwd=root, timeout=120)
    return hashed.ok and hashed.stdout.split() == [blobs[f] for f in read]


def _registry_commit(root: Path, read: Iterable[str] = ()) -> tuple[str | None, str, bool]:
    """(HEAD commit or None, the directory's prefix inside its repo, dirty). None when not a git checkout, when
    it has no commit, when anything under the directory differs from HEAD, or when a file in `read` is not
    HEAD's blob (module docstring)."""
    top = gitrun.run_git("rev-parse", "--show-prefix", cwd=root, timeout=60)
    if not top.ok:
        return None, "", False
    prefix = top.stdout.strip()
    head = gitrun.run_git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", cwd=root, timeout=60)
    if not head.ok:
        return None, prefix, False
    diff = gitrun.run_git("--no-optional-locks", "diff", "--quiet", "HEAD", "--", ".", cwd=root, timeout=120)
    untracked = gitrun.run_git("--no-optional-locks", "ls-files", "--others", "--exclude-standard", "--", ".",
                               cwd=root, timeout=120)
    dirty = (diff.returncode != 0 or not untracked.ok or bool(untracked.stdout.strip())
             or not _read_files_match_head(root, read))
    return (None if dirty else head.stdout.strip()), prefix, dirty


class Registry:
    """A loaded registry. Construct it with load_registry()."""

    def __init__(self, path: Path, files: dict[str, dict], commit: str | None, prefix: str = "",
                 dirty: bool = False):
        self.path = path
        self.commit = commit                # the registry commit it was read at (module docstring)
        self.prefix = prefix                # the directory's path inside its git repo ("" at the top)
        self.dirty = dirty
        self._files = files
        cat = files["catalogue.yaml"]
        self._components = {c["id"]: c for c in cat["components"]}
        self._externals = {x["id"]: x for x in cat.get("external", [])}
        self._configurations = {c["id"]: c for c in files["eligibility.yaml"]["configurations"]}
        self._defects = {d["id"]: d for f, d in files.items() if f.startswith("defects/")}
        self._errata = {e["id"]: e for f, e in files.items() if f.startswith("errata/")}
        self._repos: dict[tuple[str, str], tuple[str | None, Role, Repo]] = {}
        for c in cat["components"]:
            self._repos[Repo.of(c["canonical"]).key()] = (c["id"], "canonical", Repo.of(c["canonical"]))
            for role, key in (("copy", "copies"), ("alias", "aliases")):
                for r in c.get(key, []):
                    self._repos[Repo.of(r).key()] = (c["id"], role, Repo.of(r))
        for x in cat.get("external", []):
            self._repos[Repo.of(x["repo"]).key()] = (x["id"], "external", Repo.of(x["repo"]))
        for x in cat.get("ignored", []):
            self._repos[Repo.of(x["repo"]).key()] = (None, "ignored", Repo.of(x["repo"]))

    def __repr__(self) -> str:
        return "<Registry %s @ %s: %d components, %d configurations, %d defects, %d errata>" % (
            self.path, (self.commit or "uncommitted")[:12], len(self._components), len(self._configurations),
            len(self._defects), len(self._errata))

    # -- records, as schema documents -------------------------------------------------------------------
    def components(self) -> list[dict]:
        """The catalogue's components, in file order."""
        return copy.deepcopy(list(self._components.values()))

    def component(self, component_id: str) -> dict | None:
        c = self._components.get(component_id)
        return copy.deepcopy(c) if c is not None else None

    def characterisations(self) -> dict:
        """CR-WF-catalogue (interfaces.md section 23.1): {component id: Characterisation} in catalogue order, the
        front page's group, type, role, wraps and wrapped_by of every component, defaults derived from class."""
        from nanosoc_rm.catalogue.model import characterise_all
        return characterise_all(self._components.values())

    def externals(self) -> list[dict]:
        return copy.deepcopy(list(self._externals.values()))

    def ignored(self) -> list[dict]:
        return copy.deepcopy(self._files["catalogue.yaml"].get("ignored", []))

    def configurations(self) -> list[dict]:
        """The registered configurations (eligibility.yaml), in file order."""
        return copy.deepcopy(list(self._configurations.values()))

    def configuration(self, configuration_id: str) -> dict | None:
        c = self._configurations.get(configuration_id)
        return copy.deepcopy(c) if c is not None else None

    def defects(self) -> list[dict]:
        return copy.deepcopy(list(self._defects.values()))

    def defect(self, defect_id: str) -> dict | None:
        d = self._defects.get(defect_id)
        return copy.deepcopy(d) if d is not None else None

    def errata(self, configuration: str | None = None) -> list[dict]:
        """Every erratum, or one configuration's."""
        return copy.deepcopy([e for e in self._errata.values()
                              if configuration is None or e["configuration"] == configuration])

    def imports(self) -> dict | None:
        """The F5 import ledger (imports.yaml), or None when the registry has none."""
        doc = self._files.get("imports.yaml")
        return copy.deepcopy(doc) if doc is not None else None

    def tiers(self, configuration_id: str) -> dict:
        """CR-12: {tier: command spec} from tiers.yaml for one configuration ({} when none is declared)."""
        doc = self._files.get("tiers.yaml") or {}
        return copy.deepcopy((doc.get("configurations") or {}).get(configuration_id) or {})

    def files(self) -> dict[str, dict]:
        """{registry-relative file: document} for every file read."""
        return copy.deepcopy(self._files)

    # -- repos --------------------------------------------------------------------------------------------
    def resolve_repo(self, repo: Any) -> tuple[str | None, Role]:
        """(id, role) of the catalogue entry holding `repo` (a Repo, {host, path} or (host, path)): the component
        id for `canonical`, `copy` and `alias`, the external's id for `external`, None for `ignored` and
        `unknown`. Paths match case-insensitively, as both hosts route them."""
        hit = self._repos.get(Repo.of(repo).key())
        if hit is None:
            return None, "unknown"
        return hit[0], hit[1]

    def catalogued_repo(self, repo: Any) -> Repo | None:
        """The catalogue's own spelling of `repo`, with an alias redirected to its component's canonical repo
        (B9); None when the catalogue does not hold it."""
        hit = self._repos.get(Repo.of(repo).key())
        if hit is None:
            return None
        cid, role, spelled = hit
        if role == "alias":
            return Repo.of(self._components[cid]["canonical"])
        return spelled

    def repos(self, component_id: str) -> list[tuple[Repo, Role]]:
        """Every repo of a component (canonical first, then copies), or of an external. Aliases are names of
        the canonical repo, not repos of their own, so they are not listed."""
        if component_id in self._externals:
            return [(Repo.of(self._externals[component_id]["repo"]), "external")]
        c = self._components.get(component_id)
        if c is None:
            raise KeyError("component %s is not in the catalogue" % component_id)
        return [(Repo.of(c["canonical"]), "canonical")] + [(Repo.of(r), "copy") for r in c.get("copies", [])]

    def canonical(self, component_id: str) -> Repo:
        return self.repos(component_id)[0][0]

    # -- facts --------------------------------------------------------------------------------------------
    def records(self, observed_at: str | None = None) -> list[dict]:
        """Every record as a registry_record observation (A3, H1). See nanosoc_rm.registry.records."""
        from nanosoc_rm.registry.records import registry_records
        return registry_records(self, observed_at=observed_at)


def load_registry(path: str | Path) -> Registry:
    """Load and validate the registry at `path`. RegistryError, with every problem, unless it is clean."""
    root = Path(path).expanduser().resolve()
    problems: list[Problem] = []
    if not root.is_dir():
        raise RegistryError([Problem("", "", "layout", "%s is not a directory" % root)])
    docs = rules.Docs()
    files: dict[str, dict] = {}
    for rel in ("catalogue.yaml", "eligibility.yaml", "imports.yaml", "tiers.yaml"):
        if not (root / rel).is_file():
            if rel not in ("imports.yaml", "tiers.yaml"):
                problems.append(Problem(rel, "", "layout", "missing: a registry holds %s (README section 1)" % rel))
            continue
        doc = _read(root, rel, SCHEMA_OF[rel], problems, POINTER_OF.get(rel))
        if doc is not None:
            files[rel] = doc
            setattr(docs, rel.split(".")[0], (rel, doc))
    for top, target in (("defects", docs.defects), ("errata", docs.errata)):
        records, others = _record_files(root, top)
        for rel in others:
            problems.append(Problem(rel, "", "rule-7", "not a record file, so it is not read: a defect lives at "
                                    "defects/<id>.yaml, an erratum at errata/<configuration>/ERR-NNN.yaml"))
        for rel in records:
            doc = _read(root, rel, SCHEMA_OF[top], problems)
            if doc is not None:
                files[rel] = doc
                target.append((rel, doc))
    problems += [Problem(*p) for p in rules.check_all(docs)]
    if problems:
        raise RegistryError(problems)
    commit, prefix, dirty = _registry_commit(root, files)
    return Registry(root, files, commit, prefix, dirty)
