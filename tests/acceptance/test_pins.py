"""Acceptance tests: pins, trees and hosts (B1, B2, B3, B4, B5, B6, B8).

Specification: docs/acceptance/pins.md (one entry AT-<ID> per requirement).
Contracts: docs/contracts/ and schemas/.

Gate 2 (PR3): these are skeletons. Every acceptance test carries NOT_BUILT
(xfail, raises=ImportError, strict): it reports XFAIL until its module is built,
FAILS on any other error, and FAILS as XPASS(strict) once it passes, so its builder
must remove the marker (tests/conftest.py, interfaces.md section 17.4).

Each requirement has a pair of tests (PR6):

    test_AT_<ID>                   check_<ID>() on the real (or hermetic) input
    test_AT_<ID>_negative_control  the SAME check_<ID>() on a deliberately broken
                                   input; it passes only if the check reports a failure

Because both call one check function, the control proves that this exact check
can fail.

Fixtures (pins.md, "Fixtures"):

    live       real mirrors, fetched read-only from both hosts into a test-owned
               data dir. Needs NANOSOC_RM_CONFIG (the private service config: its
               read tokens and host table are used, its data dir is not).
               NANOSOC_RM_TEST_DATA optionally names a test-owned data dir to reuse
               between runs. Never writes to a host (A2, PR8). Marked `live`.
    shadow     a copy of the live data dir whose repos borrow the live objects
               through objects/info/alternates. Its refs are edited locally to
               build broken inputs. Never synced: a sync would fetch the refs back.
    hermetic   local bare repos stand in for both hosts (git url.insteadOf), plus
               a fake host API. No network.

A live test without NANOSOC_RM_CONFIG FAILS; it never skips and never passes
(interfaces.md section 17.4). Off the service VM, deselect with -m "not live".

test_selfcheck_* are NOT acceptance tests of the tool. They test this file: each
check_<ID>() passes on a consistent real input and fails on the broken one
(PR6 at checker level), and the hermetic fixtures really hold the states they
claim (a raw-git oracle, no tool code). They need only git and Python, and run
now; pins.md, "Negative controls run today", records their result.

Every name beyond the schemas is fixed in interfaces.md section 17 (the gap
index there says where each gap G<n> of pins.md was resolved). Each such name
lives in one helper, so a change reaches one place: service_config(), rm(),
as_doc(), ref_prefix(), pin_level_state().
"""
from __future__ import annotations

import contextlib
import copy
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "schemas"
EXAMPLES = SCHEMAS / "examples"


