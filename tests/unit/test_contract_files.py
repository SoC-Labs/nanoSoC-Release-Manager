"""The interfaces contract is split: sections 0 to 17 in docs/contracts/interfaces.md, sections 18 to 27 each in a
file of their own beside it. Code, tests, schemas and docs keep citing "interfaces.md section N"; these tests keep
every such citation resolvable and every link of the split files live."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "docs" / "contracts"
SPLIT = range(18, 28)
SCANNED = ("src", "tests", "docs", "schemas", "scripts")
SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml"}
# History cites sections as they were numbered on its date (the bump proposals were section 18 before the matrix).
HISTORY = {Path("docs/CHANGES.md"), Path("docs/archive")}
# "interfaces.md section 26.7", "interfaces section 20", "interfaces.md §19", "interfaces.md 26.4, 26.7",
# "interfaces.md sections 25 and 26": every number after the citation up to the end of the clause is one.
_NUM = r"\d+[a-z]?(?:\.\d+)*"
CITATION = re.compile(r"interfaces(?:\.md)?,?\s+(?:sections?\s+|§\s?)?(%s(?:(?:,\s*|\s+and\s+)%s)*)\b" % (_NUM, _NUM))
NUMBER = re.compile(r"\b(\d+)(?:\.(\d+))?")


def _fences_out(text):
    """The lines of a Markdown text outside fenced code blocks."""
    out, fence = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        elif not fence:
            out.append(line)
    return out


def _split_files():
    """{section number: path} from the stub headings of interfaces.md."""
    files = {}
    lines = _fences_out((CONTRACTS / "interfaces.md").read_text())
    for i, line in enumerate(lines):
        m = re.match(r"^## (\d+)\. ", line)
        if m and int(m.group(1)) in SPLIT:
            link = re.search(r"\]\(([^)#]+)\)", "\n".join(lines[i + 1:i + 4]))
            assert link, "interfaces.md section %s links no file" % m.group(1)
            files[int(m.group(1))] = CONTRACTS / link.group(1)
    return files


def _subsections(path):
    return {m.group(1) for line in _fences_out(path.read_text()) for m in [re.match(r"^## (\d+\.\d+) ", line)] if m}


def test_every_split_section_has_its_stub_and_file():
    files = _split_files()
    assert sorted(files) == list(SPLIT)
    for n, path in files.items():
        assert path.is_file(), path
        assert path.read_text().startswith("# %d. " % n), path
        assert path.name.startswith("%d-" % n), path


def test_the_split_files_hold_no_other_top_section():
    """A file holds its one section: no "## N." heading of any section in it, its own or another."""
    for path in _split_files().values():
        assert not [line for line in _fences_out(path.read_text()) if re.match(r"^## \d+\. ", line)], path


def _citations():
    for top in SCANNED:
        for path in sorted((REPO / top).rglob("*")):
            rel = path.relative_to(REPO)
            if path.suffix not in SUFFIXES or not path.is_file() or "__pycache__" in path.parts:
                continue
            if rel in HISTORY or any(h in rel.parents for h in HISTORY):
                continue
            for lineno, line in enumerate(path.read_text(errors="replace").split("\n"), 1):
                for m in CITATION.finditer(line):
                    for num in NUMBER.finditer(m.group(1)):
                        yield rel, lineno, int(num.group(1)), num.group(2)


def test_every_citation_of_a_split_section_resolves():
    files = _split_files()
    subs = {n: _subsections(p) for n, p in files.items()}
    bad, seen = [], 0
    for where, lineno, n, sub in _citations():
        if n not in SPLIT:
            continue
        seen += 1
        if sub is not None and "%d.%s" % (n, sub) not in subs[n]:
            bad.append("%s:%d cites section %d.%s, which %s does not have" % (where, lineno, n, sub, files[n].name))
    assert seen > 50, "the citation scan found %d citations: the pattern no longer matches" % seen
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("n", list(SPLIT))
def test_the_links_of_a_split_file_resolve(n):
    path = _split_files()[n]
    for target in re.findall(r"\]\(([^)\s]+)\)", "\n".join(_fences_out(path.read_text()))):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        assert (path.parent / target.split("#", 1)[0]).exists(), "%s links %s" % (path.name, target)
