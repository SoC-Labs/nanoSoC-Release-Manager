"""Submodule compatibility check (CR-11, MVP S2; interfaces.md section 9a).

    compat(configuration, overrides={tree path: sha}, ...) -> dict     one compat_result observation

1. **Resolve** the configuration's tree with the pin walker (section 4), each override replacing the gitlink at its
   tree path; the tree below an override is walked from the overriding commit's own .gitmodules. An override at a
   tree path that holds no gitlink pin is a usage error (OverrideError, exit 3). Every pin keeps its walker facts:
   component, commit, URL kind, state on its pinned host, `split` (B7: its component is pinned at more than one
   commit in this tree).
2. **Judge** the tree with the configuration T0 gates, LOCALLY (`t0.local.run_local`, run mode `local`): the same
   gate code and negative controls as the compat-ci job, anonymous git in a temporary work directory with an empty
   HOME and TMPDIR (so ~/.netrc and stored credentials are never read). The request is the one the trigger would
   send (`ci.trigger.build_request`), with canonical_urls from this walk and the local-only `pin_overrides`, so the
   gates walk and judge the overridden pins. `t0.runner.hardened` is NOT-RUN ("local run"); with overrides,
   `t0.clone.upgrade-path` is NOT-RUN too (it clones the registered commit, not the what-if tree).
3. **Roll up** with `rollup(mode="local")`, `apply_waivers` (the configuration's t0_waivers) and `run_verdict`
   (README section 6). An override the gates' own walk did not reach (below a tree no anonymous fetch can read)
   makes the run UNVERIFIED: the what-if was not judged.

4. **Tier** (CR-12, `tier="t1"|"t2"`): after the T0 roll-up, the registry's tiers.yaml command for the
   configuration runs in a sandbox checkout of the same walked tree (`analysis.tierrun`). A T0 FAIL skips it
   (NOT-RUN) unless `force_tier` (a person's override; the result is UNVERIFIED for release). `run_verdict` is the
   ladder over the T0 verdict and the tier's; the exit code follows it.

Not stored: a what-if is not a fact. `store=True` (only without overrides) stores the walk as `walk` does and the
compat_result as an event observation. The work directory is deleted unless `keep_work` names one.
"""
from __future__ import annotations

import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from nanosoc_rm import clock

