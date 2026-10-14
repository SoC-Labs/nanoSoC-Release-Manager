"""Property tests: patch generation (quality/patch.py combine + file_patch, bumps/patchfile.py patch_text).

Every patch the tool writes is for a person to `git apply`, so the oracle is git: `git apply --check` must accept
it against the analysed file, and applying it must give exactly the lines the tool computed. `combine` is
checked against a direct model (one edit = slice replacement; disjoint edits commute).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from prop_helpers import assume, budget, example, git, given, st

from nanosoc_rm.bumps.engine import REGEN_NOTE
from nanosoc_rm.bumps.patchfile import patch_text
from nanosoc_rm.quality.patch import combine, file_patch

LINE = st.sampled_from(["a", "b", "", " ", "c d", "\\ x", "+x", "-x", "@@ -1 +1 @@", "\t", "x\r", "\u00fcn\u00ef",
                        "--- a/f", "+++ b/f", "diff --git a/f b/f", "\\ No newline at end of file"])
PATHS = st.sampled_from(["f.txt", "d/f.v", "my file.sh", "\u00fc.py", "a b/c.txt", 'q"x.c', "-lead.c", " lead.c",
                         "back\\slash.c"])               # a tab or newline in a path: ROB-property-13


def git_apply(path: str, text: str, patch: str, *extra: str) -> str:
    """The file after `git apply` (asserting --check first)."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d, path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.encode("utf-8"))
        Path(d, "p.diff").write_bytes(patch.encode("utf-8"))
        chk = git("apply", "--check", *extra, "p.diff", cwd=d, check=False)
        assert chk.returncode == 0, (path, text, patch, chk.stderr.decode(errors="replace"))
        git("apply", *extra, "p.diff", cwd=d)
        return f.read_bytes().decode("utf-8")


def split(text: str) -> list[str]:
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


@budget(80)
@given(st.lists(LINE, min_size=1, max_size=8), st.lists(LINE, max_size=8), st.booleans(), PATHS)
def test_file_patch_applies_with_git_and_gives_the_new_lines(old, new, ends_nl, path):
    text = "\n".join(old) + ("\n" if ends_nl else "")
    assume(text != "")                                       # the empty file is ROB-property-12
    ends_nl = text.endswith("\n")                           # a last empty line also ends the text with \n
    patch = file_patch(path, text, new)
    if split(text) == new:
        assert patch == ""
        return
    want = "\n".join(new) + ("\n" if ends_nl and new else "")
    assert git_apply(path, text, patch) == want


EDIT = st.builds(lambda s, n, rep: {"start": s, "end": s + n, "replacement": rep},
                 st.integers(1, 8), st.integers(0, 2), st.lists(LINE, max_size=3))


@given(st.lists(LINE, min_size=1, max_size=8), EDIT)
def test_one_valid_edit_is_a_slice_replacement(original, edit):
    new, skipped = combine(original, [edit])
    s, e = edit["start"], edit["end"]
    if s < 1 or e > len(original):
        assert skipped == [edit] and new == original
    else:
        assert skipped == [] and new == original[:s - 1] + edit["replacement"] + original[e:]


@given(st.lists(LINE, min_size=4, max_size=10), st.lists(EDIT, min_size=2, max_size=4), st.randoms())
def test_disjoint_edits_commute_and_none_is_skipped(original, edits, rnd):
    edits = [e for e in edits if e["end"] <= len(original)]
    edits.sort(key=lambda e: e["start"])
    disjoint = all(a["end"] < b["start"] for a, b in zip(edits, edits[1:]))
    assume(edits and disjoint)
    want = list(original)
    for e in reversed(edits):
        want[e["start"] - 1:e["end"]] = e["replacement"]
    shuffled = list(edits)
    rnd.shuffle(shuffled)
    new, skipped = combine(original, shuffled)
    assert skipped == [] and new == want


@given(st.lists(LINE, min_size=2, max_size=8),
       st.lists(EDIT, min_size=1, max_size=4, unique_by=lambda e: (e["start"], e["end"])), st.randoms())
def test_combine_is_order_independent(original, edits, rnd):
    """Order-free for edits with distinct ranges (two edits of one range: the first wins, as documented)."""
    shuffled = list(edits)
    rnd.shuffle(shuffled)
    a, sa = combine(original, edits)
    b, sb = combine(original, shuffled)
    assert a == b
    key = lambda e: (e["start"], e["end"], e["replacement"])   # noqa: E731
    assert sorted(sa, key=key) == sorted(sb, key=key)


