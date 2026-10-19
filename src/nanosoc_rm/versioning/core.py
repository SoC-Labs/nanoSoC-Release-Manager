"""The versioning capabilities as functions that return observations (interfaces.md sections 0.1 and 14b).

    changelog_lint(target, *, ref, path, config, store) -> changelog_lint observation        C2 (and C5's lint rule)
    plan(target, *, ref, path, config, store, regenerator) -> version_plan observation       C1, C3, C6-C9, C11, C12
    tag_check(target, *, config, store, alerts) -> tag_check observation                     C10
    manifest_init(target, *, ref, path, config) -> (manifest_proposal observation, patch)    C11
    overview(config) -> [stored version_plan per catalogue component]                        the /versions page

Every observation is validated by nanosoc_rm.contracts before it is returned (`observe`); `store=True` puts it
in the store as an event (README section 4 rows added by CR-VER).
"""
from __future__ import annotations

from nanosoc_rm import clock, contracts
from nanosoc_rm.versioning import changelog as cl
from nanosoc_rm.versioning import classify as cf
from nanosoc_rm.versioning import manifest as mf
from nanosoc_rm.versioning import semver
from nanosoc_rm.versioning import tags as tagmod
from nanosoc_rm.versioning.repo import RepoView, VersioningUsage, baseline, open_repo, registry_policy

__all__ = ["KINDS", "observe", "changelog_lint", "plan", "tag_check", "manifest_init", "overview", "stored",
           "subject_key", "verdict_of_plan", "yanked_versions"]

KINDS = ("changelog_lint", "version_plan", "tag_check", "manifest_proposal", "tool_release_draft", "yank_proposal",
         "defect_trailers")


def subject_key(fact: dict) -> str:
    """`<target>:<commit>` (`<commit>+worktree` for a work-tree read, `worktree` outside git), as quality's."""
    c = fact.get("commit") or "worktree"
    return "%s:%s%s" % (fact["target"], c, "+worktree" if fact.get("worktree") and fact.get("commit") else "")


def observe(kind: str, fact: dict, sources: list[dict], command: str, *, subject: str | None = None) -> dict:
    """The observation envelope (A3), validated against the contracts; ValueError when it breaks them."""
    seen, srcs = set(), []
    for s in [{"kind": "derived", "command": command}] + list(sources):
        k = (s["kind"], s["command"])
        if k not in seen:
            seen.add(k)
            srcs.append(s)
    doc = {"schema": 1, "kind": kind, "subject": subject or "%s:%s" % (kind, subject_key(fact)),
           "observed_at": clock.iso(), "sources": srcs[:60], "fact": fact}
    problems = contracts.validate_observation(doc)
    if problems:
        raise ValueError("%s breaks the observation contract: %s" % (kind, "; ".join(problems[:6])))
    return doc


def _store(obs: dict, config) -> None:
    from nanosoc_rm import db
    db.put(obs, config=config)


def _base_fact(view: RepoView) -> dict:
    return {"target": view.target, "commit": view.commit, "ref": view.ref, "worktree": view.worktree}


def yanked_versions(policy: dict, component: str) -> set[str]:
    """G6: versions of `component` the registry's versioning.yaml records as yanked."""
    return {str(y["version"]) for y in policy.get("yanked") or [] if y.get("component") == component}


def _confirmed_baselines(policy: dict, component: str) -> set[str]:
    """Grandfathered tags a person confirmed as `component`'s version baseline (versioning.yaml, review item 4)."""
    return {g["tag"] for g in policy.get("grandfathered") or [] if g.get("component") == component
            and g.get("baseline")}


def _grandfathered(policy: dict, component: str) -> set[str]:
    return {g["tag"] for g in policy.get("grandfathered") or [] if g.get("component") == component}


# ---------------------------------------------------------------------------------------------------------
# C2: the changelog linter
# ---------------------------------------------------------------------------------------------------------
def _lint(view: RepoView, policy: dict) -> dict:
    text = view.head_text(cl.FILE)
    tags = view.canonical_tags() if view.git_dir is not None else None
    findings = cl.lint(text, tags=tags, grandfathered=_grandfathered(policy, view.target) | _all_gf(tags, policy),
                       base_url=view.base_url)
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    parsed = cl.parse(text) if text is not None else None
    unrel = cl.unreleased_classes(parsed) if parsed else {}
    return {"path": cl.FILE, "present": text is not None, "findings": findings,
            "counts": {"error": errors, "warning": warnings},
            "unreleased": {k: len(v) for k, v in unrel.items()},
            "releases": [s.version_text for s in parsed.releases if s.version_text] if parsed else [],
            "verdict": "FAIL" if errors else ("WARN" if warnings else "PASS")}


