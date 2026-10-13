"""The errata document of one release (F4): ERRATA.md and ERRATA.html, rendered from the registry's records.

    document(registry, configuration, *, release=None) -> ErrataDoc
    revision_check(doc, earlier) -> release_check     F4 numbering against earlier drafts of the configuration

Revision numbering (F4). Each erratum record carries `doc_revision`: the revision of its configuration's errata
document in which that record last changed. The document's revision is therefore the highest `doc_revision` of
its records (1 when it has none). A silicon configuration is one silicon revision, so its document is that
revision's own document with its own number. A change to any record must raise that record's `doc_revision` above
the document's last revision; `revision_check` compares this document with every earlier draft of the same
configuration and fails when the entries changed but the revision did not move, or when it went backwards.

`digest` is the md5 of the rendered entries (ERRATA.md below its header), so a new registry commit that does not
touch these records keeps the digest, and a changed word in one record changes it.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from nanosoc_rm.releases.core import PUBLISH_NOTE, check, md5_bytes

__all__ = ["ErrataDoc", "document", "revision_check", "OPEN_STATUSES"]

OPEN_STATUSES = ("open", "permanent")
_ORDER = {"A": 0, "B": 1, "C": 2, None: 3}


@dataclass
class ErrataDoc:
    configuration: str
    title: str
    revision: int
    records: list[dict]
    markdown: str
    html: str
    digest: str
    history: list[tuple[int, list[str]]]


def _text(v: Any) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def _entry_md(e: dict, defect: dict | None) -> list[str]:
    exp = e["exposure"]
    out = ["## %s: %s" % (e["id"], _text(defect["title"]) if defect else e["defect"]), "",
           "| Field | Value |", "|---|---|",
           "| Defect | %s |" % e["defect"],
           "| Category | %s |" % (e["category"] or "undecided"),
           "| Status | %s |" % e["status"],
           "| Exposure | %s (established by %s) |" % (exp["level"], exp["method"]),
           "| Forms present | %s |" % (", ".join(e["forms_present"]) or "none"),
           "| Effects present | %s |" % (", ".join(e["effects_present"]) or "none"),
           "| Changed in revision | %d |" % e["doc_revision"], ""]
    out += ["**Evidence.** %s" % _text(exp.get("evidence")), ""]
    out += ["**Conditions.** %s" % _text(e["conditions"]), ""]
    w = e.get("workaround")
    if w:
        out += ["**Workaround.** %s" % _text(w["rules"]), ""]
        for imp in w.get("implementations") or []:
            out.append("- %s `%s`%s" % (imp["component"], imp["commit"][:12], "" if imp["merged"] else " (not merged)"))
        if w.get("implementations"):
            out.append("")
    else:
        out += ["**Workaround.** None known.", ""]
    return out


def document(registry: Any, configuration: str, *, release: dict | None = None) -> ErrataDoc:
    """The errata document of `configuration` from `registry` (nanosoc_rm.registry.Registry). `release` (optional:
    {component, version, tag, commit, silicon}) names what it is the document of."""
    cfg = registry.configuration(configuration) or {}
    records = sorted(registry.errata(configuration), key=lambda e: (_ORDER.get(e.get("category"), 3), e["id"]))
    defects = {d["id"]: d for d in registry.defects()}
    revision = max([e["doc_revision"] for e in records] or [1])
    sil = cfg.get("silicon")
    subject = ("%s silicon %s" % (sil["chip"], sil["revision"])) if sil else configuration
    title = "Errata: %s" % subject
    history: dict[int, list[str]] = {}
    for e in records:
        history.setdefault(e["doc_revision"], []).append(e["id"])
    body = ["## Summary", "", "| Erratum | Defect | Category | Exposure | Status |", "|---|---|---|---|---|"]
    body += ["| %s | %s | %s | %s | %s |" % (e["id"], e["defect"], e["category"] or "undecided",
                                           e["exposure"]["level"], e["status"]) for e in records]
    if not records:
        body = ["No erratum is recorded for %s." % configuration]
    body.append("")
    for e in records:
        body += _entry_md(e, defects.get(e["defect"]))
    body += ["## Revision history", "", "| Revision | Errata that last changed in it |", "|---|---|"]
    body += ["| %d | %s |" % (r, ", ".join(sorted(ids))) for r, ids in sorted(history.items())] or ["| 1 | none |"]
    body_text = "\n".join(body).rstrip() + "\n"
    head = ["# %s" % title, "", "Document revision %d. Configuration `%s`%s." % (
        revision, configuration,
        "" if not release else ", release %s (`%s`, commit `%s`)" % (release["version"], release["tag"],
                                                                     release["commit"][:12])),
        "Generated from the registry's errata records by nanosoc-release-manager; edit the records, not this file.", ""]
    if release:
        head[-1:] = ["", "Draft. %s" % PUBLISH_NOTE, ""]
    markdown = "\n".join(head) + "\n" + body_text
    return ErrataDoc(configuration, title, revision, records, markdown,
                     _html(title, revision, configuration, release, records, defects, history),
                     md5_bytes(body_text.encode()), sorted(history.items()))


_CSS = """:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#deddd8;--red:#b3261e;--red-bg:#fbe9e7;
--amber:#8a5a00;--amber-bg:#fff4dc;--green:#1e6b34;--green-bg:#e7f4ea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--red:#ff8a80;
--red-bg:#3a1c1a;--amber:#ffcc66;--amber-bg:#3a2f14;--green:#8fd6a2;--green-bg:#183121}}
body{margin:0 auto;max-width:60rem;padding:1rem;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
table{border-collapse:collapse;width:100%;margin:.5rem 0}th,td{border:1px solid var(--line);padding:.3rem .5rem;text-align:left}
.note{color:var(--muted)}.state-red{background:var(--red-bg);color:var(--red)}
.state-amber{background:var(--amber-bg);color:var(--amber)}.state-green{background:var(--green-bg);color:var(--green)}
code{font-size:.9em}"""
_EXPOSURE = {"exposed": "state-red", "affected": "state-red", "probably-exposed": "state-amber",
             "probably-not-affected": "state-amber", "not-affected": "state-green"}


def _html(title, revision, configuration, release, records, defects, history) -> str:
    e_ = html.escape
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
             "<title>%s</title><style>%s</style></head><body>" % (e_(title), _CSS),
             "<h1>%s</h1><p>Document revision <strong data-revision=\"%d\">%d</strong>. Configuration <code>%s</code>%s.</p>"
             % (e_(title), revision, revision, e_(configuration),
                "" if not release else ", release %s (<code>%s</code>, commit <code>%s</code>)"
                % (e_(release["version"]), e_(release["tag"]), e_(release["commit"][:12]))),
             "<p class=\"note\">Generated from the registry's errata records; edit the records, not this file.</p>"]
    if release:
        parts.append("<p class=\"note\" data-publish-note>Draft. %s</p>" % e_(PUBLISH_NOTE))
    if not records:
        parts.append("<p>No erratum is recorded for %s.</p>" % e_(configuration))
    else:
        parts.append("<table><thead><tr><th>Erratum</th><th>Defect</th><th>Category</th><th>Exposure</th>"
                     "<th>Status</th></tr></thead><tbody>")
        for e in records:
            lvl = e["exposure"]["level"]
            parts.append("<tr data-erratum=\"%s\"><td><a href=\"#%s\">%s</a></td><td>%s</td><td>%s</td>"
                         "<td class=\"%s\">%s</td><td>%s</td></tr>"
                         % (e_(e["id"]), e_(e["id"]), e_(e["id"]), e_(e["defect"]), e_(e["category"] or "undecided"),
                            _EXPOSURE.get(lvl, ""), e_(lvl), e_(e["status"])))
        parts.append("</tbody></table>")
    for e in records:
        d = defects.get(e["defect"])
        exp, w = e["exposure"], e.get("workaround")
        parts.append("<h2 id=\"%s\">%s: %s</h2>" % (e_(e["id"]), e_(e["id"]), e_(_text(d["title"]) if d else e["defect"])))
        rows = [("Defect", e["defect"]), ("Category", e["category"] or "undecided"), ("Status", e["status"]),
                ("Exposure", "%s (established by %s)" % (exp["level"], exp["method"])),
                ("Forms present", ", ".join(e["forms_present"]) or "none"),
                ("Effects present", ", ".join(e["effects_present"]) or "none"),
                ("Changed in revision", str(e["doc_revision"]))]
        parts.append("<table>%s</table>" % "".join("<tr><th>%s</th><td>%s</td></tr>" % (e_(k), e_(v)) for k, v in rows))
        parts.append("<p><strong>Evidence.</strong> %s</p>" % e_(_text(exp.get("evidence"))))
        parts.append("<p><strong>Conditions.</strong> %s</p>" % e_(_text(e["conditions"])))
        if w:
            parts.append("<p><strong>Workaround.</strong> %s</p>" % e_(_text(w["rules"])))
            if w.get("implementations"):
                parts.append("<ul>%s</ul>" % "".join(
                    "<li>%s <code>%s</code>%s</li>" % (e_(i["component"]), e_(i["commit"][:12]),
                                                        "" if i["merged"] else " (not merged)")
                    for i in w["implementations"]))
        else:
            parts.append("<p><strong>Workaround.</strong> None known.</p>")
    parts.append("<h2>Revision history</h2><table><thead><tr><th>Revision</th><th>Errata that last changed in it</th>"
                 "</tr></thead><tbody>%s</tbody></table>" % ("".join(
                     "<tr><td>%d</td><td>%s</td></tr>" % (r, e_(", ".join(sorted(ids)))) for r, ids in sorted(history.items()))
                     or "<tr><td>1</td><td>none</td></tr>"))
    parts.append("</body></html>\n")
    return "\n".join(parts)


def revision_check(doc: ErrataDoc, earlier: list[dict]) -> dict:
    """F4: `earlier` = the `errata` blocks ({revision, digest}, plus `version`) of earlier drafts of the same
    configuration. FAIL when the entries changed but the revision did not rise, or when it went down."""
    problems = []
    for e in earlier:
        rev, dig = e.get("revision"), e.get("digest")
        if rev is None:
            continue
        if doc.revision < rev:
            problems.append("revision %d is below draft %s's revision %d" % (doc.revision, e.get("version"), rev))
        elif doc.revision == rev and dig and dig != doc.digest:
            problems.append("the errata entries changed since draft %s but the revision is still %d: raise "
                            "doc_revision on each changed record" % (e.get("version"), rev))
    if problems:
        return check("errata.revision", "FAIL", "; ".join(problems), requirement="F4")
    same = [e.get("version") for e in earlier if e.get("revision") == doc.revision and e.get("digest") == doc.digest]
    return check("errata.revision", "PASS", "document revision %d (%d record(s))%s" % (
        doc.revision, len(doc.records), "; same as draft(s) %s" % ", ".join(map(str, same)) if same
        else "; first document at this revision" if not earlier else ""), requirement="F4")
