"""Q3: README accuracy against the tree, flows, and the generated Repository layout section."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, quality
from nanosoc_rm.cli import main
from nanosoc_rm.quality.readmes import layout_patch, layout_section
from nanosoc_rm.quality.source import WorktreeSource
from qfix import git, make_repo

pytestmark = pytest.mark.requirement("Q3")

SUB_SHA = "1" * 40
README = """# Demo SoC

Build with `make build`, lint with `make lint`, then simulate with `make sim`.

Sources are in `src/rtl/` and the guide is `docs/old-guide.md`; see also [the guide](docs/guide.md).

```sh
./scripts/run.sh --fast
git submodule update --init tidelink
```

The `ip/foo-ip` submodule holds the FOO IP; the `tidelink` submodule holds the die-to-die link.
Outputs land in `build/top.bit`. Replace `<board>` with your board, e.g. `targets/<board>/top.xdc`.
"""
FILES = {
    "README.md": README,
    "Makefile": "include mk/common.mk\n\nbuild:\n\tbash scripts/build.sh\n",
    "mk/common.mk": "lint:\n\t@echo lint\n",
    "scripts/build.sh": "#!/bin/sh\necho build\n",
    "scripts/run.sh": "#!/bin/sh\necho run\n",
    "docs/guide.md": "# Guide\n\nHow to use it.\n",
    "src/README.md": "# src\n\nSource code of the demo SoC.\n",
    "src/rtl/top.sv": "module top;\nendmodule\n",
    ".gitignore": "build/\n",
    ".gitmodules": '[submodule "tidelink"]\n\tpath = tidelink\n\turl = https://example.invalid/tidelink.git\n',
}


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "demo", FILES, executable=("scripts/run.sh",), gitlinks={"tidelink": SUB_SHA})


def test_exactly_the_three_planted_mismatches(repo):
    obs = quality.readme(None, path=repo)
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    got = sorted((m["kind"], m["token"]) for m in f["mismatches"])
    assert got == [("make_target", "sim"), ("path", "docs/old-guide.md"), ("submodule", "ip/foo-ip")], f["mismatches"]
    ok = {(c["kind"], c["token"]) for c in f["checks"] if c["status"] == "ok"}
    assert {("make_target", "build"), ("make_target", "lint"), ("path", "src/rtl//".rstrip("/") + "/"),
            ("path", "docs/guide.md"), ("script", "./scripts/run.sh"), ("submodule", "tidelink"),
            ("flow", "make build")} <= ok, ok
    ignored = {c["token"] for c in f["checks"] if c["status"] == "ignored"}
    assert "build/top.bit" in ignored, "a .gitignore'd output is not a stale path"


def test_generated_layout_matches_the_tree(repo):
    lay = layout_section(WorktreeSource(repo))
    assert [(e["path"], e["kind"]) for e in lay["entries"]] == [
        ("docs", "dir"), ("mk", "dir"), ("scripts", "dir"), ("src", "dir"), ("src/rtl", "dir"),
        ("tidelink", "submodule")]
    md = lay["markdown"]
    assert md.startswith("## Repository layout\n") and "- `src/`: Source code of the demo SoC." in md
    assert "  - `src/rtl/`" in md and "- `tidelink/`: submodule" in md


def test_layout_patch_applies_and_is_idempotent(repo, tmp_path):
    out = tmp_path / "layout.diff"
    res = CliRunner().invoke(main, ["quality", "readme", "--path", str(repo), "--patch", str(out)])
    assert res.exit_code == 1 and "Repository layout" in res.output
    git("apply", "--check", str(out), cwd=repo)
    git("apply", str(out), cwd=repo)
    text = (repo / "README.md").read_text()
    assert text.count("## Repository layout") == 1
    again = layout_patch(WorktreeSource(repo))
    assert again == "", "a README that already holds the generated section needs no patch"


def test_broken_flow_non_executable_script_and_unresolved_include(tmp_path):
    files = {
        "README.md": "Flash with `make flash`; start with `./tools/go.sh`; and `make check`.\n",
        "Makefile": "include $(shell pwd)/extra.mk\n\nflash:\n\tpython3 tools/flash.py --board kr260\n",
        "tools/go.sh": "#!/bin/sh\n",
    }
    repo = make_repo(tmp_path / "r", files)
    f = quality.readme(None, path=repo)["fact"]
    got = sorted((m["kind"], m["token"]) for m in f["mismatches"])
    assert got == [("flow", "make flash"), ("not_executable", "./tools/go.sh")], f["mismatches"]
    assert [(u["kind"], u["token"]) for u in f["unverified"]] == [("make_target", "check")], \
        "a target missing from a Makefile with an unresolved include is unverified, not missing"


def test_layout_section_entries_are_checked(tmp_path):
    files = {"README.md": "## Repository layout\n\n```\nrepo/\n├── src/\n│   └── rtl/\n└── gone/\n```\n",
             "src/rtl/a.sv": "module a; endmodule\n", "docs/x.md": "x\n"}
    repo = make_repo(tmp_path / "r", files)
    f = quality.readme(None, path=repo)["fact"]
    assert sorted((m["kind"], m["token"]) for m in f["mismatches"]) == [("layout", "gone"),
                                                                        ("layout_undocumented", "docs/")]


def test_makefile_reader_handles_patterns_inline_recipes_and_variables(tmp_path):
    from nanosoc_rm.quality.makefile import read_makefile
    files = {"Makefile": "TOOLS := scripts\nNAME ?= demo\npublish-%: ; echo $*\n$(NAME)-sim: ; $(TOOLS)/sim.sh\n"
                         ".PHONY: clean\nclean:\n\trm -rf build\n",
             "scripts/sim.sh": "#!/bin/sh\n"}
    repo = make_repo(tmp_path / "r", files)
    mk = read_makefile(WorktreeSource(repo), "Makefile")
    assert mk.has("publish-x") and mk.has("demo-sim") and mk.has("clean") and not mk.has("nosuch")
    assert mk.targets["demo-sim"] == ["$(TOOLS)/sim.sh"] and mk.unresolved == []


# Review Q-3: recipe and prose shapes from a real README that gave false mismatches.
FLOW_FILES = {
    "README.md": "# Flows\n\nRun `make help`, `make bscan-sim`, `make tx-gate` and `make broken`.\n\n"
                 "`ASIC/legacy/` is the old flow (driven by the `tidelink` submodule).\n\n"
                 "`0/0 pads survived` is a vacuous pass.\n",
    "Makefile": ".ONESHELL:\nTOP := $(abspath .)\n\nhelp:\n\t@echo usage\n\tawk '\n"
                "\t  /^##@[0-9]/ { n = substr($$0, 4) + 0; t = substr($$0, 4);\n"
                "\t                sub(/^[0-9]+[ \\t]*/, \"\", t); title[n] = t; cur = n; next }\n"
                "\t' Makefile\n\n"
                "bscan-sim:\n\tcd \"$(TOP)/verif/bscan\" && ./run.sh\n\n"
                "tx-gate:\n\tvcs -sverilog tb.sv -o simv_tx_gate\n\t./simv_tx_gate\n\n"
                "broken:\n\t./scripts/gone.sh\n",
    "verif/bscan/run.sh": "#!/bin/sh\necho sim\n",
    "ASIC/legacy/flow.tcl": "puts legacy\n",
    ".gitmodules": '[submodule "tidelink"]\n\tpath = tidelink\n\turl = https://example.invalid/tidelink.git\n',
}


def test_flow_and_script_checks_follow_cd_built_files_and_skip_prose(tmp_path):
    repo = make_repo(tmp_path / "flows", FLOW_FILES, executable=("verif/bscan/run.sh",),
                     gitlinks={"tidelink": SUB_SHA})
    f = quality.readme(None, path=repo)["fact"]
    got = sorted((m["kind"], m["token"], m["detail"]) for m in f["mismatches"])
    assert got == [("flow", "make broken", "its recipe in Makefile runs ./scripts/gone.sh, which is not in the tree")], \
        got
    ok = {(c["kind"], c["token"], c["detail"]) for c in f["checks"] if c["status"] == "ok"}
    assert ("flow", "make bscan-sim", "Makefile -> verif/bscan/run.sh") in ok, "the recipe's cd is followed"