def _all_gf(tags: dict | None, policy: dict) -> set[str]:
    """Tags CL12 does not ask a section for: grandfathered by date (before versioning.yaml tag_scheme_from) or
    every existing tag while the scheme is not adopted."""
    if not tags:
        return set()
    frm = policy.get("tag_scheme_from")
    if not frm:
        return set(tags)
    return {t for t, i in tags.items() if i.get("created_at") and clock.parse(i["created_at"]) < clock.parse(frm)}


def _local_never_stored(path, store: bool, alerts: bool = False) -> None:
    """Review item 5: a read of a local checkout (`--path`) is never stored as, or alerted for, the component's
    answer; only a read of its mirror is."""
    if path is not None and (store or alerts):
        raise VersioningUsage("--store and --alerts take a read of the component's mirror, not of a local checkout "
                              "(--path): a local work tree or commit is never stored as the component's answer")


def changelog_lint(target: str | None, *, ref: str | None = None, path=None, config=None, store: bool = False) -> dict:
    """C2: the changelog_lint observation of `target` at `ref` (or the checkout `path`)."""
    _local_never_stored(path, store)
    view = open_repo(target, ref=ref, path=path, config=config)
    policy, psrc = registry_policy(view.config)
    fact = dict(_base_fact(view), **_lint(view, policy))
    obs = observe("changelog_lint", fact, view.sources + psrc,
                  "nanosoc-rm changelog lint %s%s" % (target or "--path %s" % path, " --ref %s" % ref if ref else ""))
    if store and view.config is not None:
        _store(obs, view.config)
    return obs


# ---------------------------------------------------------------------------------------------------------
# C1, C3, C6-C9, C11, C12: the version plan ("version next")
# ---------------------------------------------------------------------------------------------------------
def _reexports(view: RepoView, base_commit: str | None, reexports: list[str]) -> list[dict]:
    """C6: for each re-exported component whose gitlink moved since the baseline, the class it passes on (the
    most severe class of the child's changelog entries added between the two pins)."""
    if not reexports or not base_commit or not view.commit or view.registry is None:
        return []
    from nanosoc_rm.analysis import inputs as inp
    from nanosoc_rm.quality.source import parse_gitmodules
    from nanosoc_rm.versioning.repo import component_for_url, read_files
    gm = view.head_text(".gitmodules") or ""
    names = parse_gitmodules(gm)
    urls, cur = {}, None
    for line in gm.splitlines():
        s = line.strip()
        if s.startswith("[submodule"):
            cur = s.split('"')[1] if '"' in s else None
        elif s.startswith("url") and "=" in s and cur:
            urls[cur] = s.split("=", 1)[1].strip()
    path_of = {}
    for name, p in names.items():
        cid = component_for_url(urls.get(name, ""), view.config, view.registry, view.canonical)
        if cid:
            path_of.setdefault(cid, p)
    now, then = view.gitlinks(view.commit), view.gitlinks(base_commit)
    out = []
    for cid in reexports:
        p = path_of.get(cid)
        if p is None:
            out.append({"component": cid, "from": None, "to": None, "class": None,
                        "detail": "no submodule of %s in .gitmodules" % cid})
            continue
        a, b = then.get(p), now.get(p)
        if a == b:
            continue
        cls = None
        mirror = inp.mirror_path(view.config, cid)
        if a and b and (mirror / "HEAD").is_file():
            new = read_files(mirror, b, [cl.FILE], view.sources)[cl.FILE]
            old = read_files(mirror, a, [cl.FILE], view.sources)[cl.FILE]
            if new is not None:
                olds = {(e.cls, e.text) for s in cl.parse(old or "").sections for e in s.entries}
                added = [e.cls for s in cl.parse(new).sections for e in s.entries if (e.cls, e.text) not in olds]
                cls = semver.max_class(added) or ("none" if old is not None else None)
        out.append({"component": cid, "from": a, "to": b, "class": cls,
                    "detail": "pin moved; class from its changelog" if cls else "pin moved; its class is unmeasured"})
    return out


