"""`nanosoc-rm test-bump CONFIG PATH TO`: test one submodule move, Now against After (interfaces.md section 26.3).

    test_bump(configuration, path, to, *, config, host=None, plan_only=False, ...) -> dict    one test_bump document
    resolve_to(config, component, to) -> (commit, resolved_as, sources)                       TestBumpUsage when none

1. **Resolve.** PATH is a tree path of the configuration's tree; its component's mirror resolves TO: a full sha, a
   tag, a branch (the canonical repo's first), or a unique sha prefix of 7+ hex. The move applies to EVERY tree path
   that pins that component (B7: a split component moves together).
2. **Plan.** Both trees are walked (the registered one, and the one with the move: never stored), every pin's
   `.nanosoc/capabilities.yaml` is read at its commit from its mirror (capabilities.manifest.Reader), and the run
   set follows capabilities.plan (own gates of the moved trees, consumer gates whose `affected_by` names them).
   `--plan` stops here: nothing runs.
3. **Run, twice, in one job.** Now and After each run `compat` (the T0 gates, locally) and, on the same laid-out
   tree, every planned capability of that side, with the host's site data (registry tiers.yaml `hosts.<host>`):
   tool bin directories first on PATH, the site env values the tree declares, siblings at the paths this tree pins
   them, then the tree's own `env.provides`. A host that cannot run a capability (a tool, version, licence or need
   it lacks) makes that row INFRA without running it. Same root commit, same host, same job: the two sides compare.
4. **Read the verdict.** exit status (`verdict.exit`), `forbid` text in the log is a FAIL row, `require` text must
   appear at least `min` times, `rows`: verdicts-tsv and junit files and `regex` log lines become rows; a format
   with no parser yet is one UNVERIFIED row, never a pass.
5. **Compare.** Per row (block gates decide; report gates are shown): PASS->PASS same, red->PASS better, PASS->red
   worse (FAIL), red->red same-red (WARN), a red After with no green Now baseline UNVERIFIED, an After that did not
   run UNVERIFIED; a gate the After side no longer declares is `gone` (PASS) only when After's manifest reads ok,
   else `not-run` (UNVERIFIED: a deleted or broken manifest is no evidence). T0 is HYGIENE (David 2026-10-16 decision 4): reported per side, never hiding the functional rows.
   The functional verdict: FAIL > UNVERIFIED > WARN > PASS; no planned row at all is UNVERIFIED.

The document is the job's result (jobs.sqlite) and the bump gate's evidence (capabilities.gate); it is never a
stored fact. Nothing is written to any repo; a tier never sources a set_env.sh.
"""
from __future__ import annotations

import glob
import os
import re
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping

from nanosoc_rm import clock, contracts
from nanosoc_rm.capabilities import manifest as MF
from nanosoc_rm.capabilities import plan as PL

__all__ = ["test_bump", "resolve_to", "TestBumpUsage", "CannotTest", "compare", "functional_verdict",
           "spec_for", "SCHEMA", "UNPARSED"]

SCHEMA = "test-bump"
_HEX = re.compile(r"^[0-9a-f]{7,40}\Z")
_SHA = re.compile(r"^[0-9a-f]{40}\Z")
UNPARSED = ("regress-table", "tidelink-sim-gate")
_GREEN = ("PASS", "WARN")
_ORDER = ("FAIL", "UNVERIFIED", "WARN", "PASS")


class TestBumpUsage(ValueError):
    """The question cannot be asked: unknown configuration, no pin at PATH, TO resolves to nothing. Exit 3."""
    __test__ = False
    exit_code = 3


class CannotTest(RuntimeError):
    """The data to answer is missing (no mirror, the walk fails, the registry does not load). Exit 2."""
    exit_code = 2


