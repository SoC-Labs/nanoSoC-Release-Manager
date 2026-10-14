"""Catalogue-change preview: GUI choices -> the catalogue.yaml patch a person applies (interfaces.md section 23.3).

    preview(registry, choices) -> Preview(patch, changes, problems, catalogue)
    PreviewUsage                     a choice that cannot be read (unknown op or id, bad field): CLI exit 3

The tool never writes the registry. Like the review basket, it hands back a unified diff (`a/` and `b/`
prefixes, paths relative to the registry repo's top, so `git apply` works there) that a person reviews and
applies by merge request. The diff edits catalogue.yaml in place, line by line: one component entry is one
flow-mapping line (`- {"id": ..., ...}  # comment`), and the edit rewrites only that line's mapping, keeping its
comment. An entry written over several lines cannot be edited this way and is reported in `problems`.

Choices (a list, applied in order):

    {"op": "set", "id": ID, "fields": {FIELD: VALUE | null, ...}}
        FIELD in group, type, role, wraps, question. null removes the field. Setting a field confirms it (it
        leaves `guessed`; `question` goes when `group` is no longer guessed). Setting group to soc or subsystem
        fills `type` to match; a group outside the ladder drops type and role, and outside project/soc, wraps.
    {"op": "confirm", "id": ID, "fields": [FIELD, ...]}
        FIELD in group, type, role, wraps, inside: take it out of `guessed`. A field the entry does not state
        (derived from class) is written out with its derived value, so the confirmation is recorded.
    {"op": "add", "repo": {"host": H, "path": P}, "group": G, "id": ID?, "type": T?, "role": R?, "wraps": [..]?}
        A new component from the new-repo queue: class from the group (model.class_for), id from the repo name
        when not given, appended after the last component line.
    {"op": "ignore", "repo": {"host": H, "path": P}, "reason": TEXT}
        Not nanoSoC: appended to `ignored` with its reason.

`problems` is what the registry loader would say about the patched catalogue (its schema and the cross-file rules,
against the registry's other files as loaded): an empty list means the patch, once applied, loads.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nanosoc_rm import contracts
from nanosoc_rm.catalogue.model import (GROUPS, GUESSABLE, LADDER_GROUPS, ROLES, TYPES, characterise, class_for)

__all__ = ["preview", "Preview", "PreviewUsage", "SETTABLE", "entry_line"]

SETTABLE = ("group", "type", "role", "wraps", "question")
_CHAR_ORDER = ("group", "type", "role", "wraps", "guessed", "question")
_LINE = re.compile(r"^(?P<lead>\s*-\s+)(?P<rest>\{.*)$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class PreviewUsage(ValueError):
    """A choice the preview cannot read. Nothing is produced."""


@dataclass
class Preview:
    patch: str                                       # unified diff of catalogue.yaml ("" when nothing changes)
    changes: list[str] = field(default_factory=list)  # one line per applied choice, for the page
    problems: list[str] = field(default_factory=list)  # the loader's problems with the patched catalogue
    catalogue: str = ""                              # the patched catalogue.yaml text

    @property
    def valid(self) -> bool:
        return not self.problems

    def as_doc(self) -> dict:
        return {"patch": self.patch, "changes": self.changes, "problems": self.problems, "valid": self.valid}


def _split_mapping(rest: str) -> tuple[str, str] | None:
    """(the `{...}` flow mapping, what follows it) from the text after `- `, honouring quotes; None if unbalanced."""
    depth, quote, esc = 0, None, False
    for i, ch in enumerate(rest):
        if quote:
            if esc:
                esc = False
            elif ch == "\\" and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return rest[:i + 1], rest[i + 1:]
    return None


def entry_line(lines: list[str], cid: str) -> tuple[int, str, dict, str] | None:
    """(index, lead, mapping, tail) of the one-line flow entry whose id is `cid`, or None."""
    for n, line in enumerate(lines):
        m = _LINE.match(line)
        if not m or cid not in line:
            continue
        split = _split_mapping(m.group("rest"))
        if split is None:
            continue
        try:
            doc = contracts.load_yaml(split[0])
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("id") == cid and "class" in doc:
            return n, m.group("lead"), doc, split[1]
    return None


def _render(doc: dict) -> str:
    """The entry as one JSON flow mapping, characterisation fields right after `class`."""
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if k in _CHAR_ORDER:
            continue
        out[k] = v
        if k == "class":
            for c in _CHAR_ORDER:
                if c in doc:
                    out[c] = doc[c]
    if "class" not in doc:
        for c in _CHAR_ORDER:
            if c in doc:
                out[c] = doc[c]
    return json.dumps(out, ensure_ascii=False)


def _drop_guess(doc: dict, *names: str) -> None:
    g = [x for x in doc.get("guessed") or [] if x not in names]
    if g:
        doc["guessed"] = g
    else:
        doc.pop("guessed", None)
    if "group" in names or "guessed" not in doc:
        doc.pop("question", None)


def _normalise(doc: dict) -> None:
    """Keep the entry consistent after a group change (module docstring, op set)."""
    group = doc.get("group")
    if group in ("soc", "subsystem"):
        doc["type"] = group
    if group not in LADDER_GROUPS:
        doc.pop("type", None)
        doc.pop("role", None)
        _drop_guess(doc, "type", "role", "inside")
    if group not in ("project", "soc"):
        doc.pop("wraps", None)
        _drop_guess(doc, "wraps")


def _check_value(fname: str, value: Any) -> None:
    ok = {"group": lambda v: v in GROUPS, "type": lambda v: v in TYPES, "role": lambda v: v in ROLES,
          "wraps": lambda v: isinstance(v, list) and v and all(isinstance(x, str) and _ID.match(x) for x in v),
          "question": lambda v: isinstance(v, str) and v.strip() != ""}[fname]
    if value is not None and not ok(value):
        raise PreviewUsage("%s %r is not a valid value" % (fname, value))


def _repo(choice: dict) -> dict:
    r = choice.get("repo")
    if not (isinstance(r, dict) and r.get("host") in ("github", "gitlab") and isinstance(r.get("path"), str)
            and r["path"].strip("/") and "\n" not in r["path"]):
        raise PreviewUsage("op %s needs repo {host: github|gitlab, path}" % choice.get("op"))
    return {"host": r["host"], "path": r["path"].strip("/")}


def _new_id(path: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", path.rsplit("/", 1)[-1].lower()).strip("-.") or "new-repo"


def _last_line(lines: list[str], section: str) -> int | None:
    """Index of the last list item line of a top-level `section:` (flow-style items), or None."""
    start = next((n for n, ln in enumerate(lines) if re.match(r"^%s:(\s|$)" % re.escape(section), ln)), None)
    if start is None:
        return None
    last = None
    for n in range(start + 1, len(lines)):
        ln = lines[n]
        if re.match(r"^\S", ln) and not ln.startswith("#") and not ln.startswith("-"):
            break
        if _LINE.match(ln):
            last = n
    return last


def _apply(lines: list[str], choices: list[dict], ids: set[str]) -> tuple[list[str], list[str], list[str]]:
    changes, problems = [], []
    for k, ch in enumerate(choices):
        if not isinstance(ch, dict):
            raise PreviewUsage("choice %d is not a mapping" % k)
        op = ch.get("op")
        if op in ("set", "confirm"):
            cid = ch.get("id")
            if cid not in ids:
                raise PreviewUsage("choice %d: %r is not a catalogue component" % (k, cid))
            hit = entry_line(lines, cid)
            if hit is None:
                problems.append("catalogue.yaml: %s is not a one-line entry, so the preview cannot edit it; edit it "
                                "by hand" % cid)
                continue
            n, lead, doc, tail = hit
            if op == "set":
                fields = ch.get("fields")
                if not isinstance(fields, dict) or not fields or any(f not in SETTABLE for f in fields):
                    raise PreviewUsage("choice %d: fields must map some of %s to values" % (k, ", ".join(SETTABLE)))
                derived = characterise(doc)
                if "group" not in doc and ("type" in fields or "role" in fields):
                    doc["group"] = derived.group             # a type or role needs its group written out
                for fname, value in fields.items():
                    _check_value(fname, value)
                    if value is None:
                        doc.pop(fname, None)
                    else:
                        doc[fname] = value
                    if fname != "question":
                        _drop_guess(doc, fname)
                if "group" in fields:
                    _normalise(doc)
                    if doc.get("group") in LADDER_GROUPS and "role" not in doc and derived.role:
                        doc["role"] = derived.role
                changes.append("%s: set %s" % (cid, ", ".join("%s=%s" % (f, json.dumps(v)) for f, v in fields.items())))
            else:
                names = ch.get("fields")
                if not isinstance(names, list) or not names or any(f not in GUESSABLE for f in names):
                    raise PreviewUsage("choice %d: fields must list some of %s" % (k, ", ".join(GUESSABLE)))
                derived = characterise(doc)
                for fname in names:
                    if fname in ("group", "type", "role") and fname not in doc and getattr(derived, fname):
                        doc[fname] = getattr(derived, fname)
                if any(f in doc for f in ("type", "role")) and "group" not in doc:
                    doc["group"] = derived.group
                _drop_guess(doc, *names)
                changes.append("%s: confirm %s" % (cid, ", ".join(names)))
            lines[n] = lead + _render(doc) + tail
        elif op == "add":
            repo = _repo(ch)
            group = ch.get("group")
            if group not in GROUPS:
                raise PreviewUsage("choice %d: group %r is not one of %s" % (k, group, ", ".join(GROUPS)))
            cid = ch.get("id") or _new_id(repo["path"])
            if not isinstance(cid, str) or not _ID.match(cid):
                raise PreviewUsage("choice %d: %r is not a component id" % (k, cid))
            if cid in ids:
                raise PreviewUsage("choice %d: id %s is already in the catalogue" % (k, cid))
            for fname in ("type", "role", "wraps"):
                _check_value(fname, ch.get(fname))
            doc: dict[str, Any] = {"id": cid, "class": class_for(group, ch.get("role")), "group": group}
            for fname in ("type", "role", "wraps"):
                if ch.get(fname) is not None:
                    doc[fname] = ch[fname]
            _normalise(doc)
            doc["canonical"] = repo
            at = _last_line(lines, "components")
            if at is None:
                raise PreviewUsage("catalogue.yaml has no flow-style components list to append to")
            lead = _LINE.match(lines[at]).group("lead")
            lines.insert(at + 1, lead + _render(doc) + "  # added from the new-repo queue")
            ids.add(cid)
            changes.append("%s: add %s:%s as %s (class %s)" % (cid, repo["host"], repo["path"], group, doc["class"]))
        elif op == "ignore":
            repo = _repo(ch)
            reason = ch.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise PreviewUsage("choice %d: ignore needs a reason" % k)
            item = json.dumps({"repo": repo, "reason": " ".join(reason.split())}, ensure_ascii=False)
            at = _last_line(lines, "ignored")
            if at is None:
                lines += ["ignored:", "  - " + item]
            else:
                lines.insert(at + 1, _LINE.match(lines[at]).group("lead") + item)
            changes.append("ignore %s:%s (%s)" % (repo["host"], repo["path"], " ".join(reason.split())))
        else:
            raise PreviewUsage("choice %d: op %r is not set, confirm, add or ignore" % (k, op))
    return lines, changes, problems


def _problems(registry, text: str) -> list[str]:
    """What the loader would say about the registry with this catalogue text (schema, then rules 1 to 15)."""
    from nanosoc_rm.registry import rules
    from nanosoc_rm.registry.loader import _pointer
    try:
        doc = contracts.load_yaml(text)
    except yaml.YAMLError as e:
        return ["catalogue.yaml: not valid YAML: %s" % " ".join(str(e).split())]
    out = ["catalogue.yaml:%s: %s [schema]" % (_pointer(e.absolute_path), e.message)
           for e in sorted(contracts.validator("component-catalogue").iter_errors(doc),
                           key=lambda e: list(map(str, e.absolute_path)))]
    files = registry.files()
    docs = rules.Docs(catalogue=("catalogue.yaml", doc), eligibility=("eligibility.yaml", files["eligibility.yaml"]))
    if "imports.yaml" in files:
        docs.imports = ("imports.yaml", files["imports.yaml"])
    if "tiers.yaml" in files:
        docs.tiers = ("tiers.yaml", files["tiers.yaml"])
    docs.defects = [(f, d) for f, d in sorted(files.items()) if f.startswith("defects/")]
    docs.errata = [(f, d) for f, d in sorted(files.items()) if f.startswith("errata/")]
    out += ["%s:%s: %s [%s]" % (f, ptr, msg, rule) for f, ptr, rule, msg in rules.check_all(docs)]
    return out


def preview(registry: Any, choices: list[dict]) -> Preview:
    """The patch for `choices` against `registry` (a loaded Registry, or a path that loads). RegistryError when
    the registry itself does not load (nothing to patch against); PreviewUsage for a bad choice."""
    from nanosoc_rm.quality.patch import file_patch
    from nanosoc_rm.registry import load_registry
    if not hasattr(registry, "files"):
        registry = load_registry(Path(registry))
    if not isinstance(choices, list):
        raise PreviewUsage("choices must be a list")
    text = (Path(registry.path) / "catalogue.yaml").read_text(encoding="utf-8")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    ids = {c["id"] for c in registry.components()}
    new_lines, changes, problems = _apply(list(lines), choices, ids)
    new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    patch = file_patch(registry.prefix + "catalogue.yaml", text, new_lines) if new_lines != lines else ""
    problems += _problems(registry, new_text)
    return Preview(patch, changes, problems, new_text)
