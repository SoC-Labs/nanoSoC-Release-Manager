"""nanosoc_rm.t0.tiers (D1): seven tiers, budgets or reasons, the T0 command, the budget check, the CLI."""
from __future__ import annotations

import dataclasses
import json

import pytest
from click.testing import CliRunner

from nanosoc_rm.contracts import load_schemas
from nanosoc_rm.t0 import tiers


def test_tiers_are_exactly_the_result_bundle_enum():
    enum = load_schemas()[0]["result-bundle"]["properties"]["tier"]["enum"]
    assert list(tiers.TIERS) == enum == list(tiers.TIER_IDS)
    assert tiers.validate_tiers(tiers.TIERS) == []


def test_budgets_follow_d1():
    b = {k: t.budget_s for k, t in tiers.TIERS.items()}
    assert b == {"t0": 120, "t1": 600, "t2": 900, "t3": 10800, "t4": None, "t5": None, "t6": 23400}
    assert tiers.TIERS["t5"].manual and tiers.TIERS["t6"].manual and not tiers.TIERS["t0"].manual
    assert all(tiers.TIERS[t].budget_reason for t in ("t4", "t5"))


@pytest.mark.parametrize("change, named", [
    ({"budget_s": None, "budget_reason": None}, "t2"),
    ({"budget_s": None, "budget_reason": "  "}, "t2"),
    ({"budget_s": 0}, "t2"),
    ({"budget_s": True}, "t2"),
    ({"id": "tx"}, "t2"),
])
def test_validate_tiers_names_the_broken_row(change, named):
    broken = dict(tiers.TIERS)
    broken["t2"] = dataclasses.replace(broken["t2"], **change)
    problems = tiers.validate_tiers(broken)
    assert problems and all(named in p for p in problems), problems


def test_validate_tiers_wants_all_seven():
    assert tiers.validate_tiers({k: v for k, v in tiers.TIERS.items() if k != "t3"})
    assert tiers.validate_tiers({})


def test_check_budget():
    assert tiers.check_budget("t0", "2026-10-01T02:03:30Z", "2026-10-01T02:05:30Z") == tiers.Budget(True, 120, 120)
    assert tiers.check_budget("t0", "2026-10-01T02:03:30Z", "2026-10-01T02:05:31Z") == tiers.Budget(False, 121, 120)
    assert tiers.check_budget("t4", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z").ok     # no budget: never over
    with pytest.raises(ValueError):
        tiers.check_budget("t0", "2026-10-01T02:05:00Z", "2026-10-01T02:04:00Z")


def test_t0_has_a_command_per_configuration_and_later_tiers_none():
    c = tiers.tier_commands("eth-rc4")
    assert c["t0"] == "nanosoc-rm t0 trigger eth-rc4"
    assert all(c[t] is None for t in tiers.TIER_IDS[1:])


def test_cli_tiers_json_matches_the_function():
    from nanosoc_rm.cli import main
    res = CliRunner().invoke(main, ["tiers", "eth-main", "--json"], standalone_mode=False)
    assert res.return_value == 0, res.output
    rows = [json.loads(line) for line in res.output.splitlines()]
    assert [r["tier"] for r in rows] == list(tiers.TIER_IDS)
    assert rows[0]["command"] == tiers.tier_commands("eth-main")["t0"]
    assert set(rows[0]) == {"tier", "budget_s", "budget_reason", "manual", "phase", "command"}
    table = CliRunner().invoke(main, ["tiers"], standalone_mode=False)
    assert table.return_value == 0 and "t6" in table.output


def test_cli_tiers_exits_1_on_a_broken_table(monkeypatch):
    from nanosoc_rm.cli import main
    monkeypatch.setitem(tiers.TIERS, "t2", dataclasses.replace(tiers.TIERS["t2"], budget_s=None, budget_reason=None))
    assert CliRunner().invoke(main, ["tiers"], standalone_mode=False).return_value == 1
