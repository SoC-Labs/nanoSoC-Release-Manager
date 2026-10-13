"""The component manifest (C11, nanosoc-component.yaml), API.md (C12) and the 1.0.0 readiness check (C9).

    status(view) -> {present, valid, version, problems, reexports}            the manifest at the head
    propose(view, *, version=None) -> (manifest dict, text, placeholders)
    patch(text, existing) -> unified diff that adds (or replaces) nanosoc-component.yaml, for a person
    readiness(view, plan) -> {applies, ready, items: [{item, ok, detail}]}   C9: what a 1.0.0 tag needs

The proposal is built from the registry catalogue (id, class, canonical URL), the tree's .gitmodules and gitlinks
(requires, each with a Cargo range from the pinned commit's release tag when it has one) and the version baseline.
What the tool cannot know (variants' file-list commands, test entry points, provides, re-exports) is a TODO
placeholder that still validates, listed in `placeholders`. It is written as a patch; the tool never commits it.
"""
from __future__ import annotations

import difflib
import json

from nanosoc_rm import contracts
from nanosoc_rm.versioning import semver

__all__ = ["FILE", "API_FILE", "status", "propose", "patch", "readiness", "range_for"]

FILE = "nanosoc-component.yaml"
API_FILE = "API.md"
TODO = "TODO"


def status(view) -> dict:
    """The manifest at the head: present, valid against component-manifest, its version and re-exports."""
    text = view.head_text(FILE)
    if text is None:
        return {"present": False, "valid": None, "version": None, "problems": [], "reexports": []}
    try:
        doc = contracts.load_yaml(text)
    except Exception as e:                                         # noqa: BLE001 - any YAML error is a problem
        return {"present": True, "valid": False, "version": None, "problems": ["not YAML: %s" % e], "reexports": []}
    problems = contracts.validate("component-manifest", doc)
    if isinstance(doc, dict) and view.component is not None and doc.get("id") not in (None, view.component.id):
        problems.append("id %r is not the catalogue id %r" % (doc.get("id"), view.component.id))
    if isinstance(doc, dict) and view.comp_class and doc.get("class") not in (None, view.comp_class):
        problems.append("class %r is not the catalogue class %r" % (doc.get("class"), view.comp_class))
    return {"present": True, "valid": not problems, "version": (doc or {}).get("version") if isinstance(doc, dict) else None,
            "problems": problems[:20], "reexports": list((doc or {}).get("reexports") or []) if isinstance(doc, dict) else []}


def range_for(tag: str | None) -> str | None:
    """The Cargo range a requirement pinned at release tag `tag` gets: ^X.Y (^0.Y below 1.0), or None."""
    v = semver.parse_tag(tag) if tag else None
    if v is None or v.pre is not None:
        return None
    return "^%d.%d" % (v.major, v.minor) if v.major == 0 else "^%d" % v.major


def _https(view) -> str:
    if view.base_url:
        return view.base_url + ".git"
    return "https://%s.invalid/%s.git" % (TODO.lower(), view.target)


def _tags_at(view, component_id: str, commit: str) -> list[str]:
    """Release tags of `component_id` (from its own mirror) that point at `commit`."""
    from nanosoc_rm.analysis import _inputs as inp
    from nanosoc_rm.versioning import tags as tagmod
    if view.config is None or view.registry is None:
        return []
    try:
        comp = view.registry.component(component_id)
    except Exception:                                              # noqa: BLE001
        return []
    mirror = inp.mirror_path(view.config, comp.id)
    if not (mirror / "HEAD").is_file() or comp.canonical is None:
        return []
    found = tagmod.read_tags(mirror, inp.ref_prefix(comp.canonical), view.sources)
    return sorted(t for t, i in found.items() if i["commit"] == commit and semver.is_release_tag(t))


