#!/usr/bin/env python3
"""Validate the gate-2 contract examples, and prove the schemas can fail.

    python3 schemas/check_examples.py            # exit 0 only if every check below holds

1. Every schemas/*.schema.json is a valid Draft 2020-12 schema.
2. Every schemas/examples/<name>.* validates against schemas/<name>.schema.json
   (the schema is the file-name prefix up to the first dot; .jsonl = one document per line).
   A file listed in DEF_EXAMPLES validates against one $defs entry of its schema instead.
3. Every schemas/examples/invalid/<name>.* FAILS, and fails for the reason its
   first line names:
       # expect-error: <validator keyword> at <instance pointer> [~ <message substring>]
   e.g. `# expect-error: required at /exposure ~ 'detector'`. Some error (nested
   oneOf/anyOf/if-then contexts included) must have that keyword at that instance
   path. Matching the keyword and path, not the message wording, keeps this check
   stable across jsonschema releases (4.19 says "is too long", 4.26 "is expected to
   be empty" for the same maxItems error). This is the negative control for step 2 (PR6).
4. verdicts.tsv files follow the row contract: 5 tab-separated fields, each row
   valid against result-bundle#/$defs/verdict_row, nonce equal to the paired
   provenance's run_nonce, timestamp inside [started_at, finished_at] (D5).

YAML is loaded with timestamps left as strings and duplicate keys rejected: a
registry file with a repeated key is an error, never a silent overwrite.

Needs: jsonschema >= 4.18 (with `referencing`) and PyYAML (requirements-dev.txt).
"""
import glob
import json
import os
import re
import sys

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "examples")
BAD = os.path.join(EX, "invalid")


class StrictLoader(yaml.SafeLoader):
    """SafeLoader without the timestamp resolver, rejecting duplicate keys."""


StrictLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _no_dup_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate key %r" % (key,), key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def load_schemas():
    schemas, resources = {}, []
    for path in sorted(glob.glob(os.path.join(HERE, "*.schema.json"))):
        with open(path) as f:
            doc = json.load(f)
        Draft202012Validator.check_schema(doc)
        name = os.path.basename(path)[: -len(".schema.json")]
        schemas[name] = doc
        resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    return schemas, Registry().with_resources(resources)


def docs_in(path):
    """Yield (label, document) for a .json, .jsonl or .yaml file."""
    with open(path) as f:
        text = f.read()
    if path.endswith(".jsonl"):
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                yield "%s:%d" % (os.path.basename(path), n), json.loads(line)
    elif path.endswith(".json"):
        yield os.path.basename(path), json.loads(text)
    else:
        yield os.path.basename(path), yaml.load(text, Loader=StrictLoader)


def schema_for(path):
    return os.path.basename(path).split(".", 1)[0]


def errors(validator, doc):
    return sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))


def validator_for(schemas, registry, name, pointer=None):
    schema = schemas[name]
    if pointer:
        schema = {"$ref": schema["$id"] + "#" + pointer}
    return Draft202012Validator(schema, registry=registry)


def check_tsv(path, row_validator, nonce, t_start, t_end):
    """Return a list of problems with a verdicts.tsv file (empty = conforms)."""
    problems = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return ["no rows: an empty verdicts.tsv is rejected (D5)"]
    for n, line in enumerate(lines, 1):
        fields = line.split("\t")
        if len(fields) != 5:
            problems.append("line %d: expected 5 tab-separated fields, got %d" % (n, len(fields)))
            continue
        row = dict(zip(["utc", "status", "gate", "detail", "nonce"], fields))
        for e in row_validator.iter_errors(row):
            problems.append("line %d: %s" % (n, e.message))
        if row["nonce"] != nonce:
            problems.append("line %d: nonce %s does not match run_nonce %s" % (n, row["nonce"], nonce))
        if not (t_start <= row["utc"] <= t_end):
            problems.append("line %d: %s is outside the job window %s..%s" % (n, row["utc"], t_start, t_end))
    return problems


# Examples of one $defs entry, not of a whole document: file -> (schema, JSON pointer).
DEF_EXAMPLES = {
    "result-bundle.rm-request.example.json": ("result-bundle", "/$defs/rm_request"),
}
# Valid and invalid examples of one $defs entry, by file-name prefix (CR-12: registry tiers.yaml).
DEF_PREFIXES = {"eligibility.tiers": ("eligibility", "/$defs/tiers_file"),
                "eligibility.quality": ("eligibility", "/$defs/quality_file"),       # CR-14: registry quality.yaml
                "matrix.cell": ("matrix", "/$defs/matrix_cell"),                     # CR-18-matrix (D3, D4, D8)
                "matrix.quarantine": ("matrix", "/$defs/matrix_quarantine"),
                "tool-release.versioning": ("tool-release", "/$defs/versioning_file"),   # CR-VER: registry versioning.yaml
                "release-lock.draft": ("release-lock", "/$defs/draft_lock")}         # CR-R1: release.lock.draft.yaml
