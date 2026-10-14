"""The freshness scan (interfaces.md section 23.1): how far each stored pin lags its upstream, and when it last moved.

    scan(configurations=None, *, config=None, store=True, today=None) -> [freshness observations]

Inputs, all read-only:
- the latest stored walk of each configuration (`pin_state`, section 4); nothing is re-walked;
- the component mirrors (section 3), through `gitrun` only: `rev-list --count`, `log -1`, `merge-base
  --is-ancestor`, `for-each-ref --contains`, `rev-parse`. Every commit is first tested with `rev-list -n1
  --missing=print --no-walk`, which never lazy-fetches, so a partial-clone mirror is never asked to fetch;
- the latest `mirror_status` per repo (whether a namespace's refs are from a failed sync).

Per pin it records: commits behind and ahead of the canonical host's default branch (`lower_bound` when that
repo's last sync failed, so upstream may have moved further), the pin's committer date, whether the pin is on the
default branch, the branches holding it, unpushed (no branch or tag of any readable host holds it), and the last
bump: `git log -1 <parent commit> -- <path>` in the parent's mirror, with its author, date and the number of
commits that ever touched the gitlink. Per tree: the rules.judge_tree summary on the scan day.

One snapshot per configuration, scope `freshness:<configuration>`: one `level: tree` document (subject
`freshness:<configuration>`) and one `level: pin` document per pin (subject `freshness:<configuration>:<first
tree path>`). The stored `state` is the judgement on `judged_on`; pages re-judge the stored facts on the day they
are drawn (freshness.view), because "not bumped for 90 days" changes with the calendar.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config, load_config
from nanosoc_rm.freshness import rules
from nanosoc_rm.gitrun import GitResult
from nanosoc_rm.mirror.catalogue import Catalogue, Entry, load_catalogue
from nanosoc_rm.mirror.layout import default_branch, mirror_path, ref_prefix, repo_key, run_in

__all__ = ["scan", "FreshnessUsage", "KIND", "BRANCHES_SHOWN", "GIT_TIMEOUT_S", "scope_of", "subject_of"]

KIND = "freshness"
BRANCHES_SHOWN = 10            # branches holding a pin, listed per pin (the rest counted)
GIT_TIMEOUT_S = 120


class FreshnessUsage(ValueError):
    """An unknown configuration: CLI exit 3, nothing stored."""


def scope_of(configuration: str) -> str:
    return "%s:%s" % (KIND, configuration)


def subject_of(configuration: str, tree_path: str | None = None) -> str:
    return scope_of(configuration) + ("" if tree_path is None else ":" + tree_path)


def _utc(git_iso: str | None) -> str | None:
    """A git %cI date (with its offset) as a contract UTC timestamp."""
    if not git_iso:
        return None
    return clock.iso(dt.datetime.fromisoformat(git_iso.strip()))


@dataclass
class _Git:
    """git calls in one mirror, recording each source (A3)."""
    mirror: Path
    sources: list[dict] = field(default_factory=list)

    def run(self, *args: str, record: bool = True) -> GitResult:
        r = run_in(self.mirror, "--no-optional-locks", *args, timeout=GIT_TIMEOUT_S)
        if record:
            self.sources.append(r.source())
        return r

    def out(self, *args: str, record: bool = True) -> str | None:
        r = self.run(*args, record=record)
        return r.stdout.strip() if r.ok else None

    def present(self, sha: str) -> bool:
        """The commit is in the mirror. `--missing=print` makes rev-list never fetch a missing object."""
        return self.run("rev-list", "-n1", "--missing=print", "--no-walk", sha, record=False).ok


@dataclass
class _Repo:
    repo: dict
    role: str
    has_refs: bool
    status: dict | None                       # the latest mirror_status fact, or None (never synced)

    @property
    def readable(self) -> bool:
        """Its refs in the mirror are from a successful sync (or it has refs and no status was ever stored)."""
        return self.has_refs and (self.status is None or bool(self.status.get("ok")))


class _Measurer:
    """Measures (entry, commit) pairs and bumps, caching both: a pin shared by four trees is read once."""

    def __init__(self, config: Config, catalogue: Catalogue, statuses: dict):
        self.config, self.catalogue, self.statuses = config, catalogue, statuses
        self._pins: dict = {}
        self._bumps: dict = {}
        self._repos: dict = {}
        self._present: dict = {}

    def _git(self, entry_id: str) -> _Git:
        return _Git(mirror_path(self.config, entry_id))

    def _is_present(self, entry_id: str, g: _Git, sha: str) -> bool:
        k = (entry_id, sha)
        if k not in self._present:
            self._present[k] = g.present(sha)
        return self._present[k]

    def _repos_of(self, entry: Entry, g: _Git) -> list[_Repo]:
        if entry.id not in self._repos:
            out = []
            for repo, role in entry.repos:
                pfx = ref_prefix(repo["host"], repo["path"])
                refs = g.out("for-each-ref", "--count=1", "--format=%(refname)", pfx + "/", record=False)
                out.append(_Repo(repo, role, bool(refs), self.statuses.get((repo["host"], repo["path"]))))
            self._repos[entry.id] = out
        return self._repos[entry.id]

    def pin(self, component: str | None, sha: str | None, stored_default: str | None) -> dict:
        """Facts about one pinned commit of one catalogue entry (cached)."""
        k = (component, sha)
        if k not in self._pins:
            self._pins[k] = self._measure(component, sha, stored_default)
        return self._pins[k]

    def _measure(self, component: str | None, sha: str | None, stored_default: str | None) -> dict:
        m = {"pin_date": None, "upstream": None, "behind": None, "ahead": None, "lower_bound": False,
             "on_default": None, "reachable": None, "unpushed": None, "branches": [], "branches_more": 0,
             "note": None, "sources": [], "mirror_at": None}
        if sha is None:
            m["note"] = "no pinned commit (the parent holds no gitlink at this path)"
            return m
        if component is None or not self.catalogue.has(component):
            m["note"] = "the URL matches no catalogued repo"
            return m
        entry = self.catalogue.entry(component)
        g = self._git(entry.id)
        if not (g.mirror / "HEAD").is_file():
            m["note"] = "no mirror of %s yet" % entry.id
            return m
        repos = self._repos_of(entry, g)
        canon = repos[0]
        m["upstream"] = {"repo": canon.repo, "default_branch": None, "head": None, "head_date": None}
        if canon.status and canon.status.get("last_success_at"):
            m["mirror_at"] = canon.status["last_success_at"]
        present = self._is_present(entry.id, g, sha)
        if present:
            m["pin_date"] = _utc(g.out("log", "-1", "--format=%cI", sha))
            prefixes = [ref_prefix(r.repo["host"], r.repo["path"]) for r in repos if r.has_refs]
            refs = []
            if prefixes:
                listed = g.out("for-each-ref", "--contains", sha, "--format=%(refname)",
                               *[p + kind for p in prefixes for kind in ("/heads/", "/tags/")])
                refs = (listed or "").splitlines()
            m["reachable"] = bool(refs)
            cheads = ref_prefix(canon.repo["host"], canon.repo["path"]) + "/heads/"
            branches = sorted(r[len(cheads):] for r in refs if r.startswith(cheads))
            m["branches"], m["branches_more"] = branches[:BRANCHES_SHOWN], max(0, len(branches) - BRANCHES_SHOWN)
        else:
            m["reachable"] = False
        unreadable = [r for r in repos if not r.readable]
        if not m["reachable"]:
            if unreadable:
                m["note"] = ("the pinned commit is on no ref of the readable hosts, and %s could not be read"
                                     % ", ".join("%s:%s" % (r.repo["host"], r.repo["path"]) for r in unreadable))
                if not present:
                    return self._done(m, g)
            else:
                m["unpushed"] = True
        else:
            m["unpushed"] = False
        if not canon.has_refs:
            cause = (canon.status or {}).get("cause") or (canon.status or {}).get("error") or "never synced"
            m["note"] = m["note"] or ("the canonical repo %s:%s has no refs in the mirror (%s)"
                                                      % (canon.repo["host"], canon.repo["path"], str(cause)[:120]))
            return self._done(m, g)
        key = repo_key(canon.repo["host"], canon.repo["path"])
        pfx = ref_prefix(canon.repo["host"], canon.repo["path"])
        branch = default_branch(g.mirror, key) or stored_default
        if not branch:
            for cand in ("main", "master"):
                if g.run("rev-parse", "--verify", "-q", "%s/heads/%s" % (pfx, cand), record=False).ok:
                    branch = cand
                    break
        m["upstream"]["default_branch"] = branch
        head = g.out("rev-parse", "--verify", "-q", "%s/heads/%s^{commit}" % (pfx, branch)) if branch else None
        if not head:
            m["note"] = m["note"] or ("no default branch %s on %s:%s in the mirror"
                                                      % (branch or "(unknown)", canon.repo["host"], canon.repo["path"]))
            return self._done(m, g)
        m["upstream"]["head"] = head
        m["upstream"]["head_date"] = _utc(g.out("log", "-1", "--format=%cI", head))
        if not present:
            m["note"] = m["note"] or "the pinned commit is not in the mirror"
            return self._done(m, g)
        behind = g.out("rev-list", "--count", "%s..%s" % (sha, head))
        ahead = g.out("rev-list", "--count", "%s..%s" % (head, sha))
        if behind is None or ahead is None:
            m["note"] = "git could not count commits between the pin and %s" % head[:12]
            return self._done(m, g)
        m["behind"], m["ahead"] = int(behind), int(ahead)
        anc = g.run("merge-base", "--is-ancestor", sha, head)
        m["on_default"] = True if anc.returncode == 0 else False if anc.returncode == 1 else None
        m["lower_bound"] = not canon.readable
        return self._done(m, g)

    @staticmethod
    def _done(m: dict, g: _Git) -> dict:
        m["sources"] = list(g.sources)
        g.sources.clear()
        return m

    def bump(self, parent_component: str | None, parent_commit: str, path: str) -> tuple[dict | None, list[dict]]:
        """The last commit of the parent that changed the gitlink at `path`, from the parent's mirror."""
        k = (parent_component, parent_commit, path)
        if k not in self._bumps:
            self._bumps[k] = self._bump(parent_component, parent_commit, path)
        return self._bumps[k]

    def _bump(self, parent_component, parent_commit, path):
        if not parent_component or not self.catalogue.has(parent_component):
            return None, []
        g = self._git(parent_component)
        if not (g.mirror / "HEAD").is_file() or not self._is_present(parent_component, g, parent_commit):
            return None, []
        line = g.out("log", "-1", "--format=%H%x1f%cI%x1f%an%x1f%s", parent_commit, "--", path)
        if not line:
            return None, list(g.sources)
        commit, date, author, subject = line.split("\x1f", 3)
        total = g.out("rev-list", "--count", parent_commit, "--", path)
        return ({"commit": commit, "date": _utc(date), "author": author, "subject": subject[:200],
                 "total": int(total) if total and total.isdigit() else None}, list(g.sources))


