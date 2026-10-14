"""Coverage angle (rob/coverage): quality/patch.py, the patches a person applies with `git apply` (Q2-Q5).

Property: whatever combine() decides, file_patch() must produce a diff that `git apply` accepts and that turns
the file into exactly the combined lines, including files without a final newline, deletions of the last line
or the whole file, and edits that grow a region. Conflicting and out-of-range edits are reported, never applied."""
from __future__ import annotations

import os
import subprocess

import pytest

from nanosoc_rm.quality.patch import combine, file_patch, patch_for

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _e(start, end, *replacement):
    return {"start": start, "end": end, "replacement": list(replacement)}


def _lines(text):
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


CASES = {
    "replace the last line, no final newline": ("a\nb\nc", [_e(3, 3, "C")], "a\nb\nC"),
    "grow the last line, no final newline": ("a\nb\nc", [_e(3, 3, "C", "D")], "a\nb\nC\nD"),
    "delete the last line, no final newline": ("a\nb\nc", [_e(3, 3)], "a\nb"),
    "delete every line": ("a\nb\n", [_e(1, 2)], ""),
    "delete the only line, no final newline": ("a", [_e(1, 1)], ""),
    "grow one line and replace the next": ("a\nb\nc\n", [_e(2, 2, "B1", "B2"), _e(3, 3, "C")], "a\nB1\nB2\nC\n"),
    "a file of one empty line": ("\n", [_e(1, 1, "x")], "x\n"),
    "a lone CR is kept": ("a\rb\nc\n", [_e(2, 2, "C")], "a\rb\nC\n"),
    "shrink two lines to one, no final newline": ("a\nb\nc", [_e(1, 2, "x")], "x\nc"),
    "same edit twice": ("a\nb\n", [_e(1, 1, "A"), _e(1, 1, "A")], "A\nb\n"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_patch_applies_and_gives_the_combined_file(tmp_path, name):
    text, edits, want = CASES[name]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=ENV)
    (tmp_path / "f.txt").write_bytes(text.encode())
    new, skipped = combine(_lines(text), edits)
    assert skipped == []
    patch = file_patch("f.txt", text, new)
    (tmp_path / "p.diff").write_text(patch)
    r = subprocess.run(["git", "apply", "p.diff"], cwd=tmp_path, capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "f.txt").read_bytes().decode() == want


def test_no_change_is_no_patch():
    assert file_patch("f", "a\n", ["a"]) == ""
    assert combine(["a"], [_e(1, 1, "a")]) == (["a"], [])


def test_conflicting_and_out_of_range_edits_are_reported_not_applied():
    lines = ["a", "b", "c"]
    bad = [_e(0, 1, "x"), _e(3, 4, "x"), _e(2, 1, "x")]
    new, skipped = combine(lines, bad)
    assert new == lines and sorted(map(repr, skipped)) == sorted(map(repr, bad))
    first, second = _e(2, 2, "B"), _e(2, 2, "Z")                   # same line, different text: the first wins
    new, skipped = combine(lines, [first, second])
    assert new == ["a", "B", "c"] and skipped == [second]
    grow, inside = _e(1, 2, "x", "y", "z"), _e(2, 2, "Q")           # a growing edit cannot share its lines
    new, skipped = combine(lines, [grow, inside])
    assert new == ["x", "y", "z", "c"] and skipped == [inside]
    first, grow2 = _e(1, 1, "A"), _e(1, 2, "p", "q", "r")
    new, skipped = combine(lines, [first, grow2])
    assert new == ["A", "b", "c"] and skipped == [grow2]


class _Src:
    def __init__(self, files):
        self.files = files

    def read_many(self, paths):
        return {p: self.files.get(p) for p in paths}


def test_patch_for_skips_crlf_and_unreadable_files_and_findings_without_edits():
    fa, fb, fc = ({"path": "a.v", "edit": _e(1, 1, "// A")}, {"path": "b.v", "edit": _e(1, 1, "x")},
                  {"path": "c.v", "edit": _e(1, 1, "x")})
    no_edit, conflict = {"path": "a.v", "edit": None}, {"path": "a.v", "edit": _e(1, 1, "// Z")}
    src = _Src({"a.v": b"// a\nmodule a;\n", "b.v": b"x\r\ny\r\n"})
    patch, skipped = patch_for(src, [fa, fb, fc, no_edit, conflict])
    assert patch.startswith("diff --git a/a.v b/a.v\n") and "+// A" in patch and "+// Z" not in patch
    assert sorted(map(id, skipped)) == sorted(map(id, [fb, fc, conflict]))
    assert patch_for(src, [no_edit]) == ("", [])
