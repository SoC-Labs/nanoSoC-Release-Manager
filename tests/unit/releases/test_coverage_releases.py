"""Coverage angle (rob/coverage): the release modules' small decisions, without a host scene.

Version and id parsing, the --waiver syntax, the verdict ladder, finding a draft, listing drafts whose draft.json
is broken, the folder `--replace` may delete, the ledger's signature blanking (G2), pin drift, tag checks and an
earlier draft's unreadable lock."""
from __future__ import annotations


import pytest

from nanosoc_rm.config import Config
from nanosoc_rm.releases import builder as D
from nanosoc_rm.releases import checklist as cl
from nanosoc_rm.releases.core import (ReleaseError, ReleaseUsage, draft_path, exit_for, is_safe_id, ladder,
                                      parse_version)

C1, C2 = "1" * 40, "2" * 40


@pytest.fixture
def cfg(tmp_path):
    return Config({"service": {"data_dir": str(tmp_path / "data")}}, tmp_path / "rm.toml")


# ----- parsing -----------------------------------------------------------------------------------------------
def test_parse_version_accepts_only_plain_semver():
    assert parse_version("10.0.3") == (10, 0, 3)
    for bad in ("", None, "1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", "1.0.0 ", "\n1.0.0", "1.².0"):
        with pytest.raises(ReleaseUsage):
            parse_version(bad)


def test_ids_and_draft_paths(cfg):
    assert is_safe_id("eth-rc4") and is_safe_id("a.b_c")
    for bad in ("", "Eth", "-x", ".x", "a..b", "a/b", None, 3, "x" * 129):
        assert not is_safe_id(bad), bad
    assert draft_path(cfg, "eth", "1.2.3") == cfg.data_dir / "releases" / "eth" / "1.2.3"
    with pytest.raises(ReleaseUsage, match="configuration id"):
        draft_path(cfg, "../etc", "1.2.3")
    with pytest.raises(ReleaseUsage, match="MAJOR.MINOR.PATCH"):
        draft_path(cfg, "eth", "../../x")


def test_parse_waiver():
    assert D.parse_waiver(" a/b , c =  known issue ") == {"tree_paths": ["a/b", "c"], "reason": "known issue"}
    assert D.parse_waiver("*=all of it=really")["reason"] == "all of it=really"     # only the first = splits
    for bad in ("a/b", "=reason", " , =reason", "a/b=", "a/b=   "):
        with pytest.raises(ReleaseUsage):
            D.parse_waiver(bad)
    pin = {"tree_paths": ["x/y", "z"]}
    assert D._waived(pin, [{"tree_paths": ["*"]}]) and D._waived(pin, [{"tree_paths": ["q", "z"]}])
    assert not D._waived(pin, [{"tree_paths": ["x"]}]) and not D._waived(pin, [])


def test_ladder_and_exit_codes():
    assert ladder([]) == "UNVERIFIED" and ladder(["NOT-RUN"]) == "UNVERIFIED"   # nothing checked is never PASS
    assert ladder(["PASS", "NOT-RUN", "WARN"]) == "WARN"
    assert ladder(["PASS", "WAIVED", "WARN"]) == "WAIVED"
    assert ladder(["PASS", "INFRA", "WAIVED"]) == "INFRA"
    assert ladder(["FAIL", "UNVERIFIED"]) == "FAIL"
    assert ladder(["PASS", "MYSTERY"]) == "PASS"
    assert [exit_for(v) for v in ("PASS", "WARN", "WAIVED", "FAIL", "UNVERIFIED", "INFRA")] == [0, 0, 0, 1, 2, 2]


# ----- finding and listing drafts ----------------------------------------------------------------------------
def test_find_draft_by_path_or_configuration_version(cfg, tmp_path):
    d = cfg.data_dir / "releases" / "eth" / "1.0.0"
    d.mkdir(parents=True)
    (d / D.DRAFT_FILE).write_text("{}")
    assert D.find_draft(cfg, "eth/1.0.0") == d and D.find_draft(cfg, "/eth/1.0.0/") == d
    assert D.find_draft(cfg, str(d)) == d
    with pytest.raises(ReleaseUsage, match="no release draft"):
        D.find_draft(cfg, "eth/2.0.0")
    with pytest.raises(ReleaseUsage, match="no release draft"):
        D.find_draft(cfg, "a/b/c")
    with pytest.raises(ReleaseUsage):
        D.find_draft(cfg, "../1.0.0")                            # never a path outside the releases directory


