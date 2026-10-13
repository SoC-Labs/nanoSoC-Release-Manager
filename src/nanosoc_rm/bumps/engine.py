"""Bump proposals in shadow mode (E1, E2, E3, E5, E7; interfaces.md section 18).

    scan(configuration, *, config=None, store=False) -> (bump_scan observation, [bump_proposal observations])

Inputs, all read, none written: the configuration's latest stored walk (section 4; never re-walked here), the
component mirrors (section 3: tags, branch tips, gitlinks, ancestry, changelogs, manifests), the registry
(catalogue, eligibility, defects) and the local job records (a compat what-if already run for the move).

Per pin of the walk (a gitlink at `path` in a consumer repo):

1. The consumer branch a pull request would target: the configuration's own branch for its root, else the
   consumer repo's default branch (as the mirror recorded it). A release/* branch is never a target (E5). The
   gitlink is read at that branch's tip in the mirror, so the patch applies to what the branch holds now.
2. The dependency's release tags (`vX.Y.Z`, `X.Y.Z`, grandfathered `vX.Y`; never a pre-release) from every repo
   of the component in its mirror, the canonical repo's tag winning a name clash. "Newer" = the tag's commit is not
   already in the pin and its version is above the newest release the pin contains.
3. The range the consumer's manifest declares for that path (nanosoc-component.yaml at the branch tip, C11), when
   it declares one: the proposal is the newest release the range allows; one above it is reported as `newest`.
4. The change: the changelog sections between the two versions with their declared class (C2), or the class the
   versioning module computes when it is built (interfaces.py); the commits between the two; whether the pin is
   an ancestor of the tag (else `diverged`).
5. Defects (registry): fixed = a `fixed_in` commit of the dependency that the tag contains and the pin does not;
   introduced = an `origin` commit likewise.
6. The matrix cell for exactly this move when one is stored (matrix module, or a finished compat what-if job).
7. Order: deepest first (E1); `waits_for` names proposals inside the dependency itself.

Frozen and silicon configurations are refused before any of this (E3, E5): their scan has no rows. The decision
is always shadow: `would` open a pull request, `auto_merge` false, `goes_to` person. The engine never writes a
branch, never pushes and never calls a host's write API: there is no bump writer credential (H3), and every git
call goes through the one runner, which refuses writes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from nanosoc_rm import clock, contracts
from nanosoc_rm.analysis import _inputs as inp
from nanosoc_rm.bumps import changelog as clog
from nanosoc_rm.bumps import interfaces, store
from nanosoc_rm.bumps.versions import RangeError, Version, allows, parse_tag
from nanosoc_rm.config import Config
from nanosoc_rm.mirror import layout
from nanosoc_rm.registry import RegistryError, load_registry

__all__ = ["scan", "scan_all", "proposal_id", "BumpUsage", "CannotAnswer", "SHADOW_BLOCKER", "REGEN_NOTE",
           "MANIFEST", "schedule_status", "writer_problems"]

MANIFEST = "nanosoc-component.yaml"
SHADOW_BLOCKER = "shadow mode (E2): the bot only records what it would do; a person applies the patch and merges"
REGEN_NOTE = ("Gitlink move only. E1 asks a bump to regenerate generated RTL: that needs the generator "
              "(NANOSOC_GEN_OK=1), which this tool never runs. After applying, a person regenerates build_soc "
              "if the consumer commits generated output, and runs the configuration's T1/T2.")
CannotAnswer = inp.CannotAnswer


class BumpUsage(inp.UsageProblem):
    """Unknown configuration or proposal id. CLI exit 3."""


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def proposal_id(consumer_repo: dict, branch: str, path: str, from_sha: str, to_sha: str) -> str:
    key = "%s:%s|%s|%s|%s|%s" % (consumer_repo["host"], consumer_repo["path"], branch, path, from_sha, to_sha)
    return "bp-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _registry_source(reg, rel: str) -> dict:
    return {"kind": "registry", "command": "git -C %s show %s:%s" % (reg.path, reg.commit or "HEAD", rel)}


def schedule_status(repo: Path | None = None) -> dict:
    """The bump schedule as shipped: disabled, naming the Phase 4 entry gate and its recorded status."""
    status = "unknown"
    try:
        from nanosoc_rm import phasegates
        root = repo or phasegates.find_repo()
        doc = contracts.load_yaml((Path(root) / phasegates.REGISTER).read_text())
        status = next((g.get("status") or "unknown" for g in doc.get("phases") or []
                       if isinstance(g, dict) and g.get("phase") == "Phase 4"), "unknown")
    except Exception:                                   # noqa: BLE001 - a deployed wheel may carry no docs/
        status = "unknown (docs/phase-gates.yaml not readable here)"
    return {"enabled": False, "gate": "Phase 4", "gate_status": str(status),
            "reason": ("ships disabled (CR-15: built and used by hand now; scheduling it is Phase 4 automation). "
                       "Enabling needs the Phase 4 entry gate in docs/phase-gates.yaml checked by a person and a CR "
                       "that adds the job to nanosoc_rm.schedule; until then run `nanosoc-rm bumps --store` by hand.")}


def writer_problems(config: Config) -> list[str]:
    """H3: no bump writer or bot tag credential may exist before the Phase 4 gate. One is a problem, never used."""
    creds = config.get("credentials") or {}
    return ["credentials.%s is configured: the bump writer does not exist before the Phase 4 gate (H3). The bump "
            "engine never reads it; remove it from the config." % n for n in ("bump_writer", "bot_tag") if n in creds]


@dataclass
class _Git:
    """Mirror reads for one scan, with the sources each proposal cites (A3)."""
    cfg: Config
    sources: list[dict] = field(default_factory=list)

    def run(self, mirror: Path, *args: str, timeout: float = 300):
        r = inp.git(mirror, *args, timeout=timeout)
        self.sources.append(r.source())
        return r

    def take(self) -> list[dict]:
        out, seen = [], set()
        for s in self.sources:
            if s["command"] not in seen:
                seen.add(s["command"])
                out.append(s)
        self.sources = []
        return out


@dataclass(frozen=True)
class _Tag:
    name: str
    version: Version
    commit: str
    repo: dict


class _Mirrors:
    """Cached reads of the component mirrors for one scan."""

    def __init__(self, cfg: Config, reg, git: _Git):
        self.cfg, self.reg, self.git = cfg, reg, git
        self._tags: dict[str, list[_Tag]] = {}
        self._manifest: dict[tuple, tuple] = {}
        self._changelog: dict[tuple, tuple] = {}

    def mirror(self, component: str) -> Path:
        return inp.mirror_path(self.cfg, component)

    def has(self, component: str) -> bool:
        return (self.mirror(component) / "HEAD").is_file()

    def tags(self, component: str) -> list[_Tag]:
        """Release tags of every repo of the component, newest version first; the canonical repo wins a clash."""
        if component in self._tags:
            return self._tags[component]
        out: dict[str, _Tag] = {}
        if self.has(component):
            canon = self.reg.canonical(component)
            r = self.git.run(self.mirror(component), "for-each-ref",
                             "--format=%(refname)%00%(objectname)%00%(*objectname)", "refs/r/")
            for line in r.stdout.splitlines() if r.ok else []:
                ref, obj, peeled = (line.split("\0") + ["", ""])[:3]
                if "/tags/" not in ref:
                    continue
                key, _, name = ref[len("refs/r/"):].partition("/tags/")
                v = parse_tag(name)
                if v is None or not v.release:
                    continue
                host, path = layout.split_key(key)
                tag = _Tag(name, v, peeled or obj, {"host": host, "path": path})
                is_canon = (host, path.lower()) == canon.key()
                if name not in out or is_canon:
                    out[name] = tag
        self._tags[component] = sorted(out.values(), key=lambda t: t.version.key(), reverse=True)
        return self._tags[component]

    def is_ancestor(self, component: str, a: str, b: str) -> bool | None:
        """a is an ancestor of (or equal to) b; None when either commit is missing from the mirror."""
        m = self.mirror(component)
        for c in (a, b):
            if not self.git.run(m, "cat-file", "-e", "%s^{commit}" % c).ok:
                return None
        return self.git.run(m, "merge-base", "--is-ancestor", a, b).returncode == 0

    def has_commit(self, component: str, sha: str) -> bool:
        return self.has(component) and self.git.run(self.mirror(component), "cat-file", "-e", "%s^{commit}" % sha).ok

    def tip(self, component: str, repo: dict, branch: str) -> str | None:
        r = self.git.run(self.mirror(component), "rev-parse", "--verify", "-q",
                         "%s/heads/%s^{commit}" % (inp.ref_prefix(repo), branch))
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    def gitlink(self, component: str, commit: str, path: str) -> str | None:
        r = self.git.run(self.mirror(component), "ls-tree", commit, "--", path)
        for line in r.stdout.splitlines() if r.ok else []:
            meta, _, name = line.partition("\t")
            parts = meta.split()
            if name == path and len(parts) == 3 and parts[0] == "160000":
                return parts[2]
        return None

    def on_repo(self, component: str, sha: str, repo: dict) -> bool:
        r = self.git.run(self.mirror(component), "for-each-ref", "--count=1", "--contains", sha,
                         "--format=%(refname)", inp.ref_prefix(repo) + "/")
        return r.ok and bool(r.stdout.strip())

    def blob(self, component: str, commit: str, path: str) -> str | None:
        """A file's text at a commit; None when the commit has no such file (a partial mirror fetches the blob
        anonymously from its promisor remote, as the walker's .gitmodules reads do)."""
        m = self.mirror(component)
        r = self.git.run(m, "ls-tree", "--name-only", commit, "--", path)
        if not r.ok or path not in r.stdout.splitlines():
            return None
        r = self.git.run(m, "cat-file", "-p", "%s:%s" % (commit, path), timeout=120)
        return r.stdout if r.ok else None

    def manifest_range(self, component: str, commit: str, path: str, dep: str) -> tuple[str | None, str | None, str | None]:
        """(declared range, source, problem) from the consumer's manifest at `commit`."""
        key = (component, commit)
        if key not in self._manifest:
            text = self.blob(component, commit, MANIFEST)
            doc, problem = None, None
            if text is not None:
                try:
                    doc = contracts.load_yaml(text)
                    errs = contracts.validate("component-manifest", doc)
                    if errs:
                        problem = "%s at %s does not meet component-manifest: %s" % (MANIFEST, commit[:12], errs[0])
                        doc = doc if isinstance(doc, dict) else None
                except Exception as e:                    # noqa: BLE001 - a bad manifest is reported, not fatal
                    doc, problem = None, "%s at %s does not parse: %s" % (MANIFEST, commit[:12], str(e)[:200])
            self._manifest[key] = (text is not None, doc, problem)
        present, doc, problem = self._manifest[key]
        if not present:
            return None, None, None
        src = "%s:%s@%s" % (component, MANIFEST, commit)
        reqs = (doc or {}).get("requires") or []
        hit = next((r for r in reqs if isinstance(r, dict) and r.get("path") == path), None) \
            or next((r for r in reqs if isinstance(r, dict) and r.get("id") == dep), None)
        rng = hit.get("range") if isinstance(hit, dict) and isinstance(hit.get("range"), str) else None
        return rng, src, problem

    def changelog(self, component: str, commit: str) -> tuple[str | None, str | None]:
        key = (component, commit)
        if key not in self._changelog:
            r = self.git.run(self.mirror(component), "ls-tree", "--name-only", commit)
            name = clog.pick_file(r.stdout.splitlines()) if r.ok else None
            self._changelog[key] = (name, self.blob(component, commit, name) if name else None)
        return self._changelog[key]

    def commits(self, component: str, a: str, b: str) -> tuple[int, list[dict]]:
        m = self.mirror(component)
        r = self.git.run(m, "rev-list", "--count", "%s..%s" % (a, b))
        count = int(r.stdout.strip()) if r.ok and r.stdout.strip().isdigit() else 0
        r = self.git.run(m, "log", "--no-merges", "-n", "20", "--format=%H%x1f%s", "%s..%s" % (a, b))
        shown = []
        for line in r.stdout.splitlines() if r.ok else []:
            sha, _, subj = line.partition("\x1f")
            if len(sha) == 40:
                shown.append({"commit": sha, "subject": subj[:200]})
        return count, shown


