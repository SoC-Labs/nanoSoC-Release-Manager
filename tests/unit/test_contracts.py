"""nanosoc_rm.contracts: the packaged schemas, validate(), the strict YAML reader, verdict rows."""
from __future__ import annotations

import pytest
import yaml

from nanosoc_rm import contracts

from conftest import EXAMPLES

# schemas/check_examples.py's mapping of example files to a $defs entry (it is the contracts' own checker).
DEF_EXAMPLES = {"result-bundle.rm-request.example.json": ("result-bundle", "/$defs/rm_request")}


def _target(name):
    if name.startswith("result-bundle.items."):
        return "result-bundle", "/$defs/gate_item"
    if name.startswith("matrix.cell"):                            # CR-18-matrix: $defs library examples
        return "matrix", "/$defs/matrix_cell"
    if name.startswith("matrix.quarantine"):
        return "matrix", "/$defs/matrix_quarantine"
    if name.startswith("eligibility.tiers"):                     # CR-12: registry tiers.yaml
        return "eligibility", "/$defs/tiers_file"
    if name.startswith("eligibility.quality"):                   # CR-14: registry quality.yaml
        return "eligibility", "/$defs/quality_file"
    if name.startswith("tool-release.versioning"):               # CR-VER: registry versioning.yaml
        return "tool-release", "/$defs/versioning_file"
    if name.startswith("release-lock.draft"):                    # CR-R1: release.lock.draft.yaml
        return "release-lock", "/$defs/draft_lock"
    if name.startswith("regen.class"):                           # section 20: the regen_class fact
        return "regen", "/$defs/regen_class"
    if name.startswith("store.index"):                           # section 22: the store_index fact
        return "store", "/$defs/store_index"
    if name.startswith("tapeout-record.draft"):                  # CR-ART-publish: registry tapeouts/ draft
        return "tapeout-record", "/$defs/tapeout_draft"
    if name.startswith("executor.claim"):                        # section 27: an executor claim
        return "executor", "/$defs/claim"
    if name.startswith("executor.result"):                       # section 27: an executor result
        return "executor", "/$defs/result"
    return DEF_EXAMPLES.get(name) or (name.split(".", 1)[0], None)


def test_all_schemas_load():
    assert contracts.schema_names() == sorted([
        "bump-proposal", "component-catalogue", "component-manifest", "defect", "defs", "eligibility", "erratum",
        "executor",                                                              # CR-WG-executor
        "import-ledger", "matrix", "observation", "pin-state", "regen", "release-lock", "release-manifest",
        "result-bundle", "store", "tapeout-record", "tool-release"])         # CR-WE-index, CR-ART-publish
    assert (contracts.schemas_dir() / "defs.schema.json").is_file()


@pytest.mark.parametrize("path", sorted(p for p in EXAMPLES.glob("*.*") if p.suffix != ".tsv"), ids=lambda p: p.name)
def test_valid_examples_validate(path):
    schema, pointer = _target(path.name)
    for label, doc in contracts.docs_in(path):
        assert contracts.validate(schema, doc, pointer) == [], label


@pytest.mark.parametrize("path", sorted(p for p in (EXAMPLES / "invalid").glob("*.*") if p.suffix != ".tsv"),
                         ids=lambda p: p.name)
def test_invalid_examples_fail(path):
    # The schemas' own negative control (PR6), through the package's loader.
    for label, doc in contracts.docs_in(path):
        schema, pointer = _target(path.name)
        assert contracts.validate(schema, doc, pointer), "%s validated: the schema cannot fail" % label