def _load_check_examples():
    """Reuse the gate-2 checker's strict YAML loader and schema registry (PR9)."""
    spec = importlib.util.spec_from_file_location("rm_check_examples", SCHEMAS / "check_examples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CE = _load_check_examples()
SCHEMA_DOCS, SCHEMA_REGISTRY = CE.load_schemas()

CLASSES = frozenset({"soc_ref", "example", "generator", "arch_tech", "ip_tech", "toolkit", "infra", "legacy"})

# Not built yet: XFAIL while the module is missing, red on any other error, red as XPASS(strict)
# once it passes (interfaces.md section 17.4). A builder removes it from each test their module passes.
NOT_BUILT = pytest.mark.xfail(raises=ImportError, strict=True, reason="not implemented: gate 2")

# --------------------------------------------------------------------------------------------
# Real facts, observed 2026-09-30 with read-only calls (pins.md, "Facts").
# --------------------------------------------------------------------------------------------
RC4 = "ab73e40924317e45e7b3b941f52a7ed35f20832e"
ETH_MAIN_20260930 = "8e6d0434fc8331191607ad5b2af73646e995c892"          # 43 pins, 13 keys shared with rc4
MULTICORE_MASTER_20260930 = "127e3d230cba6d8c0760685413d91755d02be786"  # 35 pins, depth <= 4

GEN_FIX_036D381 = "036d3811e630c89b2344b56aa617b40a99cd652b"  # SL-DEF-0001 fix, form A (nanosoc_gen)
GEN_ORIGIN_B0A6069 = "b0a606960c349aeac4a00f67b3dbd13f55cb5f24"  # SL-DEF-0001 origin in nanosoc_gen
GEN_D9973B3 = "d9973b3740b4592c714e0ce722ec0a66cb1f860b"
GEN_1D7772D = "1d7772dd0cd0e135f86f7d9906f4852883c4670e"
GEN_D04DE2E = "d04de2ec25d79c1fc3223f853ddb6725cbf2831c"
# nanosoc_gen ancestry (merge-base --is-ancestor, 2026-09-30): b0a6069 -> ... -> 1d7772d -> d9973b3
# -> c2ca528 -> 036d381 -> adbb9ab (main); d04de2e branches off d9973b3. So neither rc4 generator
# contains the fix 036d381, both contain the origin b0a6069, and only d04de2e contains d9973b3.

TIDELINK_RC4 = "5e8bdb5a2141ff5a82103813a326a2c9df2b43ed"                  # v0.90; absent on the GitLab copy
TIDELINK_GITLAB_MAIN_20260930 = "9092300b576d9b090e941ba73746c3518ed9e636"  # an ancestor of 5e8bdb5 (293 behind)

RC4_GEN_PATH_A = "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen"                        # d04de2e
RC4_GEN_PATH_B = "nanosoc-multicore-system/ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen"  # 1d7772d

# B3: the second host of every rc4 pin whose component has a copy. tree path -> (host, state, min lag)
RC4_OTHER_HOST = {
    "tidelink": ("gitlab", "MISSING", None),
    "nanosoc-multicore-system/nanosoc_arch_tech": ("github", "BRANCH_ONLY", None),
    "nanosoc-multicore-system/ethernet-subsystem-ahb/nanosoc_arch_tech": ("github", "ON_DEFAULT", 73),
    "nanosoc-multicore-system/ahb_qspi": ("github", "MISSING", None),
    "ASIC/asic-flows": ("gitlab", "MISSING", None),
    "tidelink/deps/axi-chiplet-controller/asic_flow": ("github", "ON_DEFAULT", 5),
    "nanosoc-multicore-system/syn/asic/asic_flow": ("github", "ON_DEFAULT", 4),
}

# B3: .gitmodules branch= exactly as written at rc4. Recorded, never used. Three of them
# (tidelink-phy, asic-toolkit, ethernet-mac-ahb) say `main` for commits that are NOT on main
# (all PROTECTED_BY_TAG): a tool that trusted branch= would call them ON_DEFAULT.
RC4_BRANCH_FIELDS = {
    "tidelink/deps/tidelink-phy": "main",
    "ASIC/asic-toolkit": "main",
    "nanosoc-multicore-system/ethernet-subsystem-ahb/nanosoc_arch_tech": "main",
    "nanosoc-multicore-system/ethernet-subsystem-ahb/ethernet-mac-ahb": "main",
    "ASIC/asic-flows": "lpddr4-pll",
}

# B3: multicore-master at 127e3d2, state on the pinned host. API-derived on 2026-09-30
# (walk.py + tag/lag enrichment); a person confirms it by hand (HAND_CHECK).
# (tree path, component, pinned host, commit, state, min lag, url_kind, branch_field)
MULTICORE_BASELINE = [
    ('ahb_qspi', 'ahb_qspi', 'gitlab', "a0d3d517c603fb9434498a2931faa24735be08a5", 'ON_DEFAULT', 5, 'https', None),
    ('ahb_qspi/soctools_flow', 'soctools_flow', 'gitlab', "0784ddc7183d3d4cf246985d89c9b7589e0bb430", 'ON_DEFAULT', 1, 'https', None),
    ('ethernet-subsystem-ahb', 'ethernet-subsystem-ahb', 'gitlab', "d6e200615bce0e03cbb4a0e0b8c49c42c65a71af", 'ON_DEFAULT', 32, 'https', None),
    ('ethernet-subsystem-ahb/ethernet-mac-ahb', 'ethernet-mac-ahb', 'gitlab', "47080d400acec545cc688cdd45fe6250ce9c4395", 'ON_DEFAULT', 5, 'https', 'main'),
    ('ethernet-subsystem-ahb/ethernet-mac-ahb/amba_wb_bridges', 'ahb-wb-bridges', 'gitlab', "e3a77b45acbd1107fb81e2e572463506a6154d4d", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/firmware/libtelnet', 'libtelnet', 'github', "5f5ecee776b9bdaa4e981e5f807079a9c79d633e", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/firmware/picotcp', 'picotcp', 'github', "46120abecc9fb79f7dfb2cc8192341a42e40fc8b", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech', 'nanosoc_arch_tech', 'gitlab', "06b4d24b8a377e6128d645e19c244f5577ab89bd", 'ON_DEFAULT', 62, 'https', 'main'),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/nanosoc_gen', 'nanosoc_gen', 'gitlab', "1d7772dd0cd0e135f86f7d9906f4852883c4670e", 'ON_DEFAULT', 39, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/extio8x4-axis', 'extio8x4-axis', 'gitlab', "5db46c803946656b773a04268d1ebdf183ebfd09", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/hostio4', 'hostio4', 'gitlab', "b692d01c8d9372d572558737e564f14d109c3647", 'ON_DEFAULT', 2, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/sl_ams_tech', 'sl_ams_tech', 'gitlab', "e9ed3dbfe43a2e0205f4fb86d914ec23c97286eb", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/slcorem0_tech', 'slcorem0_tech', 'gitlab', "d8db2016b5e5aef39cb42aadb3707a54e93d727f", 'ON_DEFAULT', 1, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/slcorem0p_tech', 'slcorem0p_tech', 'gitlab', "eda4fdfb1026f82bba5c0a0be775463096300192", 'ON_DEFAULT', 1, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/sldma230_tech', 'sldma230_tech', 'gitlab', "7105d696b8d67e4b70c9c54cb14c807a311a24b3", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/sldma350_tech', 'sldma350_tech', 'gitlab', "2b77bd0c160e75dec2479c4ed934ba85276dc994", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/socdebug_tech', 'socdebug_tech', 'gitlab', "247a0afe2a4317369af893a5b5454487c20ab90e", 'ON_DEFAULT', 5, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/socdebug_tech/socket/uart_axi_master/core_dbg_bridge', 'core_dbg_bridge', 'github', "3ea8f99cb458acf3f6cfe27862afe77d560caf08", 'ON_DEFAULT', 0, 'https', None),
    ('ethernet-subsystem-ahb/nanosoc_arch_tech/rtl/synopsys_28nm_slm_integration', 'synopsys_28nm_slm_integration', 'gitlab', "7b6139d3a9c7bcdce9f7a02034bbbd82e205a8db", 'ON_DEFAULT', 0, 'https', None),
    ('inter-processor-communications-ahb', 'inter-processor-communications-ahb', 'gitlab', "8f88eb4b44bab93b02eebf37c9241d1c4be8f755", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech', 'nanosoc_arch_tech', 'gitlab', "490d6d04a8b4546acbd742cc06ea1b456b6319af", 'ON_DEFAULT', 36, 'https', None),
    ('nanosoc_arch_tech/nanosoc_gen', 'nanosoc_gen', 'gitlab', "c2ca5288d5a923d9979d3fa043e3b31b40b10378", 'ON_DEFAULT', 25, 'https', None),
    ('nanosoc_arch_tech/rtl/coresight_soc400_tech', 'soc400-tech', 'github', "9bdae19b3517ed2be0f97e023e89d94801fc1587", 'ON_DEFAULT', 0, 'ssh', None),
    ('nanosoc_arch_tech/rtl/extio8x4-axis', 'extio8x4-axis', 'gitlab', "5db46c803946656b773a04268d1ebdf183ebfd09", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/hostio4', 'hostio4', 'gitlab', "a72b000d7c7f1833d87745214b2b5a5d159ec79e", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/sl_ams_tech', 'sl_ams_tech', 'gitlab', "e9ed3dbfe43a2e0205f4fb86d914ec23c97286eb", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/slcorem0_tech', 'slcorem0_tech', 'gitlab', "d8db2016b5e5aef39cb42aadb3707a54e93d727f", 'ON_DEFAULT', 1, 'https', None),
    ('nanosoc_arch_tech/rtl/slcorem0p_tech', 'slcorem0p_tech', 'gitlab', "eda4fdfb1026f82bba5c0a0be775463096300192", 'ON_DEFAULT', 1, 'https', None),
    ('nanosoc_arch_tech/rtl/sldma230_tech', 'sldma230_tech', 'gitlab', "7105d696b8d67e4b70c9c54cb14c807a311a24b3", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/sldma350_tech', 'sldma350_tech', 'gitlab', "2b77bd0c160e75dec2479c4ed934ba85276dc994", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/socdebug_tech', 'socdebug_tech', 'gitlab', "c0a796def1fb72c873bb15b599c21e14e971f684", 'ON_DEFAULT', 4, 'https', None),
    ('nanosoc_arch_tech/rtl/socdebug_tech/socket/uart_axi_master/core_dbg_bridge', 'core_dbg_bridge', 'github', "3ea8f99cb458acf3f6cfe27862afe77d560caf08", 'ON_DEFAULT', 0, 'https', None),
    ('nanosoc_arch_tech/rtl/synopsys_28nm_slm_integration', 'synopsys_28nm_slm_integration', 'gitlab', "7b6139d3a9c7bcdce9f7a02034bbbd82e205a8db", 'ON_DEFAULT', 0, 'https', None),
    ('ptp-hardware-clock-ahb', 'ptp-hardware-clock-ahb', 'gitlab', "0874ab62772783aa4a4da4bb97e18f9dad49160f", 'ON_DEFAULT', 0, 'https', None),
    ('syn/asic/asic_flow', 'asic-flow', 'gitlab', "46c88d3fcc3f49dc8b4ed1913d2c18bd31a3a066", 'ON_DEFAULT', 1, 'https', None),
]
MULTICORE_OTHER_HOST = {
    "nanosoc_arch_tech": ("github", "ON_DEFAULT", 47),
    "ethernet-subsystem-ahb/nanosoc_arch_tech": ("github", "ON_DEFAULT", 73),
    "ahb_qspi": ("github", "ON_DEFAULT", 1),
    "syn/asic/asic_flow": ("github", "ON_DEFAULT", 4),
}

# B3 done-when: "The states match a hand check on rc4 and multicore." A person re-runs the
# golden's commands by hand, then fills these in. AT-B3 fails while either is empty.
HAND_CHECK = {
    "eth-rc4": {"by": None, "on": None},
    "multicore-master-20260930": {"by": None, "on": None},
}

# The live registry adds two test-only frozen configurations (fixed trees) and the one
# component multicore pins that the example catalogue lacks. ASSUMED (G13): a frozen
# configuration on a branch ref is walked at its `commit` and raises no alert when the branch moves.
SOC400 = {"id": "soc400-tech", "class": "ip_tech", "canonical": {"host": "github", "path": "SoC-Labs/SoC-Labs-SoC400-Tech"}}
TEST_CONFIGURATIONS = [
    {"id": "eth-main-20260930", "component": "nanosoc-ethernet-chiplet", "ref": {"branch": "main"},
     "commit": ETH_MAIN_20260930, "frozen": True},
    {"id": "multicore-master-20260930", "component": "nanosoc-multicore-system", "ref": {"branch": "master"},
     "commit": MULTICORE_MASTER_20260930, "frozen": True},
]


# --------------------------------------------------------------------------------------------
# Contract access. Every assumption beyond interfaces.md sits in one of these helpers.
# --------------------------------------------------------------------------------------------
def rm(dotted):
    """Import a contracted function or class (module paths: interfaces.md section 17.2):
    registry.load_registry, registry.RegistryError, walker.walk, analysis.hosts.compare_hosts,
    analysis.contains.contains, analysis.protect.silicon_protection, mirror.sync_component,
    mirror.detect_rewrite, db.latest."""
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module("nanosoc_rm." + module), name)


def as_doc(obj):
    """A contract document as JSON data: a dict, or a pydantic model dumped by alias (section 17.2)."""
    if isinstance(obj, dict):
        return obj
    return obj.model_dump(mode="json", by_alias=True)


def ref_prefix(host, path):
    """One repo's namespace in its component mirror (interfaces.md section 3: quote(path, safe=""))."""
    return "refs/r/%s/%s" % (host, urllib.parse.quote(path, safe=""))


def pin_level_state(fact):
    """LOCAL_URL and NO_GITLINK have no host, so the pin-level `fact.state` carries them (pin-state
    schema, resolved G4). null for every other pin."""
    if "state" in fact:
        return fact["state"]
    if fact["url_kind"] == "local":
        return "LOCAL_URL"
    if fact["entry_type"] != "commit":
        return "NO_GITLINK"
    return None


def service_config(*, registry, data_dir, hosts=None, base=None):
    """The service config of interfaces.md section 1, pointed at test data. Keys (section 17.1):
    registry.path, service.data_dir, hosts.<key>.{web, api, ssh, org|group}."""
    cfg = json.loads(json.dumps(base or {}))
    cfg.setdefault("registry", {}).update({"path": str(registry), "ref": "main"})
    cfg.setdefault("service", {})["data_dir"] = str(data_dir)
    if hosts is not None:
        cfg["hosts"] = hosts
    return cfg


def toml_dumps(table, prefix=""):
    """Enough TOML for service_config(): strings, ints, bools, lists and nested tables."""
    lines = []
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    if prefix and scalars:
        lines.append("[%s]" % prefix)
    lines += ["%s = %s" % (k, json.dumps(v)) for k, v in scalars.items()]
    for k, v in table.items():
        if isinstance(v, dict):
            lines.append(toml_dumps(v, "%s.%s" % (prefix, k) if prefix else k))
    return "\n".join(line for line in lines if line) + "\n"


# --------------------------------------------------------------------------------------------
# Plain helpers
# --------------------------------------------------------------------------------------------
def git(*args, env=None, input=None, check=True):
    proc = subprocess.run(["git", *map(str, args)], env=env, input=input, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), proc.stderr.strip()))
    return proc.stdout.strip()


def ancestry(git_dir, ancestor, descendant):
    """True/False from merge-base --is-ancestor; None when an object is missing (no answer)."""
    proc = subprocess.run(["git", "--git-dir", str(git_dir), "merge-base", "--is-ancestor", ancestor, descendant],
                          capture_output=True, text=True)
    return {0: True, 1: False}.get(proc.returncode)


def refs_containing(git_dir, prefix, commit):
    """Refs under `prefix` that reach `commit`; [] when the object is absent."""
    out = git("--git-dir", git_dir, "for-each-ref", "--contains", commit, "--format=%(refname)", prefix, check=False)
    return [line for line in out.splitlines() if line]


def delete_refs(git_dir, refs):
    if refs:
        git("--git-dir", git_dir, "update-ref", "--stdin", input="".join("delete %s\n" % r for r in refs))


def set_ref(git_dir, ref, sha):
    git("--git-dir", git_dir, "update-ref", ref, sha)


def load_yaml(path):
    return yaml.load(Path(path).read_text(), Loader=CE.StrictLoader)


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def cli_docs(proc):
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def schema_errors(doc, schema="observation"):
    validator = Draft202012Validator(SCHEMA_DOCS[schema], registry=SCHEMA_REGISTRY)
    return ["%s: %s" % ("/".join(map(str, e.absolute_path)) or "<root>", e.message) for e in validator.iter_errors(doc)]


def pin_key(pin):
    """B2 identity of a pin: (parent host, parent path, parent commit, path).
    Works on a pin_state fact and on a release-lock pin. parent.repo is the repo the parent was
    pinned by, after alias normalisation, as the rc4 lock records it (interfaces.md section 4 step 3)."""
    return (pin["parent"]["repo"]["host"], pin["parent"]["repo"]["path"], pin["parent"]["commit"], pin["path"])


def facts(docs, kind):
    return [as_doc(d)["fact"] for d in docs if as_doc(d)["kind"] == kind]


def reached_by_default(fact):
    """B5: the pinned host's default branch reaches the pin (ON_DEFAULT, or TAGGED with a lag)."""
    state = next((s for s in fact["hosts"].values() if s["pinned"]), None)
    return state is not None and (state["state"] == "ON_DEFAULT" or (state["state"] == "TAGGED" and state["lag"] is not None))


# --------------------------------------------------------------------------------------------
# Goldens (all real, observed 2026-09-30)
# --------------------------------------------------------------------------------------------
def example_catalogue():
    return load_yaml(EXAMPLES / "component-catalogue.example.yaml")


def example_eligibility():
    return load_yaml(EXAMPLES / "eligibility.example.yaml")


def example_observations(kind):
    return [d for d in load_jsonl(EXAMPLES / "observation.example.jsonl") if d["kind"] == kind]


def rc4_golden_pins():
    pins = load_yaml(EXAMPLES / "release-lock.eth-rc4.example.yaml")["pins"]
    assert len(pins) == 44 and len({pin_key(p) for p in pins}) == 44, "the B2 golden must hold rc4's 44 distinct pins"
    return pins


def rc4_state_golden():
    """B3 golden for eth-rc4: pinned host from the rc4 lock, second host from RC4_OTHER_HOST."""
    golden = {}
    for p in rc4_golden_pins():
        path = p["tree_paths"][0]
        golden[path] = {
            "pin": {"commit": p["commit"], "url_kind": p["url_kind"], "branch_field": RC4_BRANCH_FIELDS.get(path)},
            "hosts": {p["repo"]["host"]: {"state": p["state"], "lag": p.get("lag"), "tags_include": list(p["tags"]),
                                          "pinned": True}},
        }
    for path, (host, state, lag) in RC4_OTHER_HOST.items():
        golden[path]["hosts"][host] = {"state": state, "lag": lag, "pinned": False}
    return golden


def multicore_state_golden():
    golden = {}
    for path, _component, host, commit, state, lag, url_kind, branch in MULTICORE_BASELINE:
        golden[path] = {"pin": {"commit": commit, "url_kind": url_kind, "branch_field": branch},
                        "hosts": {host: {"state": state, "lag": lag, "pinned": True}}}
    for path, (host, state, lag) in MULTICORE_OTHER_HOST.items():
        golden[path]["hosts"][host] = {"state": state, "lag": lag, "pinned": False}
    assert len(golden) == 35
    return golden


def rc4_protection_golden():
    """B5 golden (interfaces.md section 7): 11 rows on the pinned host, all covered (the done-when's
    11 commits), and 2 rows on the canonical host where it differs, both uncovered (question 9)."""
    rows = [d["fact"] for d in example_observations("silicon_protection")]
    pinned = [r for r in rows if r["pinned"]]
    assert len(pinned) == 11 and all(r["covered"] for r in pinned), "the B5 golden must hold rc4's 11 covered commits"
    assert {(r["component"], r["repo"]["host"]) for r in rows if not r["pinned"]} == \
        {("nanosoc_arch_tech", "github"), ("ahb_qspi", "github")}
    return rows


def host_compare_golden():
    rows = {d["fact"]["component"]: d["fact"] for d in example_observations("host_compare")}
    assert set(rows) == {"tidelink", "ahb_qspi", "asic-flow"}
    return rows


# B6 done-when on the heads observed 2026-09-30 (rev-list in one object store holding both hosts).
B6_DONE_WHEN = {
    "tidelink": {"relation": "AHEAD", "github_only": 298, "gitlab_only": 0},
    "ahb_qspi": {"relation": "DIVERGED", "github_only": 1, "gitlab_only": 5},
    "asic-flow": {"relation": "DIVERGED", "github_only": 4, "gitlab_only": 1},
}


# --------------------------------------------------------------------------------------------
# Registry and tool environment
# --------------------------------------------------------------------------------------------
def write_registry(dst, catalogue, eligibility, examples=True):
    """The registry layout of docs/contracts/README.md §1. With examples=True it also holds the
    SL-DEF-0001 defect, the ETH-R0P0-ERR-001 erratum and the import ledger (they cite eth-rc4)."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "catalogue.yaml").write_text(yaml.safe_dump(catalogue, sort_keys=False))
    (dst / "eligibility.yaml").write_text(yaml.safe_dump(eligibility, sort_keys=False))
    if examples:
        (dst / "defects").mkdir(exist_ok=True)
        shutil.copy(EXAMPLES / "defect.SL-DEF-0001.example.yaml", dst / "defects" / "SL-DEF-0001.yaml")
        (dst / "errata" / "eth-rc4").mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLES / "erratum.ETH-R0P0-ERR-001.example.yaml", dst / "errata" / "eth-rc4" / "ERR-001.yaml")
        shutil.copy(EXAMPLES / "import-ledger.example.yaml", dst / "imports.yaml")
    return dst


def live_registry(dst):
    catalogue = example_catalogue()
    catalogue["components"].append(dict(SOC400))
    eligibility = example_eligibility()
    eligibility["configurations"].extend(dict(c) for c in TEST_CONFIGURATIONS)
    return write_registry(dst, catalogue, eligibility)


class ToolEnv:
    """Where the tool under test reads its config, and through it its registry and data."""

    def __init__(self, root, cfg, data_dir, registry, extra_env=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.registry = Path(registry)
        self.config = self.root / "nanosoc-rm.toml"
        self.config.write_text(toml_dumps(cfg))
        self.env = {"NANOSOC_RM_CONFIG": str(self.config), **(extra_env or {})}

    @contextlib.contextmanager
    def active(self):
        """In-process calls find the config through $NANOSOC_RM_CONFIG, read per call, never
        cached at import (interfaces.md section 1)."""
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            yield self
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def cli(self, *args, timeout=1800):
        exe = shutil.which("nanosoc-rm")
        assert exe, "nanosoc-rm is not on PATH (PR4: every capability has a CLI)"
        return subprocess.run([exe, *map(str, args)], env={**os.environ, **self.env},
                              capture_output=True, text=True, timeout=timeout)

    def mirror(self, component):
        return self.data_dir / "mirrors" / ("%s.git" % component)


def make_shadow(src, root):
    """A copy of src's data dir for broken-input controls on real data. Each mirror is a fresh
    bare repo that borrows src's objects (objects/info/alternates) and copies its refs, so a ref
    deleted or moved here changes nothing in src or on any host. Never sync a shadow."""
    root = Path(root)
    data = root / "data"
    (data / "mirrors").mkdir(parents=True)
    for mirror in sorted((src.data_dir / "mirrors").glob("*.git")):
        copy = data / "mirrors" / mirror.name
        git("init", "--quiet", "--bare", copy)
        shutil.copy(mirror / "config", copy / "config")
        (copy / "objects" / "info" / "alternates").write_text(str((mirror / "objects").resolve()) + "\n")
        refs = git("--git-dir", mirror, "for-each-ref", "--format=%(objectname) %(refname)")
        stdin = "".join("create %s %s\n" % (ref, sha) for sha, ref in (line.split(" ", 1) for line in refs.splitlines() if line))
        if stdin:
            git("--git-dir", copy, "update-ref", "--stdin", input=stdin)
    database = src.data_dir / "rm.sqlite"
    if database.exists():
        with contextlib.closing(sqlite3.connect(database)) as a, contextlib.closing(sqlite3.connect(data / "rm.sqlite")) as b:
            a.backup(b)
    extra = {k: v for k, v in src.env.items() if k != "NANOSOC_RM_CONFIG"}
    return ToolEnv(root, service_config(registry=src.registry, data_dir=data, base=src.cfg), data, src.registry, extra)


# --------------------------------------------------------------------------------------------
# Hermetic hosts
# --------------------------------------------------------------------------------------------
class LocalHosts:
    """Two local 'hosts' made of bare repos under <root>/hosts/<host>/<path>.git.

    Fixture .gitmodules files pin real-looking URLs; git's url.insteadOf sends every fetch and
    push to the local repo, so the tool's URL handling runs unchanged. Commits are made in a
    developer repo and pushed, so a force-push here is a real force-push. FakeApi answers the
    host API calls the walker makes for objects its mirror lacks (interfaces.md §4 step 4)."""

    WEB = {"github": "https://github.com/", "gitlab": "https://gitlab.example.invalid/"}
    SSH = {"github": "git@github.com:", "gitlab": "git@gitlab.example.invalid:"}

    def __init__(self, root):
        self.root = Path(root)
        self.hosts_dir = self.root / "hosts"
        self.dev_dir = self.root / "dev"
        self.gitconfig = self.root / "gitconfig"
        lines = ["[user]", "\tname = rm-fixture", "\temail = rm-fixture@example.invalid",
                 '[protocol "file"]', "\tallow = always", "[init]", "\tdefaultBranch = main"]
        for host in ("github", "gitlab"):
            lines += ['[url "%s/"]' % (self.hosts_dir / host).as_uri(),
                      "\tinsteadOf = %s" % self.WEB[host], "\tinsteadOf = %s" % self.SSH[host]]
        self.root.mkdir(parents=True, exist_ok=True)
        self.gitconfig.write_text("\n".join(lines) + "\n")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0"}
        self.api = FakeApi(self)

    def git(self, *args, input=None):
        return git(*args, env=self.env, input=input)

    def url(self, host, path, ssh=False):
        return "%s%s.git" % (self.SSH[host] if ssh else self.WEB[host], path)

    def bare(self, host, path):
        """The host's repo; created on first use."""
        repo = self.hosts_dir / host / ("%s.git" % path)
        if not repo.exists():
            self.git("init", "--quiet", "--bare", repo)
            for key, value in (("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true"),
                               ("receive.denyNonFastForwards", "false"), ("receive.denyDeletes", "false")):
                self.git("--git-dir", repo, "config", key, value)
        return repo

    def dev(self, host, path):
        """A developer's repo for (host, path): commits are made here, then pushed."""
        repo = self.dev_dir / host / ("%s.git" % path)
        if not repo.exists():
            self.git("init", "--quiet", "--bare", repo)
        return repo

    def commit(self, host, path, *, parents=(), files=None, gitlinks=None, gitmodules=None, on_host=False,
               message="fixture"):
        """Write one commit. gitlinks: {path: sha} (mode 160000); gitmodules: [{path, url, branch?}].
        on_host=True writes the object straight into the host repo with no ref (an ORPHAN)."""
        repo = self.bare(host, path) if on_host else self.dev(host, path)
        index = self.root / "index.tmp"
        if index.exists():
            index.unlink()
        env = {**self.env, "GIT_INDEX_FILE": str(index)}
        entries = dict(files or {})
        if gitmodules:
            entries[".gitmodules"] = "".join(
                '[submodule "%s"]\n\tpath = %s\n\turl = %s\n%s'
                % (m["path"], m["path"], m["url"], "\tbranch = %s\n" % m["branch"] if m.get("branch") else "")
                for m in gitmodules)
        args = []
        for name, text in sorted(entries.items()):
            blob = git("--git-dir", repo, "hash-object", "-w", "--stdin", env=env, input=text)
            args += ["--cacheinfo", "100644,%s,%s" % (blob, name)]
        for name, sha in sorted((gitlinks or {}).items()):
            args += ["--cacheinfo", "160000,%s,%s" % (sha, name)]
        if args:
            git("--git-dir", repo, "update-index", "--add", *args, env=env)
        tree = git("--git-dir", repo, "write-tree", env=env)
        parent_args = [a for p in parents for a in ("-p", p)]
        return git("--git-dir", repo, "commit-tree", tree, *parent_args, "-m", message, env=env)

    def push(self, host, path, src, ref, *, force=False, to_host=None, to_path=None):
        """Push from the developer's repo through the pinned-style URL (insteadOf applies)."""
        target_host, target_path = to_host or host, to_path or path
        self.bare(target_host, target_path)
        if not ref.startswith("refs/"):
            ref = "refs/heads/" + ref
        self.git("--git-dir", self.dev(host, path), "push", "--quiet", *(["--force"] if force else []),
                 self.url(target_host, target_path), "%s:%s" % (src, ref))

    def tag(self, host, path, name, sha, annotated=True):
        self.git("--git-dir", self.dev(host, path), "tag", "-f", *(["-a", "-m", name] if annotated else []), name, sha)
        self.push(host, path, "refs/tags/" + name, "refs/tags/" + name, force=True)

    def delete_remote_ref(self, host, path, ref):
        self.git("--git-dir", self.dev(host, path), "push", "--quiet", self.url(host, path), ":" + ref)

    def hosts_table(self):
        return {
            "github": {"web": self.WEB["github"].rstrip("/"), "api": self.api.base + "/gh",
                       "ssh": self.SSH["github"], "org": "SoC-Labs"},
            "gitlab": {"web": self.WEB["gitlab"].rstrip("/"), "api": self.api.base + "/gl/api/v4",
                       "ssh": self.SSH["gitlab"], "group": "soclabs"},
        }

    def tool_env(self, registry, name="tool"):
        root = self.root / name
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        # ASSUMED (G15): the service's git subprocesses inherit this environment, so url.insteadOf
        # routes every fetch to the local hosts. ASSUMED (G1): API calls go to hosts.<key>.api, and
        # GitLab projects are addressed by URL-encoded path, as in the contract examples.
        extra = {k: self.env[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")}
        return ToolEnv(root, service_config(registry=registry, data_dir=data, hosts=self.hosts_table()), data,
                       registry, extra)

    def api_answer(self, raw_path):
        """(status, JSON body) for one GET, answered from the host repos."""
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(raw_path).path.split("/") if p]
        if parts[:2] == ["gh", "repos"] and len(parts) >= 4:
            host, path, rest = "github", "/".join(parts[2:4]), parts[4:]
        elif parts[:4] == ["gl", "api", "v4", "projects"] and len(parts) >= 5:
            host, path, rest = "gitlab", parts[4], parts[5:]
        else:
            return 404, {"message": "Not Found"}
        repo = self.hosts_dir / host / ("%s.git" % path)
        if not repo.exists():
            return 404, {"message": "404 Project Not Found" if host == "gitlab" else "Not Found"}

        def run(*args):
            return subprocess.run(["git", "--git-dir", str(repo), *args], env=self.env, capture_output=True,
                                  text=True).stdout.strip()

        if not rest:
            head = run("symbolic-ref", "--short", "HEAD")
            return 200, {"default_branch": head, "private": False, "visibility": "public", "full_name": path,
                         "path_with_namespace": path}
        if host == "github" and rest[0] == "commits" and len(rest) >= 2:
            sha, sub = rest[1], rest[2:]
        elif host == "gitlab" and rest[:2] == ["repository", "commits"] and len(rest) >= 3:
            sha, sub = rest[2], rest[3:]
        else:
            return 404, {"message": "Not Found"}
        full = run("rev-parse", "--verify", "--quiet", sha + "^{commit}")
        if not full:
            if host == "github":
                return 422, {"message": "No commit found for SHA: %s" % sha}
            return 404, {"message": "404 Commit Not Found"}
        if not sub:
            return 200, ({"sha": full} if host == "github" else {"id": full})
        if sub in (["pulls"], ["merge_requests"]):
            namespace = "refs/pull" if host == "github" else "refs/merge-requests"
            refs = run("for-each-ref", "--contains", full, "--format=%(refname)", namespace).split()
            numbers = sorted({int(r.split("/")[2]) for r in refs})
            key = "number" if host == "github" else "iid"
            return 200, [{key: n, "state": "open"} for n in numbers]
        if host == "gitlab" and sub == ["refs"]:
            out = []
            for kind, namespace in (("branch", "refs/heads/"), ("tag", "refs/tags/")):
                for ref in run("for-each-ref", "--contains", full, "--format=%(refname)", namespace.rstrip("/")).split():
                    out.append({"type": kind, "name": ref[len(namespace):]})
            return 200, out
        return 404, {"message": "Not Found"}


class FakeApi:
    """GitHub under <base>/gh, GitLab under <base>/gl/api/v4. GET only, like the tool (A2)."""

    def __init__(self, hosts):
        answer = hosts.api_answer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, body = answer(self.path)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _chain(h, host, path, n, branch="main"):
    """n commits in a line on `branch`; returns their SHAs, oldest first."""
    shas, parents = [], []
    for i in range(n):
        sha = h.commit(host, path, parents=parents, files={"n": "%s %d\n" % (path, i)})
        shas.append(sha)
        parents = [sha]
    h.push(host, path, shas[-1], branch)
    return shas


FX_MISSING = "0123456789abcdef0123456789abcdef01234567"
FX_KNOWN_LOST = "0badc0de0badc0de0badc0de0badc0de0badc0de"
FX_UNREADABLE = "fedcba9876543210fedcba9876543210fedcba98"


def build_state_table(h):
    """B3 hermetic fixture: one pin per state of README §5, plus the SSH badge, the branch= trap,
    a keep/* tag that must not count as TAGGED, and a pin whose two hosts disagree.
    Returns (registry, expected, rewrite); call rewrite() after the first sync."""
    gh, gl = "github", "gitlab"
    t = _chain(h, gh, "SoC-Labs/fx-tagged", 3)
    h.tag(gh, "SoC-Labs/fx-tagged", "v1.0.0", t[1])
    d = _chain(h, gl, "soclabs/fx-ondefault", 4)
    p = _chain(h, gl, "soclabs/fx-protected", 2)
    x = h.commit(gl, "soclabs/fx-protected", parents=[p[-1]], files={"n": "feature x\n"})
    h.push(gl, "soclabs/fx-protected", x, "feat/x")
    h.tag(gl, "soclabs/fx-protected", "keep/fx-states/protected", x)
    b = _chain(h, gl, "soclabs/fx-branchonly", 2)
    y = h.commit(gl, "soclabs/fx-branchonly", parents=[b[-1]], files={"n": "feature y\n"})
    h.push(gl, "soclabs/fx-branchonly", y, "feat/y")
    r = _chain(h, gl, "soclabs/fx-rewritten", 3)
    q = _chain(h, gh, "SoC-Labs/fx-pronly", 2)
    pr = h.commit(gh, "SoC-Labs/fx-pronly", parents=[q[-1]], files={"n": "pull request\n"})
    h.push(gh, "SoC-Labs/fx-pronly", pr, "refs/pull/7/head")
    o = _chain(h, gh, "SoC-Labs/fx-orphan", 2)
    orphan = h.commit(gh, "SoC-Labs/fx-orphan", parents=[o[-1]], files={"n": "orphan\n"}, on_host=True)
    _chain(h, gl, "soclabs/fx-missing", 2)
    dual = _chain(h, gh, "SoC-Labs/fx-dual", 2)
    h.push(gh, "SoC-Labs/fx-dual", dual[0], "main", to_host=gl, to_path="soclabs/fx-dual")  # the copy lags by one

    pins = [  # (path, url, gitlink or None, branch=)
        ("tagged", h.url(gh, "SoC-Labs/fx-tagged"), t[1], None),
        ("on-default", h.url(gl, "soclabs/fx-ondefault"), d[1], None),
        ("protected", h.url(gl, "soclabs/fx-protected"), x, None),
        ("branch-only", h.url(gl, "soclabs/fx-branchonly"), y, "main"),
        ("rewritten-reflog", h.url(gl, "soclabs/fx-rewritten"), r[2], None),
        ("rewritten-known", h.url(gl, "soclabs/fx-rewritten"), FX_KNOWN_LOST, None),
        ("pr-only", h.url(gh, "SoC-Labs/fx-pronly"), pr, None),
        ("orphan", h.url(gh, "SoC-Labs/fx-orphan"), orphan, None),
        ("missing", h.url(gl, "soclabs/fx-missing"), FX_MISSING, None),
        ("unreadable", h.url(gl, "soclabs/fx-unreadable"), FX_UNREADABLE, None),
        ("local", "/srv/fixture/fx-local.git", t[0], None),
        ("no-gitlink-tree", h.url(gh, "SoC-Labs/fx-tagged"), None, None),
        ("no-gitlink-absent", h.url(gh, "SoC-Labs/fx-tagged"), None, None),
        ("ssh", h.url(gh, "SoC-Labs/fx-tagged", ssh=True), t[2], None),
        ("dual", h.url(gh, "SoC-Labs/fx-dual"), dual[1], None),  # object in the shared mirror, absent on GitLab (G6)
    ]
    soc = h.commit(gh, "SoC-Labs/fx-states-soc",
                   files={"README": "B3 state table\n", "no-gitlink-tree/README": "a directory, not a gitlink\n"},
                   gitlinks={path: sha for path, _url, sha, _br in pins if sha},
                   gitmodules=[{"path": path, "url": url, "branch": br} for path, url, _sha, br in pins])
    h.push(gh, "SoC-Labs/fx-states-soc", soc, "main")

    def ip(cid, host, path, **extra):
        return {"id": cid, "class": "ip_tech", "canonical": {"host": host, "path": path}, **extra}

    catalogue = {"schema": 1, "components": [
        {"id": "fx-states-soc", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/fx-states-soc"}},
        ip("fx-tagged", gh, "SoC-Labs/fx-tagged"),
        ip("fx-ondefault", gl, "soclabs/fx-ondefault"),
        ip("fx-protected", gl, "soclabs/fx-protected"),
        ip("fx-branchonly", gl, "soclabs/fx-branchonly"),
        ip("fx-rewritten", gl, "soclabs/fx-rewritten",
           known_rewrites=[{"host": gl, "branch": "main", "date": "2026-09-01", "lost": [FX_KNOWN_LOST]}]),
        ip("fx-pronly", gh, "SoC-Labs/fx-pronly"),
        ip("fx-orphan", gh, "SoC-Labs/fx-orphan"),
        ip("fx-missing", gl, "soclabs/fx-missing"),
        ip("fx-unreadable", gl, "soclabs/fx-unreadable"),  # catalogued; the host has no such repo
        ip("fx-dual", gh, "SoC-Labs/fx-dual", copies=[{"host": gl, "path": "soclabs/fx-dual"}]),
    ]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "fx-states", "component": "fx-states-soc", "ref": {"branch": "main"}, "commit": soc, "frozen": True}]}
    registry = write_registry(h.root / "registry-states", catalogue, eligibility, examples=False)

    def rewrite():
        """Force-push fx-rewritten main from r0-r1-r2 to r0-r1-w: the pinned r2 leaves every ref."""
        w = h.commit(gl, "soclabs/fx-rewritten", parents=[r[1]], files={"n": "rewritten\n"})
        h.push(gl, "soclabs/fx-rewritten", w, "main", force=True)

    def host(state, **kw):
        return dict(state=state, **kw)

    expected = {
        "tagged": {"pin": {"url_kind": "https"}, "hosts": {gh: host("TAGGED", lag=1, tags_include=["v1.0.0"], pinned=True)}},
        "on-default": {"hosts": {gl: host("ON_DEFAULT", lag=2)}},
        "protected": {"hosts": {gl: host("PROTECTED_BY_TAG", lag=None, tags_include=["keep/fx-states/protected"],
                                         branches_include=["feat/x"])}},
        "branch-only": {"pin": {"branch_field": "main"}, "hosts": {gl: host("BRANCH_ONLY", lag=None, branches_include=["feat/y"])}},
        "rewritten-reflog": {"hosts": {gl: host("REWRITTEN")}},
        "rewritten-known": {"hosts": {gl: host("REWRITTEN")}},
        "pr-only": {"hosts": {gh: host("PR_ONLY")}},
        "orphan": {"hosts": {gh: host("ORPHAN")}},
        "missing": {"hosts": {gl: host("MISSING")}},
        "unreadable": {"hosts": {gl: host("UNREADABLE")}},
        "local": {"pin": {"state": "LOCAL_URL", "url_kind": "local"}, "hosts": {}},
        "no-gitlink-tree": {"pin": {"state": "NO_GITLINK", "entry_type": "tree"}, "hosts": {}},
        "no-gitlink-absent": {"pin": {"state": "NO_GITLINK", "entry_type": "absent"}, "hosts": {}},
        "ssh": {"pin": {"url_kind": "ssh"}, "hosts": {gh: host("ON_DEFAULT", lag=0)}},
        "dual": {"hosts": {gh: host("ON_DEFAULT", lag=0, pinned=True), gl: host("MISSING", pinned=False)}},
    }
    return registry, expected, rewrite


def build_rewrite_fixture(h):
    """B8 fixture. soclabs/rm-rewrite-fixture main = A-B-C-D-E. The frozen configuration
    rm-fixture pins D at deps/rewrite, so a rewrite that drops C, D and E loses a registered pin."""
    gh, gl = "github", "gitlab"
    a, b, c, d, e = _chain(h, gl, "soclabs/rm-rewrite-fixture", 5)
    soc = h.commit(gh, "SoC-Labs/rm-fixture-soc", files={"README": "rewrite fixture\n"}, gitlinks={"deps/rewrite": d},
                   gitmodules=[{"path": "deps/rewrite", "url": h.url(gl, "soclabs/rm-rewrite-fixture")}])
    h.push(gh, "SoC-Labs/rm-fixture-soc", soc, "main")
    catalogue = {"schema": 1, "components": [
        {"id": "rm-fixture-soc", "class": "soc_ref", "canonical": {"host": gh, "path": "SoC-Labs/rm-fixture-soc"}},
        {"id": "rm-rewrite-fixture", "class": "ip_tech", "canonical": {"host": gl, "path": "soclabs/rm-rewrite-fixture"}},
    ]}
    eligibility = {"schema": 1, "configurations": [
        {"id": "rm-fixture", "component": "rm-fixture-soc", "ref": {"branch": "main"}, "commit": soc, "frozen": True}]}
    registry = write_registry(h.root / "registry-rewrite", catalogue, eligibility, examples=False)
    return {"A": a, "B": b, "C": c, "D": d, "E": e, "S": soc, "registry": registry}


def rewrite_alert_expected(fx, new_head):
    return {"component": "rm-rewrite-fixture", "repo": {"host": "gitlab", "path": "soclabs/rm-rewrite-fixture"},
            "branch": "main", "old_head": fx["E"], "new_head": new_head, "lost_commits": 3,
            "pinned_lost": [{"configuration": "rm-fixture", "tree_path": "deps/rewrite", "commit": fx["D"]}]}


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def live(tmp_path_factory):
    """Real mirrors of every repo in the live registry, fetched read-only (A2, PR8)."""
    base_path = os.environ.get("NANOSOC_RM_CONFIG")
    if not base_path:
        pytest.fail("live input missing: set NANOSOC_RM_CONFIG to the private service config holding the v1 "
                    "read tokens (a live test never skips; deselect with -m 'not live')")
    import tomllib  # Python >= 3.11, like the package

    base = tomllib.loads(Path(base_path).read_text())
    root = tmp_path_factory.mktemp("live")
    data = Path(os.environ.get("NANOSOC_RM_TEST_DATA") or root / "data")
    data.mkdir(parents=True, exist_ok=True)
    registry = live_registry(root / "registry")
    env = ToolEnv(root, service_config(registry=registry, data_dir=data, base=base), data, registry)
    catalogue = load_yaml(registry / "catalogue.yaml")
    with env.active():
        sync = rm("mirror.sync_component")
        for entry in catalogue["components"] + catalogue.get("external", []):
            sync(entry["id"])
    return env


@pytest.fixture
def shadow(live, tmp_path):
    return make_shadow(live, tmp_path / "shadow")


@pytest.fixture
def hermetic(tmp_path):
    hosts = LocalHosts(tmp_path / "hermetic")
    yield hosts
    hosts.api.close()


@pytest.fixture
def registry_under_test():
    """B1 checks the real registry: NANOSOC_RM_REGISTRY names a checkout of it. The example catalogue's
    classes are judgement, so passing on them would prove nothing about the registry (coverage review
    Medium 5); without the real one the AT fails."""
    real = os.environ.get("NANOSOC_RM_REGISTRY")
    if not real:
        pytest.fail("live input missing: NANOSOC_RM_REGISTRY must name a checkout of the registry")
    return Path(real)


# --------------------------------------------------------------------------------------------
# The checks. Each AT and its negative control call the same one.
# --------------------------------------------------------------------------------------------
def check_B1(registry_dir):
    """[] when the registry loads and every catalogue component has exactly one of the 8 classes,
    at least one entry is legacy, and no configuration tests a legacy component."""
    load_registry, registry_error = rm("registry.load_registry"), rm("registry.RegistryError")
    try:
        registry = load_registry(Path(registry_dir))
    except registry_error as err:
        return ["%s:%s: %s [%s]" % (p.file, p.pointer, p.message, p.rule) for p in err.problems] or ["RegistryError"]
    problems = []
    raw = load_yaml(Path(registry_dir) / "catalogue.yaml")["components"]
    components = [as_doc(c) for c in registry.components()]
    if len(components) != len(raw):
        problems.append("loaded %d components, catalogue.yaml has %d entries" % (len(components), len(raw)))
    ids = Counter(c["id"] for c in components)
    problems += ["component %s appears %d times" % (i, n) for i, n in ids.items() if n > 1]
    for c in components:
        cls = c.get("class")
        if not isinstance(cls, str) or cls not in CLASSES:
            problems.append("%s: class %r is not exactly one of %s" % (c["id"], cls, sorted(CLASSES)))
    legacy = {c["id"] for c in components if c.get("class") == "legacy"}
    if not legacy:
        problems.append("no legacy entry, so 'listed but never tested' is not exercised")
    for cfg in map(as_doc, registry.configurations()):
        if cfg["component"] in legacy:
            problems.append("configuration %s tests legacy component %s" % (cfg["id"], cfg["component"]))
    return problems


def check_B2(pin_docs, golden, root_commit, configuration):
    """[] when the walk is exactly the golden tree: the same (parent repo, parent commit, path)
    keys, no duplicates, and per pin the same child repo, commit, component, tree paths and depth."""
    problems, got = [], {}
    for doc in map(as_doc, pin_docs):
        problems += ["schema: %s" % e for e in schema_errors(doc, "pin-state")[:3]]
        if doc.get("kind") != "pin_state":
            problems.append("not a pin_state observation: %r" % doc.get("kind"))
            continue
        key = pin_key(doc["fact"])
        if key in got:
            problems.append("duplicate pin %s" % (key,))
        got[key] = doc["fact"]
    want = {pin_key(p): p for p in golden}
    problems += ["missing pin %s" % (k,) for k in sorted(set(want) - set(got))]
    problems += ["extra pin %s" % (k,) for k in sorted(set(got) - set(want))]
    for key in sorted(set(want) & set(got)):
        w, g = want[key], got[key]
        for label, have, need in (("configuration", g["configuration"], configuration),
                                  ("child commit", g["child"]["commit"], w["commit"]),
                                  ("child repo", g["child"]["repo"], w["repo"]),
                                  ("child component", g["child"]["component"], w["component"]),
                                  ("tree paths", sorted(g["tree_paths"]), sorted(w["tree_paths"])),
                                  ("depth", g["depth"], w["depth"]),
                                  ("entry type", g["entry_type"], "commit")):
            if have != need:
                problems.append("%s: %s is %r, golden %r" % (w["tree_paths"][0], label, have, need))
    for g in got.values():
        if g["depth"] == 1 and g["parent"]["commit"] != root_commit:
            problems.append("depth-1 pin %s hangs off %s, not the configuration's commit" % (g["path"], g["parent"]["commit"][:7]))
    return problems


def check_B3(pin_docs, golden, *, exact_lag):
    """[] when every pin's state on every golden host matches the hand check. Live lags only
    grow, so exact_lag=False accepts lag >= golden."""
    problems, got = [], {}
    for doc in map(as_doc, pin_docs):
        problems += ["schema: %s" % e for e in schema_errors(doc, "pin-state")[:3]]
        for path in doc["fact"]["tree_paths"]:
            got[path] = doc["fact"]
    problems += ["%s: no pin at this path" % p for p in sorted(set(golden) - set(got))]
    problems += ["%s: not in the hand check" % p for p in sorted(set(got) - set(golden))]
    for path, want in sorted(golden.items()):
        fact = got.get(path)
        if fact is None:
            continue
        pin = want.get("pin", {})
        for field in ("url_kind", "branch_field", "entry_type"):
            if field in pin and fact[field] != pin[field]:
                problems.append("%s: %s %r, hand check %r" % (path, field, fact[field], pin[field]))
        if "commit" in pin and fact["child"]["commit"] != pin["commit"]:
            problems.append("%s: commit %s, hand check %s" % (path, fact["child"]["commit"], pin["commit"]))
        if "state" in pin and pin_level_state(fact) != pin["state"]:
            problems.append("%s: pin state %s, hand check %s" % (path, pin_level_state(fact), pin["state"]))
        hosts = fact["hosts"]
        if want.get("hosts") == {} and hosts:
            problems.append("%s: host states %s for a pin with no host" % (path, sorted(hosts)))
        if hosts and sum(1 for s in hosts.values() if s["pinned"]) != 1:
            problems.append("%s: %d hosts marked pinned" % (path, sum(1 for s in hosts.values() if s["pinned"])))
        for host, w in want.get("hosts", {}).items():
            state = hosts.get(host)
            label = "%s@%s" % (path, host)
            if state is None:
                problems.append("%s: no state" % label)
                continue
            if state["state"] != w["state"]:
                problems.append("%s: state %s, hand check %s" % (label, state["state"], w["state"]))
            if "lag" in w:
                if w["lag"] is None and state["lag"] is not None:
                    problems.append("%s: lag %r, hand check none" % (label, state["lag"]))
                elif w["lag"] is not None:
                    ok = state["lag"] == w["lag"] if exact_lag else (state["lag"] is not None and state["lag"] >= w["lag"])
                    if not ok:
                        problems.append("%s: lag %r, hand check %s%d" % (label, state["lag"], "" if exact_lag else ">=", w["lag"]))
            problems += ["%s: tag %s missing" % (label, t) for t in w.get("tags_include", []) if t not in state["tags"]]
            problems += ["%s: branch %s missing" % (label, b) for b in w.get("branches_include", []) if b not in state["branches"]]
            if "pinned" in w and state["pinned"] != w["pinned"]:
                problems.append("%s: pinned %r, hand check %r" % (label, state["pinned"], w["pinned"]))
    return problems


def check_B4(docs, expected):
    """[] when the containment rows are exactly `expected`:
    {(configuration, host): (answer, {tree_path: (pin commit, contains)})}, each decided answer is
    consistent with its pins, and each decided pin carries its existence proof in `sources` (A3)."""
    problems, got = [], {}
    for doc in map(as_doc, docs):
        problems += ["schema: %s" % e for e in schema_errors(doc)[:3]]
        if doc.get("kind") != "containment":
            problems.append("not a containment observation: %r" % doc.get("kind"))
            continue
        key = (doc["fact"]["configuration"], doc["fact"]["host"])
        if key in got:
            problems.append("%s: two rows" % (key,))
        got[key] = doc
    problems += ["%s: no row" % (k,) for k in sorted(set(expected) - set(got))]
    problems += ["%s: unexpected row" % (k,) for k in sorted(set(got) - set(expected))]
    for key in sorted(set(expected) & set(got)):
        answer, pins = expected[key]
        doc = got[key]
        fact = doc["fact"]
        if fact["answer"] != answer:
            problems.append("%s: answer %s, expected %s" % (key, fact["answer"], answer))
        have = {p["tree_path"]: (p["commit"], p["contains"]) for p in fact["pins"]}
        if have != pins:
            problems.append("%s: pins %s, expected %s" % (key, have, pins))
        values = [c for _commit, c in have.values()]
        derived = ("absent" if not values else "unknown" if None in values else
                   "yes" if all(values) else "no" if not any(values) else "partial")
        if derived != fact["answer"]:
            problems.append("%s: answer %s disagrees with its own pins (%s)" % (key, fact["answer"], derived))
        problems += ["%s: %s undecided without a reason" % (key, p["tree_path"])
                     for p in fact["pins"] if p["contains"] is None and not p["reason"]]
        commands = " ".join(s["command"] for s in doc["sources"])
        for sha in {fact["query"]["commit"], *(p["commit"] for p in fact["pins"] if p["contains"] is not None)}:
            if fact["answer"] != "absent" and sha not in commands and sha[:7] not in commands:
                problems.append("%s: no existence proof for %s in sources" % (key, sha[:7]))
    return problems


def check_B5(docs, golden):
    """[] when the silicon-protection rows are exactly the golden (component, host, commit) rows, each
    judged on the golden host roles (pinned / canonical), covered exactly when the golden is (by at
    least its golden tags), and none on its default branch."""
    problems, got = [], {}
    for doc in map(as_doc, docs):
        problems += ["schema: %s" % e for e in schema_errors(doc)[:3]]
        if doc.get("kind") != "silicon_protection":
            problems.append("not a silicon_protection observation: %r" % doc.get("kind"))
            continue
        f = doc["fact"]
        got[(f["component"], f["repo"]["host"], f["repo"]["path"], f["commit"])] = f
    want = {(g["component"], g["repo"]["host"], g["repo"]["path"], g["commit"]): g for g in golden}
    problems += ["%s %s@%s: missing row" % (k[0], k[3][:7], k[1]) for k in sorted(set(want) - set(got))]
    problems += ["%s %s@%s: extra row" % (k[0], k[3][:7], k[1]) for k in sorted(set(got) - set(want))]
    for key in sorted(set(want) & set(got)):
        f, g = got[key], want[key]
        label = "%s %s@%s" % (key[0], key[3][:7], key[1])
        if f["configuration"] != g["configuration"]:
            problems.append("%s: configuration %s" % (label, f["configuration"]))
        for role in ("pinned", "canonical"):
            if f[role] != g[role]:
                problems.append("%s: %s %r, golden %r" % (label, role, f[role], g[role]))
        if f["covered"] != g["covered"]:
            problems.append("%s: %s" % (label, "not covered by any tag" if g["covered"] else "covered, golden uncovered"))
        problems += ["%s: covering tag %s missing" % (label, t) for t in g["covering_tags"] if t not in f["covering_tags"]]
        if f["default_branch"] in f["branches"]:
            problems.append("%s: on its default branch %s, so not a protection row" % (label, f["default_branch"]))
        if sorted(f["tree_paths"]) != sorted(g["tree_paths"]):
            problems.append("%s: tree paths %s" % (label, f["tree_paths"]))
    return problems


def check_B6(docs, expected):
    """[] when each compared copy's relation and counts match. expected maps a component id (it
    has exactly one copy) or a (component, copy host, copy path) key to
    {"relation": R, and counts as "github_only"/"gitlab_only" or "canonical_only"/"copy_only"}.
    A count is an int, or ">0". A missing count key is not checked. Counts must come from
    rev-list in the mirror: one host's compare API cannot see the other host's commits."""
    problems, got = [], {}
    for doc in map(as_doc, docs):
        problems += ["schema: %s" % e for e in schema_errors(doc)[:3]]
        if doc.get("kind") != "host_compare":
            problems.append("not a host_compare observation: %r" % doc.get("kind"))
            continue
        f = doc["fact"]
        key = (f["component"], f["copy"]["repo"]["host"], f["copy"]["repo"]["path"])
        if key in got:
            problems.append("%s: two rows" % (key,))
        got[key] = doc
    for want_key, want in sorted(expected.items(), key=lambda kv: str(kv[0])):
        if isinstance(want_key, tuple):
            keys = [want_key] if want_key in got else []
        else:
            keys = [k for k in got if k[0] == want_key]
        label = want_key if isinstance(want_key, str) else "%s@%s:%s" % want_key
        if len(keys) != 1:
            problems.append("%s: %d host_compare rows, expected 1" % (label, len(keys)))
            continue
        doc = got[keys[0]]
        f = doc["fact"]
        have = {"canonical_only": f["canonical_only"], "copy_only": f["copy_only"]}
        if f["canonical"]["repo"]["host"] != f["copy"]["repo"]["host"]:
            have[f["canonical"]["repo"]["host"] + "_only"] = f["canonical_only"]
            have[f["copy"]["repo"]["host"] + "_only"] = f["copy_only"]
        if f["relation"] != want["relation"]:
            problems.append("%s: relation %s, expected %s" % (label, f["relation"], want["relation"]))
        for name in ("github_only", "gitlab_only", "canonical_only", "copy_only"):
            need = want.get(name)
            if need is None:
                continue
            value = have.get(name)
            if not (isinstance(value, int) and value > 0 if need == ">0" else value == need):
                problems.append("%s: %s commits %r, expected %s" % (label, name.replace("_only", "-only"), value, need))
        if not any(s["kind"] == "git" and "rev-list" in s["command"] for s in doc["sources"]):
            problems.append("%s: counts not from rev-list in the mirror (one host's compare API cannot see the "
                            "other host's commits)" % label)
    return problems


def check_B8(docs, expected):
    """[] when `docs` hold exactly the expected rewrite alert, or none when expected is None."""
    problems = []
    alerts = [as_doc(d) for d in docs if as_doc(d).get("kind") == "rewrite_alert"]
    for alert in alerts:
        problems += ["schema: %s" % e for e in schema_errors(alert)[:3]]
    if expected is None:
        if alerts:
            problems.append("%d rewrite alert(s) where none should fire" % len(alerts))
        return problems
    if len(alerts) != 1:
        return problems + ["expected exactly one rewrite_alert, got %d" % len(alerts)]
    fact = alerts[0]["fact"]
    problems += ["rewrite_alert %s = %r, expected %r" % (k, fact.get(k), v) for k, v in expected.items() if fact.get(k) != v]
    subject = "rewrite_alert:%s:%s:%s:%s" % (fact["repo"]["host"], fact["repo"]["path"], fact["branch"], fact["old_head"])
    if alerts[0]["subject"] != subject:
        problems.append("subject %r, expected %r" % (alerts[0]["subject"], subject))
    return problems


def oracle_containment(env, component, host, repo_path, query, tree):
    """B4 expected row for a branch configuration, computed with raw git in the mirror (not the
    tool's code): each pin's answer is merge-base --is-ancestor, but only when both commits are
    reachable from a branch or tag on `host`."""
    mirror = env.mirror(component)
    prefix = ref_prefix(host, repo_path)
    query_exists = bool(refs_containing(mirror, prefix, query))
    pins = {}
    for fact in tree:
        if fact["child"]["component"] != component:
            continue
        commit = fact["child"]["commit"]
        decided = query_exists and bool(refs_containing(mirror, prefix, commit))
        for path in fact["tree_paths"]:
            pins[path] = (commit, ancestry(mirror, query, commit) if decided else None)
    values = [c for _commit, c in pins.values()]
    answer = ("absent" if not values else "unknown" if None in values else
              "yes" if all(values) else "no" if not any(values) else "partial")
    return answer, pins


def oracle_host_compare(env, fact):
    """B6 expected row for a live compare, from raw git in env's mirror (not the tool's code):
    the two default-branch heads as the mirror holds them, the two counts, and the relation."""
    mirror = env.mirror(fact["component"])
    canonical = "%s/heads/%s" % (ref_prefix(**fact["canonical"]["repo"]), fact["canonical"]["default_branch"])
    copy = "%s/heads/%s" % (ref_prefix(**fact["copy"]["repo"]), fact["copy"]["default_branch"])
    assert fact["canonical"]["head"] == git("--git-dir", mirror, "rev-parse", canonical)
    assert fact["copy"]["head"] == git("--git-dir", mirror, "rev-parse", copy)
    canonical_only = int(git("--git-dir", mirror, "rev-list", "--count", "%s..%s" % (copy, canonical)))
    copy_only = int(git("--git-dir", mirror, "rev-list", "--count", "%s..%s" % (canonical, copy)))
    related = git("--git-dir", mirror, "merge-base", canonical, copy, check=False) != ""
    relation = ("UNRELATED" if not related else "SAME" if canonical_only == copy_only == 0 else
                "AHEAD" if copy_only == 0 else "BEHIND" if canonical_only == 0 else "DIVERGED")
    return {"relation": relation, "canonical_only": canonical_only, "copy_only": copy_only}


def replay_heads(env, golden):
    """B6: point each compared default branch in env's mirrors at the head observed on 2026-09-30."""
    for component, fact in golden.items():
        mirror = env.mirror(component)
        for side in ("canonical", "copy"):
            r = fact[side]
            assert git("--git-dir", mirror, "cat-file", "-t", r["head"], check=False) == "commit", \
                "replay object %s is not in the %s mirror" % (r["head"][:7], component)
            set_ref(mirror, "%s/heads/%s" % (ref_prefix(**r["repo"]), r["default_branch"]), r["head"])


# ============================================================================================
# AT-B1
# ============================================================================================
@pytest.mark.requirement("B1")
@pytest.mark.live
@NOT_BUILT
def test_AT_B1(registry_under_test, tmp_path):
    # Given the real registry catalogue (NANOSOC_RM_REGISTRY). The example catalogue's classes are
    #   judgement, so the AT does not fall back to it (coverage review Medium 5).
    env = ToolEnv(tmp_path / "env", service_config(registry=registry_under_test, data_dir=tmp_path / "data"),
                  tmp_path / "data", registry_under_test)
    # When it is loaded and validated
    with env.active():
        problems = check_B1(registry_under_test)
    proc = env.cli("registry", "validate", registry_under_test, "--json")
    # Then every component has exactly one of the 8 classes, legacy entries are listed,
    # and no configuration (the only thing T0 tests) names a legacy component
    assert problems == []
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with env.active():
        listed = {as_doc(c)["id"]: as_doc(c)["class"] for c in rm("registry.load_registry")(registry_under_test).components()}
    assert "legacy" in listed.values()


@pytest.mark.requirement("B1")
@NOT_BUILT
def test_AT_B1_negative_control(tmp_path):
    # Given five registries, each the example with one deliberate defect
    def broken(name, catalogue_edit=None, eligibility_edit=None):
        catalogue, eligibility = example_catalogue(), example_eligibility()
        if catalogue_edit:
            catalogue_edit(catalogue)
        if eligibility_edit:
            eligibility_edit(eligibility)
        return write_registry(tmp_path / name, catalogue, eligibility)

    def tidechart(catalogue):
        return next(c for c in catalogue["components"] if c["id"] == "tidechart")

    cases = {
        "no class": broken("no-class", lambda c: tidechart(c).pop("class")),
        "two classes": broken("two-classes", lambda c: tidechart(c).update({"class": ["ip_tech", "legacy"]})),
        "unknown class": broken("unknown-class", lambda c: tidechart(c).update({"class": "library"})),
        "same id, two classes": broken("duplicate-id", lambda c: c["components"].append(
            {"id": "tidechart", "class": "toolkit", "canonical": {"host": "github", "path": "SoC-Labs/TideChart-Duplicate"}})),
        "legacy under test": broken("legacy-tested", eligibility_edit=lambda e: e["configurations"].append(
            {"id": "secworks-sha256", "component": "secworks-sha256-project", "ref": {"branch": "main"}})),
    }
    env = ToolEnv(tmp_path / "env", service_config(registry=cases["no class"], data_dir=tmp_path / "data"),
                  tmp_path / "data", cases["no class"])
    # When the B1 check runs on each / Then it fails on every one
    with env.active():
        for name, registry in cases.items():
            assert check_B1(registry), "check_B1 passed a registry with %s" % name


# ============================================================================================
# AT-B2
# ============================================================================================
@pytest.mark.requirement("B2")
@pytest.mark.live
@NOT_BUILT
def test_AT_B2(live):
    # Given eth-rc4 registered frozen at ab73e40 and every repo in its tree mirrored
    # When the walker resolves it from that commit's own .gitmodules and gitlinks
    with live.active():
        pins = rm("walker.walk")("eth-rc4")
    proc = live.cli("walk", "eth-rc4", "--json")
    # Then the tree is exactly rc4's 44 pins: same keys, child commits, tree paths, depths
    assert check_B2(pins, rc4_golden_pins(), RC4, "eth-rc4") == []
    depths = Counter(as_doc(p)["fact"]["depth"] for p in pins)
    assert depths == Counter({1: 5, 2: 9, 3: 17, 4: 12, 5: 1})
    # and the CLI front end gives the same tree (PR4)
    assert proc.returncode in (0, 1), proc.stderr
    assert check_B2(cli_docs(proc), rc4_golden_pins(), RC4, "eth-rc4") == []


@pytest.mark.requirement("B2")
@pytest.mark.live
@NOT_BUILT
def test_AT_B2_negative_control(live):
    # Given a different real tree: eth main frozen at its 2026-09-30 head (43 pins, 13 shared with rc4)
    with live.active():
        other = rm("walker.walk")("eth-main-20260930")
        rc4 = rm("walker.walk")("eth-rc4")
    # When the B2 check compares it with rc4's golden / Then it fails
    problems = check_B2(other, rc4_golden_pins(), RC4, "eth-rc4")
    assert any(p.startswith("missing pin") for p in problems), problems
    # and the comparator fails on the TRUE tree when the golden lacks one pin
    assert any(p.startswith("extra pin") for p in check_B2(rc4, rc4_golden_pins()[1:], RC4, "eth-rc4"))


# ============================================================================================
# AT-B3
# ============================================================================================
@pytest.mark.requirement("B3")
@pytest.mark.live
@NOT_BUILT
def test_AT_B3(request, hermetic):
    # The hermetic half runs first, so it is exercised (and its result visible) even where the live
    # half then fails for want of lab inputs (coverage review High 4).
    # Given the hermetic table: one pin per state, the SSH badge, the branch= trap, a keep/* tag
    registry, expected, rewrite = build_state_table(hermetic)
    env = hermetic.tool_env(registry, "states")
    with env.active():
        sync = rm("mirror.sync_component")
        for component in load_yaml(registry / "catalogue.yaml")["components"]:
            sync(component["id"])
        rewrite()
        sync("fx-rewritten")
        # When it is walked
        table = rm("walker.walk")("fx-states")
    # Then each of the 11 states appears exactly where the fixture put it
    assert check_B3(table, expected, exact_lag=True) == []
    assert env.cli("pins", "fx-states", "--json").returncode == 1  # red pins on their pinned host

    # Given rc4 and multicore-master (frozen at 127e3d2 for a fixed tree), mirrored from both hosts
    live = request.getfixturevalue("live")
    # When the walker gives every pin a state on each host
    with live.active():
        rc4 = rm("walker.walk")("eth-rc4")
        multicore = rm("walker.walk")("multicore-master-20260930")
    # Then the states match the hand check: rc4's 44 pinned-host states (32 ON_DEFAULT,
    # 11 PROTECTED_BY_TAG, TideLink TAGGED v0.90) and its 7 second-host states (TideLink MISSING
    # on GitLab, arch_tech ecf76aa BRANCH_ONLY on GitHub, ...); branch= recorded, never used
    assert check_B3(rc4, rc4_state_golden(), exact_lag=False) == []
    assert check_B3(multicore, multicore_state_golden(), exact_lag=False) == []
    assert live.cli("pins", "eth-rc4", "--json").returncode == 0  # nothing red on its pinned host; SSH is a badge

    # and a person has confirmed both real goldens by hand (the done-when)
    for configuration, record in HAND_CHECK.items():
        assert record["by"] and record["on"], "B3 done-when: %s's golden is not hand-checked yet" % configuration


@pytest.mark.requirement("B3")
@pytest.mark.live
@NOT_BUILT
def test_AT_B3_negative_control(request, hermetic):
    # Hermetic half first (see test_AT_B3).
    # Given the hermetic table with tag v1.0.0 deleted on the host after the fixture was built
    registry, expected, rewrite = build_state_table(hermetic)
    env = hermetic.tool_env(registry, "states-broken")
    with env.active():
        sync = rm("mirror.sync_component")
        for component in load_yaml(registry / "catalogue.yaml")["components"]:
            sync(component["id"])
        rewrite()
        sync("fx-rewritten")
        hermetic.delete_remote_ref("github", "SoC-Labs/fx-tagged", "refs/tags/v1.0.0")
        sync("fx-tagged")
        table = rm("walker.walk")("fx-states")
    # Then the check fails at the TAGGED pin
    problems = check_B3(table, expected, exact_lag=True)
    assert any(p.startswith("tagged@github: state") for p in problems), problems

    # Given rc4's real mirrors with keep/eth-rc4/nanosoc_gen deleted (shadow only): d04de2e is
    # then reachable only from wip/uncommitted-2026-08-18
    shadow = request.getfixturevalue("shadow")
    gen = shadow.mirror("nanosoc_gen")
    delete_refs(gen, [ref_prefix("gitlab", "soclabs/nanosoc_gen") + "/tags/keep/eth-rc4/nanosoc_gen"])
    # When rc4 is walked and checked against the hand check
    with shadow.active():
        rc4 = rm("walker.walk")("eth-rc4")
    # Then the check fails at the generator pin (PROTECTED_BY_TAG -> BRANCH_ONLY)
    problems = check_B3(rc4, rc4_state_golden(), exact_lag=False)
    assert any(p.startswith(RC4_GEN_PATH_A + "@gitlab: state") for p in problems), problems


# ============================================================================================
# AT-B4
# ============================================================================================
@pytest.mark.requirement("B4")
@pytest.mark.live
@NOT_BUILT
def test_AT_B4(live):
    # Given eth-rc4, eth-main and multicore-master walked, and nanosoc_gen mirrored (GitLab only).
    # ASSUMED (G16): walk() stores its pin_state observations, and contains() and
    # silicon_protection() read the latest stored tree
    configurations = ["eth-rc4", "eth-main", "multicore-master"]
    with live.active():
        trees = {c: facts(rm("walker.walk")(c), "pin_state") for c in configurations}
        contains = rm("analysis.contains.contains")
        # When we ask which configurations contain the cpu_ss_hsel fix 036d381
        fix = contains("036d381", component="nanosoc_gen", configurations=configurations)
        # and three real queries whose answer is not "no": the defect's origin b0a6069 (in both
        # generators), d9973b3 (in one), and TideLink's GitLab head (on one host only)
        partial = contains(GEN_D9973B3[:7], component="nanosoc_gen", configurations=["eth-rc4"])
        yes = contains(GEN_ORIGIN_B0A6069[:7], component="nanosoc_gen", configurations=["eth-rc4"])
        tidelink = contains(TIDELINK_GITLAB_MAIN_20260930[:7], component="tidelink",
                            configurations=["eth-rc4", "multicore-master"])
    # Then the prefix resolved to the full SHA, and there is one GitLab row per configuration
    # (nanosoc_gen has no GitHub repo, so no GitHub row)
    assert {f["query"]["commit"] for f in facts(fix, "containment")} == {GEN_FIX_036D381}
    expected = {("eth-rc4", "gitlab"): ("no", {RC4_GEN_PATH_A: (GEN_D04DE2E, False), RC4_GEN_PATH_B: (GEN_1D7772D, False)})}
    for configuration in ("eth-main", "multicore-master"):  # branches move: a raw-git oracle decides
        expected[(configuration, "gitlab")] = oracle_containment(live, "nanosoc_gen", "gitlab", "soclabs/nanosoc_gen",
                                                                 GEN_FIX_036D381, trees[configuration])
        # The oracle is built from the walk, so an empty walk would make "absent" equal "absent".
        # Both trees pin nanosoc_gen twice today (eth-main d9973b3 + 1d7772d; multicore c2ca528 + 1d7772d).
        assert expected[(configuration, "gitlab")][1], "%s: the walk found no nanosoc_gen pin" % configuration
    assert check_B4(fix, expected) == []
    # The done-when list, per host. Observed 2026-09-30: empty on gitlab, no github row. No registered
    # configuration carries the fix (eth-main pins d9973b3 and 1d7772d; multicore c2ca528 and 1d7772d).
    print("configurations containing 036d381:", sorted(k for k, (a, _p) in expected.items() if a in ("yes", "partial")))
    # and the tool can say yes, partial, unknown and absent on real data, so "no" is not its only answer:
    # rc4 holds the defect's origin in both generators (yes) and d9973b3 in one of them (partial, B7)
    assert check_B4(yes, {("eth-rc4", "gitlab"): (
        "yes", {RC4_GEN_PATH_A: (GEN_D04DE2E, True), RC4_GEN_PATH_B: (GEN_1D7772D, True)})}) == []
    assert check_B4(partial, {("eth-rc4", "gitlab"): (
        "partial", {RC4_GEN_PATH_A: (GEN_D04DE2E, True), RC4_GEN_PATH_B: (GEN_1D7772D, False)})}) == []
    assert check_B4(tidelink, {
        ("eth-rc4", "github"): ("yes", {"tidelink": (TIDELINK_RC4, True)}),
        ("eth-rc4", "gitlab"): ("unknown", {"tidelink": (TIDELINK_RC4, None)}),  # 5e8bdb5 is not on the GitLab copy
        ("multicore-master", "github"): ("absent", {}),  # one row per host holding the component (section 6 step 4)
        ("multicore-master", "gitlab"): ("absent", {}),
    }) == []
    # CLI: 0 when every answer is decided (including "no"), 2 when any is unknown
    assert live.cli("contains", "036d381", "--component", "nanosoc_gen", "--config", "eth-rc4", "--json").returncode == 0
    assert live.cli("contains", "9092300", "--component", "tidelink", "--config", "eth-rc4", "--json").returncode == 2


@pytest.mark.requirement("B4")
@pytest.mark.live
@NOT_BUILT
def test_AT_B4_negative_control(shadow):
    # Given rc4's real mirrors where d04de2e is still an object but no branch or tag reaches it
    gen = shadow.mirror("nanosoc_gen")
    prefix = ref_prefix("gitlab", "soclabs/nanosoc_gen")
    delete_refs(gen, refs_containing(gen, prefix, GEN_D04DE2E))
    assert git("--git-dir", gen, "cat-file", "-t", GEN_D04DE2E) == "commit"
    # When the same question is asked
    with shadow.active():
        rm("walker.walk")("eth-rc4")
        fix = rm("analysis.contains.contains")("036d381", component="nanosoc_gen", configurations=["eth-rc4"])
    # Then the AT's expectation ("no", both pins proven) fails: the pin cannot be proven, so the
    # answer must be unknown, never "no" (B4 Why: a missing commit reads as "not contained")
    problems = check_B4(fix, {("eth-rc4", "gitlab"): (
        "no", {RC4_GEN_PATH_A: (GEN_D04DE2E, False), RC4_GEN_PATH_B: (GEN_1D7772D, False)})})
    assert problems, "check_B4 accepted 'no' for a pin that no branch or tag reaches"
    assert [f["answer"] for f in facts(fix, "containment")] == ["unknown"]
    # and a commit that exists nowhere gets no answer at all
    assert shadow.cli("contains", "0000000", "--component", "nanosoc_gen", "--json").returncode == 3


# ============================================================================================
# AT-B5
# ============================================================================================
@pytest.mark.requirement("B5")
@pytest.mark.live
@NOT_BUILT
def test_AT_B5(live):
    # Given eth-rc4 (frozen, silicon eth-chiplet r0p0) and the 11 keep/eth-rc4/* tags on the hosts
    with live.active():
        tree = facts(rm("walker.walk")("eth-rc4"), "pin_state")
        protect = rm("analysis.protect.silicon_protection")
        # When the silicon-protection list is built
        rows = protect("eth-rc4")
        branch_rows = protect("eth-main") + protect("multicore-master")
    golden = rc4_protection_golden()
    # Then it is exactly the golden rows: the 11 commits on their pinned host, each covered by its keep
    # tag (the done-when), plus the 2 canonical-host rows (arch_tech ecf76aa and ahb_qspi e908ac8 on
    # GitHub), uncovered (interfaces.md section 7, question 9)
    assert check_B5(rows, golden) == []
    pinned = [f for f in facts(rows, "silicon_protection") if f["pinned"]]
    assert Counter(f["repo"]["host"] for f in pinned) == Counter({"github": 3, "gitlab": 8})
    assert all(f["covered"] for f in pinned)
    # and the pinned rows are exactly the pins whose default branch cannot reach them on the pinned host
    assert {(f["child"]["component"], f["child"]["commit"]) for f in tree if not reached_by_default(f)} \
        == {(f["component"], f["commit"]) for f in pinned}
    assert branch_rows == []  # neither frozen nor silicon: [] rather than an error (section 7)
    # --uncovered lists exactly the uncovered golden rows and exits 1 while any exists
    uncovered = live.cli("silicon-protection", "eth-rc4", "--uncovered", "--json")
    want_uncovered = [g for g in golden if not g["covered"]]
    assert uncovered.returncode == (1 if want_uncovered else 0)
    assert check_B5(cli_docs(uncovered), want_uncovered) == []
    listed = live.cli("silicon-protection", "eth-rc4", "--json")
    assert listed.returncode in (0, 1) and check_B5(cli_docs(listed), golden) == []


@pytest.mark.requirement("B5")
@pytest.mark.live
@NOT_BUILT
def test_AT_B5_negative_control(shadow):
    # Given rc4's real mirrors with keep/eth-rc4/tidechart deleted (shadow only)
    delete_refs(shadow.mirror("tidechart"), [ref_prefix("gitlab", "soclabs/tidechart") + "/tags/keep/eth-rc4/tidechart"])
    # When the list is built
    with shadow.active():
        rm("walker.walk")("eth-rc4")
        rows = rm("analysis.protect.silicon_protection")("eth-rc4")
    uncovered = shadow.cli("silicon-protection", "eth-rc4", "--uncovered", "--json")
    # Then the B5 check fails on tidechart, and the CLI adds exactly that row to the uncovered list
    assert any(p.startswith("tidechart 4b4b898@gitlab") for p in check_B5(rows, rc4_protection_golden()))
    assert uncovered.returncode == 1
    baseline = {(g["component"], g["repo"]["host"]) for g in rc4_protection_golden() if not g["covered"]}
    assert {(f["component"], f["repo"]["host"]) for f in facts(cli_docs(uncovered), "silicon_protection")} \
        == baseline | {("tidechart", "gitlab")}


# ============================================================================================
# AT-B6
# ============================================================================================
@pytest.mark.requirement("B6")
@pytest.mark.live
@NOT_BUILT
def test_AT_B6(live, shadow):
    golden = host_compare_golden()
    # Given the three components' mirrors replayed at the heads observed on 2026-09-30 (shadow)
    replay_heads(shadow, golden)
    # When the copies are compared
    with shadow.active():
        replay = [o for c in golden for o in rm("analysis.hosts.compare_hosts")(c)]
    # Then TideLink shows GitHub ahead; AHB-QSPI and ASIC-Flow show diverged
    assert check_B6(replay, B6_DONE_WHEN) == []  # AHEAD 298/0; DIVERGED 1/5; DIVERGED 4/1
    # and diverged is red: the CLI exits 1 while any component is DIVERGED, 0 for TideLink alone
    assert shadow.cli("hosts", "--json").returncode == 1
    assert shadow.cli("hosts", "tidelink", "--json").returncode == 0

    # Given today's live mirrors / When every component with a copy is compared / Then each answer
    # equals raw git in the same mirror (heads, counts, relation). The hosts move, so today's
    # relations are not hard-coded: the replay above is the done-when on fixed heads.
    with live.active():
        catalogue = rm("registry.load_registry")(live.registry)
        with_copies = [as_doc(c)["id"] for c in catalogue.components() if as_doc(c).get("copies")]
        today = [o for c in with_copies for o in rm("analysis.hosts.compare_hosts")(c)]
    assert sorted({f["component"] for f in facts(today, "host_compare")}) == sorted(with_copies)
    assert check_B6(today, {(f["component"], f["copy"]["repo"]["host"], f["copy"]["repo"]["path"]): oracle_host_compare(live, f)
                            for f in facts(today, "host_compare")}) == []


@pytest.mark.requirement("B6")
@pytest.mark.live
@NOT_BUILT
def test_AT_B6_negative_control(shadow):
    golden = host_compare_golden()
    replay_heads(shadow, golden)
    # Given two broken inputs (shadow only):
    #   ahb_qspi: the GitLab copy's master set to GitHub's head, as if the copy had been synced
    ahb = golden["ahb_qspi"]
    set_ref(shadow.mirror("ahb_qspi"), "%s/heads/%s" % (ref_prefix(**ahb["copy"]["repo"]), ahb["copy"]["default_branch"]),
            ahb["canonical"]["head"])
    #   asic-flow: every GitLab ref gone, as if the copy could not be read
    flow = shadow.mirror("asic-flow")
    gitlab_prefix = ref_prefix(**golden["asic-flow"]["copy"]["repo"])
    delete_refs(flow, git("--git-dir", flow, "for-each-ref", "--format=%(refname)", gitlab_prefix).split())
    # When the copies are compared
    with shadow.active():
        observed = rm("analysis.hosts.compare_hosts")("ahb_qspi") + rm("analysis.hosts.compare_hosts")("asic-flow")
    # Then the AT's expectation fails on both, and an unreadable side is UNKNOWN, never SAME or AHEAD:
    # a side whose default-branch ref is absent from the mirror is unreadable, and an UNKNOWN row
    # carries null counts and may carry a null head (interfaces.md section 5)
    problems = check_B6(observed, {c: B6_DONE_WHEN[c] for c in ("ahb_qspi", "asic-flow")})
    assert any(p.startswith("ahb_qspi:") for p in problems) and any(p.startswith("asic-flow:") for p in problems), problems
    assert {f["component"]: f["relation"] for f in facts(observed, "host_compare")}["asic-flow"] == "UNKNOWN"


# ============================================================================================
# AT-B8
# ============================================================================================
@pytest.mark.requirement("B8")
@NOT_BUILT
def test_AT_B8(hermetic):
    # Given a local test repo standing in for a GitLab repo, main = A-B-C-D-E, mirrored once,
    # and a registered frozen configuration pinning D
    fx = build_rewrite_fixture(hermetic)
    env = hermetic.tool_env(fx["registry"], "rewrite")
    with env.active():
        sync, walk, latest = rm("mirror.sync_component"), rm("walker.walk"), rm("db.latest")
        assert check_B8(sync("rm-fixture-soc") + sync("rm-rewrite-fixture"), None) == []
        walk("rm-fixture")
        # When main is force-pushed to A-B-F (C, D, E dropped) and the poller syncs
        new = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["B"]], files={"n": "rewritten\n"})
        hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", new, "main", force=True)
        observed = sync("rm-rewrite-fixture")
        # Then exactly one alert: old and new head, 3 lost commits, the lost pin named
        assert check_B8(observed, rewrite_alert_expected(fx, new)) == []
        subject = next(as_doc(o)["subject"] for o in observed if as_doc(o)["kind"] == "rewrite_alert")
        # the dashboard banner's source. ASSUMED (G12): sync_component stores what it returns, and
        # pinned_lost is computed from the latest stored pin_state observations
        assert subject in [as_doc(o)["subject"] for o in latest("rewrite_alert")]
        # and a quiet re-sync neither raises a second alert nor clears the first
        assert check_B8(sync("rm-rewrite-fixture"), None) == []
        assert subject in [as_doc(o)["subject"] for o in latest("rewrite_alert")]
        # and the lost pin now reads REWRITTEN (B3, from the mirror's reflog)
        pin = next(f for f in facts(walk("rm-fixture"), "pin_state") if f["path"] == "deps/rewrite")
        assert pin["hosts"]["gitlab"]["state"] == "REWRITTEN"
        # and the detector itself fires on a non-descendant and stays quiet on a descendant
        detect = rm("mirror.detect_rewrite")
        key = ref_prefix("gitlab", "soclabs/rm-rewrite-fixture")[len("refs/r/"):]
        assert detect(env.mirror("rm-rewrite-fixture"), key, "main", fx["E"], new) is not None
        assert detect(env.mirror("rm-rewrite-fixture"), key, "main", fx["B"], fx["E"]) is None