# ---------------------------------------------------------------------------------------------------------
# One configuration
# ---------------------------------------------------------------------------------------------------------
def _load(cfg: Config):
    path = Path(cfg.require("registry", "path"))
    if not path.is_dir():
        raise CannotAnswer("registry path %s is not a directory" % path)
    try:
        return load_registry(path)
    except RegistryError as e:
        shown = "; ".join(str(p) for p in (getattr(e, "problems", None) or [])[:5])
        raise CannotAnswer("the registry at %s does not load: %s" % (path, shown)) from None


def _refusal(conf: dict) -> str | None:
    if conf.get("silicon"):
        return ("silicon configuration (%s %s): never proposed (E3, E5)"
                % (conf["silicon"].get("chip"), conf["silicon"].get("revision")))
    if conf.get("frozen"):
        return "frozen configuration: never proposed (E3, E5)"
    return None


def _version_of(tags: list[_Tag], mirrors: _Mirrors, component: str, commit: str) -> _Tag | None:
    """The newest release tag the commit contains."""
    for t in tags:
        if t.commit == commit or mirrors.is_ancestor(component, t.commit, commit):
            return t
    return None


def _tagref(t: _Tag | None, commit: str, repo: bool = False) -> dict:
    d = {"commit": commit, "tag": t.name if t else None, "version": str(t.version) if t else None,
         "exact": t is not None and t.commit == commit}
    if t is not None and t.version.two_part:
        d["grandfathered"] = True
    if repo and t is not None:
        d["repo"] = t.repo
    return d