# rm-bundle/items/<gate>.jsonl lines: result-bundle.items.<gate>.example.jsonl (EXCERPTS: the non-pass items
# of rc4's four non-pass gates, which the T0 waivers in eligibility.example.yaml cover; README section 6).
ITEMS_PREFIX = "result-bundle.items."


def def_example(base):
    if base.startswith(ITEMS_PREFIX):
        return ("result-bundle", "/$defs/gate_item")
    for prefix, target in DEF_PREFIXES.items():
        if base.startswith(prefix):
            return target
    return DEF_EXAMPLES.get(base)

EXPECT_RX = re.compile(r"^#\s*expect-error:\s*(\S+)\s+at\s+(\S+)(?:\s+~\s+(.+?))?\s*$")


def all_errors(validator, doc):
    """Every error, descending into oneOf/anyOf/if-then contexts, so a nested reason is visible."""
    out, stack = [], list(validator.iter_errors(doc))
    while stack:
        e = stack.pop()
        out.append(e)
        stack.extend(e.context or [])
    return out


def pointer_of(error):
    return "/" + "/".join(str(p) for p in error.absolute_path)


def describe(error):
    return "%s at %s: %s" % (error.validator, pointer_of(error), error.message)


TSV_EXPECT = {
    "result-bundle.verdicts.four-columns.tsv": "expected 5 tab-separated fields",
    "result-bundle.verdicts.foreign-nonce.tsv": "does not match run_nonce",
}


def main():
    schemas, registry = load_schemas()
    print("schemas: %d, all valid Draft 2020-12" % len(schemas))
    ok = fail = 0

    def report(good, label, msg=""):
        nonlocal ok, fail
        if good:
            ok += 1
            print("  ok    %s%s" % (label, ("  " + msg) if msg else ""))
        else:
            fail += 1
            print("  FAIL  %s  %s" % (label, msg))

    prov_path = os.path.join(EX, "result-bundle.provenance.example.json")
    with open(prov_path) as f:
        prov = json.load(f)
    row_v = validator_for(schemas, registry, "result-bundle", "/$defs/verdict_row")

    print("valid examples (must pass):")
    covered = set()
    for path in sorted(glob.glob(os.path.join(EX, "*.*"))):
        if path.endswith(".tsv"):
            probs = check_tsv(path, row_v, prov["run_nonce"], prov["started_at"], prov["finished_at"])
            report(not probs, os.path.basename(path), "; ".join(probs))
            continue
        name, pointer = def_example(os.path.basename(path)) or (schema_for(path), None)
        v = validator_for(schemas, registry, name, pointer)
        if pointer is None:
            covered.add(name)
        for label, doc in docs_in(path):
            errs = errors(v, doc)
            report(not errs, label, "; ".join("%s: %s" % ("/".join(map(str, e.absolute_path)), e.message) for e in errs[:3]))

    print("invalid examples (must fail, for the named reason):")
    bad_covered = set()
    for path in sorted(glob.glob(os.path.join(BAD, "*.*"))):
        base = os.path.basename(path)
        if path.endswith(".tsv"):
            probs = check_tsv(path, row_v, prov["run_nonce"], prov["started_at"], prov["finished_at"])
            want = TSV_EXPECT.get(base)
            hit = bool(want) and any(want in p for p in probs)
            report(hit, base, "expected %r, got %s" % (want, probs) if not hit else "rejected: " + probs[0])
            continue
        name = schema_for(path)
        bad_covered.add(name)
        with open(path) as f:
            first = f.readline().strip()
        m = EXPECT_RX.match(first)
        if not m:
            report(False, base, "first line must be '# expect-error: <keyword> at <pointer> [~ <text>]'")
            continue
        keyword, where, text = m.group(1), m.group(2), m.group(3)
        v = validator_for(schemas, registry, *(def_example(base) or (name, None)))
        for label, doc in docs_in(path):
            errs = all_errors(v, doc)
            hits = [e for e in errs if e.validator == keyword and pointer_of(e) == where
                    and (text is None or text in e.message)]
            report(bool(hits), label, ("rejected: " + describe(hits[0])) if hits
                   else "expected %s at %s%s; got %s" % (
                       keyword, where, (" ~ %r" % text) if text else "",
                       [describe(e) for e in errs[:4]] or "NO ERROR (the schema cannot fail here)"))

    doc_schemas = set(schemas) - {"defs"}
    # $defs-only libraries (no top-level document) are covered by their $defs examples (DEF_PREFIXES)
    libraries = {"matrix"}
    missing_valid = sorted(doc_schemas - covered - libraries)
    missing_bad = sorted(doc_schemas - bad_covered)
    if missing_valid:
        report(False, "coverage", "no valid example for: " + ", ".join(missing_valid))
    if missing_bad:
        report(False, "coverage", "no invalid example for: " + ", ".join(missing_bad))
    print("%d ok, %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