__all__ = ["compat", "parse_pins", "CompatUsage", "exit_for", "ladder", "GATE_COLUMNS", "UPGRADE_NOT_RUN",
           "FORCED_NOTE"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
# per-pin item columns: gate id -> short column name
GATE_COLUMNS = {"t0.pins.fetchable": "fetchable", "t0.pins.reachable": "reachable", "t0.urls.no-ssh-public": "no_ssh",
                "t0.manifest.valid": "manifest", "t0.vendor.no-arm-ip": "vendor"}
UPGRADE_NOT_RUN = ("pin overrides given: the upgrade-path clone checks out the registered commit, not the "
                   "what-if tree")


FORCED_NOTE = ("--force-tier: a person ran the tier past a T0 FAIL; this result is UNVERIFIED for release whatever "
               "the tier says")
_LADDER = ("FAIL", "UNVERIFIED", "INFRA", "WAIVED", "WARN", "PASS")


def ladder(*verdicts: str) -> str:
    """README section 6 run ladder over verdicts (NOT-RUN entries are ignored)."""
    vs = [v for v in verdicts if v in _LADDER]
    return min(vs, key=_LADDER.index) if vs else "UNVERIFIED"


class CompatUsage(ValueError):
    """A usage problem: a malformed --pin, --store with --pin, a --keep-work directory that is not empty. Exit 3."""
    exit_code = 3


def parse_pins(specs) -> dict[str, str]:
    """{tree path: sha} from `TREE_PATH=SHA` strings. The sha must be the full 40 hex (a what-if names exactly one
    commit); a tree path given twice is a usage error."""
    out: dict[str, str] = {}
    for spec in specs or ():
        path, sep, sha = str(spec).partition("=")
        path, sha = path.strip().strip("/"), sha.strip().lower()
        if not sep or not path:
            raise CompatUsage("--pin %r: expected TREE_PATH=SHA" % spec)
        if not _SHA.match(sha):
            raise CompatUsage("--pin %s: %r is not a full 40-hex commit id" % (path, sha))
        if path in out:
            raise CompatUsage("--pin %s given twice" % path)
        out[path] = sha
    return out


def exit_for(verdict: str) -> int:
    """interfaces.md section 0.2: 0 PASS, WAIVED, WARN; 1 FAIL; 2 UNVERIFIED, INFRA."""
    return 0 if verdict in ("PASS", "WAIVED", "WARN") else 1 if verdict == "FAIL" else 2


def _host_bases(config) -> dict[str, str]:
    out = {}
    for key, h in (config.get("hosts") or {}).items():
        base = (h or {}).get("git") or (h or {}).get("web")
        if base:
            out[key] = str(base).rstrip("/")
    return out


def _workdir(keep_work) -> tuple[Path, bool]:
    if keep_work is None:
        return Path(tempfile.mkdtemp(prefix="nanosoc-rm-compat-")), False
    d = Path(keep_work).absolute()
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise CompatUsage("--keep-work %s: must be a new or empty directory" % d)
    d.mkdir(parents=True, exist_ok=True)
    return d, True


def _pinned_state(fact: dict) -> str | None:
    if fact.get("state"):
        return fact["state"]
    pinned = next((h for h in (fact.get("hosts") or {}).values() if h.get("pinned")), None)
    return pinned["state"] if pinned else None


def compat(configuration: str, overrides: Mapping[str, str] | None = None, *, config=None, store: bool = False,
           keep_work: str | Path | None = None, url_map: Mapping[str, str] | None = None,
           nonce: str | None = None, argv: str | None = None, tier: str | None = None, force_tier: bool = False,
           environ: Mapping[str, str] | None = None, lock_dir: str | Path | None = None) -> dict:
    """One compat_result observation (module docstring). Raises walker.UnknownConfiguration or OverrideError
    (exit 3), CompatUsage (3), walker.WalkError, ci.trigger.TriggerError or ci.configs.RegistryUnreadable (2).
    `url_map` is the test-only seam of `Context.for_local`."""
    from nanosoc_rm import db
    from nanosoc_rm.ci import configs
    from nanosoc_rm.ci.trigger import build_request, canonical_urls_from
    from nanosoc_rm.config import load_config
    from nanosoc_rm.t0 import gates as G
    from nanosoc_rm.t0.gitwork import iter_pins
    from nanosoc_rm.t0.local import run_local
    from nanosoc_rm.t0.tiers import check_budget
    from nanosoc_rm.walker import walk
    from nanosoc_rm.analysis import tierrun

    overrides = dict(overrides or {})
    for path, sha in overrides.items():
        if not _SHA.match(sha):
            raise CompatUsage("--pin %s: %r is not a full 40-hex commit id" % (path, sha))
    if store and overrides:
        raise CompatUsage("--store records real runs only: a run with --pin is a what-if, never stored")
    if force_tier and not tier:
        raise CompatUsage("--force-tier needs --tier")
    config = config if config is not None else load_config()
    spec = tierrun.check_spec(configs.tiers(configuration, config).get(tier), tier, configuration, environ) \
        if tier else None
    started_at = clock.iso()

    # 1. the tree, overrides applied (UnknownConfiguration, OverrideError, WalkError propagate)
    replaced: dict[str, str] = {}
    docs = walk(configuration, config=config, store=store, overrides=overrides or None, alerts=store,
                replaced=replaced)

    # 2. the gates, locally, on the request the trigger would send plus the overrides
    nonce = nonce or secrets.token_hex(16)
    req, req_sources = build_request(configuration, nonce, config)
    request = {k: v for k, v in req.items() if k not in ("schema", "nonce")}
    request["canonical_urls"] = dict(request.get("canonical_urls") or {}, **canonical_urls_from(config, docs))
    if overrides:
        request["pin_overrides"] = dict(overrides)
    extra = {"t0.clone.upgrade-path": UPGRADE_NOT_RUN} if overrides and request.get("upgrade_from") else {}
    work, kept = _workdir(keep_work)
    try:
        ctx = G.Context.for_local(nonce=nonce, workdir=work / "run", request=request,
                                  registry_dir=Path(config.require("registry", "path")),
                                  host_bases=_host_bases(config), url_map=url_map)
        rows, items = run_local(ctx, "configuration", extra)
        root = ctx.walk()
        judged = {p.tree_path: p for p in iter_pins(root)} if root is not None else {}

        # 3. the roll-up (README section 6), waivers from the registry
        rolled = G.rollup(rows, "configuration", mode="local")
        waivers = configs.waivers(configuration, config)
        gates, unused = G.apply_waivers(rolled, items, waivers)
        verdict, not_run = G.run_verdict(gates)
        unjudged = sorted(t for t in overrides if t not in judged or judged[t].override_of is None)
        reason = None
        if unjudged:
            verdict = "UNVERIFIED"
            reason = ("the gates' anonymous walk did not reach the override(s) at %s: the what-if was not judged"
                      % ", ".join(unjudged))
        t0_finished_at = clock.iso()

        # 4. the tier (CR-12), in a sandbox checkout of the same tree
        tier_doc = None
        if spec is not None:
            if verdict == "FAIL" and not force_tier:
                tier_doc = tierrun.skipped(spec, tier, "T0 is FAIL: the %s tier was skipped (--force-tier runs it "
                                                       "anyway, UNVERIFIED for release)" % tier)
            elif root is None:
                tier_doc = tierrun._doc(spec, tier, reason="no tree to check out: the T0 walk did not start")
            else:
                layout = tierrun.materialise(ctx.git, root, work / "tree", spec.get("pins"))
                tier_doc = tierrun.run_tier(spec, tier, work / "tree", work, nonce, layout=layout,
                                            lock_dir=lock_dir)
            if force_tier and verdict == "FAIL":
                tier_doc["forced"] = True
                tier_doc["reason"] = FORCED_NOTE + ("; " + tier_doc["reason"] if tier_doc["reason"] else "")
    finally:
        if not kept:
            shutil.rmtree(work, ignore_errors=True)
    finished_at = clock.iso()

    # per pin: walker facts + each gate's item at the pin's tree path
    by_gate = {gid: {i["tree_path"]: i["result"] for i in its if i.get("tree_path")} for gid, its in items.items()}
    pins = []
    for d in docs:
        f = d["fact"]
        tps = f["tree_paths"]
        results = {}
        for gid, col in GATE_COLUMNS.items():
            got = next((by_gate.get(gid, {})[tp] for tp in tps if tp in by_gate.get(gid, {})), None)
            if got is not None:
                results[col] = got
        over = next((tp for tp in tps if tp in overrides), None)
        pins.append({"tree_path": tps[0], "tree_paths": list(tps), "component": f["child"]["component"],
                     "commit": f["child"]["commit"], "overridden": over is not None,
                     "replaces": replaced.get(over) if over is not None else None,
                     "url_kind": f["url_kind"], "state": _pinned_state(f), "split": bool(f["split"]),
                     "items": results})
    splits: dict[str, list[dict]] = {}
    for p in pins:
        if p["split"] and p["component"]:
            splits.setdefault(p["component"], []).append({"tree_path": p["tree_path"], "commit": p["commit"]})
    budget = check_budget("t0", started_at, t0_finished_at)
    fact = {
        "configuration": configuration, "commit": request["commit"], "mode": "local", "nonce": nonce,
        "overrides": [{"tree_path": t, "commit": s, "replaces": replaced.get(t)} for t, s in sorted(overrides.items())],
        "pins": pins,
        "splits": [{"component": c, "pins": ps} for c, ps in sorted(splits.items())],
        "gates": gates, "verdict": verdict, "reason": reason, "not_run": not_run,
        "unjudged_overrides": unjudged, "unused_waivers": len(unused),
        "started_at": started_at, "finished_at": finished_at, "over_budget": not budget.ok,
        "work_dir": str(work) if kept else None,
    }
    if tier_doc is not None:
        fact["tier"] = tier_doc
        fact["run_verdict"] = ladder(verdict, tier_doc["verdict"])
    times = [started_at] + [d["observed_at"] for d in docs]
    doc = {"schema": 1, "kind": "compat_result", "subject": "compat_result:%s:%s" % (configuration, nonce),
           "observed_at": min(times, key=clock.parse),
           "sources": [{"kind": "derived", "command": argv or "nanosoc-rm compat %s%s%s" % (
               configuration, "".join(" --pin %s=%s" % kv for kv in sorted(overrides.items())),
               (" --tier %s%s" % (tier, " --force-tier" if force_tier else "")) if tier else "")}] + list(req_sources),
           "fact": fact}
    if store:
        db.put(doc, config=config)
    return doc