# ---------------------------------------------------------------------------------------------------------
# 1. Resolve
# ---------------------------------------------------------------------------------------------------------
def resolve_to(config, component: str, to: str) -> tuple[str, str, list[dict]]:
    """(commit, how it resolved, git sources) of TO in the component's mirror."""
    from nanosoc_rm.ci import configs
    from nanosoc_rm.mirror import layout
    mirror = layout.mirror_path(config, component)
    if not (mirror / "HEAD").is_file():
        raise CannotTest("component %s has no mirror: run `nanosoc-rm mirror sync --component %s`"
                         % (component, component))
    to = (to or "").strip()
    if not to or to.startswith("-") or any(c in to for c in " \t\n~^:?*[\\"):
        raise TestBumpUsage("%r is not a tag, branch or commit" % to)
    srcs: list[dict] = []
    if _SHA.match(to.lower()):
        r = layout.run_in(mirror, "cat-file", "-e", "%s^{commit}" % to.lower(), timeout=60)
        srcs.append(r.source())
        if not r.ok:
            raise TestBumpUsage("commit %s is not in the %s mirror (sync it, or it is unpushed)" % (to, component))
        return to.lower(), "commit", srcs
    try:
        canon = configs._registry(config).canonical(component)
        first = layout.repo_key(canon.host, canon.path)
    except Exception:                                              # noqa: BLE001 - no canonical preference then
        first = None
    refs = layout.run_in(mirror, "for-each-ref", "--format=%(refname)\t%(objectname)", "refs/r/", timeout=60)
    srcs.append(refs.source())
    tags, heads = [], []
    for line in (refs.stdout or "").splitlines():
        name, _, _sha = line.partition("\t")
        key, kind, rest = _split_ref(name)
        if rest == to and kind in ("tags", "heads"):
            (tags if kind == "tags" else heads).append((0 if key == first else 1, name))
    for kind, hits in (("tag", tags), ("branch", heads)):
        if hits:
            name = sorted(hits)[0][1]
            r = layout.run_in(mirror, "rev-parse", "--verify", "--quiet", name + "^{commit}", timeout=60)
            srcs.append(r.source())
            if r.ok and r.stdout.strip():
                return r.stdout.strip(), "%s %s" % (kind, to), srcs
    if _HEX.match(to.lower()):
        r = layout.run_in(mirror, "rev-parse", "--verify", "--quiet", "%s^{commit}" % to.lower(), timeout=60)
        srcs.append(r.source())
        if r.ok and _SHA.match(r.stdout.strip()):
            return r.stdout.strip(), "commit prefix %s" % to.lower(), srcs
    raise TestBumpUsage("%s: no tag, branch or commit %r in its mirror" % (component, to))


def _split_ref(name: str) -> tuple[str, str, str]:
    """refs/r/<host>/<quoted path>/<heads|tags>/<rest> -> (key, kind, rest)."""
    parts = name.split("/")
    if len(parts) < 6 or parts[0] != "refs" or parts[1] != "r":
        return "", "", ""
    return "%s/%s" % (parts[2], parts[3]), parts[4], "/".join(parts[5:])


def _root_node(config, configuration: str, docs: list[dict]) -> PL.Node:
    from nanosoc_rm.ci import configs
    cfg = configs.configuration(configuration, config)
    commit = next((d["fact"]["parent"]["commit"] for d in docs if d["fact"].get("depth") == 1), None)
    if commit is None:
        from nanosoc_rm import walker
        from nanosoc_rm.mirror.catalogue import load_catalogue
        commit, _ = walker._resolve_commit(config, load_catalogue(config), cfg, alerts=False)
    canon = configs.canonical_repo(cfg["component"], config)
    return PL.Node("", cfg["component"], commit, canon["host"])


# ---------------------------------------------------------------------------------------------------------
# 3. Run one capability on a laid-out tree
# ---------------------------------------------------------------------------------------------------------
def _label(item: PL.Item) -> str:
    own = re.sub(r"[^a-z0-9_-]+", "-", item.owner.lower()).strip("-")
    return ("%s.%s" % (own, item.cap["id"])) if own else item.cap["id"]


