"""Mutation-killing tests for web.verdict (robustness angle: mutation).

Each test pins one decision of spec 2.2 that a surviving mutant of nanosoc_rm/web/verdict.py showed no test
reached (scripted AST mutation run, 2026-10-14). The mutant each test kills is named in its docstring."""
from __future__ import annotations

import pytest

from nanosoc_rm.web import verdict as V

from test_verdict import doc, gate, pin


def test_no_result_and_empty_docs():
    """verdict_of({}) / causes(None) / deciding(None).infra: kills `return NO_RESULT -> None`, `return [] -> None`,
    `False -> True` in the no-result Decision."""
    assert V.verdict_of(None) == V.NO_RESULT and V.verdict_of({}) == V.NO_RESULT
    assert V.verdict_of({"fact": {}}) == V.NO_RESULT
    assert V.causes(None) == [] and V.causes({}) == []
    d = V.deciding(None)
    assert d.infra is False and d.kind == "no-result" and d.gate is None


@pytest.mark.parametrize("n, want", [
    (3, "p0, p1, p2"),
    (4, "p0, p1, p2 and 1 more"),
    (6, "p0, p1, p2 and 3 more"),
])
def test_paths_shown_cap(n, want):
    """_join: 3 shown, then "and N more". Kills `<= -> <`, `- -> +`, `_PATHS_SHOWN 3 -> 4`."""
    pins = [pin("p%d" % i, manifest="fail") for i in range(n)]
    (c,) = V.causes(doc("x", [gate("t0.manifest.valid", "FAIL")], pins))
    assert c.label == "manifest invalid in " + want


def test_unverified_items_count_as_failing_paths():
    """_failing reads fail AND unverified items. Kills dropping 'unverified' from the tuple."""
    pins = [pin("a", reachable="unverified"), pin("b", reachable="fail"), pin("c")]
    (c,) = V.causes(doc("x", [gate("t0.pins.reachable", "UNVERIFIED")], pins, verdict="UNVERIFIED"))
    assert c.label == "2 pins not reachable" and c.tree_paths == ("a", "b")


def test_not_reached_counts_pins_below_any_failing_path():
    """_not_reached_below uses any(path) not all(paths). Kills `any -> all`."""
    pins = [pin("a", manifest="fail"), pin("b", manifest="fail"), pin("a/x", vendor="not-reached"),
            pin("b/y", vendor="not-reached"), pin("c/z", vendor="not-reached")]
    (c,) = V.causes(doc("x", [gate("t0.manifest.valid", "FAIL")], pins))
    assert c.not_reached == 2


def test_a_tier_without_a_name_is_ignored_not_a_crash():
    """_tier_row: `not dict or not tier` -> `and` raises KeyError on {"verdict": "FAIL"}."""
    d = doc("x", [gate("t0.pins.fetchable", "PASS")], [pin("a")], verdict="FAIL")
    d["fact"]["tier"] = {"verdict": "FAIL"}
    assert V.causes(d) == []
    assert V.deciding(d).gate is None


def test_tier_label_names_the_tier_upper_case():
    """_label: "T2 is red" from gid.split('.', 1)[1]. Kills split maxsplit 1 -> 2 (still fine) and the label."""
    t = {"tier": "t2", "verdict": "FAIL", "reason": "exit 2"}
    d = doc("x", [gate("t0.pins.fetchable", "PASS")], [pin("a")], verdict="FAIL", tier=t)
    assert V.deciding(d).label == "T2 is red"


@pytest.mark.parametrize("uf, want", [
    ("ab73e40924317e45e7b3b941f52a7ed35f20832e", "ab73e40"),                 # a bare string: first 7
    ({"commit": "0123456789abcdef0123456789abcdef01234567"}, "0123456"),
])
def test_upgrade_from_record_is_cut_to_seven(uf, want):
    """_upgrade_from string branch and its [:7]. Kills `return str(uf)[:7] -> None`, `7 -> 8`."""
    d = doc("x", [gate("t0.clone.upgrade-path", "FAIL", "no detail")], [pin("a")])
    (c,) = V.causes(d, {"fact": {"record": {"upgrade_from": uf}}})
    assert c.label == "upgrade path from %s does not clone anonymously" % want


def test_upgrade_from_detail_is_cut_to_seven_and_absent_falls_back():
    """_upgrade_from detail regex [:7]; an empty dict record falls through to the detail (`and -> or`)."""
    d = doc("x", [gate("t0.clone.upgrade-path", "FAIL", "upgrade_from 0123456789abcdef does not resolve")],
            [pin("a")])
    (c,) = V.causes(d, {"fact": {"record": {"upgrade_from": {}}}})
    assert c.label == "upgrade path from 0123456 does not clone anonymously"
    (c,) = V.causes(doc("x", [gate("t0.clone.upgrade-path", "FAIL", "nothing")], [pin("a")]))
    assert c.label == "upgrade path does not clone anonymously"