def propose(view, *, version: str | None = None) -> tuple[dict, str, list[str]]:
    """(manifest, YAML text, placeholder JSON pointers). The manifest validates against component-manifest."""
    from nanosoc_rm.quality.source import parse_gitmodules
    from nanosoc_rm.versioning.repo import component_for_url
    placeholders: list[str] = []
    comp_id = view.component.id if view.component is not None else view.target
    gm_text = view.head_text(".gitmodules") or ""
    names = parse_gitmodules(gm_text)
    urls = {}
    cur = None
    for line in gm_text.splitlines():
        s = line.strip()
        if s.startswith("[submodule"):
            cur = s.split('"')[1] if '"' in s else None
        elif s.startswith("url") and "=" in s and cur:
            urls[cur] = s.split("=", 1)[1].strip()
    links = view.gitlinks(view.commit) if view.commit else {}
    requires, comments = [], {}
    for name, path in sorted(names.items(), key=lambda x: x[1]):
        if path not in links:
            continue
        cid = component_for_url(urls.get(name, ""), view.config, view.registry, view.canonical)
        pinned = links[path]
        tags = _tags_at(view, cid, pinned) if cid else []
        rng = range_for(tags[-1]) if tags else None
        i = len(requires)
        requires.append({"id": cid or (TODO.lower() + "-" + path.replace("/", "-").lower().strip("-"))[:60],
                         "path": path, "range": rng or ">=0.0.0"})
        if cid is None:
            placeholders.append("/requires/%d/id" % i)
            comments[path] = "TODO: %s is not in the catalogue (B9)" % urls.get(name, "?")
        elif rng is None:
            placeholders.append("/requires/%d/range" % i)
            comments[path] = "TODO: pinned at %s, which has no release tag; set the range you accept" % pinned[:12]
        else:
            comments[path] = "pinned at %s (%s)" % (pinned[:12], tags[-1])
    doc = {
        "schema": 1,
        "id": comp_id,
        "class": view.comp_class or "ip_tech",
        "version": version or "0.1.0",
        "canonical": _https(view),
        "provides": [],
        "requires": requires,
        "reexports": [],
        "variants": {"sim": {"flist": "TODO: the command that prints the resolved simulation file list"}},
        "tests": {"t0": {"run": "TODO: the T0 entry point", "control": "TODO: its negative control (must exit 1)"}},
    }
    if view.comp_class is None:
        placeholders.append("/class")
    if not view.base_url:
        placeholders.append("/canonical")
    placeholders += ["/variants/sim/flist", "/tests/t0/run", "/tests/t0/control"]
    keep = _existing(view)
    if keep:                                                       # a person's answers are kept, never replaced
        for k in ("provides", "reexports", "variants", "tests", "generated", "workarounds", "fork_contract"):
            if k in keep:
                doc[k] = keep[k]
                placeholders = [p for p in placeholders if not p.startswith("/%s/" % k)]
        old_req = {(r.get("id"), r.get("path")): r.get("range") for r in keep.get("requires") or [] if isinstance(r, dict)}
        for i, r in enumerate(doc["requires"]):
            prev = old_req.get((r["id"], r["path"]))
            if prev and "/requires/%d/range" % i in placeholders:
                r["range"] = prev
                placeholders.remove("/requires/%d/range" % i)
                comments[r["path"]] = "kept from the current manifest (the pin has no release tag)"
    problems = contracts.validate("component-manifest", doc)
    if problems:                                                   # a bug here, never a person's problem
        raise ValueError("the proposed manifest breaks component-manifest: %s" % "; ".join(problems))
    return doc, _render(doc, view, comments), placeholders


def _existing(view) -> dict | None:
    """The manifest at the head when it reads as a mapping (its values are kept by the proposal)."""
    t = view.head_text(FILE)
    try:
        doc = contracts.load_yaml(t) if t else None
    except Exception:                                              # noqa: BLE001 - unreadable: start afresh
        return None
    return doc if isinstance(doc, dict) else None