def test_drafts_lists_broken_folders_with_their_problem(cfg):
    root = cfg.data_dir / "releases"
    assert D.drafts(cfg) == []                                    # no releases directory yet
    for conf, ver, text in (("eth", "1.0.0", "{not json"), ("eth", "1.1.0", '{"kind": "release_draft"}'),
                            ("other", "1.0.0", "\xff"), ("eth", "2.0.0", "[]")):
        (root / conf / ver).mkdir(parents=True)
        (root / conf / ver / D.DRAFT_FILE).write_text(text, encoding="latin-1")
    rows = D.drafts(cfg)
    assert len(rows) == 4 and all(r["doc"] is None and r["problems"] for r in rows)
    assert {r["version"] for r in D.drafts(cfg, "eth")} == {"1.0.0", "1.1.0", "2.0.0"}
    with pytest.raises(ReleaseError, match="not a valid release draft"):
        D.read_draft(root / "eth" / "1.1.0")
    with pytest.raises(ValueError):
        D.read_draft(root / "eth" / "1.0.0")                     # not JSON: the caller's exit 2


# ----- the folder --replace may delete -----------------------------------------------------------------------
def test_check_target_refuses_what_the_tool_did_not_write(tmp_path):
    t = tmp_path / "out"
    D._check_target(t, False)                                     # absent
    t.mkdir()
    D._check_target(t, False)                                     # empty
    (t / "x.v").write_text("module x; endmodule\n")
    with pytest.raises(ReleaseUsage, match="already holds"):
        D._check_target(t, False)
    with pytest.raises(ReleaseUsage, match="never deletes another directory"):
        D._check_target(t, True)
    (t / D.DRAFT_FILE).write_text("{}")
    D._check_target(t, True)                                      # an earlier draft: may be replaced
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(ReleaseUsage, match="not a directory"):
        D._check_target(f, True)
    link = tmp_path / "link"
    link.symlink_to(t)
    with pytest.raises(ReleaseUsage, match="not a directory"):
        D._check_target(link, True)                               # a symlink to a draft is still refused


def test_safe_name_never_escapes_the_folder():
    assert D._safe_name("../../etc/passwd") == "_.._etc_passwd"
    assert D._safe_name("...") == "file" and D._safe_name("") == "file"
    assert D._safe_name(".hidden.tar") == "hidden.tar" and D._safe_name("ok-1.2_x.tgz") == "ok-1.2_x.tgz"


# ----- the ledger's signatures (G2) --------------------------------------------------------------------------
def _release():
    return {"component": "soc", "version": "1.0.0", "configuration": "soc-v1", "commit": C1, "kind": "soc"}


def test_signing_the_ledger_only_changes_signature_cells():
    text = cl.ledger(_release(), [D.check("a", "PASS", "x|y")])
    assert "x/y" in text                                          # a | in a detail cannot add a column
    blank, roles = cl.unsigned(text)
    assert blank == text and roles == []
    signed = text.replace("| Release approver | | | |", "| Release approver | Ann | 2026-10-14 | AB |")
    blank, roles = cl.unsigned(signed)
    assert blank == text and roles == ["Release approver"]
    # an edit above the signatures (a verdict changed by hand) survives the blanking, so the md5 still differs
    forged = signed.replace("| `a` | PASS |", "| `a` | FAIL |")
    assert cl.unsigned(forged)[0] != text
    # a signature row added by hand is blanked but stays a new line: still not the drafted file
    extra = signed + "| Someone else | Bob | today | B |\n"
    assert cl.unsigned(extra)[0] != text


def test_checklist_lists_open_checks_and_commands():
    checks = [D.check("a", "PASS", "ok"), D.check("b", "UNVERIFIED", "missing"), D.check("c", "NOT-RUN", "n/a")]
    steps = [{"step": "Tag it", "who": "a person", "command": "git tag x\ngit push o x"}, {"step": "Sign", "who": "D"}]
    text = cl.checklist(_release(), checks, steps)
    assert "`b` (UNVERIFIED)" in text and "`a`" not in text and "`c`" not in text
    assert "   git push o x" in text and "2. [ ] Sign (D)" in text
    assert "- nothing open" in cl.checklist(_release(), checks[:1], [])
    assert len(cl.approvals("soc", False, False)) == 1 and "second reviewer" in cl.approvals("soc", True, True)[0]["who"]