def verdict_of_plan(fact: dict) -> str:
    """PASS: a next version from a complete class; WARN: from a partial class (some C3 signal unmeasured);
    UNVERIFIED: no class at all; FAIL: a finding (a 1.0.0 that C9 refuses, a next version already tagged)."""
    if fact["findings"]:
        return "FAIL"
    c = fact["classification"]
    if c["class"] is None or (fact.get("current") or {}).get("ambiguous"):
        return "UNVERIFIED"
    return "PASS" if c["status"] == "complete" else "WARN"


def _plan(view: RepoView, policy: dict, regenerator=None) -> dict:
    yanked = yanked_versions(policy, view.target)
    tags = view.canonical_tags() if view.git_dir is not None else {}
    base = baseline(tags, yanked, _confirmed_baselines(policy, view.target))
    text = view.head_text(cl.FILE)
    parsed = cl.parse(text) if text is not None else None
    unrel = cl.unreleased_classes(parsed) if parsed else {}
    lint = _lint(view, policy)
    api_now = view.head_text(mf.API_FILE)
    api_then = view.files_at(base["commit"], [mf.API_FILE])[mf.API_FILE] if base["commit"] else None
    man = mf.status(view)
    count, messages = view.commits(base["commit"], view.commit) if view.commit else (None, [])
    passed = _reexports(view, base["commit"], man["reexports"])
    from nanosoc_rm.versioning.consumers import regen_signals
    consumers, build = regen_signals(view.target, base["commit"], view.commit, view.config, regenerator)
    signals = [cf.declared_signal(unrel, text is not None),
               cf.api_signal(api_then, api_now),
               consumers,
               cf.reexport_signal(passed),
               build]
    klass = cf.classify(signals)
    findings: list[str] = []
    notes = list(base["notes"])
    current = semver.parse(base["version"]) if base["version"] else None
    nxt, reason = None, None
    if count == 0 and base["commit"]:
        reason = "no commit since %s: nothing to release" % base["tag"]
    elif base.get("ambiguous"):
        reason = ("baseline ambiguous: %s is the highest grandfathered version but not the newest tag; a person "
                  "confirms the baseline (versioning.yaml grandfathered[].baseline: true)" % base["tag"])
    elif klass["class"] is None:
        reason = "class unmeasured: no changelog class declared and no signal measured"
    elif klass["class"] == "none":
        reason = "every change is class none: no release"
    elif current is None:
        declared = man["version"] if man["present"] and man["valid"] else _pyproject_version(view)
        nxt = declared or "0.1.0"
        reason = "first release: %s" % ("the version the repo declares" if declared else "0.1.0 (C1)")
    else:
        nv = semver.bump(current, klass["class"])
        nxt = str(nv) if nv else None
        reason = "%s change after %s (C1%s)" % (klass["class"], base["version"],
                                                 ", Cargo pre-1.0 rule" if current.major == 0 else "")
    if nxt and lint["verdict"] == "FAIL":                       # review item 3: no class rests on an unlinted changelog
        findings.append("changelog lint FAIL (%d error(s)%s): a release needs a classed changelog that lints clean "
                        "(C2); see `nanosoc-rm changelog lint`" % (
                            lint["counts"]["error"], ", %d unclassed entr%s" % (
                                len(unrel["unclassed"]), "y" if len(unrel["unclassed"]) == 1 else "ies")
                            if unrel.get("unclassed") else ""))
    if nxt and ("v" + nxt) in tags:
        findings.append("v%s already exists on the canonical host" % nxt)
    if nxt and nxt in yanked:
        findings.append("%s is yanked (G6): a yanked version is never released again" % nxt)
    ready = None
    if nxt and semver.parse(nxt).major >= 1 and (current is None or current.major == 0):
        ready = mf.readiness(view, changelog_clean=lint["verdict"] == "PASS", api_md=api_now is not None,
                             manifest=_manifest_doc(view), soc_release_using=None)
        if not ready["ready"]:
            findings.append("C9 refuses 1.0.0: %s" % "; ".join(i["item"] for i in ready["items"] if i["ok"] is not True))
    fact = dict(_base_fact(view), **{
        "repo_class": view.comp_class,
        "current": dict({k: base[k] for k in ("tag", "version", "kind", "commit", "created_at")},
                        **({"ambiguous": True} if base.get("ambiguous") else {})),
        "grandfathered": base["grandfathered"][:20],
        "commits_since": count,
        "unreleased": {k: [e["text"] for e in v][:50] for k, v in unrel.items()},
        "classification": klass,
        "hint": cf.commit_hint(messages),
        "next": {"version": nxt, "reason": reason},
        "changelog": {"present": text is not None, "verdict": lint["verdict"], "counts": lint["counts"]},
        "manifest": {k: man[k] for k in ("present", "valid", "version", "problems")},
        "api_md": api_now is not None,
        "yanked": sorted(yanked),
        "readiness_1_0": ready,
        "notes": notes,
        "findings": findings,
    })
    fact["verdict"] = verdict_of_plan(fact)
    return fact


