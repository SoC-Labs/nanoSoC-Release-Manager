"""The class measurement: regenerate one configuration at two commits of a component and diff (section 20).

    measure(configuration, component, frm, to, *, config, store, ...) -> regen_class observation
    stored_signals(component, frm, to, *, config) -> (consumers signal, build signal) | None
    pending(config) -> [{configuration, component, from, to, why}]       pairs a plan or a bump would use, unmeasured

The class is versioning.classify.diff_outputs over the two normalised renders: the C3 rules of the decision table
(docs/versioning.md): a removed file, or a removed or changed port, instance, address or IRQ line, is breaking; an
added file or shape line is feature; any other text change is fix; identical output is none. C7: build-affecting
when any `flist/*` variant's file list differs.

A generator that renders the configuration at `frm` and fails for it at `to` is `breaking` (C5: a consumer that
built no longer builds); C7 is then not measured (build_affecting null).

Verdict: PASS when the class is measured; FAIL when the generator failed at `frm` (no baseline to diff); INFRA
when a render could not run on this host (a pin not fetched, a tool absent); UNVERIFIED when an override was not
reached. Only a PASS carries a class.
"""
from __future__ import annotations

import secrets

from nanosoc_rm import clock, contracts
from nanosoc_rm.regen import sandbox as SB

__all__ = ["KIND", "measure", "observe", "subject_key", "stored", "stored_signals", "pending", "EXIT", "MAX_REASONS"]

KIND = "regen_class"
EXIT = {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2, "INFRA": 2}
MAX_REASONS = 40
_LADDER = ("FAIL", "UNVERIFIED", "INFRA", "PASS")


def subject_key(f: dict) -> str:
    return "%s:%s:%s:%s" % (f["component"], f["from"]["commit"], f["to"]["commit"], f["configuration"])


def observe(fact: dict, sources: list[dict], command: str) -> dict:
    seen, srcs = set(), []
    for s in [{"kind": "derived", "command": command}] + list(sources):
        k = (s["kind"], s["command"])
        if k not in seen:
            seen.add(k)
            srcs.append(s)
    doc = {"schema": 1, "kind": KIND, "subject": "%s:%s" % (KIND, subject_key(fact)), "observed_at": clock.iso(),
           "sources": srcs[:60], "fact": fact}
    problems = contracts.validate_observation(doc)
    if problems:
        raise ValueError("%s breaks the observation contract: %s" % (KIND, "; ".join(problems[:6])))
    return doc


def _files(before: dict, after: dict) -> dict:
    a, b = set(before), set(after)
    changed = sorted(p for p in a & b if before[p] != after[p])
    return {"added": sorted(b - a)[:100], "removed": sorted(a - b)[:100], "changed": changed[:100],
            "counts": {"added": len(b - a), "removed": len(a - b), "changed": len(changed),
                       "unchanged": len(a & b) - len(changed)}}


def measure(configuration: str, component: str, frm: str, to: str, *, config, store: bool = False,
            paths: list[str] | None = None, url_map=None, fresh: bool = False, argv: str | None = None) -> dict:
    """The regen_class observation of moving `component` from `frm` to `to` in `configuration` (module docstring).
    RegenUsage (exit 3) for a refused spec or an unknown tree path; walker.UnknownConfiguration propagates."""
    from nanosoc_rm.versioning.classify import diff_outputs
    started = clock.iso()
    request, req_sources = SB.request_for(configuration, config)
    a = SB.render(configuration, component, frm, config=config, request=request, paths=paths, url_map=url_map,
                  fresh=fresh)
    b = SB.render(configuration, component, to, config=config, request=request, paths=paths, url_map=url_map,
                  fresh=fresh)
    verdict = next(v for v in _LADDER if v in (a.verdict, b.verdict) or v == "PASS")
    cls, reasons, files, flists_changed, build = None, [], None, [], None
    reason = None
    if a.status == "ok" and b.status == "failed":
        # C5: the configuration rendered at `frm` and the generator fails for it at `to`: a consumer that built
        # before no longer builds, the most breaking change there is. Measured; C7 is not (no file list at `to`).
        verdict, cls = "PASS", "breaking"
        reasons = ["the generator fails for %s at %s and rendered at %s (C5: a consumer that built no longer "
                   "builds): %s" % (configuration, to[:12], frm[:12], b.reason)]
    elif verdict == "PASS":
        cls, reasons = diff_outputs(a.outputs, b.outputs)
        files = _files(a.outputs, b.outputs)
        if cls == "fix" and not reasons:
            reasons = ["%d file(s) changed, no port, instance, address or IRQ line among the changes: %s"
                       % (files["counts"]["changed"], ", ".join(files["changed"][:5]))]
        flists_changed = sorted(v for v in set(a.flists) | set(b.flists) if a.flists.get(v) != b.flists.get(v))
        build = bool(flists_changed)
        gone = sorted(set(a.binary) - set(b.binary))
        if gone:
            cls = "breaking"
            reasons.append("removed %d binary or oversized file(s), e.g. %s" % (len(gone), gone[0]))
    else:
        bad = [r for r in (a, b) if r.verdict == verdict]
        reason = "; ".join("%s at %s: %s" % (r.status, r.commit[:12], r.reason) for r in bad)[:600]
    coverage = sorted(set(a.coverage) | set(b.coverage))[:40]
    fact = {
        "configuration": configuration, "component": component, "tree_paths": list(a.tree_paths),
        "root_commit": request.get("commit"), "generator": "sandbox",
        "from": a.summary(), "to": b.summary(),
        "class": cls, "reasons": reasons[:MAX_REASONS], "files": files,
        "build_affecting": build, "flists_changed": flists_changed[:50],
        "coverage": coverage,
        "verdict": verdict, "reason": reason,
        "started_at": started, "finished_at": clock.iso(), "nonce": secrets.token_hex(16),
    }
    obs = observe(fact, list(req_sources) + a.sources[:1],
                  argv or "nanosoc-rm regen class %s --component %s --from %s --to %s" % (configuration, component,
                                                                                        frm, to))
    if store:
        from nanosoc_rm import db
        db.put(obs, config=config)
    return obs