def test_one_unfetched_tree_is_singular_and_no_items_says_the_tree():
    """_vendor_label: "1 tree" (kills `n == 1 -> 2`), and the no-item fallback (kills `return ... -> None`)."""
    pins = [pin("."), pin("t", vendor="not-reached")]
    (c,) = V.causes(doc("x", [gate("t0.vendor.no-arm-ip", "UNVERIFIED")], pins, verdict="UNVERIFIED"))
    assert c.label == "1 tree not scanned (not fetched)"
    (c,) = V.causes(doc("x", [gate("t0.vendor.no-arm-ip", "UNVERIFIED")], [pin(".")], verdict="UNVERIFIED"))
    assert c.label == "Arm source scan unverified in the tree"


def test_probe_label():
    (c,) = V.causes(doc("x", [gate("t0.infra.probe", "FAIL")], [pin("a")]))
    assert (c.kind, c.label) == ("probe", "anonymous git does not work on this host")


def _ssh_pair(fetch="FAIL", nossh="FAIL"):
    pins = [pin("t", url_kind="ssh", fetchable="fail", no_ssh="fail")]
    return doc("x", [gate("t0.pins.fetchable", fetch), gate("t0.urls.no-ssh-public", nossh)], pins,
               verdict="FAIL" if "FAIL" in (fetch, nossh) else "UNVERIFIED")


def test_an_ssh_path_red_on_one_gate_only_is_a_single_cause():
    """A group of one gate falls back to a single cause. Kills `continue -> pass` (a duplicate ssh-pin cause)."""
    d = _ssh_pair(nossh="PASS")
    cs = V.causes(d)
    assert [(c.kind, c.gates) for c in cs] == [("gate", ("t0.pins.fetchable",))]


@pytest.mark.parametrize("fetch, nossh, want", [
    ("UNVERIFIED", "UNVERIFIED", "UNVERIFIED"), ("FAIL", "UNVERIFIED", "FAIL"), ("UNVERIFIED", "FAIL", "FAIL")])
def test_ssh_pin_cause_verdict_is_fail_if_any_gate_fails(fetch, nossh, want):
    """Kills the negated/`all`/`!=` mutants of the ssh-pin cause's verdict."""
    (c,) = V.causes(_ssh_pair(fetch, nossh))
    assert c.kind == "ssh-pin" and c.verdict == want


def test_min_beats_max_in_the_gate_order_tiebreak():
    """Two 2-gate ssh causes, equal not-reached: the sort key min(_order(gates)) decides. Kills `min -> max`."""
    pins = [pin("s1", url_kind="ssh", fetchable="fail", vendor="fail"),       # gates 2 and 7
            pin("s2", url_kind="ssh", reachable="fail", no_ssh="fail")]       # gates 3 and 4
    gs = [gate("t0.pins.fetchable", "FAIL"), gate("t0.pins.reachable", "FAIL"),
          gate("t0.urls.no-ssh-public", "FAIL"), gate("t0.vendor.no-arm-ip", "FAIL")]
    cs = V.causes(doc("x", gs, pins))
    assert [c.tree_paths for c in cs] == [("s1",), ("s2",)]          # min 2 < 3; max would put s2 (4) before s1 (7)


def test_an_infra_gate_without_a_tier_is_infra_and_named():
    """_infra gate branch and deciding's INFRA gid from a gate. Kills `True -> False`, `return ... -> None`,
    `and -> or` / `== -> !=` on the INFRA lookups."""
    d = doc("x", [gate("t0.pins.fetchable", "PASS"), gate("t0.infra.probe", "INFRA", "no git here")], [pin("a")],
            verdict="INFRA")
    dec = V.deciding(d)
    assert dec.infra is True and dec.kind == "infra" and dec.gate == "t0.infra.probe"
    assert dec.infra_text == "This host cannot run t0.infra.probe. Nothing to fix in the design. no git here"


def test_an_infra_tier_names_the_tier_upper_case_and_its_reason():
    t = {"tier": "t1", "verdict": "INFRA", "reason": "no tool"}
    d = doc("x", [gate("t0.pins.fetchable", "PASS"), gate("t0.infra.probe", "INFRA")], [pin("a")],
            verdict="PASS", tier=t)
    dec = V.deciding(d)
    assert dec.gate == "tier.t1" and dec.infra_text == "This host cannot run T1. Nothing to fix in the design. no tool"


def test_subject_per_kind():
    """deciding's subject: ssh-pin -> first path; other kinds -> joined paths; no paths -> None."""
    assert V.deciding(_ssh_pair()).subject == "t"
    pins = [pin("a", manifest="fail"), pin("b", manifest="fail")]
    assert V.deciding(doc("x", [gate("t0.manifest.valid", "FAIL")], pins)).subject == "a, b"
    assert V.deciding(doc("x", [gate("t0.infra.probe", "FAIL")], [pin("a")])).subject is None


