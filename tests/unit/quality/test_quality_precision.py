"""Wave D quality precision (CR-WD-Q1..Q5): false positives sampled from eth, multicore and tidelink stay quiet,
their true-positive twins still fire; severity per kind, the decoration bulk rule, stored context, README output
blocks and URLs, and the "repos in my configurations" filter. Each fixture line is a shortened real case."""
from __future__ import annotations

import pytest

from nanosoc_rm import contracts, quality
from nanosoc_rm.quality.findings import BULK_KINDS, BULK_RULE, SEVERITY_BY_KIND
from qfix import make_repo

pytestmark = [pytest.mark.requirement("Q2"), pytest.mark.requirement("Q3")]

SUB_SHA = "2" * 40


def _findings(tmp_path, files, kind=None, gitlinks=None):
    repo = make_repo(tmp_path / "r", files, gitlinks=gitlinks)
    obs = quality.comments(None, path=repo)
    assert contracts.validate_observation(obs) == []
    fs = obs["fact"]["findings"]
    return [f for f in fs if kind is None or f["kind"] == kind], obs


# ----- commented_out_code: prose that looks like code ------------------------------------------------------
PROSE_LOOKS_LIKE_CODE = {
    # a shell comment that is an English sentence after an assignment (multicore ci/lib.sh:69)
    "lib.sh": 'CI_WARN=0\n\n# CI_APPEND=1 means "somebody upstream already started this run".\n'
              "# Without it, each script would truncate the shared file.\necho ok\n",
    # an assignment then plain words (tidelink imp/hw_gate/repeat_ab.sh:4)
    "ab.sh": "#!/bin/sh\n# n=1 cannot separate a flake from a real conversion of the signature\necho ab\n",
    # an option list in a usage header (multicore pynq/scripts/hw_bringup.sh:78)
    "hw.sh": "#!/bin/sh\n# Options:\n#   SKIP_DEPLOY=1 / SKIP_UART=1 / SKIP_DAP=1\necho hw\n",
    # hyphenated words parse as a call (tidelink cocotb/lint/xdc_lint.py:114)
    "lint.py": "def f(raw, pats):\n    # Allow-list (per-line)\n    if any(p.search(raw) for p in pats):\n"
               "        return 1\n    return 0\n",
    # an HDL bit range in a Python comment (multicore python/tests/test_ethernet.py:264)
    "regs.py": "REGS = {\n    18: 0x8000,\n    # prime[7:0]<<24 | timeout_count<<16 | phy_addr<<8\n"
               "    19: (0x21 << 24) | (7 << 16),\n}\n",
    # `name (words)`: no Python call has a space before its parenthesis (tidelink kr260_onchip_soak.py:23)
    "soak.py": "# die_a (sender / inst0)\nDIE_A = 0\n",
    # the end of an unfinished sentence (tidelink test_fc_adapter_arbiter.py:328)
    "arb.py": "# the returner word vanished while the returner saw\n# hready=1.\n\nX = 1\n",
    # a label for the next line, which uses the name (tidelink test_tidelink_pair_doorbell.py:443)
    "door.py": "async def f(apb, APB_R8_SLOT0, R8_SLOT0_TRAIN_ONLY):\n    # slot0 = 0x1\n"
               "    await apb.write(APB_R8_SLOT0, R8_SLOT0_TRAIN_ONLY)\n",
    # placeholders a TODO introduces (tidelink fpga/targets/mps3/mps3_tidelink.xdc:99)
    "pins.tcl": "## TX data lanes\n# TODO: Replace TODO_PIN_TX_x with confirmed FPGA package pins\n"
                "# set_property -dict {PACKAGE_PIN TODO_PIN_TX_0 IOSTANDARD LVCMOS33} [get_ports {pad_tx[0]}]\n"
                "# set_property -dict {PACKAGE_PIN TODO_PIN_TX_1 IOSTANDARD LVCMOS33} [get_ports {pad_tx[1]}]\n"
                "puts done\n",
    # indented example lines under a `label:` line (multicore pynq/Makefile:1427)
    "usage.mk": "# leasing:\n#   TOKEN=$(make lease-acquire BOARD=b | tail -1 | sed 's/.*token=//')\n"
                "#   make bringup-b\n#   make lease-release BOARD=b LEASE_TOKEN=$TOKEN\nX ?= 1\n",
    # a Tcl command name in a sentence (tidelink syn/asic/fusion-compiler/scripts/3_clock.tcl:34)
    "clk.tcl": "# set_qor_strategy in U-2022.12 only accepts -metric\nputs clk\n",
    # hyphenated words in an HDL comment (tidelink src/rtl/asic/tidelink_dft_wrapper.sv:130)
    "w.sv": "module w(input a, output b);\n  // S2 scaffold: PHY v2 select pass-through (default 0 = bit-identical;\n"
            "  assign b = a;\nendmodule\n",
}
DEAD_CODE = {
    # real commented-out code: kept as findings (multicore test_soc_ptp_slave.py:321, tidelink Makefile:42)
    "slave.py": "async def t(dut):\n    x = 1\n    # cocotb.start_soon(mii_clock_gen(dut))  # retired\n\n"
                "    return x\n",
    "Makefile": "all:\n\t@echo all\n# SIM_ARGS     += +vcs+dumpvars+waves.vcd\n\ninclude other.mk\n",
    "t.xdc": "# Proven safe to disable.\n# set _regs [get_cells -hier -filter {NAME =~ \"*stage_reg[*]\"}]\n"
             "# set_max_delay -datapath_only -from [get_clocks wclk] -to $_regs 70.000\n\nset x 1\n",
    "near.py": "def f():\n    # y = compute(1)\n    y = compute(2)\n    return y\n",
}