def _class(dep: str, frm: str, to: _Tag, frm_tag: _Tag | None, mirrors: _Mirrors, cfg: Config) -> tuple[dict, list[dict]]:
    """The change block: changelog excerpt, class, commits, divergence."""
    extra: list[dict] = []
    name, text = mirrors.changelog(dep, to.commit)
    secs = clog.excerpt(text, frm_tag.version if frm_tag else None, to.version) if text else []
    body = "\n\n".join(s for _, s in secs)
    truncated = len(body) > clog.MAX_EXCERPT
    declared = [clog.declared_class(s) for _, s in secs]
    cls, src, detail = "unknown", "none", "no changelog section names a class between the two versions"
    if secs and all(declared):
        cls, src = clog.most_severe(declared), "changelog"
        detail = "declared in %s (most severe of %d section%s)" % (name, len(secs), "" if len(secs) == 1 else "s")
    elif not text:
        detail = "the tagged commit has no changelog (%s)" % ", ".join(clog.NAMES)
    classify = interfaces.provider(interfaces.CLASSIFIER)
    if classify is not None:
        got = classify(dep, frm, to.commit, config=cfg)
        if got and got.get("class") in clog.SEVERITY:
            cls, src, detail = got["class"], "classifier", str(got.get("detail") or "computed by the classifier (C3)")
            extra += list(got.get("sources") or [])
    count, shown = mirrors.commits(dep, frm, to.commit)
    anc = mirrors.is_ancestor(dep, frm, to.commit)
    return ({"class": cls, "class_source": src, "class_detail": detail,
             "changelog": {"file": name, "excerpt": body[:clog.MAX_EXCERPT], "truncated": truncated},
             "commits": {"count": count, "shown": shown}, "diverged": anc is False}, extra)