def spec_for(item: PL.Item, comp: PL.Composition, site: dict, host: str | None,
             environ: Mapping[str, str]) -> tuple[dict | None, str | None, str | None]:
    """(tierrun spec, None, None) for one planned capability on one host; or (None, verdict, reason) when it
    cannot run here: INFRA for what the host lacks, NOT-RUN for what the tool refuses (a regenerating capability
    without NANOSOC_GEN_OK=1)."""
    from nanosoc_rm.analysis import tierrun
    cap = item.cap
    read = comp.reads.get(item.owner)
    doc = read.doc if read is not None and read.ok else {"tools": {}, "env": {"provides": {}, "site": [],
                                                                               "siblings": []}}
    if host is None:
        return None, "INFRA", ("no executor host named (--host, or [executor] host in the config): the site data "
                               "of registry tiers.yaml hosts decides what can run")
    missing = PL.host_check(cap, doc["tools"], site)
    if missing:
        return None, "INFRA", "host %s cannot run it: %s" % (host, "; ".join(missing))
    cmd = cap["run"]
    parts = [cmd] if isinstance(cmd, str) else list(cmd)
    if any(tierrun.SOURCES_SET_ENV.search(p) for p in parts):
        return None, "NOT-RUN", "the command sources a set_env.sh; capabilities never do (declare env instead)"
    if cap.get("regenerates") and environ.get("NANOSOC_GEN_OK") != "1":
        return None, "NOT-RUN", "it regenerates (regenerates: true): set NANOSOC_GEN_OK=1 to allow it"
    env: dict[str, str] = {}
    bins = []
    for t in cap["tools"]:
        b = ((site.get("tools") or {}).get(t) or {}).get("bin")
        if b and b not in bins:
            bins.append(b)
    if bins:
        env["PATH"] = ":".join(bins) + ":${PATH}"
    site_env = site.get("env") or {}
    for s in doc["env"]["site"]:
        if s["name"] in site_env:
            env[s["name"]] = site_env[s["name"]].replace("$", "$$")
        elif not s.get("optional"):
            return None, "INFRA", "host %s has no value for %s (%s): add it to tiers.yaml hosts.%s.env" % (
                host, s["name"], s.get("what") or "a site variable", host)
    for s in doc["env"]["siblings"]:
        if s.get("for") and cap["id"] not in s["for"]:
            continue
        tp = comp.tree_of_component(s["component"])
        if tp is None:
            return None, "UNVERIFIED", "sibling %s (%s) is not pinned in this tree" % (s["component"], s["name"])
        env[s["name"]] = "${TREE}/" + tp
    for k, v in doc["env"]["provides"].items():
        env[k] = v.replace("${TREE}", "${TREE}/" + item.owner) if item.owner else v
    rows = (cap.get("verdict") or {}).get("rows") or {}
    spec = {"command": cmd, "cwd": PL._join(item.owner, cap["cwd"]) or ".", "budget_s": int(cap["budget_s"]),
            "env": env, "needs": list(cap["needs"]), "regenerates": bool(cap.get("regenerates"))}
    if rows.get("format") == "verdicts-tsv" and rows.get("path"):
        spec["verdicts"] = PL._join(item.owner, rows["path"])
    return spec, None, None


def _row(gate: str, status: str, detail: str, nonce: str) -> dict:
    from nanosoc_rm.analysis import tierrun
    return tierrun._row(gate, status, detail, nonce)


def _extra_rows(cap: dict, label: str, tree: Path, owner: str, log: Path, rc: int | None, nonce: str) -> list[dict]:
    """Rows from the verdict block beyond the exit status (module docstring step 4)."""
    v = cap.get("verdict") or {}
    rows: list[dict] = []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for f in v.get("forbid") or []:
        n = text.count(f)
        if n:
            rows.append(_row("tier.%s.forbid" % label, "FAIL", "%r appears %d time(s) in the log: a %s is not a "
                             "pass" % (f, n, f.lower()), nonce))
    for r in v.get("require") or []:
        n = text.count(r["pattern"])
        rows.append(_row("tier.%s.require" % label, "PASS" if n >= r["min"] else "FAIL",
                         "%r appears %d time(s), at least %d needed" % (r["pattern"], n, r["min"]), nonce))
    spec = v.get("rows") or {}
    fmt, path = spec.get("format"), spec.get("path")
    base = tree / owner if owner else tree
    if fmt == "junit":
        files = sorted(glob.glob(str(base / path))) if path else []
        if not files:
            rows.append(_row("tier.%s.junit" % label, "UNVERIFIED", "declared JUnit file %s was not written"
                             % (path or "(no path)"), nonce))
        for fp in files:
            rows += _junit(Path(fp), label, nonce)
    elif fmt == "regex":
        src = (base / path) if path else log
        try:
            lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = None
        if lines is None:
            rows.append(_row("tier.%s.regex" % label, "UNVERIFIED", "declared file %s was not written" % path, nonce))
        else:
            rx = re.compile(spec.get("fail") or r"(?!)")
            bad = [x for x in lines if rx.search(x)]
            rows.append(_row("tier.%s.regex" % label, "FAIL" if bad else "PASS",
                             ("%d line(s) match %r, first: %s" % (len(bad), spec.get("fail"), bad[0][:200])) if bad
                             else "no line matches %r" % spec.get("fail"), nonce))
    elif fmt in UNPARSED:
        rows.append(_row("tier.%s.rows" % label, "UNVERIFIED", "rows format %s has no parser yet: its rows are not "
                         "read, so they cannot pass" % fmt, nonce))
    return rows