@pytest.mark.requirement("B8")
@NOT_BUILT
def test_AT_B8_negative_control(hermetic):
    # Given the same fixture, mirrored once
    fx = build_rewrite_fixture(hermetic)
    env = hermetic.tool_env(fx["registry"], "rewrite-broken")
    with env.active():
        sync = rm("mirror.sync_component")
        sync("rm-fixture-soc")
        sync("rm-rewrite-fixture")
        rm("walker.walk")("rm-fixture")
        # When main moves by a fast-forward (E-G): no history is lost
        ff = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["E"]], files={"n": "forward\n"})
        hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", ff, "main")
        after_ff = sync("rm-rewrite-fixture")
        # and a NON-default branch is force-pushed
        side = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["C"]], files={"n": "side\n"})
        hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", side, "feat/side")
        sync("rm-rewrite-fixture")
        side2 = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["B"]], files={"n": "side 2\n"})
        hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", side2, "feat/side", force=True)
        after_side = sync("rm-rewrite-fixture")
    # Then the B8 check (expecting one alert) fails on both: neither is a default-branch rewrite
    assert check_B8(after_ff, rewrite_alert_expected(fx, ff)), "check_B8 passed on a fast-forward"
    assert check_B8(after_side, rewrite_alert_expected(fx, side2)), "check_B8 passed on a non-default branch"
    # and the tool raised no alert for either (no false alarm, interfaces.md §8: default branch only)
    assert check_B8(after_ff, None) == []
    assert check_B8(after_side, None) == []


