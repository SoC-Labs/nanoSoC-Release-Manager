"""Q1: the per-language lexers (comment markers inside strings never count) and the density report."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, quality
from nanosoc_rm.cli import main
from nanosoc_rm.quality.lexers import lex
from qfix import make_repo

pytestmark = pytest.mark.requirement("Q1")

C, M, D, B = "code", "comment", "docstring", "blank"

# Each fixture: the text and the expected kind of every line. The tricky lines hold comment markers in strings.
TRICKY = {
    "python": ('"""Module docstring."""\n'
               'import os  # trailing comment\n'
               's = "# not a comment"\n'
               "t = '''\n"
               '# inside a string, not a comment\n'
               "'''\n"
               '# a real comment\n'
               '\n'
               'def f():\n'
               '    """Function docstring\n'
               '    over two lines."""\n'
               "    return '#'\n",
               [D, C, C, C, C, C, M, B, C, D, D, C]),
    "verilog": ('// header comment\n'
                'module m; /* block\n'
                '   still block */ assign a = 8\'h2F; // tail\n'
                '  initial $display("// not a comment /* nor this");\n'
                '/*\n'
                '\n'
                '*/\n'
                'endmodule\n',
                [M, C, C, C, M, M, M, C]),
    "c": ('#include <stdio.h>\n'
          "char q = '\"'; // a quote char\n"
          'const char *s = "/* not */ // not";\n'
          '/*\n'
          ' * block\n'
          ' */\n'
          'int x; /* trailing */\n',
          [C, C, C, M, M, M, C]),
    "shell": ('#!/bin/bash\n'
              'echo "a # not" \'b # not\' ${#arr[@]} a#b\n'
              '# a comment\n'
              'cat <<END\n'
              '# inside a here-document\n'
              'END\n'
              'x=1 # trailing\n',
              [C, C, M, C, C, C, C]),
    "make": ('# a comment\n'
             'all: build # trailing\n'
             '\techo "# not a make comment"\n'
             'X = a\\#b\n',
             [M, C, C, C]),
    "tcl": ('# a comment\n'
            'set x "a # b" ;# trailing\n'
            'puts {# braced, not a comment}\n'
            'proc p {} {\n'
            '  # a comment in a body\n'
            '}\n',
            [M, C, C, C, M, C]),
    "yaml": ('# a comment\n'
             'a: "x # y"\n'
             'b: |\n'
             '  # inside a block scalar\n'
             '  text\n'
             'c: d # trailing\n'
             'url: http://example.invalid/x#frag\n',
             [M, C, C, C, C, C, C]),
    "toml": ('# a comment\n'
             'a = "x # y"\n'
             'b = """\n'
             '# inside a multi-line string\n'
             '"""\n'
             "c = 'lit # eral' # trailing\n",
             [M, C, C, C, C, C]),
}


@pytest.mark.parametrize("language", sorted(TRICKY))
def test_lexer_ignores_comment_markers_in_strings(language):
    text, kinds = TRICKY[language]
    assert lex(language, text).kinds == kinds


COMMENT_LINES = {"python": [2, 7], "verilog": [1, 2, 3, 5], "c": [2, 4, 7], "shell": [3, 7], "make": [1, 2],
                 "tcl": [1, 2, 5], "yaml": [1, 6], "toml": [1, 6]}


@pytest.mark.parametrize("language", sorted(TRICKY))
def test_markers_in_strings_never_start_a_comment(language):
    """Kinds alone can hide a false trailing comment on a code line; the comment list cannot."""
    assert sorted({c.line for c in lex(language, TRICKY[language][0]).comments if c.style != "doc"}) == \
        COMMENT_LINES[language]


def test_inline_comments_are_counted_but_the_line_is_code():
    lx = lex("verilog", TRICKY["verilog"][0])
    assert [(c.line, c.inline) for c in lx.comments if c.style == "line"] == [(1, False), (3, True)]


def test_markdown_is_documentation():
    lx = lex("markdown", "# Title\n\nSome prose.\n\n```sh\nmake all  # x\n```\n<!-- note -->\n")
    assert lx.kinds == ["prose", "blank", "prose", "blank", "code", "code", "code", "comment"]


FILES = {
    "src/a.py": "\n".join(["# c%d" % i for i in range(30)] + ["x = %d" % i for i in range(10)]) + "\n",
    "src/b.py": "\n".join(["x = %d  # inline" % i for i in range(40)]) + "\n",
    "rtl/top.sv": "// one\nmodule top;\n  assign a = b;\nendmodule\n",
    "README.md": "# R\n\nA short readme with seven words here.\n",
}


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "r", FILES)


def test_density_counts_ratios_and_outliers(repo):
    obs = quality.density(None, path=repo)
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    rows = {r["path"]: r for r in f["files"]}
    assert (rows["src/a.py"]["comment"], rows["src/a.py"]["code"], rows["src/a.py"]["ratio"]) == (30, 10, 0.75)
    assert rows["src/b.py"]["ratio"] == 0.0 and rows["src/b.py"]["inline"] == 40
    assert f["outliers"] == ["src/a.py"], "a.py is over 0.40 with 40 lines; top.sv is under min_lines"
    assert rows["src/a.py"]["excess_lines"] == 30 - int(0.4 * 10 / 0.6)
    langs = {l["language"]: l for l in f["languages"]}
    assert set(langs) == {"python", "verilog"} and langs["python"]["outliers"] == 1
    assert f["docs"]["files"] == 1 and "README.md" not in rows
    assert {d["dir"] for d in f["dirs"]} == {"src", "rtl"}, "no code file at the root, so no \".\" row"


def test_thresholds_come_from_quality_yaml(repo, tmp_path):
    qy = tmp_path / "quality.yaml"
    qy.write_text("schema: 1\nlanguages:\n  python: {max_ratio: 0.8}\n")
    f = quality.density(None, path=repo, thresholds=qy)["fact"]
    assert f["outliers"] == [] and f["thresholds"]["origin"] == "quality.yaml"
    qy.write_text("schema: 1\nlanguages:\n  cobol: {max_ratio: 0.8}\n")
    with pytest.raises(Exception, match="cobol"):
        quality.density(None, path=repo, thresholds=qy)


def test_cli_json_gives_one_row_per_file(repo):
    res = CliRunner().invoke(main, ["quality", "density", "--path", str(repo), "--json"])
    rows = [json.loads(x) for x in res.output.splitlines()]
    assert res.exit_code == 1, "an outlier is a finding"
    assert sorted(r["path"] for r in rows) == ["rtl/top.sv", "src/a.py", "src/b.py"]
    text = CliRunner().invoke(main, ["quality", "density", "--path", str(repo)]).output
    assert "python" in text and "verilog" in text and "src/a.py" in text


def test_ref_reads_git_objects_not_the_work_tree(repo):
    (repo / "src" / "a.py").write_text("x = 1\n")                 # uncommitted change
    at_head = quality.density(None, path=repo, ref="main")["fact"]
    on_disk = quality.density(None, path=repo)["fact"]
    assert {r["path"]: r["comment"] for r in at_head["files"]}["src/a.py"] == 30
    assert {r["path"]: r["comment"] for r in on_disk["files"]}["src/a.py"] == 0
    assert at_head["worktree"] is False and on_disk["worktree"] is True