def test_nested_submodule_name_strips_the_nearest_ancestor():
    """_submodule_name uses the LONGEST pinned ancestor. Kills `max -> min`."""
    pins = [pin("a"), pin("a/b"), pin("a/b/c", comp="c", url_kind="ssh", fetchable="fail", no_ssh="fail")]
    dd = doc("x", [gate("t0.pins.fetchable", "FAIL"), gate("t0.urls.no-ssh-public", "FAIL")], pins)
    a = V.next_action(V.deciding(dd), "x", doc=dd)
    assert a["code"].endswith("git submodule sync c") and "submodule.c.url" in a["code"]


def test_ssh_pin_without_a_component_falls_back_to_the_name():
    dec = V.Decision("FAIL", "t0.pins.fetchable", "l", "deps/x", ("deps/x",), (), (), False, kind="ssh-pin")
    a = V.next_action(dec, "cfg")
    assert "<https URL of deps/x>" in a["code"] and a["href"] == "/configurations/cfg/whatif?component="


def test_next_action_hrefs_for_upgrade_path_and_plain_gates():
    up = V.Decision("FAIL", "t0.clone.upgrade-path", "l", None, (), (), (), False, kind="upgrade-path")
    assert V.next_action(up, "c") == {"label": "Open the gate", "href": "/configurations/c#gate-t0.clone.upgrade-path",
                                      "code": None, "method": "get"}
    g = V.Decision("FAIL", "t0.manifest.valid", "l", None, (), (), (), False, kind="manifest")
    assert V.next_action(g, "c")["href"] == "/configurations/c#gate-t0.manifest.valid"


class _Cfg:
    def __init__(self, hosts):
        self.hosts = hosts

    def host(self, key):
        return self.hosts[key]


@pytest.mark.parametrize("canon, hosts, want", [
    ({"host": "github", "path": "org/x"}, {"github": {"web": "https://example.invalid/"}},
     "https://example.invalid/org/x.git"),
    ({"host": "github", "path": "org/x"}, {"github": {"web": "http://example.invalid"}}, None),
    ({"host": "github"}, {"github": {"web": "https://example.invalid"}}, None),
    ({"path": "org/x"}, {"github": {"web": "https://example.invalid"}}, None),
    ({"host": "nowhere", "path": "org/x"}, {}, None),
])
def test_https_url(monkeypatch, canon, hosts, want):
    """https_url: every early None and the https-only rule. Kills the and/or, not and negation mutants."""
    from nanosoc_rm.web import views
    comps = [{"fact": {"id": "other", "record": {"canonical": {"host": "github", "path": "o/o"}}}},
             {"fact": {"id": "x", "record": {"canonical": canon}}}]
    monkeypatch.setattr(views, "components", lambda config: comps)
    assert V.https_url(_Cfg(hosts), "x") == want
    assert V.https_url(_Cfg(hosts), "absent") is None


def test_a_red_run_with_an_infra_gate_still_reports_infra():
    """_infra's gate branch flag reaches a FAIL decision (not only an INFRA verdict). Kills `True -> False`."""
    d = doc("x", [gate("t0.infra.probe", "INFRA", "no git"), gate("t0.manifest.valid", "FAIL")],
            [pin("a", manifest="fail")])
    dec = V.deciding(d)
    assert dec.gate == "t0.manifest.valid" and dec.infra is True and "t0.infra.probe" in dec.infra_text


def test_an_unused_waiver_of_another_gate_at_the_same_path_is_reported():
    """A t0_waiver names ONE item: (gate, tree_path, result) (eligibility schema). t0.gates.apply_waivers -- the
    CLI's `unused_waivers` count -- reports the arm-gate waiver below as unused; web.verdict.unused_waivers (the
    inbox and configuration pages) does not, because the fetchable gate used a waiver at the same tree_path."""
    used = {"gate": "t0.pins.fetchable", "tree_path": "tidelink", "result": "fail"}
    unused = {"gate": "t0.vendor.no-arm-ip", "tree_path": "tidelink", "result": "fail"}
    rec = {"fact": {"record": {"frozen": True, "t0_waivers": [used, unused]}}}
    d = doc("x", [gate("t0.pins.fetchable", "WAIVED", raw_verdict="FAIL",
                       waived=[{"tree_path": "tidelink", "result": "fail", "covered_by": "tidelink"}]),
                  gate("t0.vendor.no-arm-ip", "PASS")], [pin("tidelink")], verdict="WAIVED")
    assert V.unused_waivers(d, rec) == [unused]


def test_an_arm_not_reached_waiver_used_by_the_foundry_gate_is_not_unused():
    """The control for the (gate, tree_path) key: an arm-gate `not-reached` waiver also covers the foundry gate's
    item (t0.gates._waiver_gates), so a foundry gate that names it has used it."""
    w = {"gate": "t0.vendor.no-arm-ip", "tree_path": "tidelink", "result": "not-reached"}
    rec = {"fact": {"record": {"frozen": True, "t0_waivers": [w]}}}
    d = doc("x", [gate("t0.vendor.no-foundry-data", "WAIVED", raw_verdict="FAIL",
                       waived=[{"tree_path": "tidelink", "result": "not-reached", "covered_by": "tidelink"}])],
            [pin("tidelink")], verdict="WAIVED")
    assert V.unused_waivers(d, rec) == []