# ============================================================================================
# Self-checks: they test THIS FILE, not the tool, and run now (PR6 at checker level).
# Each check_<ID>() passes on a consistent real input and fails on the broken one, and the
# hermetic fixtures hold the states they claim, by a raw-git oracle that shares no tool code.
# ============================================================================================
SELFCHECK_AT = "2026-09-30T17:31:59Z"
SELFCHECK_SOURCE = [{"kind": "derived", "command": "selfcheck: synthesised by tests/acceptance/test_pins.py"}]


def selfcheck_snapshot(configuration):
    return {"id": "pin_state:%s@%s" % (configuration, SELFCHECK_AT), "scope": "pin_state:%s" % configuration}


def catalogue_repos(catalogue):
    """component id -> {host: (repo, role)} for canonical, copies and externals."""
    repos = {}
    for c in catalogue["components"]:
        repos[c["id"]] = {c["canonical"]["host"]: (c["canonical"], "canonical")}
        for cp in c.get("copies", []):
            repos[c["id"]][cp["host"]] = (cp, "copy")
    for e in catalogue.get("external", []):
        repos[e["id"]] = {e["repo"]["host"]: (e["repo"], "external")}
    return repos


def host_state_doc(repo, role, pinned, state, *, default_branch=None, lag=None, branches=(), tags=()):
    return {"repo": repo, "role": role, "pinned": pinned, "state": state, "default_branch": default_branch,
            "lag": lag, "branches": list(branches), "tags": list(tags), "observed_at": SELFCHECK_AT,
            "sources": SELFCHECK_SOURCE}