def test_clone_checks_from_stored_gates():
    facts = [{"subject": "s1", "fact": {"gates": [{"id": "t0.pins.fetchable", "verdict": "PASS"}]}}]
    out = {c["id"]: c["verdict"] for c in cl.clone_checks(facts, None)}
    assert out == {"clone.clean": "PASS", "clone.upgrade-path": "NOT-RUN"}
    out = {c["id"]: c["verdict"] for c in cl.clone_checks([], {"tag": "v0"})}
    assert out == {"clone.clean": "UNVERIFIED", "clone.upgrade-path": "UNVERIFIED"}


# ----- pins and tags -----------------------------------------------------------------------------------------
def _pin(tp, commit=C1, state="ON_BRANCH", by=None):
    return {"tree_paths": [tp], "commit": commit, "state": state, "protection": {"by": by} if by else None}


def test_pin_drift_names_every_kind_of_change():
    old = [_pin("a"), _pin("b"), _pin("c"), _pin("d", by="tag"), _pin("e")]
    new = [_pin("a"), _pin("b", C2), _pin("c", state="BRANCH_ONLY"), _pin("d", by=None), _pin("f")]
    assert D._pin_drift(old, new) == ["pin e is gone from the tree", "pin f is new in the tree",
                                      "b: commit 111111111111 -> 222222222222", "c: state ON_BRANCH -> BRANCH_ONLY",
                                      "d: protection tag -> None"]
    assert D._pin_drift(old, old) == []


def test_tag_check_never_moves_a_tag():
    assert D._tag_check("t", "v1.0.0", C1, {}, "C10")["verdict"] == "UNVERIFIED"
    assert D._tag_check("t", "v1.0.0", C1, {"v1.0.0": C1}, "C10")["verdict"] == "PASS"
    bad = D._tag_check("t", "v1.0.0", C1, {"v1.0.0": C2}, "C10")
    assert bad["verdict"] == "FAIL" and "never moved" in bad["detail"]


def test_generator_pin_choice():
    pins = [{"component": "nanosoc_gen", "depth": 2, "tree_paths": ["b/gen"], "commit": C1},
            {"component": "nanosoc_gen", "depth": 1, "tree_paths": ["z/gen"], "commit": C2},
            {"component": "ip", "depth": 1, "tree_paths": ["ip"], "commit": C1}]
    assert D._generator_pin(pins, None)["commit"] == C2           # the shallowest generator
    assert D._generator_pin(pins, "b/gen")["commit"] == C1
    assert D._generator_pin(pins, "nope") is None and D._generator_pin(pins[2:], None) is None


# ----- an earlier draft's lock -------------------------------------------------------------------------------
def test_lock_of_reads_the_complete_lock_first(tmp_path):
    assert D._lock_of(tmp_path) is None
    (tmp_path / D.DRAFT_LOCK).write_text("pins: []\nwhich: draft\n")
    assert D._lock_of(tmp_path)["which"] == "draft"
    (tmp_path / D.LOCK).write_text("pins: []\nwhich: complete\n")
    assert D._lock_of(tmp_path)["which"] == "complete"


@pytest.mark.parametrize("text", ["pins: [\n", "pins: []\npins: []\n", "- just\n- a list\n"])
def test_an_unreadable_lock_reads_as_no_lock(tmp_path, text):
    (tmp_path / D.LOCK).write_text(text)
    lock = D._lock_of(tmp_path)
    assert lock is None or isinstance(lock, dict)
    (lock or {}).get("pins")                                      # what _notes and verify do with it


@pytest.mark.parametrize("what", ["parse_version", "is_safe_id", "draft_job", "job_id"])
def test_a_trailing_newline_is_not_a_version_or_an_id(cfg, what):
    from nanosoc_rm import jobs
    if what == "parse_version":
        with pytest.raises(ReleaseUsage):
            parse_version("1.0.0\n")
    elif what == "is_safe_id":
        assert not is_safe_id("eth\n")
    elif what == "draft_job":
        with pytest.raises(jobs.JobUsage):                        # a POST /releases/ form field, as the web passes it
            jobs._params("release-draft", {"configuration": "eth", "version": "1.0.0\n"})
    else:
        with pytest.raises(jobs.JobUsage):
            jobs._params("walk", {"configuration": "eth\n"})