def _defects(reg, dep: str, frm: str, to: str, mirrors: _Mirrors) -> dict:
    fixed, introduced, unjudged = [], [], []

    def moved_in(sha: str) -> bool | None:
        a, b = mirrors.is_ancestor(dep, sha, to), mirrors.is_ancestor(dep, sha, frm)
        if a is None or b is None:
            return None
        return a and not b

    for d in reg.defects():
        forms, commits = [], []
        for form in d.get("forms") or []:
            for fx in form.get("fixed_in") or []:
                if fx.get("component") != dep:
                    continue
                hit = moved_in(fx["commit"])
                if hit is None:
                    unjudged.append("%s form %s fixed_in %s" % (d["id"], form.get("id"), fx["commit"][:12]))
                elif hit:
                    forms.append(str(form.get("id")))
                    commits.append(fx["commit"])
        if commits:
            fixed.append({"id": d["id"], "title": d.get("title", ""), "forms": sorted(set(forms)),
                          "commits": sorted(set(commits))})
        origins = []
        for o in d.get("origin") or []:
            if o.get("component") != dep:
                continue
            hit = moved_in(o["commit"])
            if hit is None:
                unjudged.append("%s origin %s" % (d["id"], o["commit"][:12]))
            elif hit:
                origins.append(o["commit"])
        if origins:
            introduced.append({"id": d["id"], "title": d.get("title", ""), "commits": sorted(set(origins))})
    return {"fixed": fixed, "introduced": introduced, "unjudged": unjudged}