def synth_rc4_pin_docs():
    """rc4's 44 pin_state observations as a correct walker would emit them, rebuilt from the
    goldens (lock, RC4_OTHER_HOST, RC4_BRANCH_FIELDS). Schema-valid, so the checkers see real shapes."""
    repos = catalogue_repos(example_catalogue())
    pins = rc4_golden_pins()
    commits = {}
    for p in pins:
        commits.setdefault(p["component"], set()).add(p["commit"])
    docs = []
    for p in pins:
        path = p["tree_paths"][0]
        pinned_host = p["repo"]["host"]
        hosts = {pinned_host: host_state_doc(p["repo"], repos[p["component"]][pinned_host][1], True, p["state"],
                                             lag=p.get("lag"), tags=p["tags"])}
        if path in RC4_OTHER_HOST:
            host, state, lag = RC4_OTHER_HOST[path]
            repo, role = repos[p["component"]][host]
            hosts[host] = host_state_doc(repo, role, False, state, lag=lag,
                                         branches=["wip/uncommitted-2026-08-18"] if state == "BRANCH_ONLY" else ())
        url = ("git@example.invalid:%s.git" if p["url_kind"] == "ssh" else "https://example.invalid/%s.git") % p["repo"]["path"]
        docs.append({
            "schema": 1, "kind": "pin_state", "subject": "pin_state:eth-rc4:%s" % path,
            "observed_at": SELFCHECK_AT, "sources": SELFCHECK_SOURCE, "snapshot": selfcheck_snapshot("eth-rc4"),
            "fact": {
                "configuration": "eth-rc4",
                "parent": {"component": None, "repo": p["parent"]["repo"], "commit": p["parent"]["commit"]},
                "path": p["path"], "url": url, "url_kind": p["url_kind"], "branch_field": RC4_BRANCH_FIELDS.get(path),
                "entry_type": "commit", "state": None,
                "child": {"component": p["component"],
                          "catalogued_as": "external" if repos[p["component"]][pinned_host][1] == "external" else "component",
                          "repo": p["repo"], "commit": p["commit"]},
                "tree_paths": list(p["tree_paths"]), "depth": p["depth"],
                "split": len(commits[p["component"]]) > 1, "effective": "unknown", "hosts": hosts,
            },
        })
    return docs