def _manifest_doc(view: RepoView) -> dict | None:
    t = view.head_text(mf.FILE)
    try:
        return contracts.load_yaml(t) if t else None
    except Exception:                                              # noqa: BLE001
        return None


def _pyproject_version(view: RepoView) -> str | None:
    """A Python repo's declared [project] version (nanosoc_gen ships a wheel, G1), when it is SemVer."""
    t = view.head_text("pyproject.toml")
    if not t:
        return None
    try:
        import tomllib
        v = (tomllib.loads(t).get("project") or {}).get("version")
        return str(semver.parse(str(v))) if v else None
    except Exception:                                              # noqa: BLE001
        return None


def plan(target: str | None, *, ref: str | None = None, path=None, config=None, store: bool = False,
         regenerator=None) -> dict:
    """`nanosoc-rm version next`: the version_plan observation (current tag, unreleased entries by class, the
    class and its signals, the next version, manifest and API.md presence, 1.0.0 readiness)."""
    _local_never_stored(path, store)
    view = open_repo(target, ref=ref, path=path, config=config)
    policy, psrc = registry_policy(view.config)
    fact = _plan(view, policy, regenerator)
    obs = observe("version_plan", fact, view.sources + psrc,
                  "nanosoc-rm version next %s%s" % (target or "--path %s" % path, " --ref %s" % ref if ref else ""))
    if store and view.config is not None:
        _store(obs, view.config)
    return obs


# ---------------------------------------------------------------------------------------------------------
# C10: tag names
# ---------------------------------------------------------------------------------------------------------
def tag_check(target: str | None, *, path=None, config=None, store: bool = False, alerts: bool = False) -> dict:
    """C10: every tag of the component on every host, its kind, conformity, grandfathering and moves."""
    _local_never_stored(path, store, alerts)
    view = open_repo(target, path=path, config=config)
    policy, psrc = registry_policy(view.config)
    rows = tagmod.check(view.tags(), view.canonical_key, scheme_from=policy.get("tag_scheme_from"),
                        grandfathered=_grandfathered(policy, view.target))
    if view.config is not None:                       # review item 4: deletions and re-creations need the history
        from nanosoc_rm import db
        hist = db.history("tag_check:%s" % view.target, config=view.config)
        rows = tagmod.against_history(rows, hist, view.canonical_key)
        if hist:
            view.sources.append({"kind": "derived", "command": "compare with %d stored tag check(s) of %s"
                                 % (len(hist), view.target)})
    branches = _backport_branches(view)
    findings = sum(1 for r in rows if r["finding"]) + sum(1 for b in branches if not b["ok"])
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"] or "outside"] = kinds.get(r["kind"] or "outside", 0) + 1
    fact = {"target": view.target, "commit": None, "ref": None, "worktree": view.worktree,
            "scheme_from": policy.get("tag_scheme_from"), "canonical": view.canonical_key,
            "rows": rows, "branches": branches, "counts": kinds, "findings": findings,
            "verdict": "FAIL" if findings else ("PASS" if policy.get("tag_scheme_from") else "WARN")}
    obs = observe("tag_check", fact, view.sources + psrc, "nanosoc-rm version tags %s" % (target or "--path %s" % path),
                  subject="tag_check:%s" % view.target)
    if view.config is not None and alerts:
        from nanosoc_rm.mirror.layout import alert_once
        for r in rows:
            if (r["moved"] or r.get("deleted")) and r["kind"] == "release":
                alert_once("tag_moved", "tag_check:%s:%s" % (view.target, r["tag"]),
                           "component %s: release tag %s %s (C10: release tags are never moved or deleted)"
                           % (view.target, r["tag"], "deleted" if r.get("deleted") else "moved"),
                           obs["sources"][:3], config=view.config)
    if store and view.config is not None:
        _store(obs, view.config)
    return obs


