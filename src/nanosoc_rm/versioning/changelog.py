"""The classed changelog (C2): its format, a parser, the linter, and the release rewrite G1's draft uses.

Format (docs/versioning.md has the worked example). CHANGELOG.md at the repository root:

    # Changelog
    ## [Unreleased]
    ### breaking
    - Renamed port HSEL_O to HSEL (#12).
    ### fix
    - ...
    ## [0.2.0] - 2026-10-01
    ### feature
    - ...
    [Unreleased]: https://github.com/<org>/<repo>/compare/v0.2.0...HEAD
    [0.2.0]: https://github.com/<org>/<repo>/compare/v0.1.0...v0.2.0

Every entry is a list item under a class subsection (`### breaking|feature|fix|none`, any case). A release
heading is `## [X.Y.Z] - YYYY-MM-DD`, optionally followed by `[YANKED]` (G6).

    parse(text) -> Changelog
    lint(text, *, tags=None, grandfathered=(), base_url=None) -> [finding]       finding = {rule, severity, line, message, suggestion}
    release(text, version, date, *, previous_tag, compare_url) -> new text      Unreleased -> [version] (G1)
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from nanosoc_rm.versioning import semver

__all__ = ["Entry", "Section", "Changelog", "parse", "lint", "release", "RULES", "FILE", "unreleased_classes",
           "C5_RX"]

FILE = "CHANGELOG.md"
_H1 = re.compile(r"^#\s+(.+?)\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_H3 = re.compile(r"^###\s+(.+?)\s*$")
_RELEASE = re.compile(r"^\[(?P<v>[^\]]+)\](?:\s+-\s+(?P<date>\S+))?(?P<rest>.*)$")
# A CommonMark list item: a bullet or an ordered marker indented 0-3 spaces (review item 2: an indented or a
# numbered item is an entry too). Group 1 is the indent and marker, so the item's content offset is its length.
_ITEM = re.compile(r"^( {0,3}(?:[-*+]|[0-9]{1,9}[.)])[ \t]+)(\S.*)$")
_LINKDEF = re.compile(r"^\[(?P<label>[^\]]+)\]:\s*(?P<url>\S+)\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# C5: a new check that fails by default, or a warning turned into a failure, is breaking.
C5_RX = re.compile(r"(fails? by default|fail-by-default|now fails|now an? (error|failure)|warning[s]? (is |are )?now "
                   r"(an? )?(errors?|failures?|fatal)|warning\S* (to|into|->|→) (an? )?(errors?|failures?)|"
                   r"(promote|turn|make)s? .*warning\S* (to|into) (an? )?(errors?|failures?)|"
                   # review item 8: the other common phrasings of "a warning became a failure"
                   r"\b(is|are) now (fatal|failing|an? (hard )?error)|\bnow (fatal|errors?|exits? (1|non-?zero))\b|"
                   r"\b(treat|treats|treated|treating)\b.*\bwarnings?\b.*\bas (an? )?(errors?|failures?|fatal)|"
                   r"-Werror|\bexits? (with )?(1|non-?zero|an error)\b|"
                   r"\b(check|lint|gate|rule)s?\b[^.;]*\b(on|enabled) by default|"
                   r"\b(enable|enables|enabled|turn on|turns on)\b[^.;]*\b(check|lint|gate|rule)s?\b[^.;]*\bby default)",
                   re.I)
# Familiar headings that are not classes, and the class each most often means (a suggestion, never applied).
_NEAR = {"added": "feature", "changed": "breaking or feature", "deprecated": "feature", "removed": "breaking",
         "fixed": "fix", "security": "fix", "features": "feature", "fixes": "fix", "bug fixes": "fix",
         "breaking changes": "breaking", "other": "none", "internal": "none", "docs": "none"}

RULES = {
    "CL1": "CHANGELOG.md exists at the repository root",
    "CL2": "the first heading is `# Changelog`",
    "CL3": "exactly one `## [Unreleased]` section, before every release",
    "CL4": "a release heading is `## [X.Y.Z] - YYYY-MM-DD` (SemVer, a real date)",
    "CL5": "releases are newest first",
    "CL6": "each version appears once",
    "CL7": "a subsection heading is a change class: breaking, feature, fix or none (C2)",
    "CL8": "every entry sits under a class subsection (C2: an entry without a class is rejected)",
    "CL9": "no empty class subsection or empty release",
    "CL10": "a release's classes match its version bump under C1 (the Cargo pre-1.0 rule)",
    "CL11": "every version heading and [Unreleased] has a link definition; links are https",
    "CL12": "every release section has its vX.Y.Z tag, and every release tag has a section",
    "CL13": "C5: an entry that adds a failing check or turns a warning into a failure is class breaking",
}


@dataclass
class Entry:
    cls: str | None
    text: str
    line: int
    sub: str | None = None                 # the subsection heading it sits under (None: under none)
    kind: str = "item"                     # "item" (a list item) or "text" (a paragraph: never a classed entry)
    offset: int = 2                        # its content offset: a line indented this far continues it


@dataclass
class Section:
    heading: str
    line: int
    version: semver.Version | None = None
    version_text: str | None = None
    date: str | None = None
    yanked: bool = False
    unreleased: bool = False
    entries: list[Entry] = field(default_factory=list)
    subsections: list[tuple[str, int, int]] = field(default_factory=list)      # (heading, line, entry count)
    problems: list[dict] = field(default_factory=list)


@dataclass
class Changelog:
    title: str | None
    title_line: int | None
    sections: list[Section]
    links: dict[str, tuple[str, int]]
    loose: list[Entry]                     # entries before any version section

    @property
    def unreleased(self) -> Section | None:
        return next((s for s in self.sections if s.unreleased), None)

    @property
    def releases(self) -> list[Section]:
        return [s for s in self.sections if not s.unreleased]


def _finding(rule: str, severity: str, line: int | None, message: str, suggestion: str | None = None) -> dict:
    return {"rule": rule, "severity": severity, "line": line, "message": message, "suggestion": suggestion}


def parse(text: str) -> Changelog:
    """Read CHANGELOG.md. Never raises: what it cannot place is kept for the linter to report."""
    title = title_line = None
    sections: list[Section] = []
    links: dict[str, tuple[str, int]] = {}
    loose: list[Entry] = []
    cur: Section | None = None
    cls: str | None = None
    sub_open: list | None = None
    in_code = in_comment = blank = False
    last: Entry | None = None
    for n, raw in enumerate(_lines(text), 1):
        line = raw.rstrip()
        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.lstrip().startswith("<!--"):                          # an HTML comment is never an entry
            in_comment = "-->" not in line.lstrip()[4:]
            continue
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _LINKDEF.match(line)
        if m:
            links.setdefault(m.group("label").strip().lower(), (m.group("url"), n))
            last = None
            continue
        if _H1.match(line) and not line.startswith("##"):
            if title is None:
                title, title_line = _H1.match(line).group(1), n
            last = None
            continue
        m = _H2.match(line)
        if m and not line.startswith("###"):
            head = m.group(1)
            cur = Section(heading=head, line=n)
            cls, sub_open, last = None, None, None
            r = _RELEASE.match(head)
            if r and r.group("v").strip().lower() == "unreleased":
                cur.unreleased = True
            elif r:
                cur.version_text = r.group("v").strip()
                try:
                    cur.version = semver.parse(cur.version_text)
                except ValueError:
                    cur.version = None
                cur.date = r.group("date")
                cur.yanked = "[yanked]" in (r.group("rest") or "").lower()
            sections.append(cur)
            continue
        m = _H3.match(line)
        if m:
            name = m.group(1).strip().strip("*").strip()
            cls = name.lower() if name.lower() in semver.CLASS_RANK else None
            sub_open = [name, n, 0]
            if cur is not None:
                cur.subsections.append(sub_open)                       # type: ignore[arg-type]
            else:
                loose.append(Entry(None, "### " + name, n))
            last = None
            continue
        expanded = raw.expandtabs(4).rstrip()
        indent = len(expanded) - len(expanded.lstrip(" "))
        if line.strip() == "":
            blank = True
            continue
        if last is not None and last.kind == "item" and indent >= last.offset:
            last.text += " " + line.strip()                            # continues the item (a nested item too)
            blank = False
            continue
        m = _ITEM.match(expanded)
        if m:
            e = Entry(cls if sub_open is not None else None, m.group(2).strip(), n,
                      sub_open[0] if sub_open is not None else None, "item", len(m.group(1)))
            if sub_open is not None:
                sub_open[2] += 1
            (cur.entries if cur is not None else loose).append(e)
            last, blank = e, False
            continue
        if last is not None and not blank:
            last.text += " " + line.strip()                            # a lazy continuation of the paragraph above
            continue
        if cur is not None:
            # a paragraph inside a version section is not a list item, so it has no class (C2): it is kept as an
            # unclassed "text" entry for the linter, never dropped (review item 2)
            e = Entry(None, line.strip(), n, None, "text", 0)
            cur.entries.append(e)
            last, blank = e, False
            continue
        last, blank = None, False                                      # prose under the title: not an entry
    for s in sections:
        s.subsections = [tuple(x) for x in s.subsections]              # type: ignore[misc]
    return Changelog(title, title_line, sections, links, loose)


def unreleased_classes(cl: Changelog) -> dict[str, list[dict]]:
    """{class: [{text, line}]} of the Unreleased section's classed entries (unclassed ones under "unclassed")."""
    out: dict[str, list[dict]] = {}
    u = cl.unreleased
    for e in (u.entries if u else []):
        out.setdefault(e.cls or "unclassed", []).append({"text": e.text, "line": e.line})
    return out