def doc_at(docs, path):
    return next(d for d in docs if d["fact"]["tree_paths"][0] == path)


def test_selfcheck_B1_schema():
    """B1: the schema alone refuses a missing, doubled or unknown class; a duplicate id and a legacy
    configuration are schema-valid, so only the loader's README §3 rules 1 and 2 can catch them."""
    assert schema_errors(example_catalogue(), "component-catalogue") == []
    assert "legacy" in {c["class"] for c in example_catalogue()["components"]}
    for edit in (lambda c: c.pop("class"), lambda c: c.update({"class": ["ip_tech", "legacy"]}),
                 lambda c: c.update({"class": "library"})):
        catalogue = example_catalogue()
        edit(next(c for c in catalogue["components"] if c["id"] == "tidechart"))
        assert schema_errors(catalogue, "component-catalogue"), "the schema accepted a bad class"
    duplicate = example_catalogue()
    duplicate["components"].append({"id": "tidechart", "class": "toolkit",
                                    "canonical": {"host": "github", "path": "SoC-Labs/TideChart-Duplicate"}})
    assert schema_errors(duplicate, "component-catalogue") == []  # loader rule 1's job
    legacy = example_eligibility()
    legacy["configurations"].append({"id": "secworks-sha256", "component": "secworks-sha256-project", "ref": {"branch": "main"}})
    assert schema_errors(legacy, "eligibility") == []  # loader rule 2's job


