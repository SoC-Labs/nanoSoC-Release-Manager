"""The gate-2 contracts at run time: the packaged schemas, validate(), the strict YAML reader, WRITE_CALLS.

The schemas in the repo's `schemas/` directory are the single source (PR3). A wheel carries them inside this
package (`contracts/schemas/`, pyproject force-include); an editable install reads `schemas/` in place. Either
way they are loaded here and nowhere else, so a module never opens a schema file itself.

    validate("eligibility", doc)                          -> list of problems, [] = valid
    validate("result-bundle", row, "/$defs/verdict_row")  -> one $defs entry
    validate_observation(doc)                             -> observation (+ pin-state for kind pin_state)

Problems read `<instance path>: <message>` (`<root>` for the document itself), as the acceptance tests'
own validate() prints them.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Iterator

import yaml
from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

__all__ = ["SchemaError", "schemas_dir", "load_schemas", "schema_names", "validator", "validate", "require_valid",
           "validate_observation", "validate_gate_item", "validate_verdict_row", "validate_rm_request",
           "validate_provenance", "validate_detector_run", "validate_t0_waiver", "parse_verdict_row",
           "VERDICT_COLUMNS", "load_yaml", "docs_in", "WRITE_CALLS", "WRITE_EFFECTS"]

_PACKAGED = Path(__file__).resolve().parent / "schemas"
_IN_REPO = Path(__file__).resolve().parents[3] / "schemas"      # src/nanosoc_rm/contracts -> repo root


class SchemaError(ValueError):
    """A document does not satisfy its contract. `problems` lists each breach."""

    def __init__(self, schema_name: str, problems: list[str]):
        self.schema_name = schema_name
        self.problems = list(problems)
        super().__init__("%s: %s" % (schema_name, "; ".join(self.problems)))


def schemas_dir() -> Path:
    """Where the schemas are: inside the installed package (wheel), else the repo's schemas/ (editable)."""
    for d in (_PACKAGED, _IN_REPO):
        if (d / "defs.schema.json").is_file():
            return d
    raise FileNotFoundError("contract schemas not found in %s or %s" % (_PACKAGED, _IN_REPO))


def ecma_anchors(pattern: str) -> str:
    """`pattern` with every `$` anchor (outside a character class, unescaped) written as `\\Z`. JSON Schema
    patterns are ECMA-262, where `$` matches only at the end of the string; Python's `$` also matches before a
    final newline, so `^[0-9]+$` would accept "1\\n" (ROB-coverage-5)."""
    out, i, in_class = [], 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "^":
                out.append("[^")
                i += 2
                if pattern[i:i + 1] == "]":
                    out.append("]")
                    i += 1
                continue
            if pattern[i + 1:i + 2] == "]":
                out.append("[]")
                i += 2
                continue
        elif c == "$":
            c = r"\Z"
        out.append(c)
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=None)
def _ecma(pattern: str) -> re.Pattern:
    return re.compile(ecma_anchors(pattern))


def _pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not _ecma(pattern).search(instance):
        yield ValidationError("%r does not match %r" % (instance, pattern))


# Draft 2020-12 with the `pattern` keyword read with ECMA-262 anchors (see ecma_anchors).
_Validator = validators.extend(Draft202012Validator, {"pattern": _pattern})


@functools.lru_cache(maxsize=1)
def load_schemas() -> tuple[dict[str, dict], Registry]:
    """(name -> schema, a referencing Registry of every schema). Each is checked to be valid Draft 2020-12.
    Cached: the schemas are package data and do not change while the process runs."""
    schemas, resources = {}, []
    for path in sorted(schemas_dir().glob("*.schema.json")):
        doc = json.loads(path.read_text())
        Draft202012Validator.check_schema(doc)
        name = path.name[: -len(".schema.json")]
        schemas[name] = doc
        resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    if not schemas:
        raise FileNotFoundError("no *.schema.json in %s" % schemas_dir())
    return schemas, Registry().with_resources(resources)


def schema_names() -> list[str]:
    return sorted(load_schemas()[0])