def test_prose_that_looks_like_code_is_not_commented_out_code(tmp_path):
    fs, _ = _findings(tmp_path, PROSE_LOOKS_LIKE_CODE, "commented_out_code")
    assert fs == [], ["%s:%d %s" % (f["path"], f["line"], f["evidence"]) for f in fs]


def test_real_commented_out_code_still_fires(tmp_path):
    fs, _ = _findings(tmp_path, DEAD_CODE, "commented_out_code")
    assert {f["path"] for f in fs} == set(DEAD_CODE), [(f["path"], f["evidence"]) for f in fs]


# ----- unknown_identifier: only names in code-like text, never a variant of a name that exists ------------
VARIANTS = {
    # each comment names a variant of a name its own file holds (the close match is looked up in the same file)
    "core.sv": "module core(input role_strap_i, input clk);\n"
               "  wire u_rx_ptp_det;\n  wire u_cpu0_dbg_bridge;\n  wire gpiotx_7_io_link_data;\n  wire s_por_gate;\n"
               "  wire timestamp;\n  wire test_15;\n"
               "  // role_strap is tied low on die A.\n"                       # a port-suffix variant (family)
               "  // (u_mac/u_rx_ptp_det, u_tx_ptp_det) run in the MII domain\n"  # a tx/rx sibling (eth sdc:15)
               "  // 0xA0 -> cpu_0_dbg_bridge -> DBGAHB\n"                    # u_ prefix and a digit (family)
               "  // gpiotx_N_io_link_data = tx_lane_en_N ? x : 0\n"          # a placeholder letter (family)
               "  // cocotb holds {m,s}_por_gate low\n"                       # a fragment of an expansion
               "  // {16'd0, time_stamp[79:0]}\n"                             # time_stamp / timestamp (family)
               "  // test_10 sees entry first\n"                              # a numbered sibling (family)
               "endmodule\n",
    # no close match in the file: a backticked name would be reported unless it is a variant or a prose word
    "defs.py": "tb_top = 1\nsim_build = 2\nspacingtable = 3\n",
    "notes.py": "# Two `sim_builds`, and both `tb_tops` release identically.\n"   # plurals of names in defs.py
                "# English. `Spacingtable` in a sentence is a word.\n"        # a case variant
                "# English. `Frobnicator` in a sentence is a word, not a name.\n"  # not code-shaped, prose line
                "X = 1\n",
}


def test_variants_of_known_names_and_prose_words_are_not_unknown_identifiers(tmp_path):
    fs, _ = _findings(tmp_path, VARIANTS, "unknown_identifier")
    assert fs == [], [(f["identifier"], f.get("did_you_mean")) for f in fs]