def test_selfcheck_B2_checker():
    docs = synth_rc4_pin_docs()
    golden = rc4_golden_pins()
    assert check_B2(docs, golden, RC4, "eth-rc4") == []
    assert Counter(d["fact"]["depth"] for d in docs) == Counter({1: 5, 2: 9, 3: 17, 4: 12, 5: 1})
    # broken inputs: a pin lost, a pin twice, a child commit moved, a depth-1 pin under another commit
    assert any(p.startswith("missing pin") for p in check_B2(docs[1:], golden, RC4, "eth-rc4"))
    assert any(p.startswith("duplicate pin") for p in check_B2(docs + docs[:1], golden, RC4, "eth-rc4"))
    moved = copy.deepcopy(docs)
    doc_at(moved, "tidelink")["fact"]["child"]["commit"] = TIDELINK_GITLAB_MAIN_20260930
    assert any("child commit" in p for p in check_B2(moved, golden, RC4, "eth-rc4"))
    assert any(p.startswith("depth-1 pin") for p in check_B2(docs, golden, ETH_MAIN_20260930, "eth-rc4"))


def test_selfcheck_B3_checker():
    docs = synth_rc4_pin_docs()
    golden = rc4_state_golden()
    assert check_B3(docs, golden, exact_lag=True) == []
    # d04de2e without its keep tag: BRANCH_ONLY
    untagged = copy.deepcopy(docs)
    doc_at(untagged, RC4_GEN_PATH_A)["fact"]["hosts"]["gitlab"].update(
        state="BRANCH_ONLY", tags=[], branches=["wip/uncommitted-2026-08-18"])
    assert any(p.startswith(RC4_GEN_PATH_A + "@gitlab: state") for p in check_B3(untagged, golden, exact_lag=False))
    # branch= trusted: tidelink-phy (branch=main, not on main) called ON_DEFAULT
    trusted = copy.deepcopy(docs)
    doc_at(trusted, "tidelink/deps/tidelink-phy")["fact"]["hosts"]["github"].update(state="ON_DEFAULT", lag=0, tags=[])
    assert any(p.startswith("tidelink/deps/tidelink-phy@github: state") for p in check_B3(trusted, golden, exact_lag=False))
    # TideLink's second host silently dropped; its lag shrunk below the golden; SSH badge lost
    dropped = copy.deepcopy(docs)
    del doc_at(dropped, "tidelink")["fact"]["hosts"]["gitlab"]
    assert "tidelink@gitlab: no state" in check_B3(dropped, golden, exact_lag=False)
    shrunk = copy.deepcopy(docs)
    doc_at(shrunk, "tidelink")["fact"]["hosts"]["github"]["lag"] = 4
    assert any(p.startswith("tidelink@github: lag") for p in check_B3(shrunk, golden, exact_lag=False))
    https = copy.deepcopy(docs)
    doc_at(https, "ASIC/asic-toolkit")["fact"]["url_kind"] = "https"
    assert any(p.startswith("ASIC/asic-toolkit: url_kind") for p in check_B3(https, golden, exact_lag=False))


def test_selfcheck_B4_checker():
    real = example_observations("containment")
    assert len(real) == 1
    expected = {("eth-rc4", "gitlab"): ("no", {RC4_GEN_PATH_A: (GEN_D04DE2E, False), RC4_GEN_PATH_B: (GEN_1D7772D, False)})}
    assert check_B4(real, expected) == []
    # broken: the contract's own invalid example (undecided, no reason)
    invalid = load_yaml(EXAMPLES / "invalid" / "observation.containment-unknown-without-reason.yaml")
    assert check_B4([invalid], expected)
    # broken: "no" while a pin is undecided (a missing commit read as "not contained")
    undecided = copy.deepcopy(real)
    undecided[0]["fact"]["pins"][0].update(contains=None, reason="d04de2e is reachable from no branch or tag on gitlab")
    assert any("disagrees with its own pins" in p for p in check_B4(undecided, expected))
    # broken: "no" with no existence proof for d04de2e in the sources (A3)
    unproven = copy.deepcopy(real)
    unproven[0]["sources"] = [s for s in unproven[0]["sources"] if "d04de2e" not in s["command"]]
    assert any("no existence proof for d04de2e" in p for p in check_B4(unproven, expected))