def _junit(path: Path, label: str, nonce: str) -> list[dict]:
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError) as e:
        return [_row("tier.%s.junit" % label, "UNVERIFIED", "%s does not parse: %s" % (path.name, e), nonce)]
    cases = list(root.iter("testcase"))
    if not cases:
        return [_row("tier.%s.junit" % label, "UNVERIFIED", "%s holds no test case" % path.name, nonce)]
    failed = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    first = failed[0] if failed else None
    return [_row("tier.%s.junit" % label, "FAIL" if failed else "PASS",
                 "%d case(s): %d failed, %d skipped%s" % (len(cases), len(failed), len(skipped),
                                                         ", first %s" % first.get("name") if first is not None else ""),
                 nonce)]


def _run_item(item: PL.Item, comp: PL.Composition, site: dict, host: str | None, environ, tree: Path, work: Path,
              layout, nonce: str, lock_dir) -> dict:
    from nanosoc_rm.analysis import tierrun
    label = _label(item)
    out = {"verdict": "UNVERIFIED", "reason": None, "seconds": None, "exit_code": None, "rows": [], "log_tail": [],
           "commit": item.commit, "host": host}
    spec, verdict, reason = spec_for(item, comp, site, host, environ)
    if spec is None:
        return dict(out, verdict=verdict, reason=reason)
    try:
        d = tierrun.run_tier(spec, label, tree, work, nonce, layout=layout, lock_dir=lock_dir)
    except tierrun.TierUsage as e:
        return dict(out, verdict="NOT-RUN", reason=str(e))
    rows = list(d["rows"])
    want = int((item.cap.get("verdict") or {}).get("exit", 0))
    if rows and d.get("exit_code") is not None and want != 0 and rows[0]["gate"] == "tier.%s" % label:
        rows[0] = dict(rows[0], status="PASS" if d["exit_code"] == want else "FAIL",
                       detail="exit %d (a pass is exit %d)" % (d["exit_code"], want))
    ran = d.get("exit_code") is not None or d.get("killed")
    if ran:
        rows += _extra_rows(item.cap, label, tree, item.owner, work / ("tier-%s.log" % label), d.get("exit_code"),
                            nonce)
    v = tierrun.tier_verdict(rows) if rows else d["verdict"]
    bad = [r for r in rows if r["status"] == v and v != "PASS"]
    return dict(out, verdict=v, reason=("; ".join("%s: %s" % (r["gate"], r["detail"]) for r in bad)[:500]
                                        if bad else d.get("reason")),
                seconds=d.get("seconds"), exit_code=d.get("exit_code"), rows=rows, log_tail=d.get("log_tail") or [])


# ---------------------------------------------------------------------------------------------------------
# 5. Compare
# ---------------------------------------------------------------------------------------------------------
def compare(now: dict | None, after: dict | None) -> str:
    """same | better | worse | same-red | new-red | new | gone | not-run (module docstring step 5)."""
    a = (now or {}).get("verdict")
    b = (after or {}).get("verdict")
    if after is None:
        return "gone"
    if b not in ("PASS", "WARN", "FAIL"):
        return "not-run"
    green_b = b in _GREEN
    if now is None or a not in ("PASS", "WARN", "FAIL"):
        return "new" if green_b else "new-red"
    green_a = a in _GREEN
    if green_a and green_b:
        return "same"
    if green_b:
        return "better"
    return "worse" if green_a else "same-red"


_CHANGE_VERDICT = {"same": "PASS", "better": "PASS", "new": "PASS", "gone": "PASS", "worse": "FAIL",
                   "same-red": "WARN", "new-red": "UNVERIFIED", "not-run": "UNVERIFIED"}