@functools.lru_cache(maxsize=None)
def validator(schema_name: str, pointer: str | None = None) -> Draft202012Validator:
    """A validator for one schema, or for one of its $defs entries (`pointer`, e.g. "/$defs/gate_item")."""
    schemas, registry = load_schemas()
    if schema_name not in schemas:
        raise KeyError("unknown schema %r; known: %s" % (schema_name, ", ".join(sorted(schemas))))
    schema = schemas[schema_name]
    if pointer:
        schema = {"$ref": schema["$id"] + "#" + pointer}
    return _Validator(schema, registry=registry)


def validate(schema_name: str, doc: Any, pointer: str | None = None) -> list[str]:
    """Every breach of `doc` against the schema (or its $defs entry at `pointer`); [] means valid."""
    errs = sorted(validator(schema_name, pointer).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    return ["%s: %s" % ("/".join(map(str, e.absolute_path)) or "<root>", e.message) for e in errs]


def require_valid(schema_name: str, doc: Any, pointer: str | None = None) -> Any:
    """`doc` itself if valid; SchemaError otherwise."""
    problems = validate(schema_name, doc, pointer)
    if problems:
        raise SchemaError(schema_name + (pointer or ""), problems)
    return doc


def validate_observation(doc: Any) -> list[str]:
    """An observation envelope and its fact. A pin_state also meets pin-state.schema.json (stricter subject)."""
    problems = validate("observation", doc)
    if isinstance(doc, dict) and doc.get("kind") == "pin_state":
        problems += ["pin-state: " + p for p in validate("pin-state", doc)]
    return problems


def validate_gate_item(doc: Any) -> list[str]:
    """One line of rm-bundle/items/<gate>.jsonl."""
    return validate("result-bundle", doc, "/$defs/gate_item")


def validate_verdict_row(row: Any) -> list[str]:
    """One verdicts.tsv row as a dict {utc, status, gate, detail, nonce} (see parse_verdict_row)."""
    return validate("result-bundle", row, "/$defs/verdict_row")


def validate_rm_request(doc: Any) -> list[str]:
    """The RM_REQUEST pipeline variable, parsed."""
    return validate("result-bundle", doc, "/$defs/rm_request")


def validate_provenance(doc: Any) -> list[str]:
    """rm-bundle/provenance.json."""
    return validate("result-bundle", doc)


def validate_detector_run(doc: Any) -> list[str]:
    """An erratum's exposure.run (interfaces.md section 6a)."""
    return validate("erratum", doc, "/$defs/detector_run")


def validate_t0_waiver(doc: Any) -> list[str]:
    """One entry of a configuration's t0_waivers (GATE2 Q1)."""
    return validate("eligibility", doc, "/$defs/t0_waiver")


# verdicts.tsv: columns 1 to 4 are the toolkit's `_ci_record` output; column 5 is the run nonce (README §2).
VERDICT_COLUMNS = ("utc", "status", "gate", "detail", "nonce")


def parse_verdict_row(line: str) -> dict[str, str]:
    """One verdicts.tsv line (without its newline) as {utc, status, gate, detail, nonce}.
    ValueError unless it has exactly 5 tab-separated fields. Validity of the fields: validate_verdict_row."""
    fields = line.rstrip("\n").split("\t")
    if len(fields) != len(VERDICT_COLUMNS):
        raise ValueError("expected %d tab-separated fields, got %d" % (len(VERDICT_COLUMNS), len(fields)))
    return dict(zip(VERDICT_COLUMNS, fields))


# ---------------------------------------------------------------------------------------------------------
# YAML exactly as the contracts read it (README section 3 rule 8): timestamps stay strings, and a duplicate
# key is an error, never a silent overwrite.
# ---------------------------------------------------------------------------------------------------------
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without the timestamp resolver, rejecting duplicate keys."""


_StrictLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _no_dup_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:                                   # a list or mapping key: YAMLError (ROB-property-11)
            raise yaml.constructor.ConstructorError(None, None, "a mapping key must be a scalar, not %s"
                                                    % type(key).__name__, key_node.start_mark) from None
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, "duplicate key %r" % (key,), key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def load_yaml(text: str) -> Any:
    """One YAML document, strictly (no timestamp conversion, duplicate keys refused: yaml.YAMLError)."""
    return yaml.load(text, Loader=_StrictLoader)


def docs_in(path: str | Path) -> Iterator[tuple[str, Any]]:
    """(label, document) for each document in a .json, .jsonl or .yaml/.yml file (as check_examples.py)."""
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".jsonl":
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                yield "%s:%d" % (path.name, n), json.loads(line)
    elif path.suffix == ".json":
        yield path.name, json.loads(text)
    else:
        yield path.name, load_yaml(text)


# ---------------------------------------------------------------------------------------------------------
# WRITE_CALLS (interfaces.md section 17.5; A2, A6). Every call site a static census of this package could
# mistake for an outbound write must be covered by one entry: same file (path as the census prints it,
# relative to the directory holding the package), kind and method. AT-A2 and AT-A6 hold the package to it.
#
#   write       exactly one: POST projects/:id/trigger/pipeline, guarded to the configured CI project
#   read-post   the OAuth code exchange login/oauth/access_token; the Messages API v1/messages (CR-14, Q4 rewrite);
#               the artefact store's AQL search api/search/aql (Wave E, section 22)
#   local       a call the census mistakes for HTTP, only in a file that imports no HTTP library (none yet)
#   git-runner  at most one: nanosoc_rm.gitrun, which refuses push, send-pack and receive-pack at run time
#   publish     CR-ART-publish (David 2026-10-14): the release store's PUT and POST, only in
#               nanosoc_rm/publish/client.py, allow-listed there before any I/O (new files under
#               <release_repo>/<project>/<version>/, a build-info record, a status-only promotion), refused in the
#               web process, run only by a person's confirmed publish; never scheduled (A7: soc_release)
#   executor    CR-WG-executor (David 2026-10-16): the pull agent's POSTs to the release manager's OWN executor API
#               (claim, heartbeat, log, result), only in nanosoc_rm/executor/client.py, whose check_path allows the
#               four /executor/v1/ paths of the configured server before any I/O; it writes only the service's own
#               executor queue, never a git host, CI or store
#
# `file` names where the contract layout (interfaces.md sections 0.5, 10, 14) puts the call. A builder who
# puts the call in a different file moves the entry with it, in the same commit.
# ---------------------------------------------------------------------------------------------------------
WRITE_EFFECTS = ("write", "read-post", "local", "git-runner", "publish", "executor")

WRITE_CALLS: list[dict[str, str]] = [
    {"file": "nanosoc_rm/executor/client.py", "kind": "http", "method": "POST", "target": "/executor/v1/",
     "effect": "executor",
     "guard": "client.check_path allows only /executor/v1/claim and /executor/v1/tasks/<id>/{heartbeat,log,result} "
              "on the agent config's [agent] server, before any I/O; bearer token from the 0600 token_file only, in "
              "a header; no redirects followed; TLS verified",
     "reason": "CR-WG-executor (decision 1): the pull agent on the EDA host claims a queued tier task, heartbeats, and "
               "posts the log and result back to the release manager's own queue; nothing outside the service"},
    {"file": "nanosoc_rm/executor/sandbox.py", "kind": "exec", "method": "?", "target": "unresolved argv",
     "effect": "local",
     "guard": "run() starts only the claimed task's command (checked by task.check_task: no set_env.sh) inside "
              "bubblewrap (no home, no ssh, read-only tools) or the scrubbed-environment fallback, with a time and "
              "output limit; the probe is a fixed bwrap argv; the file imports no HTTP client",
     "reason": "CR-WG-executor: run a capability's command on the EDA host; it writes only inside the task's work dir"},
    {"file": "nanosoc_rm/ci/client.py", "kind": "http", "method": "POST", "target": "trigger/pipeline",
     "effect": "write",
     "guard": "target is the configured ci.project only; the trigger token is scoped to that project",
     "reason": "Run T0: the one outbound write of v1 (A2, A4; interfaces.md section 10 Trigger step 3)"},
    {"file": "nanosoc_rm/auth.py", "kind": "http", "method": "POST", "target": "login/oauth/access_token",
     "effect": "read-post",
     "reason": "GitHub OAuth code exchange: a POST that writes nothing (H2; interfaces.md section 14)"},
    {"file": "nanosoc_rm/quality/rewrite.py", "kind": "http", "method": "POST", "target": "v1/messages",
     "effect": "read-post",
     "guard": "made only when an Anthropic API key is configured ($ANTHROPIC_API_KEY or [quality] api_key_file) and "
              "a person runs `nanosoc-rm quality docs --rewrite`; the client is built after the key check, so without "
              "a key no connection is opened; the reply becomes a patch a person applies, never an edit",
     "reason": "CR-14 (Q4): the Messages API call that proposes a shorter rewrite of a doc section; it writes nothing"},
    {"file": "nanosoc_rm/store/client.py", "kind": "http", "method": "POST", "target": "api/search/aql",
     "effect": "read-post",
     "guard": "store.client.check_request admits exactly one POST, api/search/aql, before any request is made; the "
              "client sends a Bearer token only (never Basic, never a netrc), follows no redirect and never calls "
              "the Access or security APIs",
     "reason": "Wave E (section 22): AQL is Artifactory's only bulk read of files with their properties; the query "
               "is `items.find(...)`, which reads and writes nothing"},
    {"file": "nanosoc_rm/gitrun.py", "kind": "exec", "method": "?", "target": "git with unresolved subcommand",
     "effect": "git-runner",
     "guard": "check_argv() refuses push, send-pack, receive-pack and every subcommand or form not on its "
              "read/local allowlist, before git is started",
     "reason": "the one git runner: every git call of the package goes through nanosoc_rm.gitrun.run_git"},
    {"file": "nanosoc_rm/analysis/tierrun.py", "kind": "exec", "method": "?", "target": "unresolved argv",
     "effect": "local",
     "guard": "only `nanosoc-rm compat --tier` on a person's own machine runs it (the service and the CI runner never "
              "import tierrun); the command is the registry tiers.yaml entry (protected main), run in a sandbox "
              "checkout with a clean environment that carries no token, HOME or ssh agent of the caller",
     "reason": "CR-12: run a configuration's local T1/T2 command (lint, smoke sim); it writes only inside the "
               "sandbox work directory"},
    {"file": "nanosoc_rm/jobs.py", "kind": "exec", "method": "?", "target": "unresolved argv",
     "effect": "local",
     "guard": "the argv is always this Python running nanosoc_rm.cli with a `compat`, `mirror sync` or `walk` command "
              "built by jobs.cli_args from checked parameters (jobs._params); the child is the same read-only CLI a "
              "person runs, under the same git runner and WRITE_CALLS",
     "reason": "CR-17: the service's local job worker runs `nanosoc-rm compat`, `mirror sync` and `walk` as a child "
               "process, so a cancel can stop the whole process tree and the service never imports the tier runner"},
    {"file": "nanosoc_rm/publish/client.py", "kind": "http", "method": "PUT",
     "target": "<release_repo>/<project>/<version>/<file> and api/build",
     "effect": "publish",
     "guard": "Store.check() allows a PUT only to a new file directly under [stores.artifactory] release_repo/<project>/"
              "<version>/ (never a checksum name) or to api/build, before any network I/O; the service HEADs every "
              "target first and refuses a path that holds other bytes (never overwrite); refused in the web process "
              "(refuse_writes_here); run only by `publish-release`/`register-tapeout --confirm SHA256` after a "
              "preview, from the CLI or a job a listed publisher queued; Bearer token from [stores.artifactory] "
              "token_file only",
     "reason": "CR-ART-publish (G8, D12; David 2026-10-14): upload a signed release manifest or tapeout record, the "
               "documents beside it, and its build-info record to the release store"},
    {"file": "nanosoc_rm/publish/client.py", "kind": "http", "method": "POST",
     "target": "api/build/promote/<name>/<number>",
     "effect": "publish",
     "guard": "Store.check() allows a POST only to api/build/promote/<name>/<number>, and promote() refuses a body "
              "with a target repo, copy or artifacts: a status-only promotion (RELEASED, TAPED-OUT); same run "
              "conditions as the PUT",
     "reason": "CR-ART-publish: mark the published release or tapeout in the store's own Builds view"},
    {"file": "nanosoc_rm/publish/signing.py", "kind": "exec", "method": "?", "target": "unresolved argv",
     "effect": "local",
     "guard": "_run() starts only `<[stores.artifactory] ssh_keygen> -Y sign|verify` (refused otherwise), with a "
              "minimal environment, no stdin but the bytes to verify, a 60 s limit; the file imports no HTTP client",
     "reason": "CR-ART-publish: sign and verify a manifest with ssh-keygen -Y (OpenSSH 8.1+); it writes only a "
               "temporary file"},
]
