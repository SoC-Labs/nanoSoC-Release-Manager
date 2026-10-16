"""What each tree of a configuration can test, where, and its last result (the Capabilities page, the CLI `show`).

    tree_view(config, configuration, fetch=False) -> dict   one dict per tree of the latest STORED walk, never a re-walk
    last_results(config, configuration) -> {key: {verdict, job, side, finished_at, commit}}

Each tree's `.nanosoc/capabilities.yaml` is read at the commit the stored walk pins (from its mirror), so the page
shows what the tree at THAT commit declares. "Runs on" lists the registry tiers.yaml hosts whose site data can run
the capability (tools, versions, licences, needs); "Last result" is the newest finished test-bump row for that
capability at that same commit (the Now side of a test-bump, or the After side of one whose move has landed).
Reads only: stored walk, mirrors, registry, job records. Nothing runs and nothing is queued. By default nothing is
fetched either (the pages): a manifest blob the partial-clone mirror lacks reads "unreadable" until the CLI
(`capabilities show`, fetch=True) or a test-bump reads it.
"""
from __future__ import annotations

__all__ = ["tree_view", "last_results"]


def last_results(config, configuration: str) -> dict[str, dict]:
    from nanosoc_rm import jobs
    out: dict[str, dict] = {}
    try:
        records = jobs.jobs(config, limit=200, kind="test-bump", configuration=configuration, states=("done",))
    except Exception:                                    # noqa: BLE001 - no job store yet
        return out
    for j in records:                                    # newest first: the first seen per (key, commit) wins
        f = ((j.get("result") or {}).get("fact")) or {}
        for r in f.get("rows") or []:
            for side in ("now", "after"):
                res = r.get(side)
                if not res or not res.get("commit"):
                    continue
                k = (r["key"], res["commit"])
                if k not in out:
                    out[k] = {"verdict": res.get("verdict"), "job": j["id"], "side": side,
                              "finished_at": j.get("finished_at"), "commit": res["commit"],
                              "href": "/capabilities/%s/bump?job=%d" % (configuration, j["id"])}
    return out


def tree_view(config, configuration: str, *, fetch: bool = False) -> dict:
    """{configuration, root_commit, trees: [...], composed: [...], problems, hosts} (module docstring)."""
    from nanosoc_rm.analysis import _inputs as I
    from nanosoc_rm.capabilities import manifest as MF
    from nanosoc_rm.capabilities import plan as PL
    from nanosoc_rm.capabilities.run import _root_node
    from nanosoc_rm.ci import configs
    docs = I.stored_tree(configuration, config)           # CannotAnswer when never walked
    root = _root_node(config, configuration, docs)
    nodes = PL.nodes_of(docs, root)
    comp = PL.composed(nodes, MF.Reader(config, fetch=fetch))
    try:
        sites = configs._registry(config).sites()
    except Exception:                                    # noqa: BLE001 - the page says so
        sites = {}
    last = last_results(config, configuration)
    in_set = {it.key: it.origin for it in comp.items}
    trees = []
    for tp in sorted(nodes, key=lambda t: (t.count("/") if t else -1, t)):
        n, r = nodes[tp], comp.reads[tp]
        tools = (r.doc or {}).get("tools") or {}
        caps = []
        for c in r.capabilities():
            key = "%s:%s" % (PL.owner_label(tp), c["id"])
            hosts = PL.hosts_for(c, tools, sites)
            why_not = {h: PL.host_check(c, tools, s or {}) for h, s in sites.items() if h not in hosts}
            caps.append({"key": key, "id": c["id"], "kind": c["kind"], "tier": c.get("tier"), "gate": c["gate"],
                         "runtime": c["runtime"], "budget_s": c["budget_s"],
                         "command": c["run"] if isinstance(c["run"], str) else " ".join(c["run"]),
                         "cwd": c["cwd"], "tools": [dict(name=t, **tools.get(t, {})) for t in c["tools"]],
                         "needs": c["needs"], "affected_by": c["affected_by"], "when": c["when"],
                         "manual": c.get("manual", False), "hosts": hosts, "why_not": why_not,
                         "composed": in_set.get(key), "last": last.get((key, n.commit))})
        trees.append({"tree_path": PL.owner_label(tp), "component": n.component, "commit": n.commit,
                      "status": r.status, "problems": r.problems, "sources": r.sources,
                      "compose": (r.doc or {}).get("compose") or [], "capabilities": caps})
    return {"configuration": configuration, "root_commit": root.commit, "trees": trees,
            "composed": [{"key": it.key, "origin": it.origin, "kind": it.cap["kind"], "tier": it.cap.get("tier"),
                          "gate": it.cap["gate"]} for it in comp.items],
            "problems": comp.problems, "hosts": sorted(sites),
            "declared": sum(1 for t in trees if t["status"] == "ok"), "total": len(trees)}
