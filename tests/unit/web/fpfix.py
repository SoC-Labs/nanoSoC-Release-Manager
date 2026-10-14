"""Helpers for the Wave F front-page tests (CR-WF-gui): stored freshness trees built from the contract example's
shapes, and a local deployment (local sign-in, so the Wave F POSTs exist) on the example registry."""
from __future__ import annotations

import copy
import itertools
import datetime as dt
import json
import re
import shutil
import subprocess
import urllib.parse

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.freshness import rules
from webfix import EXAMPLES, make_registry

PASSWORD = "correct horse battery"
_SEQ = itertools.count()


def _templates():
    docs = [d for _, d in contracts.docs_in(EXAMPLES / "observation.freshness.example.jsonl")]
    tree = next(d for d in docs if d["fact"]["level"] == "tree")
    pin = next(d for d in docs if d["fact"]["level"] == "pin" and d["fact"]["tree_path"] == "lib")
    return tree, pin


def put_tree(cfg_path, cid, root, pins, *, frozen=False, today=None, root_days=40, observed_at=None):
    """Store one complete `freshness:<cid>` snapshot. `pins`: dicts with tree_path, component and optionally
    behind (None = not measured), bump_days (None = unknown), unpushed, lower_bound, author, pinned.
    Returns the stored docs."""
    today = today or clock.now().date()
    n = next(_SEQ)                       # each call its own snapshot, the later one newer (a re-scan)
    at = observed_at or clock.iso(clock.now() - dt.timedelta(minutes=5) + dt.timedelta(seconds=n))
    snap = "freshness:%s@%s#%08x" % (cid, at, n)
    t_tree, t_pin = _templates()
    paths = {p["tree_path"] for p in pins}
    facts = []
    for p in pins:
        d = copy.deepcopy(t_pin)
        f = d["fact"]
        tp = p["tree_path"]
        depth = 1 + sum(1 for q in paths if q != tp and tp.startswith(q + "/"))
        bump_days = p.get("bump_days", 10)
        behind = p.get("behind", 0)
        f.update(configuration=cid, frozen=frozen, judged_on=today.isoformat(), tree_path=tp, tree_paths=[tp],
                 depth=depth, path=tp.rsplit("/", 1)[-1], component=p["component"],
                 catalogued_as="component" if p["component"] else "uncatalogued",
                 pinned=p.get("pinned", "%040x" % (abs(hash((cid, tp))) & ((1 << 160) - 1))),
                 behind=behind, ahead=0, lower_bound=bool(p.get("lower_bound")),
                 on_default=None if behind is None else True, reachable=not p.get("unpushed"),
                 unpushed=bool(p.get("unpushed")), not_measured=None if behind is not None else "no mirror",
                 caveat=None)
        f["parent"] = {"component": root, "commit": "1" * 40}
        if bump_days is None:
            f["bump"] = None
        else:
            f["bump"] = dict(f["bump"], date=clock.iso(dt.datetime.combine(today - dt.timedelta(days=bump_days),
                                                                         dt.time(12), dt.timezone.utc)),
                             author=p.get("author", "rm-fixture"))
        j = rules.judge_pin(f, today)
        f["state"], f["would_be"] = j["state"], j["would_be"]
        d["subject"] = "freshness:%s:%s" % (cid, tp)
        d["observed_at"] = at
        d["snapshot"] = {"id": snap, "scope": "freshness:%s" % cid}
        facts.append(d)
    tree = copy.deepcopy(t_tree)
    tf = tree["fact"]
    j = rules.judge_tree(frozen, [d["fact"] for d in facts], today)
    tf.update(configuration=cid, frozen=frozen, silicon=frozen, judged_on=today.isoformat(), not_measured=None, **j)
    tf["root"] = {"component": root, "commit": "2" * 40,
                  "date": clock.iso(dt.datetime.combine(today - dt.timedelta(days=root_days), dt.time(12),
                                                        dt.timezone.utc))}
    tree["subject"], tree["observed_at"] = "freshness:%s" % cid, at
    tree["snapshot"] = {"id": snap, "scope": "freshness:%s" % cid}
    docs = [tree] + facts
    db.put_snapshot("freshness:%s" % cid, docs, config=cfg_path)
    return docs


# The example registry's configurations are eth-rc4 (frozen) and eth-main and multicore-master, both rooted at
# nanosoc-ethernet-chiplet (example catalogue). A small estate on them:
ETH_MAIN = [
    {"tree_path": "nanosoc-multicore-system", "component": "nanosoc-multicore-system", "behind": 19, "bump_days": 72},
    {"tree_path": "nanosoc-multicore-system/ethernet-subsystem-ahb", "component": "ethernet-subsystem-ahb",
     "behind": 34, "bump_days": 105},
    {"tree_path": "tidelink", "component": "tidelink", "behind": 3, "bump_days": 20},
    {"tree_path": "asic-flow", "component": "asic-flow", "behind": 0, "bump_days": 5},
    {"tree_path": "nanosoc_gen", "component": "nanosoc_gen", "behind": 0, "bump_days": 40},
]
ETH_RC4 = [
    {"tree_path": "tidelink", "component": "tidelink", "behind": 327, "bump_days": 45},
    {"tree_path": "asic-flow", "component": "asic-flow", "behind": None, "bump_days": 50},
]


def estate(cfg_path):
    put_tree(cfg_path, "eth-main", "nanosoc-ethernet-chiplet", ETH_MAIN, root_days=68)
    put_tree(cfg_path, "eth-rc4", "nanosoc-ethernet-chiplet", ETH_RC4, frozen=True, root_days=46)


def local_site(tmp_path, monkeypatch, seeded_db, *, users=("alice", "carol")):
    """A local deployment (sign-in, local POSTs) on the example registry with the seeded example store."""
    from nanosoc_rm import auth
    from nanosoc_rm.web import create_app
    reg = make_registry(tmp_path / "registry")
    # the catalogue as the example writes it (one line per entry, as the real registry), so a preview can edit it
    shutil.copy(EXAMPLES / "component-catalogue.example.yaml", reg / "catalogue.yaml")
    subprocess.run(["git", "commit", "-qam", "one line per entry"], cwd=reg, check=True, capture_output=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    shutil.copy(seeded_db, data / "rm.sqlite")
    h = auth.hash_password(PASSWORD, salt=b"0123456789abcdef")
    lines = ["[service]", 'data_dir = "%s"' % data, "[registry]", 'repo = "soclabs/nanosoc-registry"',
             'ref = "main"', 'path = "%s"' % reg, "[auth]", 'mode = "local"']
    for u in users:
        lines += ["[[auth.users]]", 'login = "%s"' % u, 'hash = "%s"' % h]
    cfg = tmp_path / "rm.toml"
    cfg.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    return {"cfg": cfg, "registry": reg, "data": data, "app": create_app(cfg)}


def sign_in(client, login="alice"):
    r = client.get("/auth/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/auth/login", data={"csrf_token": token, "login": login, "password": PASSWORD, "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return client


def csrf(text):
    return re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)


def jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def post_form(client, url, pairs, **kw):
    """POST a form with repeated fields (`pairs` [(name, value)]), as a browser sends ticked boxes."""
    return client.post(url, content=urllib.parse.urlencode(pairs),
                       headers={"content-type": "application/x-www-form-urlencoded"}, **kw)