def test_selfcheck_B5_checker():
    rows = example_observations("silicon_protection")
    assert check_B5(rows, rc4_protection_golden()) == []
    assert Counter(r["fact"]["repo"]["host"] for r in rows if r["fact"]["pinned"]) == Counter({"github": 3, "gitlab": 8})
    uncovered = copy.deepcopy(rows)
    tidechart = next(r for r in uncovered if r["fact"]["component"] == "tidechart")
    tidechart["fact"].update(covered=False, covering_tags=[], state="BRANCH_ONLY")
    assert any(p.startswith("tidechart 4b4b898@gitlab: not covered") for p in check_B5(uncovered, rc4_protection_golden()))
    assert any(p.endswith("missing row") for p in check_B5(rows[1:], rc4_protection_golden()))
    on_main = copy.deepcopy(rows)
    on_main[0]["fact"]["branches"].append(on_main[0]["fact"]["default_branch"])
    assert any("on its default branch" in p for p in check_B5(on_main, rc4_protection_golden()))
    # judged only on the pinned host: the canonical-host rows are missing (question 9 decides which is right)
    pinned_only = [r for r in rows if r["fact"]["pinned"]]
    assert sum(1 for p in check_B5(pinned_only, rc4_protection_golden()) if p.endswith("missing row")) == 2
    # a canonical-host row that claims coverage it does not have
    lying = copy.deepcopy(rows)
    arch = next(r for r in lying if r["fact"]["component"] == "nanosoc_arch_tech" and not r["fact"]["pinned"])
    arch["fact"].update(covered=True, covering_tags=["keep/eth-rc4/nanosoc_arch_tech"], state="PROTECTED_BY_TAG")
    assert any(p.startswith("nanosoc_arch_tech ecf76aa@github: covered, golden uncovered") for p in
               check_B5(lying, rc4_protection_golden()))


def test_selfcheck_B6_checker():
    counted = example_observations("host_compare")
    # The contract examples count with rev-list in one mirror: they pass.
    assert check_B6(counted, B6_DONE_WHEN) == []
    # The TideLink row as first recorded, counted by GitHub's compare API: the check refuses that
    # source, because an API on one host cannot see the other host's commits (README section 7 fact 4)
    api = copy.deepcopy(counted)
    tidelink = next(d for d in api if d["fact"]["component"] == "tidelink")
    tidelink["sources"] = [{"kind": "github_api", "command": "GET repos/SoC-Labs/TideLink-Chiplet-Interconnect-AHB/compare/%s...%s"
                            % (tidelink["fact"]["copy"]["head"], tidelink["fact"]["canonical"]["head"])}]
    assert any(p.startswith("tidelink: counts not from rev-list") for p in check_B6(api, B6_DONE_WHEN))
    synced = copy.deepcopy(counted)
    ahb = next(d for d in synced if d["fact"]["component"] == "ahb_qspi")["fact"]
    ahb.update(relation="SAME", canonical_only=0, copy_only=0)
    ahb["copy"]["head"] = ahb["canonical"]["head"]
    problems = check_B6(synced, B6_DONE_WHEN)
    assert any(p.startswith("ahb_qspi: relation SAME") for p in problems), problems
    assert any(p.startswith("asic-flow: 0 host_compare rows") for p in check_B6(counted[:2], B6_DONE_WHEN))


def test_selfcheck_B8_checker():
    real = example_observations("rewrite_alert")
    assert len(real) == 1
    f = real[0]["fact"]
    expected = {k: f[k] for k in ("component", "repo", "branch", "old_head", "new_head", "lost_commits", "pinned_lost")}
    assert check_B8(real, expected) == []
    assert check_B8([], expected)                    # no alert
    assert check_B8(real + real, expected)           # two alerts
    fewer = copy.deepcopy(real)
    fewer[0]["fact"]["lost_commits"] = 2
    assert check_B8(fewer, expected)
    assert check_B8(real, None)                      # an alert where none should fire


# -------------------------------- raw-git oracle for the hermetic fixtures --------------------------------
def oracle_sync(h, root, entry):
    """One component mirror built as interfaces.md section 3 prescribes (explicit refspecs, --no-tags,
    reflogs kept), with raw git. Returns (mirror, {(host, path): fetched ok})."""
    mirror = Path(root) / ("%s.git" % entry["id"])
    if not mirror.exists():
        h.git("init", "--quiet", "--bare", mirror)
        for key, value in (("core.logAllRefUpdates", "always"), ("gc.reflogExpire", "never"),
                           ("gc.reflogExpireUnreachable", "never"), ("gc.pruneExpire", "never")):
            h.git("--git-dir", mirror, "config", key, value)
    ok = {}
    for repo in [entry["canonical"], *entry.get("copies", [])]:
        key = ref_prefix(repo["host"], repo["path"])
        proc = subprocess.run(["git", "--git-dir", str(mirror), "fetch", "--quiet", "--no-tags", "--prune", "--filter=blob:none",
                               h.url(repo["host"], repo["path"]), "+refs/heads/*:%s/heads/*" % key,
                               "+refs/tags/*:%s/tags/*" % key], env=h.env, capture_output=True, text=True)
        ok[(repo["host"], repo["path"])] = proc.returncode == 0
    return mirror, ok


def oracle_host_state(h, mirror, readable, repo, role, pinned, commit, known_lost):
    """README §5 precedence for one host, from the mirror's refs and reflogs and the fake API."""
    host, path = repo["host"], repo["path"]
    if not readable:
        return host_state_doc(repo, role, pinned, "UNREADABLE")
    key = ref_prefix(host, path)
    symref = h.git("ls-remote", "--symref", h.url(host, path), "HEAD")
    default = re.search(r"ref: refs/heads/(\S+)\s+HEAD", symref).group(1)
    heads = [r[len(key) + len("/heads/"):] for r in refs_containing(mirror, key + "/heads", commit)]
    tags = [r[len(key) + len("/tags/"):] for r in refs_containing(mirror, key + "/tags", commit)]
    points = git("--git-dir", mirror, "for-each-ref", "--points-at", commit, "--format=%(refname)", key + "/tags", check=False).split()
    on_default = default in heads
    lag = int(git("--git-dir", mirror, "rev-list", "--count", "%s..%s/heads/%s" % (commit, key, default))) if on_default else None
    if any(not r[len(key) + len("/tags/"):].startswith("keep/") for r in points):
        state = "TAGGED"
    elif on_default:
        state = "ON_DEFAULT"
    elif tags:
        state = "PROTECTED_BY_TAG"
    elif heads:
        state = "BRANCH_ONLY"
    else:
        old_heads = git("--git-dir", mirror, "reflog", "show", "--format=%H", "%s/heads/%s" % (key, default), check=False).split()
        if commit in known_lost or any(ancestry(mirror, commit, old) for old in old_heads):
            state = "REWRITTEN"
        else:
            api = ("/gh/repos/%s/commits/%s" % (path, commit) if host == "github" else
                   "/gl/api/v4/projects/%s/repository/commits/%s" % (urllib.parse.quote(path, safe=""), commit))
            status, _body = h.api_answer(api)
            if status != 200:
                state = "MISSING"
            else:
                _status, prs = h.api_answer(api + ("/pulls" if host == "github" else "/merge_requests"))
                state = "PR_ONLY" if prs else "ORPHAN"
    return host_state_doc(repo, role, pinned, state, default_branch=default, lag=lag, branches=heads, tags=tags)


def oracle_walk_flat(h, registry, configuration, mirrors):
    """What the walker must emit for a one-level hermetic tree, as pin_state documents."""
    catalogue = load_yaml(registry / "catalogue.yaml")
    cfg = next(c for c in load_yaml(registry / "eligibility.yaml")["configurations"] if c["id"] == configuration)
    root = next(c for c in catalogue["components"] if c["id"] == cfg["component"])["canonical"]
    repos = catalogue_repos(catalogue)
    by_repo = {(r["host"], r["path"]): cid for cid, hosts in repos.items() for r, _role in hosts.values()}
    known = {cid: {(k["host"], sha) for k in c.get("known_rewrites", []) for sha in k["lost"]} for cid, c in
             ((c["id"], c) for c in catalogue["components"])}
    soc = h.bare(root["host"], root["path"])
    listing = git("--git-dir", soc, "config", "--blob", "%s:.gitmodules" % cfg["commit"], "--list")
    modules = {}
    for line in listing.splitlines():
        name_key, _, value = line.partition("=")
        name, _, field = name_key[len("submodule."):].rpartition(".")
        modules.setdefault(name, {})[field] = value
    docs = []
    for module in modules.values():
        path, url = module["path"], module["url"]
        entry = git("--git-dir", soc, "ls-tree", cfg["commit"], "--", path)
        entry_type = entry.split()[1] if entry else "absent"
        gitlink = entry.split()[2] if entry_type == "commit" else None
        repo, kind = None, "other"
        for host in ("github", "gitlab"):
            for prefix, k in ((h.WEB[host], "https"), (h.SSH[host], "ssh")):
                if url.startswith(prefix):
                    repo, kind = {"host": host, "path": url[len(prefix):][:-len(".git")]}, k
        if url.startswith("/"):
            kind = "local"
        component = by_repo.get((repo["host"], repo["path"])) if repo else None
        hosts = {}
        if kind != "local" and entry_type == "commit":
            for host, (copy_repo, role) in repos[component].items():
                readable = mirrors[component][1][(copy_repo["host"], copy_repo["path"])]
                hosts[host] = oracle_host_state(h, mirrors[component][0], readable, copy_repo, role,
                                                copy_repo == repo, gitlink,
                                                {sha for kh, sha in known.get(component, ()) if kh == host})
        pin_state = "LOCAL_URL" if kind == "local" else ("NO_GITLINK" if entry_type != "commit" else None)
        docs.append({"schema": 1, "kind": "pin_state", "subject": "pin_state:%s:%s" % (configuration, path),
                     "observed_at": SELFCHECK_AT, "sources": SELFCHECK_SOURCE,
                     "snapshot": selfcheck_snapshot(configuration),
                     "fact": {"configuration": configuration,
                              "parent": {"component": cfg["component"], "repo": root, "commit": cfg["commit"]},
                              "path": path, "url": url, "url_kind": kind, "branch_field": module.get("branch"),
                              "entry_type": entry_type, "state": pin_state,
                              "child": {"component": component, "catalogued_as": "component" if component else "unknown",
                                        "repo": repo if kind != "local" else None, "commit": gitlink},
                              "tree_paths": [path], "depth": 1, "split": False, "effective": "unknown", "hosts": hosts}})
    return docs


def test_selfcheck_B3_fixture(hermetic, tmp_path):
    """The hermetic state table really holds each of the 11 states where `expected` says, and
    deleting tag v1.0.0 really turns TAGGED into ON_DEFAULT (AT-B3's hermetic control)."""
    registry, expected, rewrite = build_state_table(hermetic)
    catalogue = load_yaml(registry / "catalogue.yaml")
    entries = {c["id"]: c for c in catalogue["components"]}
    mirrors = {cid: oracle_sync(hermetic, tmp_path / "mirrors", e) for cid, e in entries.items()}
    rewrite()
    mirrors["fx-rewritten"] = oracle_sync(hermetic, tmp_path / "mirrors", entries["fx-rewritten"])
    table = oracle_walk_flat(hermetic, registry, "fx-states", mirrors)
    assert len(table) == 15
    assert check_B3(table, expected, exact_lag=True) == []
    states = {s["state"] for d in table for s in d["fact"]["hosts"].values()} | \
             {pin_level_state(d["fact"]) for d in table if not d["fact"]["hosts"]}
    assert states == {"TAGGED", "ON_DEFAULT", "PROTECTED_BY_TAG", "BRANCH_ONLY", "REWRITTEN", "PR_ONLY", "ORPHAN",
                      "MISSING", "UNREADABLE", "LOCAL_URL", "NO_GITLINK"}
    hermetic.delete_remote_ref("github", "SoC-Labs/fx-tagged", "refs/tags/v1.0.0")
    mirrors["fx-tagged"] = oracle_sync(hermetic, tmp_path / "mirrors", entries["fx-tagged"])
    broken = oracle_walk_flat(hermetic, registry, "fx-states", mirrors)
    problems = check_B3(broken, expected, exact_lag=True)
    assert any(p.startswith("tagged@github: state ON_DEFAULT") for p in problems), problems


def test_selfcheck_B8_fixture(hermetic, tmp_path):
    """The rewrite fixture really loses C, D and E, including the registered pin D; a
    fast-forward loses nothing (AT-B8 and its control, computed with raw git)."""
    fx = build_rewrite_fixture(hermetic)
    entry = next(c for c in load_yaml(fx["registry"] / "catalogue.yaml")["components"] if c["id"] == "rm-rewrite-fixture")
    key = ref_prefix("gitlab", "soclabs/rm-rewrite-fixture")
    branch = key + "/heads/main"

    def alerts_after(new_head, force):
        mirror = tmp_path / ("m-%s" % new_head[:7])
        oracle_sync(hermetic, mirror, entry)
        old = git("--git-dir", mirror / "rm-rewrite-fixture.git", "rev-parse", branch)
        hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", new_head, "main", force=force)
        m, _ok = oracle_sync(hermetic, mirror, entry)
        new = git("--git-dir", m, "rev-parse", branch)
        assert new == new_head
        if ancestry(m, old, new):
            return []
        lost = int(git("--git-dir", m, "rev-list", "--count", old, "--not", "--glob=%s/*" % key))
        pinned = [{"configuration": "rm-fixture", "tree_path": "deps/rewrite", "commit": fx["D"]}] \
            if ancestry(m, fx["D"], old) and not refs_containing(m, key, fx["D"]) else []
        return [{"schema": 1, "kind": "rewrite_alert", "observed_at": SELFCHECK_AT, "sources": SELFCHECK_SOURCE,
                 "subject": "rewrite_alert:gitlab:soclabs/rm-rewrite-fixture:main:%s" % old,
                 "fact": {"component": "rm-rewrite-fixture", "repo": {"host": "gitlab", "path": "soclabs/rm-rewrite-fixture"},
                          "branch": "main", "old_head": old, "new_head": new, "lost_commits": lost, "pinned_lost": pinned}}]

    rewritten = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["B"]], files={"n": "rewritten\n"})
    assert check_B8(alerts_after(rewritten, force=True), rewrite_alert_expected(fx, rewritten)) == []
    hermetic.push("gitlab", "soclabs/rm-rewrite-fixture", fx["E"], "main", force=True)  # restore A-B-C-D-E
    forward = hermetic.commit("gitlab", "soclabs/rm-rewrite-fixture", parents=[fx["E"]], files={"n": "forward\n"})
    quiet = alerts_after(forward, force=False)
    assert quiet == [] and check_B8(quiet, rewrite_alert_expected(fx, forward))