def _matrix(cid: str, tree_paths: list[str], to: str, cfg: Config) -> dict:
    pins = ["%s=%s" % (tp, to) for tp in tree_paths]
    out = {"verdict": None, "source": None, "observed_at": None, "href": None, "pins": pins}
    cell = interfaces.provider(interfaces.MATRIX)
    if cell is not None:
        got = cell(cid, {tp: to for tp in tree_paths}, config=cfg)
        if got and got.get("verdict"):
            return dict(out, verdict=got["verdict"], source="matrix", observed_at=got.get("observed_at"),
                        href=got.get("href"))
    try:
        from nanosoc_rm import jobs
        records = jobs.jobs(cfg, limit=500, kind="compat", configuration=cid, states=("done",))
    except Exception:                                   # noqa: BLE001 - no job store yet: no stored result
        records = []
    for j in records:                                   # newest first
        if sorted(j["params"].get("pins") or []) == sorted(pins) and j.get("verdict"):
            at = j.get("finished_at")
            return dict(out, verdict=j["verdict"], source="compat-job",
                        observed_at=at if at and at.endswith("Z") else None, href="/compat?job=%d" % j["id"])
    return out


def _blockers(change: dict, matrix: dict, defects: dict, state_reason: str | None, auto_merge: dict,
              consumer_component: str) -> list[str]:
    b = [SHADOW_BLOCKER]
    b.append("auto_merge in eligibility.yaml is empty (E2: switched on per class only after zero disagreements)"
             if not auto_merge.get(consumer_component) else
             "auto_merge names %s for %s, but shadow mode ignores it until the Phase 4 gate"
             % (", ".join(auto_merge[consumer_component]), consumer_component))
    if change["class"] not in ("fix", "feature"):
        b.append("change class %s: only fix or feature may auto-merge (E3)" % change["class"])
    if matrix["verdict"] != "PASS":
        b.append("T0-T2 on the exact commits: %s (E3 needs PASS)" % (matrix["verdict"] or "no result stored"))
    if change["diverged"]:
        b.append("diverged: the pin is not an ancestor of the tag, so the move drops commits (E3)")
    for d in defects["introduced"]:
        b.append("introduces %s (E3: no defect detector may newly fire)" % d["id"])
    if state_reason:
        b.append(state_reason)
    b.append("build-affecting unknown until the C7 file-list diff exists: T3 may be needed first (E3)")
    return b


def _row(fact: dict, status: str, note: str, consumer: str | None = None, tag: str | None = None,
         proposal: str | None = None) -> dict:
    child = fact.get("child") or {}
    return {"tree_path": fact["tree_paths"][0], "component": child.get("component"), "commit": child.get("commit"),
            "consumer": consumer, "status": status, "tag": tag, "proposal": proposal, "note": note}


