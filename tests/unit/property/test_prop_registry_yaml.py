"""Property tests: the strict YAML reader (contracts.load_yaml) and registry loading (registry/loader.py).

load_yaml: data round-trips, timestamps stay strings, a duplicate key at any depth is refused with a YAMLError.
load_registry: "raises RegistryError(problems) on ANY problem: never partial" -- so for any damage to the
example registry's documents (wrong types, missing or extra values, broken YAML) it either loads or raises
RegistryError, never another exception.
"""
from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from prop_helpers import budget, example, given, st  # loads the profile; skips without hypothesis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "registry"))
from registry_helpers import LAYOUT, write_example_registry  # noqa: E402

from nanosoc_rm import contracts  # noqa: E402
from nanosoc_rm.registry import Registry, RegistryError, load_registry  # noqa: E402

KEY = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=6)
SCALAR = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=12),
                   st.sampled_from(["2026-10-01", "2026-10-01T10:00:00Z", "yes", "no", "on", "0x1f", "1e3", "~"]))
DATA = st.recursive(SCALAR, lambda c: st.one_of(st.lists(c, max_size=3), st.dictionaries(KEY, c, max_size=3)),
                    max_leaves=10)


@given(DATA)
def test_load_yaml_round_trips_safe_dump(data):
    assert contracts.load_yaml(yaml.safe_dump(data)) == data


@given(st.dates().map(str), st.sampled_from(["", "T10:00:00Z", " 10:00:00"]))
def test_timestamps_stay_strings(day, time):
    text = "at: %s%s\n" % (day, time)
    assert contracts.load_yaml(text) == {"at": day + time}


@given(st.dictionaries(st.from_regex(r"[a-z]{1,4}", fullmatch=True), SCALAR, min_size=1, max_size=4),
       st.integers(0, 3))
def test_a_duplicate_key_at_any_depth_is_refused(doc, depth):
    key = sorted(doc)[0]
    line = "%s: 1" % key
    body = "\n".join("%s: %s" % (k, yaml.safe_dump(v, default_flow_style=True).strip().replace("\n...", ""))
                     for k, v in doc.items()) + "\n" + line
    text = body
    for _ in range(depth):
        text = "n:\n" + "\n".join("  " + ln for ln in text.splitlines())
    with pytest.raises(yaml.YAMLError):
        contracts.load_yaml(text + "\n")


@pytest.fixture(scope="module")
def example_root(tmp_path_factory):
    root = write_example_registry(tmp_path_factory.mktemp("prop-registry") / "registry")
    assert isinstance(load_registry(root), Registry)                 # the base is clean
    return root


def _docs(root: Path) -> dict[str, object]:
    return {dst: contracts.load_yaml((root / dst).read_text()) for _src, dst in LAYOUT}


def _paths(doc, pre=()):
    if pre:
        yield pre
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield from _paths(v, pre + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from _paths(v, pre + (i,))


JUNK = st.recursive(st.one_of(st.none(), st.booleans(), st.integers(-3, 3), st.text(max_size=5),
                              st.sampled_from(["SL-DEF-0001", "eth-rc4", "ETH-R0P0-ERR-001", "github", "x/y"])),
                    lambda c: st.one_of(st.lists(c, max_size=3), st.dictionaries(st.text(max_size=4), c, max_size=3)),
                    max_leaves=6)


@budget(60)
@given(st.data())
def test_damaged_documents_load_or_raise_registry_error(example_root, tmp_path_factory, data):
    docs = _docs(example_root)
    targets = [(f, p) for f, d in docs.items() for p in _paths(d)]
    muts = data.draw(st.lists(st.tuples(st.sampled_from(targets), JUNK), min_size=1, max_size=3))
    docs = copy.deepcopy(docs)
    for (f, p), v in muts:
        node = docs[f]
        try:
            for k in p[:-1]:
                node = node[k]
            node[p[-1]] = v
        except (KeyError, IndexError, TypeError):
            pass
    root = tmp_path_factory.mktemp("damaged") / "registry"
    shutil.copytree(example_root, root)
    for f, doc in docs.items():
        (root / f).write_text(yaml.safe_dump(doc, sort_keys=False))
    try:
        load_registry(root)
    except RegistryError as e:
        assert e.problems
    finally:
        shutil.rmtree(root.parent)


def test_every_shallow_value_retyped_loads_or_raises_registry_error(example_root, tmp_path_factory):
    """Deterministic sweep beside the random one: every value up to depth 3 replaced by a value of the wrong
    shape (a mapping or list becomes a string, a scalar becomes a mapping), one at a time."""
    docs = _docs(example_root)
    root = tmp_path_factory.mktemp("retyped") / "registry"
    shutil.copytree(example_root, root)
    for f, doc in docs.items():
        for p in [p for p in _paths(doc) if len(p) <= 3]:
            bad = copy.deepcopy(docs)
            node = bad[f]
            for k in p[:-1]:
                node = node[k]
            node[p[-1]] = "x" if isinstance(node[p[-1]], (dict, list)) else {"x": 1}
            (root / f).write_text(yaml.safe_dump(bad[f], sort_keys=False))
            try:
                load_registry(root)
            except RegistryError as e:
                assert e.problems, (f, p)
        (root / f).write_text(yaml.safe_dump(doc, sort_keys=False))


_BROKEN_YAML = st.sampled_from(["a: [1, 2\n", "a: b: c\n", "\t- x\n", "a: 1\na: 2\n", "- 1\n", "x\n", "",
                                "a: !!python/object:os.system 1\n", "\xff", "a: &x [*x]\n"])  # complex keys: ROB-11


@budget(60)
@given(st.sampled_from([dst for _src, dst in LAYOUT]), _BROKEN_YAML)
@example("catalogue.yaml", "a: 1\na: 2\n")
def test_broken_yaml_files_raise_registry_error(example_root, tmp_path_factory, rel, text):
    root = tmp_path_factory.mktemp("broken") / "registry"
    shutil.copytree(example_root, root)
    try:
        (root / rel).write_text(text)
        with pytest.raises(RegistryError):
            load_registry(root)
    finally:
        shutil.rmtree(root.parent)


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ROB-property-11: the duplicate-key check puts every key in a set "
                                       "(contracts/__init__.py:166), so a complex (list or mapping) key raises "
                                       "TypeError, not YAMLError, and load_registry crashes instead of raising "
                                       "RegistryError")
@example("catalogue.yaml", "? [a, b]\n: 1\n")
@given(st.sampled_from([dst for _src, dst in LAYOUT]), st.sampled_from(["? [a, b]\n: 1\n", "{[a]: 1}\n",
                                                                        "? {a: 1}\n: 2\n"]))
def test_rob_property_11_complex_key_is_a_registry_error(example_root, tmp_path_factory, rel, text):
    root = tmp_path_factory.mktemp("complex-key") / "registry"
    shutil.copytree(example_root, root)
    try:
        (root / rel).write_text(text)
        with pytest.raises(RegistryError):
            load_registry(root)
    finally:
        shutil.rmtree(root.parent)