def _statuses(config: Config) -> dict:
    out = {}
    for d in db.latest("mirror_status", config=config):
        f = d["fact"]
        out[(f["repo"]["host"], f["repo"]["path"])] = f
    return out


def _canonical_default(fact: dict) -> str | None:
    for h in (fact.get("hosts") or {}).values():
        if h.get("role") in ("canonical", "external") and h.get("default_branch"):
            return h["default_branch"]
    return None


def _pin_doc(cid: str, frozen: bool, walk_doc: dict, m: dict, bump: dict | None, bump_sources: list[dict],
             today: dt.date, snap: dict) -> dict:
    f = walk_doc["fact"]
    if f.get("state") in ("LOCAL_URL", "NO_GITLINK"):
        m = dict(m, behind=None, unpushed=None,
                 note="pin-level state %s" % f["state"])
    fact = {
        "configuration": cid, "level": "pin", "frozen": frozen, "judged_on": today.isoformat(),
        "tree_path": f["tree_paths"][0], "tree_paths": list(f["tree_paths"]), "depth": f["depth"],
        "parent": {"component": f["parent"]["component"], "commit": f["parent"]["commit"]}, "path": f["path"],
        "component": f["child"]["component"], "catalogued_as": f["child"]["catalogued_as"],
        "pinned": f["child"]["commit"], "pin_date": m["pin_date"], "upstream": m["upstream"],
        "behind": m["behind"], "ahead": m["ahead"], "lower_bound": bool(m["lower_bound"]),
        "on_default": m["on_default"], "reachable": m["reachable"], "unpushed": m["unpushed"],
        "branches": m["branches"], "branches_more": m["branches_more"], "bump": bump,
        "not_measured": m["note"] if m["behind"] is None else None,
        "caveat": m["note"] if m["behind"] is not None else None,
    }
    j = rules.judge_pin(fact, today)
    fact["state"], fact["would_be"] = j["state"], j["would_be"]
    times = [walk_doc["observed_at"]] + ([m["mirror_at"]] if m.get("mirror_at") else [])
    observed = min(times, key=clock.parse)
    walk_src = {"kind": "derived", "command": "pin_state %s (walk snapshot %s)"
                % (walk_doc["subject"], (walk_doc.get("snapshot") or {}).get("id", "?"))}
    return {"schema": 1, "kind": KIND, "subject": subject_of(cid, fact["tree_path"]), "observed_at": observed,
            "sources": [walk_src] + m["sources"] + bump_sources, "snapshot": snap, "fact": fact}