def scan(configuration: str, *, config: Config | str | Path | None = None, store_result: bool = False,
         now: str | None = None) -> tuple[dict, list[dict]]:
    """(bump_scan observation, bump_proposal observations, deepest first) for one configuration."""
    cfg = inp.config_of(config)
    reg = _load(cfg)
    conf = reg.configuration(configuration)
    if conf is None:
        raise BumpUsage("unknown configuration %r: not in %s/eligibility.yaml" % (configuration, reg.path))
    auto_merge = (reg.files().get("eligibility.yaml") or {}).get("auto_merge") or {}
    problems = writer_problems(cfg)
    if auto_merge:
        problems.append("eligibility.yaml auto_merge is not empty (%s) before the Phase 4 gate: ignored, shadow mode "
                        "only (E2)" % ", ".join(sorted(auto_merge)))
    reg_src = _registry_source(reg, "eligibility.yaml")
    refused = _refusal(conf)
    sched = schedule_status()
    if refused:
        obs = _scan_doc(configuration, False, refused, None, [], [], problems, auto_merge, sched, now or clock.iso(),
                        [reg_src])
        if store_result:
            store.record_scan(cfg, obs)
        return obs, []

    tree = inp.stored_tree(configuration, cfg)                   # CannotAnswer when never walked
    walk_at = min((d["observed_at"] for d in tree), key=clock.parse) if tree else (now or clock.iso())
    snap = tree[0]["snapshot"]["id"] if tree else "pin_state:%s (empty)" % configuration
    walk_src = {"kind": "derived", "command": "nanosoc-rm walk %s (stored snapshot %s)" % (configuration, snap)}
    git = _Git(cfg)
    mirrors = _Mirrors(cfg, reg, git)
    root_comp = conf["component"]
    rows, props, seen = [], {}, {}
    for doc in sorted(tree, key=lambda d: (-d["fact"]["depth"], d["fact"]["tree_paths"][0])):
        f = doc["fact"]
        child, parent = f.get("child") or {}, f["parent"]
        dep, consumer = child.get("component"), parent["component"]
        if f.get("state") in ("LOCAL_URL", "NO_GITLINK") or not child.get("commit"):
            rows.append(_row(f, "no-gitlink", "not a gitlink pin (%s)" % f.get("state"), consumer))
            continue
        if child.get("catalogued_as") == "external":
            rows.append(_row(f, "external", "a third-party project: not proposed (E1 covers catalogue components)",
                             consumer))
            continue
        if child.get("catalogued_as") != "component" or not dep or reg.component(dep) is None:
            rows.append(_row(f, "not-catalogued", "the pinned URL names no catalogue component", consumer))
            continue
        # 1. the consumer branch
        if consumer == root_comp and f["depth"] == 1:
            branch = (conf.get("ref") or {}).get("branch")
            if not branch:
                rows.append(_row(f, "consumer-not-a-branch", "the configuration follows a tag (%s): no branch to bump"
                                 % (conf.get("ref") or {}).get("tag"), consumer))
                continue
        else:
            branch, _ = inp.default_branch(cfg, mirrors.mirror(consumer), parent["repo"])
            if not branch:
                rows.append(_row(f, "consumer-not-mirrored", "the default branch of %s:%s is not known (sync its mirror)"
                                 % (parent["repo"]["host"], parent["repo"]["path"]), consumer))
                continue
        if branch.startswith("release/"):
            rows.append(_row(f, "release-branch", "%s is a release branch: never bumped automatically (E5)" % branch,
                             consumer))
            continue
        tip = mirrors.tip(consumer, parent["repo"], branch)
        if tip is None:
            rows.append(_row(f, "consumer-not-mirrored", "%s:%s branch %s is not in the mirror (sync it)"
                             % (parent["repo"]["host"], parent["repo"]["path"], branch), consumer))
            continue
        frm = mirrors.gitlink(consumer, tip, f["path"])
        if frm is None:
            rows.append(_row(f, "consumer-path-gone", "%s at %s (tip of %s) has no gitlink at %s"
                             % (consumer, tip[:12], branch, f["path"]), consumer))
            continue
        okey = (parent["repo"]["host"], parent["repo"]["path"].lower(), branch, f["path"])
        if okey in seen:
            first_path, first = seen[okey]
            if first in props:
                props[first]["tree_paths"].append(f["tree_paths"][0])
            rows.append(_row(f, "duplicate", "the same gitlink in the same consumer branch as %s: one move"
                             % first_path, consumer, proposal=first if first in props else None))
            continue
        seen[okey] = (f["tree_paths"][0], None)
        # 2. release tags past the pin
        tags = mirrors.tags(dep)
        if not tags:
            rows.append(_row(f, "no-release-tag", "%s has no release tag (vX.Y.Z) in its mirror (C1, C10)" % dep, consumer))
            continue
        if not mirrors.has_commit(dep, frm):
            rows.append(_row(f, "pin-not-in-mirror", "%s is not in the %s mirror: cannot judge (sync it)"
                             % (frm[:12], dep), consumer))
            continue
        frm_tag = _version_of(tags, mirrors, dep, frm)
        newer = [t for t in tags if t.commit != frm and not mirrors.is_ancestor(dep, t.commit, frm)
                 and (frm_tag is None or t.version.key() > frm_tag.version.key())]
        if not newer:
            rows.append(_row(f, "up-to-date", "no release past %s" % (frm_tag.name if frm_tag else frm[:12]), consumer,
                             tag=frm_tag.name if frm_tag else None))
            continue
        # 3. the declared range
        rng, rng_src, rng_problem = mirrors.manifest_range(consumer, tip, f["path"], dep)
        allowed, range_allows = newer, None
        if rng is not None:
            try:
                allowed = [t for t in newer if allows(rng, t.version)]
                range_allows = bool(allowed)
            except RangeError as e:
                rng_problem, allowed = "range %r does not parse: %s" % (rng, e), []
        if not allowed:
            rows.append(_row(f, "held-by-range", "%s is newer, but %s" % (
                newer[0].name, rng_problem or "%s declares %s for %s" % (consumer, rng, f["path"])), consumer,
                tag=newer[0].name))
            continue
        to = allowed[0]
        # 4.-6.
        change, extra = _class(dep, frm, to, frm_tag, mirrors, cfg)
        defects = _defects(reg, dep, frm, to.commit, mirrors)
        state, reason = "proposed", None
        if not mirrors.on_repo(dep, to.commit, child["repo"]):
            state, reason = "blocked", ("%s (%s) is not on %s:%s, the repo %s's .gitmodules names: a person moves "
                                        "the URL or pushes the tag there first" % (to.name, to.commit[:12],
                                        child["repo"]["host"], child["repo"]["path"], consumer))
        pid = proposal_id(parent["repo"], branch, f["path"], frm, to.commit)
        seen[okey] = (f["tree_paths"][0], pid)
        props[pid] = {
            "id": pid, "configuration": configuration, "tree_paths": [f["tree_paths"][0]], "depth": f["depth"],
            "order": 0,
            "consumer": {"component": consumer, "repo": parent["repo"], "branch": branch, "base": tip,
                         "configured": parent["commit"], "at_configured": tip == parent["commit"]},
            "path": f["path"], "dependency": {"component": dep, "repo": child["repo"]},
            "from": _tagref(frm_tag, frm),
            "to": _tagref(to, to.commit, repo=True),
            "newest": _tagref(newer[0], newer[0].commit, repo=True) if newer[0] is not to else None,
            "range": {"declared": rng, "source": rng_src, "allows": range_allows, "problem": rng_problem},
            "change": change, "defects": defects, "matrix": None, "patch": None, "decision": None,
            "waits_for": [], "state": state, "reason": reason,
            "_frm_tag": frm_tag, "_sources": git.take() + extra,
        }
        rows.append(_row(f, "proposal" if state == "proposed" else "blocked",
                         "%s -> %s%s" % (frm_tag.name if frm_tag else frm[:12], to.name,
                                         "" if state == "proposed" else ": " + reason), consumer, tag=to.name,
                         proposal=pid))

    docs = _finish(configuration, props, rows, cfg, auto_merge, walk_at, [walk_src, reg_src])
    obs = _scan_doc(configuration, True, None, {"snapshot": snap, "observed_at": walk_at, "pins": len(tree)}, rows,
                    [d["fact"]["id"] for d in docs], problems, auto_merge, sched, walk_at,
                    [walk_src, reg_src] + git.take())
    if store_result:
        store.record(cfg, docs, configuration)
        store.record_scan(cfg, obs)
    return obs, docs