def test_a_renamed_identifier_still_fires(tmp_path):
    files = {"m.py": "def compute_total(values):\n    # compute_totl() adds the values; `old_helper()` rounds\n"
                     "    return sum(values)\n",
             "r.sv": "module r(input chip_core_swreset_pulse);\n"
                     "  // ~chip_core_reset_pulse gates the reset\nendmodule\n"}
    fs, _ = _findings(tmp_path, files, "unknown_identifier")
    got = {(f["identifier"], f.get("did_you_mean")) for f in fs}
    assert ("compute_totl", "compute_total") in got and ("old_helper", None) in got
    assert ("chip_core_reset_pulse", "chip_core_swreset_pulse") in got


# ----- severity, bulk rule, context ---------------------------------------------------------------------
def test_every_finding_has_its_kinds_severity_context_and_the_bulk_rule(tmp_path):
    files = {"tool.py": "# File: tool.py\n# Author: someone\n# Copyright 2024 SoC Labs\nX = 1\n",
             "c.py": "# ------------------------------------------------\n# Helpers\n"
                     "# ------------------------------------------------\nX = 1\n",
             "a.py": "def f(x):\n    # y = compute(x)\n    # return y * 2\n    return x\n"}
    fs, obs = _findings(tmp_path, files)
    kinds = {f["kind"] for f in fs}
    assert {"banner", "boilerplate", "commented_out_code"} <= kinds
    for f in fs:
        assert f["severity"] == SEVERITY_BY_KIND[f["kind"]]
        assert (f.get("bulk") == BULK_RULE) == (f["kind"] in BULK_KINDS)
        ctx = f["context"]
        ed = f.get("edit") or {"start": f["line"], "end": f["end_line"]}
        assert ctx["start"] <= ed["start"] and ed["end"] < ctx["start"] + len(ctx["lines"])
        text = (tmp_path / "r" / f["path"]).read_text().split("\n")
        assert ctx["lines"] == text[ctx["start"] - 1:ctx["start"] - 1 + len(ctx["lines"])]
    rule = obs["fact"]["bulk_rules"]
    assert rule == [{"rule": "decoration", "kinds": ["banner", "boilerplate"], "count": 2, "files": 2,
                     "edits": 2, "by_kind": {"banner": 1, "boilerplate": 1}}]


# ----- README: output, URLs, placeholders, build outputs, submodules ----------------------------------
README = """# Demo

```sh
$ ./scripts/run.sh
results/out_table.txt written
flist/flatten.py ok
```

```
flist/flatten.py   SoC flist  -> absolute paths
make build
```

```console
$ make build
build/missing_dir/ created
```

```bash
python3 -m venv .venv-docs
. .venv-docs/bin/activate
source ~/elsewhere/set_env.sh
```

| Submodule | URL | Branch |
|---|---|---|
| `tidelink` | `git.example.org/soclabs/tidelink.git` | `main` |

The `nested_ip` submodule is fetched by the recursive init. Source at release: `72c280b` + submodule `17160eb`.
Create `NN_name.sh` for a new test.
Outputs: `top.bit`, `design.xsa` and `timing.rpt`. Read `ip_wrapper.sv`. Link: [the IP](tidelink/docs/x.md).
Make a venv first; it holds `.venv/lib/site.py`. Still stale: `docs/gone.md`, `gone_dir/`, `run_all.sh`, and [old](docs/old.md).
"""