def functional_verdict(rows: list[dict]) -> tuple[str, str | None]:
    """The worst row verdict over the block gates (module docstring step 5), and why."""
    block = [r for r in rows if r["gate"] == "block"]
    if not block:
        return "UNVERIFIED", ("no capability covers this move: no tree on the path declares one in "
                              ".nanosoc/capabilities.yaml at the commits tested")
    vs = [(_CHANGE_VERDICT[r["change"]], r) for r in block]
    worst = min((v for v, _ in vs), key=_ORDER.index)
    if worst == "PASS":
        return "PASS", None
    why = [r["key"] + " " + r["change"] for v, r in vs if v == worst]
    return worst, "; ".join(why)[:500]


# ---------------------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------------------
def _site(config, host: str | None) -> dict:
    from nanosoc_rm.ci import configs
    if host is None:
        return {}
    sites = configs._registry(config).sites()
    if host not in sites:
        raise TestBumpUsage("host %r is not in the registry's tiers.yaml hosts (known: %s)"
                            % (host, ", ".join(sorted(sites)) or "none"))
    return sites[host] or {}


def default_host(config, environ: Mapping[str, str]) -> str | None:
    return (environ.get("NANOSOC_RM_EXECUTOR_HOST") or ((config.get("executor") or {}).get("host")) or None)


