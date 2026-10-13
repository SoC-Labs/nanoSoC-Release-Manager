"""F6: fill in a defect's fixed-in from commit trailers, as a registry patch for a person.

    defect_trailers(target, *, ref, config, limit) -> (defect_trailers observation, patch)

A commit on the component's default branch (canonical host, so F3 can prove it) that carries

    Defect: SL-DEF-0001            the commit fixes that defect (every form, when the defect has exactly one;
    Defect: SL-DEF-0001 form A,B   else the forms it names)
    Fixes: 49a6c25                 the commit fixes the defect whose `origin` cites that commit

is a fix of those forms. Each (defect, form, commit) is `already` (in fixed_in), `proposed` (the patch adds it),
or `unresolved` (unknown defect, ambiguous form, a Fixes: sha no defect cites). The patch edits
`defects/SL-DEF-NNNN.yaml` in the registry checkout's text (comments kept) and is checked to still validate; the
registry is only read. Applying it is a person's act (registry main is protected, GATE2 Q12).
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from nanosoc_rm import contracts
from nanosoc_rm.versioning.repo import VersioningError, open_repo, registry_policy

__all__ = ["parse_trailers", "defect_trailers", "insert_fixed_in"]

_DEFECT = re.compile(r"^Defect:\s*(SL-DEF-\d{4})(?:\s+forms?\s+([A-Z](?:\s*,\s*[A-Z])*))?\s*$", re.I | re.M)
_FIXES = re.compile(r"^Fixes:\s*([0-9a-f]{7,40})\b", re.I | re.M)


def parse_trailers(body: str) -> dict:
    """{defects: [(id, [forms])], fixes: [sha prefixes]} from a commit message's trailer lines."""
    defects = [(m.group(1).upper(), [f.strip().upper() for f in (m.group(2) or "").split(",") if f.strip()])
               for m in _DEFECT.finditer(body)]
    fixes = [m.group(1).lower() for m in _FIXES.finditer(body)]
    return {"defects": defects, "fixes": fixes}


def _form_block(lines: list[str], form_id: str) -> tuple[int, int] | None:
    """(start, end) line indices of `- id: <form_id>` inside `forms:`."""
    in_forms, start = False, None
    for i, line in enumerate(lines):
        if re.match(r"^forms:\s*$", line):
            in_forms = True
            continue
        if in_forms and re.match(r"^\S", line):
            if start is not None:
                return start, i
            in_forms = False
        if not in_forms:
            continue
        m = re.match(r"^(\s*)-\s+id:\s*['\"]?([A-Z])['\"]?\s*$", line)
        if m:
            if start is not None:
                return start, i
            if m.group(2) == form_id:
                start = i
    return (start, len(lines)) if start is not None else None


def insert_fixed_in(text: str, form_id: str, component: str, commit: str, note: str) -> str | None:
    """The defect file text with {component, commit} appended to form `form_id`'s fixed_in, or None when the
    text has no place for it that keeps the YAML readable (the caller reports it unresolved)."""
    lines = text.splitlines(keepends=True)
    blk = _form_block(lines, form_id)
    if blk is None:
        return None
    s, e = blk
    item_indent = None
    for i in range(s, e):
        m = re.match(r"^(\s*)fixed_in:\s*(\[\s*\])?\s*(#.*)?$", lines[i])
        if not m:
            continue
        key_indent = len(m.group(1))
        entry = '{component: %s, commit: "%s"}  # %s\n' % (component, commit, note)
        if m.group(2) is not None:                                  # fixed_in: []
            lines[i] = " " * key_indent + "fixed_in:\n"
            lines.insert(i + 1, " " * (key_indent + 2) + "- " + entry)
            return "".join(lines)
        j = i + 1
        while j < e and (re.match(r"^\s*-\s", lines[j]) and len(lines[j]) - len(lines[j].lstrip()) > key_indent
                         or not lines[j].strip()):
            if item_indent is None and lines[j].strip():
                item_indent = len(lines[j]) - len(lines[j].lstrip())
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        lines.insert(j, " " * (item_indent if item_indent is not None else key_indent + 2) + "- " + entry)
        return "".join(lines)
    return None