def test_readme_output_urls_venvs_build_outputs_and_submodule_names_are_not_mismatches(tmp_path):
    files = {"README.md": README, "Makefile": "build:\n\t@echo build\n", "scripts/run.sh": "#!/bin/sh\necho\n",
             ".gitmodules": '[submodule "tidelink"]\n\tpath = tidelink\n\turl = https://example.invalid/t.git\n'}
    repo = make_repo(tmp_path / "r", files, executable=("scripts/run.sh",), gitlinks={"tidelink": SUB_SHA})
    obs = quality.readme(None, path=repo)
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    got = sorted((m["kind"], m["token"]) for m in f["mismatches"])
    # a stale directory and a stale script stay mismatches even with submodules (only HDL/IP file names are not)
    assert got == [("path", "docs/gone.md"), ("path", "docs/old.md"), ("path", "gone_dir/"),
                   ("path", "run_all.sh")], f["mismatches"]
    status = {c["token"]: c["status"] for c in f["checks"]}
    assert status["./scripts/run.sh"] == "ok" and status["build"] == "ok"
    assert status["top.bit"] == status["design.xsa"] == status["timing.rpt"] == "ignored"
    assert status["ip_wrapper.sv"] == "unverified" and status["nested_ip"] == "unverified"
    assert "72c280b" not in status and "NN_name.sh" not in status
    assert status["tidelink/docs/x.md"] == "ignored"
    for token in ("results/out_table.txt", "flist/flatten.py", "build/missing_dir/", ".venv-docs/bin/activate",
                  "git.example.org/soclabs/tidelink.git", "main", ".venv/lib/site.py"):
        assert token not in status, token


def test_a_bare_name_is_still_missing_in_a_repo_without_submodules(tmp_path):
    repo = make_repo(tmp_path / "r", {"README.md": "# D\n\nRead `ip_wrapper.sv` and `out.bit`.\n"})
    f = quality.readme(None, path=repo)["fact"]
    assert [(m["kind"], m["token"]) for m in f["mismatches"]] == [("path", "ip_wrapper.sv")]


# ----- restates_code: a heading over a group, or words only in a string literal -------------------------
def test_group_headings_and_string_labels_are_not_restating(tmp_path):
    files = {
        # a heading over two clock commands (multicore syn/asic/genus-innovus/inputs/constraints.sdc:34)
        "c.tcl": "# QSPI clocks\ncreate_generated_clock -name QSPI_SCLK -source [get_ports CLK] qspi_clock\n"
                 "create_generated_clock -name QSPI_SCLK_o -source [get_pins qspi_clock_div] qspi_clock\n",
        # the words are only in a YAML desc string (eth sys_desc/tidelink_top.yaml:88)
        "p.yaml": "params:\n  # FC node\n  FC_DATA_W: { type: int, default: 48, desc: \"FC node data width\" }\n\n"
                  "  OTHER: 1\n",
    }
    fs, _ = _findings(tmp_path, files, "restates_code")
    assert fs == [], [(f["path"], f["evidence"]) for f in fs]
    one = {"e.tcl": "set x 1\n\n# Add END CAPS\nadd_endcaps -start_row_cap CAPCELL -end_row_cap CAPCELL -prefix ENDCAP\n"
                    "\nputs done\n"}                     # eth ASIC/genus-innovus/scripts/power_plan.tcl:163
    fs, _ = _findings(tmp_path / "one", one, "restates_code")
    assert [f["path"] for f in fs] == ["e.tcl"], "a comment that says what its one line does still fires"
    verb = {"env.sv": "class e;\n  function build();\n    // Create APB config agents\n"
                      "    a_cfg_apb_agent = apb_master_agent::type_id::create(\"a_cfg_apb_agent\", this);\n"
                      "    b_cfg_apb_agent = apb_master_agent::type_id::create(\"b_cfg_apb_agent\", this);\n"
                      "  endfunction\nendclass\n"}     # tidelink uvm/tidelink_system/env/tidelink_system_env.sv:118
    fs, _ = _findings(tmp_path / "verb", verb, "restates_code")
    assert [f["path"] for f in fs] == ["env.sv"], "a verb-led comment over a group still restates it"


# ----- review fixes: recall the precision rules must keep, and the decisive rules each pinned -------------------
def test_a_register_suffix_rename_is_not_a_sibling(tmp_path):
    """`count_q` -> `count_r` is how RTL renames a register (tidelink tidelink_lane_deskew.sv:449 names
    `d0_min_s`; the code declares only `d0_min_v`): a stale rename, still reported. tx/rx stays a sibling."""
    files = {"c.v": "module c(input clk, output reg [3:0] count_r);\n  // count_q holds the running total\n"
                    "  always @(posedge clk) count_r <= count_r + 1;\nendmodule\n",
             "d.sv": "module d(input clk);\n  logic [3:0] d0_min_v;\n  // d0_min_s is the smallest lane-0 delay\n"
                     "  always_ff @(posedge clk) d0_min_v <= 4'd0;\nendmodule\n"}
    fs, _ = _findings(tmp_path, files, "unknown_identifier")
    got = {(f["identifier"], f.get("did_you_mean")) for f in fs}
    assert got == {("count_q", "count_r"), ("d0_min_s", "d0_min_v")}, got