def test_bump(configuration: str, path: str, to: str, *, config, host: str | None = None, plan_only: bool = False,
              environ: Mapping[str, str] | None = None, url_map=None, lock_dir=None, nonce: str | None = None,
              argv: str | None = None) -> dict:
    """One test_bump document (module docstring). TestBumpUsage (exit 3) or CannotTest (exit 2)."""
    from nanosoc_rm import walker
    from nanosoc_rm.analysis import compat as C
    from nanosoc_rm.analysis import tierrun
    from nanosoc_rm.ci import configs
    environ = os.environ if environ is None else environ
    host = host or default_host(config, environ)
    started = clock.iso()
    nonce = nonce or secrets.token_hex(16)
    path = (path or "").strip().strip("/")
    try:
        configs.configuration(configuration, config)
    except configs.UnknownConfiguration as e:
        raise TestBumpUsage(str(e)) from None
    except configs.RegistryUnreadable as e:
        raise CannotTest(str(e)) from None
    site = _site(config, host)
    try:
        now_docs = walker.walk(configuration, config=config, store=False, alerts=False)
    except walker.WalkError as e:
        raise CannotTest("the tree of %s cannot be walked: %s" % (configuration, e)) from None
    at = next((d for d in now_docs if path in d["fact"]["tree_paths"]), None)
    if at is None:
        raise TestBumpUsage("%s has no pin at %s (its tree paths: %s)" % (
            configuration, path or "(empty)", ", ".join(sorted(tp for d in now_docs for tp in d["fact"]["tree_paths"]))
            [:400]))
    component = at["fact"]["child"]["component"]
    frm = at["fact"]["child"]["commit"]
    if not component:
        raise TestBumpUsage("the pin at %s names no catalogued component, so TO cannot be resolved" % path)
    commit, resolved_as, srcs = resolve_to(config, component, to)
    tree_paths = sorted({tp for d in now_docs if d["fact"]["child"]["component"] == component
                         for tp in d["fact"]["tree_paths"]})
    moved = [tp for tp in tree_paths if not any(tp.startswith(o + "/") for o in tree_paths)]   # outermost only
    overrides = {tp: commit for tp in moved}
    try:
        after_docs = walker.walk(configuration, config=config, store=False, alerts=False, overrides=overrides)
    except (walker.WalkError, walker.OverrideError) as e:
        raise CannotTest("the tree with the move cannot be walked: %s" % e) from None
    root = _root_node(config, configuration, now_docs)
    reader = MF.Reader(config)
    now_c = PL.composed(PL.nodes_of(now_docs, root), reader)
    after_c = PL.composed(PL.nodes_of(after_docs, root), reader)
    plan = PL.plan_bump(now_c, after_c, moved, component)
    reads = {}
    for side, comp in (("now", now_c), ("after", after_c)):
        for tp, r in comp.reads.items():
            reads.setdefault(PL.owner_label(tp), {})[side] = r.as_dict()
    fact = {"configuration": configuration, "root_commit": root.commit, "path": path, "component": component,
            "from": frm, "to": {"ref": to, "commit": commit, "resolved_as": resolved_as},
            "tree_paths": tree_paths, "moved": moved, "host": host, "plan_only": bool(plan_only),
            "plan": [r.as_dict() for r in plan],
            "manifests": [dict(tree_path=k, **v) for k, v in sorted(reads.items())],
            "problems": sorted(set(now_c.problems) | set(after_c.problems)),
            "hygiene": None, "rows": [], "verdict": "UNVERIFIED", "reason": None,
            "started_at": started, "finished_at": None}
    if plan_only:
        fact["reason"] = "plan only: nothing ran"
    elif frm == commit:
        fact["reason"] = "the pin at %s is already %s: nothing moves" % (path, commit[:12])
    else:
        results: dict[str, dict] = {"now": {}, "after": {}}
        hygiene = {}

        def runner(side: str, comp: PL.Composition, items: dict):
            def on_tree(git, root_pin, work, t0):
                if root_pin is None:
                    for k in items:
                        results[side][k] = {"verdict": "UNVERIFIED", "reason": "no tree to lay out: the T0 walk "
                                            "did not start", "rows": [], "log_tail": [], "seconds": None,
                                            "exit_code": None, "commit": items[k].commit, "host": host}
                    return
                tree = Path(work) / "tree"
                lay = tierrun.materialise(git, root_pin, tree, None) if items else None
                for k, it in items.items():
                    results[side][k] = _run_item(it, comp, site, host, environ, tree, Path(work), lay, nonce,
                                                 lock_dir)
            return on_tree

        for side, comp, ov in (("now", now_c, None), ("after", after_c, overrides)):
            items = {r.key: getattr(r, side) for r in plan if getattr(r, side) is not None}
            try:
                doc = C.compat(configuration, ov, config=config, url_map=url_map, nonce=nonce, lock_dir=lock_dir,
                               on_tree=runner(side, comp, items))
                hygiene[side] = {"verdict": doc["fact"]["verdict"], "reason": doc["fact"].get("reason"),
                                 "commit": doc["fact"]["commit"]}
            except Exception as e:                                 # noqa: BLE001 - one side cannot answer: said
                hygiene[side] = {"verdict": "INFRA", "reason": "%s: %s" % (type(e).__name__, str(e)[:300]),
                                 "commit": None}
                for k, it in items.items():
                    results[side].setdefault(k, {"verdict": "INFRA", "reason": "the %s run did not complete: %s"
                                                 % (side, str(e)[:200]), "rows": [], "log_tail": [],
                                                 "seconds": None, "exit_code": None, "commit": it.commit,
                                                 "host": host})
        fact["hygiene"] = hygiene
        for r in plan:
            now, after = results["now"].get(r.key), results["after"].get(r.key)
            c = r.cap()
            change = compare(now, after)
            if change == "gone" and (r.after is not None
                                     or not getattr(after_c.reads.get(r.owner), "ok", False)):
                # Gone only because After's manifest is absent, invalid or unreadable, or because a planned After
                # run left no result: it did not run, so it is no evidence (a bump that deletes or breaks the file
                # must not pass on that). A valid After manifest that drops the capability is a reviewed change:
                # `gone` stays.
                change = "not-run"
            fact["rows"].append({"key": r.key, "owner": PL.owner_label(r.owner), "id": r.id, "kind": c["kind"],
                                 "tier": c.get("tier"), "gate": c["gate"], "why": r.why, "now": now, "after": after,
                                 "change": change})
        fact["verdict"], fact["reason"] = functional_verdict(fact["rows"])
        roots = {h.get("commit") for h in hygiene.values()} - {None}
        if len(roots) > 1:
            fact["verdict"], fact["reason"] = "UNVERIFIED", ("Now and After ran on different root commits (%s): "
                                                             "the branch moved between them; run it again"
                                                             % ", ".join(sorted(x[:12] for x in roots)))
    fact["finished_at"] = clock.iso()
    doc = {"schema": 1, "kind": "test_bump", "subject": "test_bump:%s:%s" % (configuration, nonce),
           "observed_at": started,
           "sources": [{"kind": "derived", "command": argv or "nanosoc-rm test-bump %s %s %s%s" % (
               configuration, path, to, " --host %s" % host if host else "")}] + srcs[:20],
           "fact": fact}
    contracts.require_valid(SCHEMA, doc)
    return doc