def defect_trailers(target: str, *, ref: str | None = None, config=None, limit: int = 5000) -> tuple[dict, str]:
    """F6: (defect_trailers observation, registry patch). Reads the component's mirror and the registry only."""
    from nanosoc_rm.versioning import core
    view = open_repo(target, ref=ref, config=config)
    if view.registry is None:
        raise VersioningError("F6 needs the registry (its defects)")
    policy, psrc = registry_policy(view.config)
    defects = dict(view.registry._defects)                      # noqa: SLF001
    origin_index = {}
    for d in defects.values():
        for c in d.get("origin") or []:
            origin_index.setdefault(c["commit"], []).append(d["id"])
    rows: list[dict] = []
    edits: dict[str, list[tuple[str, str]]] = {}
    for c in view.log(None, view.commit, limit=limit):
        t = parse_trailers(c["body"])
        targets: list[tuple[str, list[str], str]] = [(d, f, "Defect: %s" % d) for d, f in t["defects"]]
        for sha in t["fixes"]:
            hit = sorted({did for full, ids in origin_index.items() if full.startswith(sha) for did in ids})
            if not hit:
                rows.append({"commit": c["sha"], "trailer": "Fixes: %s" % sha, "defect": None, "form": None,
                             "state": "unresolved", "why": "no defect's origin cites %s" % sha})
            targets += [(did, [], "Fixes: %s" % sha) for did in hit]
        for did, forms, trailer in targets:
            d = defects.get(did)
            if d is None:
                rows.append({"commit": c["sha"], "trailer": trailer, "defect": did, "form": None, "state": "unresolved",
                             "why": "no defects/%s.yaml in the registry" % did})
                continue
            all_forms = [f["id"] for f in d.get("forms") or []]
            if not forms:
                if len(all_forms) == 1:
                    forms = all_forms
                else:
                    rows.append({"commit": c["sha"], "trailer": trailer, "defect": did, "form": None,
                                 "state": "unresolved", "why": "%s has forms %s: name them (`Defect: %s form A`)"
                                 % (did, ", ".join(all_forms), did)})
                    continue
            for f in forms:
                form = next((x for x in d.get("forms") or [] if x["id"] == f), None)
                if form is None:
                    rows.append({"commit": c["sha"], "trailer": trailer, "defect": did, "form": f, "state": "unresolved",
                                 "why": "%s has no form %s" % (did, f)})
                    continue
                if any(x["commit"] == c["sha"] for x in form.get("fixed_in") or []):
                    rows.append({"commit": c["sha"], "trailer": trailer, "defect": did, "form": f, "state": "already",
                                 "why": None})
                    continue
                rows.append({"commit": c["sha"], "trailer": trailer, "defect": did, "form": f, "state": "proposed",
                             "why": None})
                edits.setdefault(did, []).append((f, c["sha"]))
    patch = _patch(view, edits, rows)
    fact = {"target": view.target, "commit": view.commit, "ref": view.ref, "worktree": view.worktree,
            "rows": rows[:500], "proposed": sum(1 for r in rows if r["state"] == "proposed"),
            "already": sum(1 for r in rows if r["state"] == "already"),
            "unresolved": sum(1 for r in rows if r["state"] == "unresolved"), "patch_lines": patch.count("\n")}
    obs = core.observe("defect_trailers", fact, view.sources + psrc, "nanosoc-rm version fixed-in %s" % target)
    return obs, patch


def _patch(view, edits: dict[str, list[tuple[str, str]]], rows: list[dict]) -> str:
    reg = Path(view.registry.path)
    out = []
    for did, items in sorted(edits.items()):
        rel = "defects/%s.yaml" % did
        old = (reg / rel).read_text()
        new = old
        for form, sha in items:
            n = insert_fixed_in(new, form, view.target, sha, "F6: trailer of %s" % sha[:12])
            if n is None:
                for r in rows:
                    if r["defect"] == did and r["form"] == form and r["commit"] == sha:
                        r.update(state="unresolved", why="could not place fixed_in in %s's text" % rel)
                continue
            new = n
        doc = contracts.load_yaml(new)
        if contracts.validate("defect", doc):
            for r in rows:
                if r["defect"] == did and r["state"] == "proposed":
                    r.update(state="unresolved", why="the edit would break the defect contract")
            continue
        body = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                         "a/" + rel, "b/" + rel))
        if body:
            out.append("diff --git a/%s b/%s\n" % (rel, rel) + "".join(body))
    return "".join(out)