def _tree_doc(cid: str, cfg: dict, frozen: bool, pins: list[dict], root: dict, walk: dict, today: dt.date,
              snap: dict, sources: list[dict], observed: str, reason: str | None) -> dict:
    j = rules.judge_tree(frozen, [p["fact"] for p in pins], today)
    fact = {"configuration": cid, "level": "tree", "frozen": frozen, "silicon": bool(cfg.get("silicon")),
            "judged_on": today.isoformat(), "root": root, "walk": walk, "not_measured": reason, **j}
    return {"schema": 1, "kind": KIND, "subject": subject_of(cid), "observed_at": observed,
            "sources": [{"kind": "derived", "command": "nanosoc-rm freshness scan %s" % cid}] + sources,
            "snapshot": snap, "fact": fact}


def _configuration(cfg: dict, catalogue: Catalogue, measurer: _Measurer, config: Config, today: dt.date) -> list[dict]:
    from nanosoc_rm.walker import stored_pins
    cid = cfg["id"]
    frozen = bool(cfg.get("frozen") or cfg.get("silicon"))
    snap = {"id": db.new_snapshot_id(scope_of(cid)), "scope": scope_of(cid)}
    walk_docs = stored_pins(cid, config=config)
    if not walk_docs:
        root = {"component": cfg["component"], "commit": None, "date": None}
        return [_tree_doc(cid, cfg, frozen, [], root, {"snapshot": None, "observed_at": None}, today, snap, [],
                          clock.iso(), "no stored walk: run `nanosoc-rm walk %s`" % cid)]
    pins = []
    for d in walk_docs:
        f = d["fact"]
        m = measurer.pin(f["child"]["component"], f["child"]["commit"], _canonical_default(f))
        bump, bsrc = measurer.bump(f["parent"]["component"], f["parent"]["commit"], f["path"])
        pins.append(_pin_doc(cid, frozen, d, m, bump, bsrc, today, snap))
    root_commit = next((d["fact"]["parent"]["commit"] for d in walk_docs if d["fact"]["depth"] == 1), None)
    root = {"component": cfg["component"], "commit": root_commit, "date": None}
    sources = []
    if root_commit and catalogue.has(cfg["component"]):
        g = measurer._git(cfg["component"])
        if (g.mirror / "HEAD").is_file() and g.present(root_commit):
            root["date"] = _utc(g.out("log", "-1", "--format=%cI", root_commit))
            sources = list(g.sources)
    walk = {"snapshot": (walk_docs[0].get("snapshot") or {}).get("id"),
            "observed_at": min((d["observed_at"] for d in walk_docs), key=clock.parse)}
    observed = min([p["observed_at"] for p in pins] + [walk["observed_at"]], key=clock.parse)
    return [_tree_doc(cid, cfg, frozen, pins, root, walk, today, snap, sources, observed, None)] + pins


def scan(configurations: list[str] | None = None, *, config: Config | str | Path | None = None, store: bool = True,
         today: dt.date | None = None) -> list[dict]:
    """Measure every registered configuration (or the named ones) from its stored walk and the mirrors; return
    the freshness documents, tree document first per configuration, and with `store` keep each configuration's
    documents as one snapshot. FreshnessUsage for an unknown configuration (nothing is measured or stored)."""
    config = config if isinstance(config, Config) else load_config(config)
    catalogue = load_catalogue(config)
    known = {c["id"]: c for c in catalogue.configurations()}
    ids = list(configurations) if configurations else list(known)
    unknown = [c for c in ids if c not in known]
    if unknown:
        raise FreshnessUsage("not a registered configuration: %s" % ", ".join(unknown))
    today = today or clock.now().date()
    measurer = _Measurer(config, catalogue, _statuses(config))
    out = []
    for cid in ids:
        docs = _configuration(known[cid], catalogue, measurer, config, today)
        if store:
            db.put_snapshot(scope_of(cid), docs, config=config)
        out += docs
    return out