@budget(60)
@given(st.lists(LINE, min_size=2, max_size=10), st.lists(EDIT, min_size=1, max_size=4), st.booleans())
def test_combined_edits_patch_applies(original, edits, ends_nl):
    text = "\n".join(original) + ("\n" if ends_nl else "")
    ends_nl = text.endswith("\n")
    new, _skipped = combine(split(text), edits)
    patch = file_patch("src/f.v", text, new)
    if patch:
        assert git_apply("src/f.v", text, patch) == "\n".join(new) + ("\n" if ends_nl and new else "")


HEX = st.from_regex(r"[0-9a-f]{40}", fullmatch=True).filter(lambda h: h.strip("0"))
GITLINK_PATH = st.sampled_from(["sub", "deps/ip core", "a/b/c", "\u00fc/x"])


def _fact(path, a, b, state, title):
    ref = lambda c, t: {"commit": c, "tag": t, "exact": True}   # noqa: E731
    return {"id": "BP-1", "path": path, "state": state, "reason": "blocked: %s" % title,
            "from": ref(a, "v0.1.0"), "to": ref(b, None), "dependency": {"component": "dep"},
            "consumer": {"component": "soc", "repo": {"host": "github", "path": "o/soc"}, "branch": "main",
                         "base": a[:12]},
            "patch": {"file": "BP-1.diff", "note": REGEN_NOTE},
            "change": {"class": "fix", "class_source": "changelog", "class_detail": title, "diverged": False,
                       "commits": {"count": 1}},
            "defects": {"fixed": [{"id": "SL-DEF-0001", "title": title}], "introduced": []},
            "matrix": {"verdict": None, "source": None}}


@budget(40)
@given(GITLINK_PATH, HEX, HEX, st.sampled_from(["proposed", "blocked"]),
       st.sampled_from(["plain", "+++ b/evil", "@@ -1 +1 @@", "\\ No newline"]))
def test_gitlink_patch_applies_to_the_index(path, a, b, state, title):
    assume(a != b)
    text = patch_text(_fact(path, a, b, state, title))
    with tempfile.TemporaryDirectory() as d:
        git("init", "-q", cwd=d)
        git("update-index", "--add", "--cacheinfo", "160000,%s,%s" % (a, path), cwd=d)
        Path(d, "p.diff").write_bytes(text.encode("utf-8"))
        git("apply", "--check", "--index", "p.diff", cwd=d)
        git("apply", "--index", "p.diff", cwd=d)
        got = git("ls-files", "-s", "--", path, cwd=d).stdout.decode()
    assert got.split()[:2] == ["160000", b]


# ---------------------------------------------------------------------------------------------------------
# Recorded defects
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ROB-property-12: file_patch() on an empty file treats it as one empty line "
                                       "without a newline (quality/patch.py:70-72), so the patch git is given "
                                       "never applies")
@example(["# header"])
@given(st.lists(LINE, min_size=1, max_size=3))
def test_rob_property_12_patch_for_an_empty_file(new):
    original = split("")                                       # what patch_for() hands combine(): [""]
    lines, skipped = combine(original, [{"start": 1, "end": 1, "replacement": new}])
    assert not skipped
    assert git_apply("README.md", "", file_patch("README.md", "", lines)) == "\n".join(lines)


@pytest.mark.xfail(strict=True, reason="ROB-property-13: file_patch() writes the path into the diff headers "
                                       "unquoted (quality/patch.py:87,82); git C-quotes a path holding a tab or a "
                                       "newline and refuses the unquoted header")
@example("t\tab.c")
@given(st.sampled_from(["t\tab.c", "doc/a\tb.md"]))
def test_rob_property_13_paths_git_must_quote(path):
    assert git_apply(path, "a\n", file_patch(path, "a\n", ["b"])) == "b\n"


@pytest.mark.xfail(strict=True, reason="ROB-property-13: bumps/patchfile.py:91 writes the gitlink path unquoted "
                                       "too; a submodule path holding a tab gives a patch git refuses")
def test_rob_property_13_gitlink_path_git_must_quote():
    a, b, path = "1" * 40, "2" * 40, "deps/a\tb"
    text = patch_text(_fact(path, a, b, "proposed", "plain"))
    with tempfile.TemporaryDirectory() as d:
        git("init", "-q", cwd=d)
        git("update-index", "--add", "--cacheinfo", "160000,%s,%s" % (a, path), cwd=d)
        Path(d, "p.diff").write_bytes(text.encode("utf-8"))
        git("apply", "--check", "--index", "p.diff", cwd=d)