def _render(doc: dict, view, comments: dict[str, str]) -> str:
    q = json.dumps                                              # a JSON string is a YAML flow scalar
    lines = [
        "# nanosoc-component.yaml (C11): PROPOSED by `nanosoc-rm manifest init %s`" % doc["id"],
        "# from the registry catalogue and the tree at %s. A person reviews every TODO and" % (view.commit or "the work tree")[:12],
        "# commits it; the tool never writes to a repository. Format: schemas/component-manifest.schema.json.",
        "schema: 1",
        "id: %s" % doc["id"],
        "class: %s" % doc["class"],
        "version: \"%s\"" % doc["version"],
        "canonical: %s" % doc["canonical"],
    ]
    lines += (["provides:"] + ["  - %s" % q(p).replace('"name"', "name").replace('"version"', "version")
                               for p in doc["provides"]]) if doc["provides"] else \
        ["provides: []          # C11: named interfaces this repo offers, e.g. {name: tidelink-ahb, version: 1}"]
    if doc["requires"]:
        lines.append("requires:")
        for r in doc["requires"]:
            lines.append("  - {id: %s, path: %s, range: %s}   # %s" % (r["id"], q(r["path"]), q(r["range"]),
                                                                        comments.get(r["path"], "")))
    else:
        lines.append("requires: []")
    lines.append("reexports: %s%s" % (q(doc["reexports"]), "" if doc["reexports"] else
                                       "         # C6: components your users call through this repo (arch_tech passes on nanosoc_gen)"))
    lines.append("variants:             # C7: the build-affecting flag is computed from these file lists")
    lines += ["  %s: {flist: %s}" % (v, q(doc["variants"][v]["flist"])) for v in doc["variants"]]
    lines.append("tests:                # C11, D1: exit 0 PASS, 1 FAIL, 2 NOT-RUN; the control must exit 1 (D6)")
    lines += ["  %s: {run: %s, control: %s}" % (t, q(doc["tests"][t]["run"]), q(doc["tests"][t]["control"]))
              for t in doc["tests"]]
    for k in ("generated", "workarounds", "fork_contract"):
        if k in doc:
            lines.append("%s: %s" % (k, q(doc[k])))
    text = "\n".join(lines) + "\n"
    assert contracts.load_yaml(text) == doc, "the rendered manifest must read back as the proposal"
    return text


def patch(new_text: str, existing: str | None, path: str = FILE) -> str:
    """A unified diff (git apply -p1) that creates `path`, or changes it from `existing`."""
    old = (existing or "").splitlines(keepends=True)
    new = new_text.splitlines(keepends=True)
    head = ["diff --git a/%s b/%s\n" % (path, path)]
    if existing is None:
        head.append("new file mode 100644\n")
    body = list(difflib.unified_diff(old, new, "/dev/null" if existing is None else "a/" + path, "b/" + path))
    return "".join(head + body) if body else ""


def readiness(view, *, changelog_clean: bool | None, api_md: bool, deterministic: bool | None = None,
              soc_release_using: list[str] | None = None, manifest: dict | None = None) -> dict:
    """C9: a repo reaches 1.0.0 only with API.md, deterministic output, golden and consumer test sets, a classed
    changelog and at least one SoC release using it. Each item: ok True/False, or None (could not be measured)."""
    tests = ((manifest or {}).get("tests") or {}) if isinstance(manifest, dict) else {}
    items = [
        {"item": "API.md declares the public interface (C12)", "ok": api_md, "detail": "present" if api_md else "missing"},
        {"item": "deterministic output (two identical regenerations)", "ok": deterministic,
         "detail": "measured" if deterministic is not None else "unmeasured: needs the generator (Phase 2 gate)"},
        {"item": "golden and consumer test sets (manifest tests beyond t0)",
         "ok": (len([t for t in tests if t != "t0"]) > 0) if manifest else False,
         "detail": ", ".join(sorted(tests)) if tests else "no manifest tests"},
        {"item": "a classed changelog that lints clean (C2)", "ok": changelog_clean,
         "detail": "clean" if changelog_clean else "not clean or missing"},
        {"item": "at least one SoC release uses it", "ok": bool(soc_release_using) if soc_release_using is not None else None,
         "detail": ", ".join(soc_release_using) if soc_release_using else
         ("none found" if soc_release_using is not None else "unmeasured: no SoC release records yet (Phase 5)")},
    ]
    ready = all(i["ok"] is True for i in items)
    return {"ready": ready, "items": items}