def lint(text: str | None, *, tags: dict[str, dict] | None = None, grandfathered: set[str] | frozenset = frozenset(),
         base_url: str | None = None) -> list[dict]:
    """Every finding of RULES in `text` (None: the file does not exist). `tags`: the repo's tags on its canonical
    host ({name: {...}}), for CL12; None skips CL12. `grandfathered`: tag names CL12 does not ask a section for."""
    if text is None:
        return [_finding("CL1", "error", None, "no CHANGELOG.md at the repository root",
                         "add one: `nanosoc-rm changelog lint` documents the format (docs/versioning.md)")]
    out: list[dict] = []
    cl = parse(text)
    if cl.title is None or cl.title.strip().lower() != "changelog":
        out.append(_finding("CL2", "error", cl.title_line or 1, "the first heading is %r, not `# Changelog`"
                            % (cl.title,) if cl.title else "no `# Changelog` heading", "# Changelog"))
    unrel = [s for s in cl.sections if s.unreleased]
    if not unrel:
        out.append(_finding("CL3", "error", (cl.sections[0].line if cl.sections else cl.title_line or 1),
                            "no `## [Unreleased]` section", "## [Unreleased]"))
    elif len(unrel) > 1:
        out.append(_finding("CL3", "error", unrel[1].line, "a second `## [Unreleased]` section"))
    elif cl.sections and not cl.sections[0].unreleased:
        out.append(_finding("CL3", "error", unrel[0].line, "`## [Unreleased]` comes after a release; it must be first"))
    for e in cl.loose:
        out.append(_finding("CL8", "error", e.line, "an entry outside any version section: %r" % e.text[:80]))
    seen: dict[str, int] = {}
    prev: Section | None = None
    for s in cl.sections:
        if s.unreleased:
            pass
        elif s.version is None or s.version.build is not None:
            out.append(_finding("CL4", "error", s.line, "release heading %r is not `[X.Y.Z] - YYYY-MM-DD`" % s.heading,
                                "## [0.1.0] - %s" % dt.date.today().isoformat()))
        else:
            if not s.date or not _DATE.match(s.date) or not _real_date(s.date):
                out.append(_finding("CL4", "error", s.line, "release %s has no valid date (YYYY-MM-DD)" % s.version_text))
            if s.version_text in seen:
                out.append(_finding("CL6", "error", s.line, "version %s also at line %d" % (s.version_text, seen[s.version_text])))
            seen.setdefault(s.version_text, s.line)
            if prev is not None and prev.version is not None:
                if not s.version < prev.version:
                    out.append(_finding("CL5", "error", s.line, "%s comes after %s: releases must be newest first"
                                        % (s.version_text, prev.version_text)))
                elif prev.date and s.date and _DATE.match(prev.date) and _DATE.match(s.date) and s.date > prev.date:
                    out.append(_finding("CL5", "warning", s.line, "%s (%s) is dated after the newer %s (%s)"
                                        % (s.version_text, s.date, prev.version_text, prev.date)))
        for name, line, count in s.subsections:
            if name.lower() not in semver.CLASS_RANK:
                hint = _NEAR.get(name.lower())
                out.append(_finding("CL7", "error", line, "subsection %r is not a change class" % name,
                                    "### %s" % hint if hint and " " not in hint else
                                    "one of ### breaking, ### feature, ### fix, ### none" + (" (%s?)" % hint if hint else "")))
            elif count == 0:
                out.append(_finding("CL9", "warning", line, "subsection %r has no entries" % name))
        for e in s.entries:
            if e.kind == "text":
                out.append(_finding("CL8", "error", e.line, "text that is not a list item, so it has no class: %r"
                                    % e.text[:80], "make it a `- ` entry under ### breaking, ### feature, ### fix "
                                    "or ### none, or move prose above the first version section"))
            elif e.cls is None and e.sub is None:                  # under a non-class heading: CL7 reports it
                out.append(_finding("CL8", "error", e.line, "entry without a class: %r" % e.text[:80],
                                    "move it under ### breaking, ### feature, ### fix or ### none"))
            elif e.cls in ("feature", "fix", "none") and C5_RX.search(e.text):
                out.append(_finding("CL13", "error", e.line, "entry reads as a new failing check or a warning turned "
                                    "into a failure, but its class is %s: %r" % (e.cls, e.text[:80]),
                                    "### breaking (C5)"))
        if not s.unreleased and s.version is not None and not s.entries:
            out.append(_finding("CL9", "warning", s.line, "release %s lists no entries" % s.version_text))
        if not s.unreleased and s.version is not None:
            older = next((t for t in cl.releases if t.version is not None and t.line > s.line), None)
            out += _bump_findings(s, older)
        if not s.unreleased and s.version is not None:
            prev = s
    out += _link_findings(cl, base_url)
    if tags is not None:
        out += _tag_findings(cl, tags, grandfathered)
    return sorted(out, key=lambda f: (f["line"] or 0, f["rule"]))