def _backport_branches(view: RepoView) -> list[dict]:
    from nanosoc_rm.gitrun import run_git
    if view.git_dir is None:
        return []
    out = []
    for key, prefix in view.prefixes.items():
        r = run_git("for-each-ref", "--format=%(refname)", prefix + "/heads/release/", cwd=view.git_dir, timeout=60)
        view.sources.append(r.source())
        for ref in str(r.stdout).split() if r.ok else []:
            name = ref[len(prefix) + len("/heads/"):]
            out.append({"repo": key, "branch": name, "ok": bool(tagmod.branch_ok(name))})
    return out


# ---------------------------------------------------------------------------------------------------------
# C11: manifest scaffolding
# ---------------------------------------------------------------------------------------------------------
def manifest_init(target: str | None, *, ref: str | None = None, path=None, config=None) -> tuple[dict, str]:
    """(manifest_proposal observation, unified diff) for a person to review and apply. Writes nothing."""
    view = open_repo(target, ref=ref, path=path, config=config)
    policy, psrc = registry_policy(view.config)
    base = baseline(view.canonical_tags() if view.git_dir is not None else {}, yanked_versions(policy, view.target),
                    _confirmed_baselines(policy, view.target))
    existing = view.head_text(mf.FILE)
    version = base["version"] if base["kind"] == "release" else (_pyproject_version(view) or None)
    doc, text, placeholders = mf.propose(view, version=version)
    diff = mf.patch(text, existing)
    fact = dict(_base_fact(view), **{"present": existing is not None, "proposed": doc, "placeholders": placeholders,
                                     "version_from": (base["tag"] if base["kind"] == "release" else
                                                      ("pyproject.toml" if version else "default 0.1.0")),
                                     "patch_lines": diff.count("\n")})
    obs = observe("manifest_proposal", fact, view.sources + psrc,
                  "nanosoc-rm manifest init %s" % (target or "--path %s" % path))
    return obs, diff


# ---------------------------------------------------------------------------------------------------------
# Reading stored results (the GUI)
# ---------------------------------------------------------------------------------------------------------
def stored(kind: str, target: str, *, config=None) -> list[dict]:
    """Stored observations of one kind for one target, oldest first (the store keeps every one)."""
    from nanosoc_rm import db
    return sorted(db.latest(kind, "%s:%s:" % (kind, target), config=config) +
                  [d for d in db.latest(kind, "%s:%s" % (kind, target), config=config)
                   if d["subject"] == "%s:%s" % (kind, target)], key=lambda d: d["observed_at"])


def stored_by_target(kind: str, *, config=None) -> dict[str, list[dict]]:
    """{target: stored(kind, target)} for every target with a stored observation of `kind`, in ONE store read
    (ROB-load-7: an overview must not read the store once per component and kind). Same documents and order as
    stored(): a subject `kind:target` or `kind:target:...`."""
    from nanosoc_rm import db
    out: dict[str, list[dict]] = {}
    for d in db.latest(kind, "%s:" % kind, config=config):
        target = d["subject"][len(kind) + 1:].split(":", 1)[0]
        out.setdefault(target, []).append(d)
    for docs in out.values():
        docs.sort(key=lambda d: d["observed_at"])
    return out


def overview(config) -> list[dict]:
    """One row per catalogue component: its latest stored version_plan, changelog_lint and tag_check (or None).
    Each kind is read once for all components (ROB-load-7)."""
    from nanosoc_rm.analysis import inputs as inp
    reg = inp.registry(config)
    by_kind = {k: stored_by_target(k, config=config) for k in ("version_plan", "changelog_lint", "tag_check")}
    rows = []
    for c in reg.catalogue["components"]:
        if c.get("class") == "legacy":
            continue
        # a plan of a local checkout (`--path ... --store`, fact.worktree) is never the component's current answer:
        # only one read from the mirror is (review item 5); worktree results stay in the page's history, marked
        latest = lambda k: ([d for d in by_kind[k].get(c["id"], [])
                             if not (d.get("fact") or {}).get("worktree")] or [None])[-1]
        rows.append({"component": c["id"], "class": c.get("class"), "plan": latest("version_plan"),
                     "lint": latest("changelog_lint"), "tags": latest("tag_check")})
    return rows