def test_typed_validators():
    obs = [d for _, d in contracts.docs_in(EXAMPLES / "observation.example.jsonl")]
    assert all(contracts.validate_observation(d) == [] for d in obs)
    pin = next(contracts.docs_in(EXAMPLES / "pin-state.example.jsonl"))[1]
    assert contracts.validate_observation(pin) == []
    bad = dict(pin, subject="pin_state:")                       # passes the envelope, fails pin-state
    assert any(p.startswith("pin-state: ") for p in contracts.validate_observation(bad))
    item = next(contracts.docs_in(EXAMPLES / "result-bundle.items.t0.pins.reachable.example.jsonl"))[1]
    assert contracts.validate_gate_item(item) == []
    assert contracts.validate_gate_item(dict(item, result="maybe"))
    req = next(contracts.docs_in(EXAMPLES / "result-bundle.rm-request.example.json"))[1]
    assert contracts.validate_rm_request(req) == []
    assert contracts.validate_rm_request(dict(req, nonce="short"))
    prov = next(contracts.docs_in(EXAMPLES / "result-bundle.provenance.example.json"))[1]
    assert contracts.validate_provenance(prov) == []
    elig = next(contracts.docs_in(EXAMPLES / "eligibility.example.yaml"))[1]
    waiver = next(c for c in elig["configurations"] if c.get("t0_waivers"))["t0_waivers"][0]
    assert contracts.validate_t0_waiver(waiver) == []
    with pytest.raises(contracts.SchemaError) as e:
        contracts.require_valid("result-bundle", dict(req, nonce="short"), "/$defs/rm_request")
    assert e.value.problems
    with pytest.raises(KeyError):
        contracts.validate("no-such-schema", {})


def test_verdict_rows():
    lines = (EXAMPLES / "result-bundle.verdicts.example.tsv").read_text().splitlines()
    rows = [contracts.parse_verdict_row(line) for line in lines if line]
    assert rows and all(contracts.validate_verdict_row(r) == [] for r in rows)
    with pytest.raises(ValueError, match="expected 5 tab-separated fields, got 4"):
        contracts.parse_verdict_row("\t".join(lines[0].split("\t")[:4]))
    four = (EXAMPLES / "invalid" / "result-bundle.verdicts.four-columns.tsv").read_text().splitlines()
    with pytest.raises(ValueError):
        for line in four:
            if line:
                contracts.parse_verdict_row(line)
    assert contracts.validate_verdict_row(dict(rows[0], detail="a\tb"))     # a tab left in a detail is refused


def test_strict_yaml():
    assert contracts.load_yaml("at: 2026-09-30T12:00:00Z\n") == {"at": "2026-09-30T12:00:00Z"}
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        contracts.load_yaml("a: 1\na: 2\n")


def test_write_calls_declare_the_v1_shape():
    writes = [w for w in contracts.WRITE_CALLS if w["effect"] == "write"]
    assert len(writes) == 1 and writes[0]["method"] == "POST" and "trigger/pipeline" in writes[0]["target"]
    assert sum(1 for w in contracts.WRITE_CALLS if w["effect"] == "git-runner") == 1
    for w in contracts.WRITE_CALLS:
        assert set(w) >= {"file", "kind", "method", "target", "effect", "reason"}
        assert w["effect"] in contracts.WRITE_EFFECTS and w["reason"]
        if w["effect"] in ("write", "git-runner"):
            assert w.get("guard")


# ROB-coverage-5: JSON Schema patterns are ECMA-262, where `$` matches only at the very end; Python's `$` also
# matches before a final newline. The validator reads `$` as `\Z`, so a trailing newline is never a valid value.
@pytest.mark.parametrize("pointer,good", [("/$defs/semver", "1.0.0"), ("/$defs/sha", "0" * 40)])
def test_a_schema_pattern_refuses_a_trailing_newline(pointer, good):
    from nanosoc_rm import contracts
    assert contracts.validate("defs", good, pointer) == []
    assert contracts.validate("defs", good + "\n", pointer) != []


@pytest.mark.parametrize("pattern,ecma", [(r"^[0-9]+$", r"^[0-9]+\Z"), (r"^(a|$)", r"^(a|\Z)"),
                                          (r"^[$^]x$", r"^[$^]x\Z"), (r"^[]$]\$$", r"^[]$]\$\Z"),
                                          (r"^[^$]$", r"^[^$]\Z")])
def test_ecma_anchors_rewrites_only_anchor_dollars(pattern, ecma):
    from nanosoc_rm import contracts
    assert contracts.ecma_anchors(pattern) == ecma