def stored(component: str, frm: str, to: str, *, config) -> list[dict]:
    """Every configuration's newest stored measurement of exactly this move."""
    from nanosoc_rm import db
    try:
        return db.latest(KIND, "%s:%s:%s:%s:" % (KIND, component, frm, to), config=config)
    except Exception:                                                  # noqa: BLE001 - no store: nothing measured
        return []


def stored_signals(component: str, frm: str | None, to: str | None, *, config) -> tuple[dict, dict] | None:
    """The C3 consumers signal and the C7 build signal from the stored measurements of this move, or None when
    none is stored. A non-PASS measurement leaves the signal unmeasured with its reason, and so does a consumer that
    declares a regen command and pins the component but has no stored measurement (the live path regenerates every
    such consumer; C3 complete needs each one)."""
    from nanosoc_rm.versioning import classify as cf
    from nanosoc_rm.versioning import semver
    if not frm or not to or config is None:
        return None
    docs = stored(component, frm, to, config=config)
    if not docs:
        return None
    per, classes, unm, build, changed = [], [], [], False, []
    for d in sorted(docs, key=lambda d: d["fact"]["configuration"]):
        f = d["fact"]
        per.append({"consumer": f["configuration"], "class": f["class"], "reasons": f["reasons"][:20],
                    "verdict": f["verdict"], "subject": d["subject"], "observed_at": d["observed_at"]})
        if f["verdict"] == "PASS":
            classes.append(f["class"])
            if f["build_affecting"]:
                build = True
                changed += ["%s/%s" % (f["configuration"], v) for v in f["flists_changed"]]
        else:
            unm.append("%s %s (%s)" % (f["configuration"], f["verdict"], f["reason"] or ""))
    have = {p["consumer"] for p in per}
    try:                     # every consumer that declares a regen command and pins the component counts (C3)
        want = SB.SandboxRegenerator(config).consumers(component)
    except Exception:                                                  # noqa: BLE001 - no registry here
        want = []
    for c in want:
        if c not in have:
            unm.append("%s not measured (no stored regen_class: `nanosoc-rm regen class %s --component %s --from %s "
                       "--to %s --store`)" % (c, c, component, frm, to))
    cls = semver.max_class(classes) if classes else None
    status = "measured" if classes and not unm else "unmeasured"
    detail = "%d consumer(s) regenerated in a sandbox (stored regen_class)" % len(classes)
    if unm:
        detail += "; not measured: " + "; ".join(unm)
    srcs = [{"kind": "derived", "command": "stored %s" % p["subject"]} for p in per]
    consumers = cf.signal("consumers", cls, status, detail[:500], generator="sandbox", consumers=per, sources=srcs)
    if classes and not unm:
        bsig = cf.signal("build", None, "measured", ("file lists changed: " + ", ".join(changed)) if changed
                         else "no file list changed", build_affecting=build)
    else:                    # as the live build signal: a changed list is known, an unchanged one is not
        why = (("file lists changed: " + ", ".join(changed) + "; ") if changed else "") + "not measured: " + \
            "; ".join(unm or ["no measurement passed"])
        bsig = cf.signal("build", None, "unmeasured", why[:500], build_affecting=True if changed else None)
    return consumers, bsig


def pending(config) -> list[dict]:
    """The moves a stored bump proposal or version plan would class with a regeneration, that no stored
    measurement covers, for each configuration that declares a regen command and pins the component."""
    from nanosoc_rm import db
    regen = SB.SandboxRegenerator(config)
    consumers = regen.consumers_all()
    if not consumers:
        return []
    moves: dict[tuple[str, str, str], str] = {}
    try:
        for d in db.latest("version_plan", config=config):
            f = d["fact"]
            base = (f.get("current") or {}).get("commit")
            if base and f.get("commit") and base != f["commit"] and not f.get("worktree"):
                moves.setdefault((f["target"], base, f["commit"]), "version plan %s" % f["target"])
    except Exception:                                                  # noqa: BLE001 - no store
        pass
    try:
        from nanosoc_rm.bumps import store as bstore
        for row in bstore.proposals(config):
            f = row["doc"]["fact"]
            moves.setdefault((f["dependency"]["component"], f["from"]["commit"], f["to"]["commit"]),
                             "bump %s" % row["id"])
    except Exception:                                                  # noqa: BLE001 - bumps not stored here
        pass
    out = []
    for (component, frm, to), why in sorted(moves.items()):
        have = {d["fact"]["configuration"] for d in stored(component, frm, to, config=config)}
        for c in consumers:
            if c not in have and SB.tree_paths(c, component, config):
                out.append({"configuration": c, "component": component, "from": frm, "to": to, "why": why})
    return out