def _finish(cid: str, props: dict, rows: list[dict], cfg: Config, auto_merge: dict, walk_at: str,
            base_sources: list[dict]) -> list[dict]:
    """Order deepest first, link waits_for, add matrix, patch and decision; build the observations."""
    order = sorted(props.values(), key=lambda p: (-p["depth"], p["tree_paths"][0]))
    docs = []
    for n, p in enumerate(order, 1):
        p["order"] = n
        p["waits_for"] = [q["id"] for q in order if q["consumer"]["component"] == p["dependency"]["component"]
                          and q["id"] != p["id"]]
        p["tree_paths"] = sorted(set(p["tree_paths"]))
        p["matrix"] = _matrix(cid, p["tree_paths"], p["to"]["commit"], cfg)
        p["patch"] = {"file": "%s.diff" % p["id"], "regenerated": False, "note": REGEN_NOTE}
        p["decision"] = {"mode": "shadow", "would": "open-pull-request" if p["state"] == "proposed" else "hold",
                         "auto_merge": False, "goes_to": "person",
                         "blockers": _blockers(p["change"], p["matrix"], p["defects"], p["reason"], auto_merge,
                                               p["consumer"]["component"])}
        sources = list(base_sources) + p.pop("_sources")
        p.pop("_frm_tag", None)
        if p["matrix"]["source"] == "compat-job":
            sources.append({"kind": "derived", "command": "nanosoc-rm compat %s %s (local job record)"
                            % (cid, " ".join("--pin " + x for x in p["matrix"]["pins"]))})
        obs = {"schema": 1, "kind": "bump_proposal", "subject": "bump_proposal:%s:%s" % (cid, p["id"]),
               "observed_at": walk_at, "sources": sources, "fact": p}
        contracts.require_valid("observation", obs)
        docs.append(obs)
    return docs