def test_a_subtraction_in_a_statement_is_code_not_hyphenated_prose(tmp_path):
    from nanosoc_rm.quality.findings import is_code_line
    assert is_code_line("verilog", "assign data_out = data-offset;") == (True, True)
    assert is_code_line("verilog", "q <= count-offset;") == (True, True)
    assert is_code_line("python", "total = price-discount")[0]
    assert is_code_line("python", "print (total)")[0]
    # the prose twins stay prose
    assert not is_code_line("verilog", "S2 scaffold: PHY v2 select pass-through (default 0 = bit-identical;")[0]
    assert not is_code_line("python", "Allow-list (per-line)")[0]
    assert not is_code_line("python", "die_a (sender / inst0)")[0]
    assert not is_code_line("shell", "SKIP_DEPLOY=1 / SKIP_UART=1 / SKIP_DAP=1")[0]
    files = {"a.v": "module a(input clk, input [3:0] count, output reg [3:0] q);\n  always @(posedge clk) begin\n"
                    "    q <= count;\n    // q <= count-offset;\n    // data_out <= data-offset;\n  end\nendmodule\n"}
    fs, _ = _findings(tmp_path, files, "commented_out_code")
    assert [f["path"] for f in fs] == ["a.v"], fs


def test_a_long_decoration_edit_stores_no_context(tmp_path, monkeypatch):
    """BULK_CONTEXT_MAX_LINES: a decoration edit longer than the cap stores no lines (uncapped, the tidelink fact
    tripled); one within the cap keeps them. The cap is lowered so a short fixture spans both sides of it."""
    from nanosoc_rm.quality import findings as fmod
    files = {"two.py": "# File: two.py\n# Author: someone\n# Copyright 2024 SoC Labs\nX = 1\n",
             "one.sv": "// ===== Setup =====\nmodule m;\nendmodule\n"}
    fs, _ = _findings(tmp_path / "wide", files)
    bulk = {f["path"]: f for f in fs if f.get("bulk") == BULK_RULE and f.get("edit")}
    span = {p: f["edit"]["end"] - f["edit"]["start"] + 1 for p, f in bulk.items()}
    assert span["one.sv"] == 1 and span["two.py"] > 1, span
    assert all("context" in f for f in bulk.values())
    monkeypatch.setattr(fmod, "BULK_CONTEXT_MAX_LINES", 1)
    fs, _ = _findings(tmp_path / "capped", files)
    bulk = {f["path"]: f for f in fs if f.get("bulk") == BULK_RULE and f.get("edit")}
    assert "context" not in bulk["two.py"] and "context" in bulk["one.sv"]


def _readme_status(tmp_path, readme, files=None):
    fs = {"README.md": readme, "Makefile": "build:\n\t@echo build\n"}
    fs.update(files or {})
    obs = quality.readme(None, path=make_repo(tmp_path / "r", fs))
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    return {c["token"]: c["status"] for c in f["checks"]}, [(m["kind"], m["token"]) for m in f["mismatches"]]


def test_untagged_fence_runs_repo_scripts_and_env_prefixed_commands(tmp_path):
    readme = ("# D\n\n```\nscripts/missing_tool.sh --all\nV=1 make build\n"
              "flist/flatten.py   SoC flist  -> absolute paths\n```\n")
    status, missing = _readme_status(tmp_path, readme)
    assert ("script", "scripts/missing_tool.sh") in missing, missing
    assert status.get("build") == "ok", status
    assert "flist/flatten.py" not in status


def test_a_console_fence_without_prompts_is_output_and_urls_are_not_paths(tmp_path):
    readme = ("# D\n\n```console\n./scripts/nope.sh\n```\n\n"
              "Clone `example.org/soclabs/tools.git` and read `example.com/docs/guide.md`.\n")
    status, missing = _readme_status(tmp_path, readme)
    assert missing == [], missing
    assert not any("nope" in t or "example" in t for t in status), status
