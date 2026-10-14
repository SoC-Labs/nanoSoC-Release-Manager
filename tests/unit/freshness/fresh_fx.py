"""A hermetic estate for the freshness tests, built on the pin-walker acceptance fixtures (LocalHosts: two local
'hosts' of bare repos reached through url.insteadOf). Every commit gets a fixed committer date, so day counts are
exact against TODAY.

    top (github SoC-Labs/top), main = c2 (TODAY - 10 d); c1 (TODAY - 200 d) added every gitlink
      mid   -> mid head (current; bumped at c1)        mid/deps/leaf -> leaf[0], 11 behind (outdated, depth 2)
      lib   -> lib[1] of 3, 1 behind, bumped at c2 (behind)
      old   -> old[0] of 3, 2 behind, bumped at c1, 200 d ago (outdated by days)
      cur   -> cur head (current)
    edge (github SoC-Labs/edge), main: cur (current), gone (a commit no host has: unpushed), u (uncatalogued URL)
    calm (github SoC-Labs/calm), main: cur (current), u (not measured), lib -> lib[1] (behind: the same pin as top)

Configurations: top-main (branch), top-v1 (frozen at c2, silicon), edge-main, calm-main.
"""
from __future__ import annotations

import datetime as dt
import functools
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TODAY = dt.date(2026, 10, 14)
UNKNOWN_URL = "https://example.org/someone/thing.git"
UNKNOWN_SHA = "1234567890abcdef1234567890abcdef12345678"


@functools.lru_cache(maxsize=None)
def at_pins():
    spec = importlib.util.spec_from_file_location("fresh_at_test_pins", REPO / "tests" / "acceptance" / "test_pins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _at(h, days_ago: int):
    """Commit dates for the next h.commit(): TODAY - days_ago, 12:00 UTC."""
    t = dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(12, 0), tzinfo=dt.timezone.utc)
    stamp = t.strftime("%Y-%m-%dT%H:%M:%S+0000")
    h.env["GIT_COMMITTER_DATE"] = h.env["GIT_AUTHOR_DATE"] = stamp


def chain(h, host, path, n, days_ago):
    shas, parents = [], []
    for i in range(n):
        _at(h, days_ago - i)
        sha = h.commit(host, path, parents=parents, files={"n": "%s %d\n" % (path, i)})
        shas.append(sha)
        parents = [sha]
    h.push(host, path, shas[-1], "main")
    return shas


def build(h):
    gh, gl = "github", "gitlab"
    leaf = chain(h, gl, "soclabs/leaf", 12, 300)
    lib = chain(h, gh, "SoC-Labs/lib", 3, 150)
    old = chain(h, gh, "SoC-Labs/old", 3, 260)
    cur = chain(h, gh, "SoC-Labs/cur", 2, 120)
    _at(h, 250)
    mid = h.commit(gh, "SoC-Labs/mid", gitlinks={"deps/leaf": leaf[0]},
                   gitmodules=[{"path": "deps/leaf", "url": h.url(gl, "soclabs/leaf")}])
    h.push(gh, "SoC-Labs/mid", mid, "main")
    _at(h, 400)
    gone = h.commit(gh, "SoC-Labs/cur", parents=[cur[-1]], files={"n": "never pushed\n"})   # in a dev repo only
    mods = lambda names: [{"path": n, "url": h.url(gh, "SoC-Labs/%s" % n)} for n in names]
    _at(h, 200)
    c1 = h.commit(gh, "SoC-Labs/top", gitlinks={"mid": mid, "lib": lib[0], "old": old[0], "cur": cur[-1]},
                  gitmodules=mods(["mid", "lib", "old", "cur"]))
    _at(h, 10)
    c2 = h.commit(gh, "SoC-Labs/top", parents=[c1], gitlinks={"mid": mid, "lib": lib[1], "old": old[0], "cur": cur[-1]},
                  gitmodules=mods(["mid", "lib", "old", "cur"]))
    h.push(gh, "SoC-Labs/top", c2, "main")
    h.tag(gh, "SoC-Labs/top", "v1", c2)
    _at(h, 5)
    edge = h.commit(gh, "SoC-Labs/edge", gitlinks={"cur": cur[-1], "gone": gone, "u": UNKNOWN_SHA},
                    gitmodules=[{"path": "cur", "url": h.url(gh, "SoC-Labs/cur")},
                                {"path": "gone", "url": h.url(gh, "SoC-Labs/cur")},
                                {"path": "u", "url": UNKNOWN_URL}])
    h.push(gh, "SoC-Labs/edge", edge, "main")
    _at(h, 40)
    calm = h.commit(gh, "SoC-Labs/calm", gitlinks={"cur": cur[-1], "u": UNKNOWN_SHA, "lib": lib[1]},
                    gitmodules=[{"path": "cur", "url": h.url(gh, "SoC-Labs/cur")}, {"path": "u", "url": UNKNOWN_URL},
                                {"path": "lib", "url": h.url(gh, "SoC-Labs/lib")}])
    h.push(gh, "SoC-Labs/calm", calm, "main")
    comp = lambda i, host, path, cls="ip_tech": {"id": i, "class": cls, "canonical": {"host": host, "path": path}}
    catalogue = {"schema": 1, "components": [
        comp("top", gh, "SoC-Labs/top", "soc_ref"), comp("edge", gh, "SoC-Labs/edge", "soc_ref"),
        comp("calm", gh, "SoC-Labs/calm", "soc_ref"), comp("mid", gh, "SoC-Labs/mid"),
        comp("leaf", gl, "soclabs/leaf"), comp("lib", gh, "SoC-Labs/lib"), comp("old", gh, "SoC-Labs/old"),
        comp("cur", gh, "SoC-Labs/cur")]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "top-main", "component": "top", "ref": {"branch": "main"}},
        {"id": "top-v1", "component": "top", "ref": {"tag": "v1"}, "commit": c2, "frozen": True,
         "silicon": {"chip": "top-chip", "revision": "r0p0"}},
        {"id": "edge-main", "component": "edge", "ref": {"branch": "main"}},
        {"id": "calm-main", "component": "calm", "ref": {"branch": "main"}}]}
    registry = at_pins().write_registry(h.root / "registry-fresh", catalogue, eligibility, examples=False)
    return {"registry": registry, "c1": c1, "c2": c2, "mid": mid, "leaf": leaf, "lib": lib, "old": old, "cur": cur,
            "gone": gone, "edge": edge, "calm": calm,
            "components": ["top", "edge", "calm", "mid", "leaf", "lib", "old", "cur"],
            "configurations": ["top-main", "top-v1", "edge-main", "calm-main"]}


def synced_and_walked(h, fx, name="fresh"):
    env = h.tool_env(fx["registry"], name)
    with env.active():
        for cid in fx["components"]:
            at_pins().rm("mirror.sync_component")(cid)
        for cfg in fx["configurations"]:
            at_pins().rm("walker.walk")(cfg)
    return env