def _scan_doc(cid, eligible, refused, walk, rows, pids, problems, auto_merge, sched, at, sources) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    obs = {"schema": 1, "kind": "bump_scan", "subject": "bump_scan:%s" % cid, "observed_at": at,
           "sources": sources or [{"kind": "derived", "command": "nanosoc-rm bumps %s" % cid}],
           "fact": {"configuration": cid, "eligible": eligible, "refused": refused, "walk": walk, "rows": rows,
                    "proposals": pids, "counts": counts, "auto_merge": auto_merge, "schedule": sched,
                    "problems": problems}}
    contracts.require_valid("observation", obs)
    return obs


def scan_all(*, config: Config | str | Path | None = None, store_result: bool = False) -> tuple[list[dict], list[dict], list[str]]:
    """Every registered configuration: (scans, proposals, unanswered sentences)."""
    cfg = inp.config_of(config)
    reg = _load(cfg)
    scans, props, unanswered = [], [], []
    for c in reg.configurations():
        try:
            s, p = scan(c["id"], config=cfg, store_result=store_result)
        except CannotAnswer as e:
            unanswered.append("%s: %s" % (c["id"], e))
            continue
        scans.append(s)
        props += p
    return scans, props, unanswered


def dumps(doc: dict) -> str:
    return json.dumps(doc, separators=(",", ":"))