def _real_date(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _bump_findings(s: Section, older: Section | None) -> list[dict]:
    classes = {e.cls for e in s.entries if e.cls}
    if not classes or s.version is None:
        return []
    top = semver.max_class(classes)
    if older is None or older.version is None:
        return []
    implied = semver.implied_class(older.version, s.version)
    want = semver.bump(older.version, top)
    if want is not None and s.version.core == want:
        return []
    if top == "none":
        return [_finding("CL10", "error", s.line, "release %s lists only class none: a none change makes no release"
                         % s.version_text)]
    return [_finding("CL10", "error", s.line, "release %s after %s implies %s, but its most severe entry is %s "
                     "(C1 gives %s)" % (s.version_text, older.version_text, implied, top, want),
                     "## [%s]" % want if want else None)]


def _link_findings(cl: Changelog, base_url: str | None) -> list[dict]:
    out = []
    labels = ["unreleased"] if cl.unreleased else []
    labels += [s.version_text.lower() for s in cl.releases if s.version_text]
    for s in cl.sections:
        label = "unreleased" if s.unreleased else (s.version_text or "").lower()
        if label and label not in cl.links:
            out.append(_finding("CL11", "warning", s.line, "no link definition for [%s]" % (s.version_text or "Unreleased"),
                                "[%s]: %s" % (s.version_text or "Unreleased",
                                              (base_url.rstrip("/") + "/compare/...") if base_url else "<compare URL>")))
    for label, (url, line) in cl.links.items():
        if not url.startswith("https://"):
            out.append(_finding("CL11", "warning", line, "link [%s] is not https: %s" % (label, url)))
        elif base_url and not url.startswith(base_url.rstrip("/") + "/"):
            out.append(_finding("CL11", "warning", line, "link [%s] points outside the canonical repo (%s)"
                                % (label, base_url)))
    return out


def _tag_findings(cl: Changelog, tags: dict[str, dict], grandfathered) -> list[dict]:
    out = []
    names = set(tags)
    have = {s.version_text for s in cl.releases if s.version_text}
    for s in cl.releases:
        if s.version is not None and "v%s" % s.version_text not in names:
            out.append(_finding("CL12", "warning", s.line, "release %s has no tag v%s on the canonical host"
                                % (s.version_text, s.version_text)))
    for t in sorted(names):
        if semver.is_release_tag(t) and t[1:] not in have and t not in grandfathered:
            out.append(_finding("CL12", "error", None, "release tag %s has no `## [%s]` section" % (t, t[1:])))
    return out


# Markdown's line endings (CommonMark 2.1): \n, \r\n or \r. Not str.splitlines(), which also breaks at \x0b,
# \x0c, \x1c-\x1e, \x85, U+2028 and U+2029, characters an entry may hold (ROB-property-7).
_EOL = re.compile(r"\r\n|\r|\n")


def _split_keep(text: str) -> list[tuple[str, str]]:
    """[(line, its line ending)], the last ending "" when the text does not end in one."""
    out, pos = [], 0
    for m in _EOL.finditer(text):
        out.append((text[pos:m.start()], m.group()))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def _lines(text: str) -> list[str]:
    return [line for line, _ in _split_keep(text)]


def release(text: str, version: str, date: str, *, previous_tag: str | None, compare_base: str | None) -> str:
    """The changelog with its Unreleased entries moved under `## [version] - date` and a fresh empty Unreleased
    section; the link definitions updated (`compare_base`: the canonical repo's web URL, None leaves links).
    Every line keeps its own line ending; a new line takes the file's first one (a CRLF file stays CRLF)."""
    pairs = _split_keep(text)
    lines, ends = [p[0] for p in pairs], [p[1] for p in pairs]
    eol = next((e for e in ends if e), "\n")
    cl = parse(text)
    u = cl.unreleased
    if u is None:
        raise ValueError("no ## [Unreleased] section to release")
    idx = u.line - 1
    lines[idx:idx + 1] = [lines[idx], "", "## [%s] - %s" % (version, date)]
    ends[idx:idx + 1] = [ends[idx] or eol, eol, eol]
    if compare_base:
        base = compare_base.rstrip("/")
        sep = "/-/compare/" if "/-/" in base or "gitlab" in base else "/compare/"
        if base.endswith("/-"):
            base = base[:-2]
        new_unrel = "[Unreleased]: %s%sv%s...HEAD" % (base, sep, version)
        new_ver = ("[%s]: %s%s%s...v%s" % (version, base, sep, previous_tag, version) if previous_tag
                   else "[%s]: %s%sv%s" % (version, base, "/releases/tag/" if sep == "/compare/" else "/-/tags/", version))
        out, out_ends, replaced = [], [], False
        for line, end in zip(lines, ends):
            m = _LINKDEF.match(line)
            if m and m.group("label").strip().lower() == "unreleased":
                out += [new_unrel, new_ver]
                out_ends += [end or eol, end or eol]
                replaced = True
            else:
                out.append(line)
                out_ends.append(end)
        if not replaced:
            if out_ends:
                out_ends[-1] = out_ends[-1] or eol
            if out and out[-1].strip():
                out.append("")
                out_ends.append(eol)
            out += [new_unrel, new_ver]
            out_ends += [eol, eol]
        lines, ends = out, out_ends
    while lines and lines[-1] == "":                 # one line ending at the end, no trailing blank lines
        lines.pop()
        ends.pop()
    if ends:
        ends[-1] = ends[-1] or eol
    return "".join(line + end for line, end in zip(lines, ends))
